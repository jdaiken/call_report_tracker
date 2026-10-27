"""Weekly Network Exposure report: every bank's reciprocal deposits against the reciprocal
cap, and its uninsured-deposit share.

Why it matters for a reciprocal deposit network
  - Reciprocal deposits are excepted from "brokered" only up to the lesser of $5 billion or
    20% of total liabilities (12 CFR 337.6(e)). A bank near that line can't take more
    reciprocal funding without it counting as brokered.
  - A bank that is not well capitalized and well rated is held to a "special cap": its
    average reciprocal deposits over the last four quarters. Exam ratings aren't public,
    so banks below well capitalized or under an enforcement order are shown as likely on it.
  - Uninsured deposits (over $250,000) are the money most likely to run, and the deposits
    a network's full-insurance products protect: high share = both risk and demand.

Data: each bank's latest Call Report (current quarter, else the one before), read from the
daily monitor's stored snapshots. Banks whose snapshot predates these fields are re-pulled
once and the snapshot is updated in place (only adds fields), capped per run.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from .ratios import (DEPOSITS_RAW_KEY, LIABILITIES_RAW_KEY, NETWORK_KEYS, RECIPROCAL_CAP_DOLLARS_000,
                     RECIPROCAL_RAW_KEY, UNINSURED_RAW_KEY)

log = logging.getLogger(__name__)
NAME = "Network Exposure Report"
NEAR_CAP = 80.0          # % of the cap
HIGH_UNINSURED = 40.0    # % of deposits
TOP_N = 20               # banks on the exposure map and the top-exposure page
QUARTERS = 5             # quarters of history for the trend pages (current + four before it)
# Left out: banks with $25B+ in assets -- every U.S. GSIB, PNC, U.S. Bank, Truist, Capital One, and large regionals and
# brokerage-affiliated banks such as Raymond James Bank. They aren't network prospects and their scale swamps the
# charts. Override with NET_MAX_ASSETS_B (in $ billions). Names are a backstop for a GSIB unit reporting under the line.
EXCLUDE_ASSETS = int(float(os.environ.get("NET_MAX_ASSETS_B") or 25) * 1_000_000)      # $000
GSIB_NAMES = ("JPMORGAN", "BANK OF AMERICA", "CITIBANK", "WELLS FARGO", "GOLDMAN SACHS", "MORGAN STANLEY",
              "BANK OF NEW YORK MELLON", "STATE STREET BANK", "PNC BANK", "U.S. BANK", "TRUIST", "CAPITAL ONE")
SEGMENTS = [("Community (<$1B)", 0, 1_000_000), ("Regional ($1B-$10B)", 1_000_000, 10_000_000),
            (f"Large ($10B-${EXCLUDE_ASSETS // 1_000_000}B)", 10_000_000, EXCLUDE_ASSETS)]


def excluded_bank(name: str, assets) -> bool:
    n = (name or "").upper()
    return (assets or 0) >= EXCLUDE_ASSETS or any(n.startswith(g) or f" {g}" in f" {n}" for g in GSIB_NAMES)
USAGE_BUCKETS = [("None", None, 0.0), ("Under 25%", 0.0, 25.0), ("25-50%", 25.0, 50.0), ("50-80%", 50.0, 80.0),
                 ("80-100%", 80.0, 100.0), ("Over 100%", 100.0, None)]


@dataclass
class NetBank:
    bank: object                     # report.Bank
    period: date
    reciprocal: Optional[float]      # $000
    cap: Optional[float]             # $000
    used: Optional[float]            # % of cap
    uninsured: Optional[float]       # $000
    uninsured_share: Optional[float]  # % of deposits
    deposits: Optional[float]
    assets: float
    special_cap: list[str] = field(default_factory=list)   # reasons it is likely on the special cap
    prior_reciprocal: Optional[float] = None
    prior_used: Optional[float] = None
    capital: dict = field(default_factory=dict)            # leverage / tier1 / total / cet1 (%)
    cushion: Optional[float] = None                        # pts above the nearest well-capitalized line
    binding: str = ""                                      # which ratio that is
    history: list = field(default_factory=list)            # [(quarter end, reciprocal $000, cap $000)] newest first
    state: str = ""

    @property
    def special_cap_est(self) -> Optional[float]:
        '''Estimated special cap: average quarter-end reciprocal deposits over the four quarters before this one
        (what the bank would be held to if it lost well-capitalized/well-rated status now). With fewer than two of
        those on hand, the quarters available including this one; None if under two in all.'''
        prior = [h[1] for h in self.history[1:5] if h[1] is not None]
        vals = prior if len(prior) >= 2 else [h[1] for h in self.history[:5] if h[1] is not None]
        return sum(vals) / len(vals) if len(vals) >= 2 else None

    @property
    def est_basis(self) -> str:
        prior = [h for h in self.history[1:5] if h[1] is not None]
        if len(prior) >= 2:
            return f"{len(prior)}Q avg"
        n = sum(1 for h in self.history[:5] if h[1] is not None)
        return f"{n}Q avg" if n >= 2 else ""

    def series(self) -> list[tuple]:
        """[(quarter end, reciprocal, cap)] oldest first, quarters with a figure only."""
        return [h for h in reversed(self.history) if h[1] is not None]

    def gap_change(self) -> Optional[float]:
        """Change in (reciprocal - cap) from the oldest quarter on hand (up to a year ago) to now; > 0 = widening."""
        ser = [h for h in self.series() if h[2]]
        if len(ser) < 2:
            return None
        return (ser[-1][1] - ser[-1][2]) - (ser[0][1] - ser[0][2])

    def special_gap_change(self) -> Optional[float]:
        """Change in reciprocal deposits over the year on hand: > 0 = drifting further above the special cap."""
        ser = self.series()
        return None if len(ser) < 2 else ser[-1][1] - ser[0][1]

    @property
    def over_special(self) -> Optional[float]:
        est = self.special_cap_est
        return None if est is None or self.reciprocal is None else self.reciprocal - est

    @property
    def headroom(self) -> Optional[float]:
        return None if self.cap is None or self.reciprocal is None else self.cap - self.reciprocal


@dataclass
class Network:
    run_date: date
    banks: list[NetBank]
    quarter_label: str
    pulled: int = 0
    pull_failed: int = 0
    missing: int = 0
    summary: str = ""
    summary_by: str = ""
    images: dict = field(default_factory=dict)
    excluded: list = field(default_factory=list)            # names of the large banks left out

    # -------- analytics
    @property
    def users(self) -> list[NetBank]:
        return [b for b in self.banks if (b.reciprocal or 0) > 0]

    def stats(self) -> dict:
        u = self.users
        used = [b.used for b in u if b.used is not None]
        unshare = [b.uninsured_share for b in self.banks if b.uninsured_share is not None]
        return {
            "banks": len(self.banks), "users": len(u),
            "user_pct": len(u) / len(self.banks) * 100 if self.banks else 0,
            "reciprocal_total": sum(b.reciprocal or 0 for b in u),
            "median_used": statistics.median(used) if used else None,
            "near_cap": sum(1 for x in used if NEAR_CAP <= x <= 100), "over_cap": sum(1 for x in used if x > 100),
            "special": sum(1 for b in u if b.special_cap),
            "special_all": sum(1 for b in self.banks if b.special_cap),
            "uninsured_total": sum(b.uninsured or 0 for b in self.banks),
            "median_uninsured": statistics.median(unshare) if unshare else None,
            "high_uninsured": sum(1 for x in unshare if x >= HIGH_UNINSURED),
            "high_unins_low_use": len(self.prospects()),
        }

    def near_cap(self) -> list[NetBank]:
        return sorted([b for b in self.users if b.used is not None and b.used >= NEAR_CAP], key=lambda b: -b.used)

    def prospects(self) -> list[NetBank]:
        """High uninsured share and plenty of cap headroom: banks that could use more network funding/coverage."""
        return sorted([b for b in self.banks if (b.uninsured_share or 0) >= HIGH_UNINSURED and (b.used or 0) < 50
                       and not b.special_cap], key=lambda b: -(b.uninsured or 0))

    def special(self) -> list[NetBank]:
        """Every bank likely held to the special cap (below well capitalized or under an active order), users first."""
        return sorted([b for b in self.banks if b.special_cap], key=lambda b: -(b.reciprocal or 0))

    def special_users(self) -> list[NetBank]:
        return [b for b in self.special() if (b.reciprocal or 0) > 0]

    def trend(self) -> list[NetBank]:
        """Banks at or over 80% of the cap with at least two quarters on hand, biggest widening of the gap first."""
        return sorted([b for b in self.near_cap() if b.gap_change() is not None], key=lambda b: -b.gap_change())

    def risk(self, n: int = 15) -> list[NetBank]:
        """Highest-risk reciprocal users: most points across the five measures, then largest reciprocal balance."""
        scored = [b for b in self.users if risk_score(b)[0] > 0]
        return sorted(scored, key=lambda b: (-risk_score(b)[0], -(b.reciprocal or 0)))[:n]

    def closest_to_special(self) -> list[NetBank]:
        '''Reciprocal users not yet on the special cap, thinnest capital cushion first.'''
        return sorted([b for b in self.users if not b.special_cap and b.cushion is not None], key=lambda b: b.cushion)

    def top_exposure(self, n: int = TOP_N) -> list[NetBank]:
        """Largest reciprocal deposit balances."""
        return sorted(self.users, key=lambda b: -(b.reciprocal or 0))[:n]

    def movers(self) -> list[NetBank]:
        """Banks with last quarter's figure on hand, largest dollar change in reciprocal deposits first."""
        return sorted([b for b in self.banks if b.prior_reciprocal is not None and b.reciprocal is not None
                       and b.reciprocal != b.prior_reciprocal], key=lambda b: -abs(b.reciprocal - b.prior_reciprocal))

    def buckets(self) -> list[tuple[str, int]]:
        out = []
        for label, lo, hi in USAGE_BUCKETS:
            if lo is None:
                n = sum(1 for b in self.banks if not (b.reciprocal or 0))
            else:
                n = sum(1 for b in self.users if b.used is not None and b.used >= lo and (hi is None or b.used < hi)
                        and not (lo == 0 and b.used == 0))
            out.append((label, n))
        return out


def due(run_date: date) -> bool:
    """Run schedule: weekly while Call Reports are coming in (3 to 9 weeks after a quarter end, which covers the
    30/35-day deadlines and most refilings), otherwise the first Monday of the month."""
    from .analytics import latest_quarter_end
    q = latest_quarter_end(run_date)
    return 21 <= (run_date - q).days <= 63 or run_date.day <= 7


# ------------------------------------------------------------------ risk score
# Five measures, each 0 (fine) to 3 (worst); the cell colors on the risk page come from the same levels.
RISK_MEASURES = ("Cap used", "Special cap", "Capital cushion", "Uninsured", "1-yr trend")


def risk_levels(b: "NetBank") -> dict[str, tuple[int, str]]:
    """{measure: (level 0-3, short text)}."""
    out = {}
    u = b.used
    out["Cap used"] = ((3 if u > 100 else 2 if u >= NEAR_CAP else 1 if u >= 60 else 0), f"{u:.0f}%") if u is not None else (0, "n/a")
    if b.special_cap:
        over = b.over_special
        out["Special cap"] = (3, "over est." if over and over > 0 else "on it")
    else:
        out["Special cap"] = (0, "no")
    c = b.cushion
    if c is None or b.special_cap:
        out["Capital cushion"] = (3 if b.special_cap and "below well capitalized" in b.special_cap else 0,
                                  "below WC" if b.special_cap and "below well capitalized" in b.special_cap else
                                  ("n/a" if c is None else f"{c:+.1f} pts"))
    else:
        out["Capital cushion"] = ((3 if c < 0 else 2 if c < 0.5 else 1 if c < 1.0 else 0), f"{c:+.1f} pts")
    sh = b.uninsured_share
    out["Uninsured"] = ((3 if sh >= 60 else 2 if sh >= 50 else 1 if sh >= HIGH_UNINSURED else 0), f"{sh:.0f}%") if sh is not None else (0, "n/a")
    g = b.special_gap_change() if b.special_cap else (b.gap_change() if (u or 0) >= NEAR_CAP else None)
    if g is None:
        out["1-yr trend"] = (0, "—")
    else:
        base = (b.series()[0][1] or 0) or 1
        rel = g / base
        out["1-yr trend"] = ((2 if rel > 0.25 else 1 if g > 0 else 0), ("+" if g >= 0 else "-") + _fmt_money(abs(g)))
    return out


def risk_score(b: "NetBank") -> tuple[int, dict]:
    lv = risk_levels(b)
    return sum(v[0] for v in lv.values()), lv


def _num(r: dict, k: str) -> Optional[float]:
    v = r.get(k)
    return float(v) if isinstance(v, (int, float)) else None


def _quarter(p: date) -> str:
    return f"Q{(p.month - 1) // 3 + 1} {p.year}"


# ------------------------------------------------------------------ collecting
def collect(conn, client, run_date: date, max_pulls: int = 5000, enforcement_sources=None,
            prior_lookup: Optional[Callable] = None, on_progress: Optional[Callable[[int], None]] = None) -> Network:
    from . import db, ratios as ratios_mod
    from .analytics import latest_quarter_end
    from .performance import prior_quarter_end
    from .report import _bank, capital_status
    current = latest_quarter_end(run_date)
    floor = prior_quarter_end(current)
    rows = conn.execute(
        "SELECT id_rssd, period_end, ratios FROM (SELECT id_rssd, period_end, ratios, ROW_NUMBER() OVER "
        "(PARTITION BY id_rssd ORDER BY period_end DESC) rn FROM call_report_snapshots) WHERE rn = 1 AND period_end >= ?",
        [floor]).fetchall()
    net = Network(run_date, [], _quarter(current))
    for rssd, period_end, raw in rows:
        meta = db.get_institution_meta(conn, rssd)
        if meta is None:
            continue
        if excluded_bank(meta.name, meta.total_assets):
            net.excluded.append(meta.name)
            continue
        r = json.loads(raw) if raw else {}
        if not any(isinstance(r.get(k), (int, float)) for k in NETWORK_KEYS[1:]):
            if client is None or net.pulled >= max_pulls:
                net.missing += 1
                continue
            try:
                fac = client.retrieve_facsimile(rssd, period_end)
                r = ratios_mod.extract_ratios(fac, period_end)
                r["total_assets"] = ratios_mod.extract_total_assets(fac)
            except Exception:
                net.pull_failed += 1
                continue
            net.pulled += 1
            if net.pulled % 100 == 0:
                conn.execute("CHECKPOINT")      # flush to the database file, so a stopped run keeps its progress
                if on_progress:
                    on_progress(net.pulled)
            merged = {**(json.loads(raw) if raw else {}), **{k: v for k, v in r.items() if v is not None}}
            conn.execute("UPDATE call_report_snapshots SET ratios = ? WHERE id_rssd = ? AND period_end = ?",
                         [json.dumps(merged), rssd, period_end])
            r = merged
        b = _bank(meta, period_end)
        rec, liab = _num(r, RECIPROCAL_RAW_KEY), _num(r, LIABILITIES_RAW_KEY)
        cap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * liab) if liab else None
        unins, deps = _num(r, UNINSURED_RAW_KEY), _num(r, DEPOSITS_RAW_KEY)
        nb = NetBank(b, period_end, rec, cap, (rec / cap * 100) if (rec is not None and cap) else None, unins,
                     (unins / deps * 100) if (unins is not None and deps) else None, deps, meta.total_assets or 0)
        if capital_status(r):
            nb.special_cap.append("below well capitalized")
        nb.state = b.location.rsplit(",", 1)[-1].strip()
        nb.capital = {k: _num(r, k) for k in ("tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio", "cet1_ratio")}
        nb.cushion, nb.binding = capital_cushion(nb.capital)
        net.banks.append(nb)
    # enforcement -> likely special cap
    if enforcement_sources is not None:
        from .enforcement import match
        for m in match([nb.bank for nb in net.banks], enforcement_sources.actions):
            if not m.action.stale:
                for nb in net.banks:
                    if nb.bank.rssd == m.bank.rssd and "under an enforcement order" not in nb.special_cap:
                        nb.special_cap.append("under an enforcement order")
    # Five quarters of history for every bank the report features: reciprocal deposits and the cap each quarter,
    # for the estimated special cap and the trend pages. Pulled once and cached (network_history).
    if prior_lookup is not None:
        focus = {}
        for b in (net.special() + net.top_exposure() + net.near_cap()[:20] + net.closest_to_special()[:15]
                  + net.prospects()[:10] + net.risk(25)):
            focus[id(b)] = b
        for nb in focus.values():
            nb.history = [(nb.period, nb.reciprocal, nb.cap)]
            q = nb.period
            for i in range(QUARTERS - 1):
                q = prior_quarter_end(q)
                try:
                    pr = prior_lookup(nb.bank.rssd, q) or {}
                except Exception:
                    pr = {}
                prec, pliab = _num(pr, RECIPROCAL_RAW_KEY), _num(pr, LIABILITIES_RAW_KEY)
                pcap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * pliab) if pliab else None
                nb.history.append((q, prec, pcap))
                if i == 0 and prec is not None:
                    nb.prior_reciprocal = prec
                    nb.prior_used = prec / pcap * 100 if pcap else None
    return net


WELL_CAP_MIN = [("tier1_leverage_ratio", 5.0, "leverage"), ("tier1_rbc_ratio", 8.0, "Tier 1 risk-based"),
                ("total_rbc_ratio", 10.0, "total risk-based"), ("cet1_ratio", 6.5, "CET1")]


def capital_cushion(cap: dict) -> tuple[Optional[float], str]:
    '''Points above the nearest well-capitalized line. A community bank leverage ratio bank (only a
    leverage ratio reported) is measured against 9%.'''
    vals = {k: v for k, v in cap.items() if v is not None}
    if not vals:
        return None, ""
    if set(vals) == {"tier1_leverage_ratio"}:
        return vals["tier1_leverage_ratio"] - 9.0, "CBLR leverage (9%)"
    gaps = [(vals[k] - m, f"{label} ({m:g}%)") for k, m, label in WELL_CAP_MIN if k in vals]
    return min(gaps, key=lambda t: t[0])


HISTORY_SQL = """CREATE TABLE IF NOT EXISTS network_history (
    id_rssd VARCHAR, period_end DATE, reciprocal DOUBLE, liabilities DOUBLE, fetched_at TIMESTAMP,
    PRIMARY KEY (id_rssd, period_end))"""


def make_history_lookup(conn, client):
    '''A past quarter's reciprocal deposits and total liabilities for a bank: cached in network_history, else the
    stored snapshot, else a Call Report pull (cached, including "nothing filed" so it isn't asked again).'''
    from datetime import datetime
    from . import db, ratios as ratios_mod
    conn.execute(HISTORY_SQL)

    def lookup(rssd: str, period_end: date) -> Optional[dict]:
        row = conn.execute("SELECT reciprocal, liabilities FROM network_history WHERE id_rssd = ? AND period_end = ?",
                           [rssd, period_end]).fetchone()
        if row is not None:
            return {RECIPROCAL_RAW_KEY: row[0], LIABILITIES_RAW_KEY: row[1]}
        snap = db.get_snapshot(conn, rssd, period_end)
        if snap is not None and isinstance(snap.ratios.get(RECIPROCAL_RAW_KEY), (int, float)):
            r = snap.ratios
        elif client is None:
            return None
        else:
            try:
                r = ratios_mod.extract_ratios(client.retrieve_facsimile(rssd, period_end), period_end)
            except Exception:
                r = {}
        rec, liab = _num(r, RECIPROCAL_RAW_KEY), _num(r, LIABILITIES_RAW_KEY)
        conn.execute("INSERT OR REPLACE INTO network_history VALUES (?, ?, ?, ?, ?)",
                     [rssd, period_end, rec, liab, datetime.now()])
        return {RECIPROCAL_RAW_KEY: rec, LIABILITIES_RAW_KEY: liab}
    return lookup


# ------------------------------------------------------------------ narrative
PROMPT = """You write the opening analysis of a weekly report for a risk manager at a firm that runs a reciprocal deposit
network. Plain English, ONE paragraph of 5 to 8 sentences. Lead with the highest-risk banks and why. Then cover: how widely banks use reciprocal
deposits and how close they are to the reciprocal cap (lesser of $5 billion or 20% of liabilities); the banks nearest or
over the cap and what that means (more placements would count as brokered); banks likely held to the special cap
(not well capitalized/rated, so limited to their four-quarter average); and uninsured-deposit exposure, including banks
with high uninsured deposits and room under the cap; and the banks closest to
the special cap (thinnest capital cushion) with their estimated special cap; and whether gaps over the cap widened or
narrowed over the past year. Name at most four banks. Up to 180 words.

Rules: use ONLY facts and numbers in the data; copy numbers exactly; no predictions, no advice, no markdown.

Data:
"""


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    d = v * 1000
    return f"${d / 1e9:,.1f}B" if d >= 1e9 else f"${d / 1e6:,.0f}M"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


def facts(net: Network) -> str:
    s = net.stats()
    lines = [f"{NAME}, {net.run_date:%B %-d, %Y}; latest Call Reports ({net.quarter_label} where filed, else the prior quarter).",
             f"Banks analyzed: {s['banks']:,}. Banks holding reciprocal deposits: {s['users']:,} ({s['user_pct']:.0f}%). "
             f"Total reciprocal deposits: {_fmt_money(s['reciprocal_total'])}.",
             f"Median share of the reciprocal cap used, among users: {_pct(s['median_used'])}. "
             f"Banks at {NEAR_CAP:.0f}-100% of the cap: {s['near_cap']}. Banks over the cap: {s['over_cap']}. "
             f"Reciprocal users likely on the special cap: {s['special']}.",
             f"Total uninsured deposits: {_fmt_money(s['uninsured_total'])}. Median uninsured share: "
             f"{_pct(s['median_uninsured'])}. Banks with uninsured share of {HIGH_UNINSURED:.0f}% or more: "
             f"{s['high_uninsured']}; of those with under half the cap used and not on the special cap: "
             f"{s['high_unins_low_use']}."]
    if net.excluded:
        lines.append(f"Left out: {len(net.excluded)} banks with ${EXCLUDE_ASSETS // 1_000_000}B or more in assets "
                     "(GSIBs, large regionals and brokerage-affiliated banks).")
    lines.append("LARGEST RECIPROCAL HOLDERS: " + "; ".join(
        f"{b.bank.name} {_fmt_money(b.reciprocal)} ({_pct(b.used)} of cap)" for b in net.top_exposure(5)) + ".")
    lines.append("CLOSEST TO THE SPECIAL CAP (thinnest capital cushion above a well-capitalized line, reciprocal users):")
    for b in net.closest_to_special()[:6]:
        est = b.special_cap_est
        lines.append(f"  {b.bank.name} ({b.bank.location}): cushion {b.cushion:.2f} pts on {b.binding}; reciprocal "
                     f"{_fmt_money(b.reciprocal)}" + (f"; estimated special cap if it slips {_fmt_money(est)}" if est else ""))
    lines.append(f"LIKELY ON THE SPECIAL CAP: {s['special_all']} banks in all (below well capitalized or under an active "
                 f"enforcement order), {s['special']} of them holding reciprocal deposits. Estimated special cap "
                 "(four-quarter average):")
    for b in net.special()[:8]:
        est, over = b.special_cap_est, b.over_special
        lines.append(f"  {b.bank.name} ({b.bank.location}): reciprocal {_fmt_money(b.reciprocal)}; "
                     + (f"estimated special cap {_fmt_money(est)}; "
                        + (f"{_fmt_money(over)} above it (would count as brokered)" if over and over > 0 else "within it")
                        if est else "special cap not estimated (history unavailable)"))
    lines.append("HIGHEST-RISK RECIPROCAL USERS (points across cap used, special cap, capital cushion, uninsured share, 1-yr trend):")
    for b in net.risk(6):
        sc, lv = risk_score(b)
        lines.append(f"  {b.bank.name} ({b.bank.location}): score {sc}; reciprocal {_fmt_money(b.reciprocal)}; "
                     + "; ".join(f"{k} {v[1]}" for k, v in lv.items()))
    tr = net.trend()
    if tr:
        wid = [b for b in tr if b.gap_change() > 0]
        lines.append(f"TREND OVER THE PAST YEAR (banks at {NEAR_CAP:.0f}%+ of cap with history on hand: {len(tr)}): gap between "
                     f"reciprocal deposits and the cap widened at {len(wid)} and narrowed at {len(tr) - len(wid)}. Largest widening: "
                     + "; ".join(f"{b.bank.name} {_fmt_money(b.gap_change())} ({_pct(b.series()[0][1] / b.series()[0][2] * 100 if b.series()[0][2] else None)} "
                                 f"of cap a year ago, {_pct(b.used)} now)" for b in wid[:3]) + ".")
    for title, lst in (("NEAREST OR OVER THE CAP", net.near_cap()[:8]), ("HIGH UNINSURED, ROOM UNDER THE CAP", net.prospects()[:8])):
        lines.append(title + ":")
        for b in lst:
            lines.append(f"  {b.bank.name} ({b.bank.location}, assets {b.bank.assets}): reciprocal {_fmt_money(b.reciprocal)}, "
                         f"{_pct(b.used)} of cap, uninsured {_pct(b.uninsured_share)} of deposits"
                         + (f"; {', '.join(b.special_cap)}" if b.special_cap else "")
                         + (f"; last quarter {b.prior_used:.1f}% of cap" if b.prior_used is not None else ""))
    return "\n".join(lines)


def rule_summary(net: Network) -> str:
    s = net.stats()
    parts = [f"{s['users']:,} of {s['banks']:,} banks ({s['user_pct']:.0f}%) hold reciprocal deposits, "
             f"{_fmt_money(s['reciprocal_total'])} in total."]
    if s["median_used"] is not None:
        parts.append(f"The typical user is at {s['median_used']:.1f}% of its reciprocal cap; {s['near_cap']} "
                     f"{'is' if s['near_cap'] == 1 else 'are'} at {NEAR_CAP:.0f}-100% and {s['over_cap']} over it, where more "
                     "placements would count as brokered.")
    if s["special"]:
        parts.append(f"{s['special']} reciprocal users are likely on the special cap (below well capitalized or under an "
                     "order), which limits them to their four-quarter average.")
    parts.append(f"{s['high_uninsured']} banks have {HIGH_UNINSURED:.0f}% or more of deposits uninsured; "
                 f"{s['high_unins_low_use']} of them use under half their cap.")
    close = net.closest_to_special()[:1]
    if close:
        b = close[0]
        parts.append(f"Closest to the special cap: {b.bank.name}, {b.cushion:.2f} pts above the {b.binding} line"
                     + (f"; its special cap would be about {_fmt_money(b.special_cap_est)}." if b.special_cap_est else "."))
    return " ".join(parts)


def write_summary(net: Network) -> tuple[str, str]:
    from .narrative import DEFAULT_MODEL, _numbers, _post
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        data = facts(net)
        try:
            r = _post(key, os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL, PROMPT + data)
            if r.status_code < 400:
                t = " ".join(x.get("text", "") for x in r.json().get("content", []) if x.get("type") == "text")
                t = re.sub(r"\s+", " ", t.replace("**", "")).strip()
                if t and len(t) < 1500 and _numbers(t) <= (_numbers(data) | {str(i) for i in range(0, 13)}):
                    return t, "Claude"
        except Exception as e:
            log.warning("network summary unavailable: %s", e)
    return rule_summary(net), "rules"


# ------------------------------------------------------------------ key takeaways (page 1)
def takeaways(net: Network) -> list[str]:
    s = net.stats()
    rk = net.risk(3)
    out = []
    if rk:
        out.append("Highest risk this week: " + "; ".join(f"{short(b.bank.name)} ({_fmt_money(b.reciprocal)})" for b in rk)
                   + ". See page 2.")
    out += [f"{s['users']:,} of {s['banks']:,} banks ({s['user_pct']:.0f}%) hold {_fmt_money(s['reciprocal_total'])} of reciprocal "
           "deposits" + (f"; the typical user is at {s['median_used']:.1f}% of its cap." if s["median_used"] is not None else ".")]
    if s["near_cap"] + s["over_cap"]:
        top = net.near_cap()[0]
        out.append(f"{s['near_cap'] + s['over_cap']} banks are at {NEAR_CAP:.0f}%+ of the cap ({s['over_cap']} over it, where more "
                   f"placements count as brokered). Highest: {short(top.bank.name)} at {top.used:.0f}%.")
    big = net.top_exposure(1)
    if big:
        b = big[0]
        out.append(f"Largest holder: {short(b.bank.name)}, {_fmt_money(b.reciprocal)} ({_pct(b.used)} of its cap).")
    if s["special_all"]:
        over = [b for b in net.special() if (b.over_special or 0) > 0]
        out.append(f"{s['special_all']} banks are likely on the special cap (enforcement order or below well capitalized), "
                   f"{s['special']} of them holding reciprocal deposits"
                   + (f"; {len(over)} hold more than their estimated four-quarter average." if over else "."))
    tr = net.trend()
    if tr:
        wid = [b for b in tr if b.gap_change() > 0]
        out.append(f"Over the past year the gap to the cap widened at {len(wid)} of {len(tr)} banks at {NEAR_CAP:.0f}%+ of cap"
                   + (f"; most at {short(wid[0].bank.name)} (+{_fmt_money(wid[0].gap_change())})." if wid else "."))
    close = net.closest_to_special()[:1]
    if close:
        b = close[0]
        out.append(f"Thinnest capital cushion among users not on the special cap: {short(b.bank.name)}, "
                   f"{b.cushion:+.2f} pts on {b.binding}.")
    out.append(f"{s['high_uninsured']:,} banks have {HIGH_UNINSURED:.0f}%+ of deposits uninsured; {s['high_unins_low_use']} of them "
               "use under half their cap and aren't on the special cap.")
    return out


def short(name: str, n: int = 28) -> str:
    t = re.sub(r",? (NATIONAL ASSOCIATION|N\.A\.|THE)$", "", (name or "").upper()).title()
    t = t.replace("And Trust", "& Trust").replace("Company", "Co.")
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ charts
def charts(net: Network) -> dict[str, bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .analytics import AMBER, BLUE, GRID, INK, MUTED, RED, SURFACE
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    out = {}
    D = chr(92) + "$"            # a literal $ in matplotlib text

    def save(fig, key, tight=True):
        buf = io.BytesIO()
        if tight:
            fig.tight_layout(pad=0.6)
        fig.savefig(buf, format="png", dpi=200, facecolor=SURFACE)
        plt.close(fig)
        out[key] = buf.getvalue()

    def axes(w, h):
        fig, ax = plt.subplots(figsize=(w, h))
        fig.patch.set_facecolor(SURFACE)
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, labelsize=11)
        return fig, ax

    def bank_axis(ax, banks):
        ax.set_yticks(range(len(banks)))
        ax.set_yticklabels([short(b.bank.name, 26) for b in banks], fontsize=10, color=INK)
        ax.set_ylim(-0.7, len(banks) - 0.3)
        ax.invert_yaxis()
        ax.xaxis.grid(True, color=GRID, zorder=0)

    # 1. How much of the cap banks use
    b = net.buckets()
    fig, ax = axes(7.2, 3.0)
    colors = [GRID, BLUE, BLUE, BLUE, AMBER, RED]
    xs = range(len(b))
    ax.bar(xs, [n for _, n in b], color=colors, width=0.66, edgecolor=SURFACE, linewidth=1.0, zorder=3)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([lab for lab, _ in b])
    ax.yaxis.grid(True, color=GRID, zorder=0)
    for x, (_, n) in zip(xs, b):
        ax.text(x, n, f"{n:,}", ha="center", va="bottom", fontsize=11.5, color=INK, fontweight="bold")
    ax.set_xlabel("Share of the reciprocal cap used (no reciprocal deposits = None)", fontsize=11)
    ax.set_ylim(0, max(n for _, n in b) * 1.18 + 1)
    save(fig, "usage")

    # 2. Segments: small multiples of cap use (share of banks in each segment)
    segs = [(lab, [x for x in net.banks if lo <= (x.assets or 0) < hi]) for lab, lo, hi in SEGMENTS]
    segs = [(lab, grp) for lab, grp in segs if grp]
    if segs:
        fig, axs = plt.subplots(1, len(segs), figsize=(7.4, 3.6), sharey=True)
        fig.patch.set_facecolor(SURFACE)
        axs = axs if len(segs) > 1 else [axs]
        shortlab = ["None", "<25", "25-50", "50-80", "80-100", ">100"]
        for ax, (lab, grp) in zip(axs, segs):
            ax.set_facecolor(SURFACE)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            sub = Network(net.run_date, grp, net.quarter_label)
            pct = [n / len(grp) * 100 for _, n in sub.buckets()]
            ax.bar(range(len(pct)), pct, color=colors, width=0.7, edgecolor=SURFACE, linewidth=1.0, zorder=3)
            ax.set_xticks(range(len(pct)))
            ax.set_xticklabels(shortlab, fontsize=8.5, rotation=45, ha="right")
            ax.yaxis.grid(True, color=GRID, zorder=0)
            ax.tick_params(length=0, labelsize=9)
            un = [x.uninsured_share for x in grp if x.uninsured_share is not None]
            ax.set_title(lab.replace("$", D), fontsize=10.5, color=INK, loc="left", fontweight="bold", pad=36)
            ax.text(0, 1.02, f"{len(grp):,} banks · {len(sub.users) / len(grp) * 100:.0f}% use it\nmedian uninsured "
                    f"{statistics.median(un) if un else 0:.0f}%", transform=ax.transAxes, fontsize=8.5, color=MUTED, va="bottom")
        axs[0].set_ylabel("% of banks in segment", fontsize=10)
        fig.text(0.5, 0.01, "Share of the reciprocal cap used, %", ha="center", fontsize=10, color=MUTED)
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        save(fig, "size", tight=False)

    # 3. Exposure map: the top 20 holders, numbered (key in the table beside it)
    top = [x for x in net.top_exposure() if x.used is not None and x.uninsured_share is not None]
    if top:
        close_ids = {id(x) for x in net.closest_to_special()[:10]}
        fig, ax = axes(7.4, 4.9)
        ax.spines["left"].set_visible(True)
        big = max(x.reciprocal for x in top)
        ymax = 150
        ax.axhspan(100, ymax + 10, color=RED, alpha=0.05, zorder=0)
        ax.axhspan(NEAR_CAP, 100, color=AMBER, alpha=0.07, zorder=0)
        ax.axhline(100, color=RED, linewidth=1.1, linestyle=(0, (4, 3)), zorder=1)
        ax.axhline(NEAR_CAP, color=AMBER, linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
        ax.axvline(HIGH_UNINSURED, color=MUTED, linewidth=0.8, linestyle=(0, (2, 3)), zorder=1)
        ax.text(0.005, 101, " over cap: more counts as brokered", transform=ax.get_yaxis_transform(), ha="left", va="bottom",
                fontsize=9.5, color=RED)
        ax.text(0.005, NEAR_CAP + 1, f" {NEAR_CAP:.0f}%+ of cap", transform=ax.get_yaxis_transform(), ha="left", va="bottom",
                fontsize=9.5, color=AMBER)
        ax.text(HIGH_UNINSURED + 0.6, 3, f"{HIGH_UNINSURED:.0f}% uninsured", fontsize=9, color=MUTED, va="bottom")
        for i, x in enumerate(top, 1):
            col = RED if x.special_cap else (AMBER if id(x) in close_ids else BLUE)
            y = min(x.used, ymax)
            ax.scatter([x.uninsured_share], [y], s=260 if x.used > ymax else 140 + 900 * (x.reciprocal / big), color=col, alpha=0.82,
                       edgecolors=SURFACE, linewidths=1.2, zorder=3, marker="^" if x.used > ymax else "o")
            ax.text(x.uninsured_share, y - (2.5 if x.used > ymax else 0), str(i), ha="center", va="center", fontsize=8.5, color="white", fontweight="bold", zorder=4)
        for lab, col in (("Reciprocal user", BLUE), ("Likely on special cap", RED), ("Thin capital cushion", AMBER)):
            ax.scatter([], [], s=70, color=col, label=lab)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=3, frameon=False, fontsize=9.5, handletextpad=0.3)
        ax.set_xlabel("Uninsured deposits, % of deposits", fontsize=11)
        ax.set_ylabel("Reciprocal cap used, %", fontsize=11)
        ax.yaxis.grid(True, color=GRID, zorder=0)
        ax.set_xlim(0, max(60, max(x.uninsured_share for x in top) * 1.12))
        ax.set_ylim(0, ymax + 10)
        ax.set_yticks([0, 25, 50, 80, 100, 125, 150])
        ax.set_yticklabels(["0", "25", "50", "80", "100", "125", "150+"])
        save(fig, "map")

    # 4. Top 20: reciprocal deposits against the cap and the estimated special cap
    top = net.top_exposure()
    if top:
        fig, ax = axes(7.4, 4.4)
        ys = range(len(top))
        ax.barh(ys, [(x.cap or 0) / 1e6 for x in top], color=GRID, height=0.72, zorder=2, label="Reciprocal cap")
        ax.barh(ys, [(x.reciprocal or 0) / 1e6 for x in top], height=0.36, zorder=3, label="Reciprocal deposits",
                color=[RED if (x.used or 0) > 100 else AMBER if (x.used or 0) >= NEAR_CAP else BLUE for x in top])
        est = [(i, x.special_cap_est / 1e6) for i, x in enumerate(top) if x.special_cap_est]
        if est:
            ax.scatter([v for _, v in est], [i for i, _ in est], marker="|", s=260, color=INK, linewidths=2.2, zorder=4,
                       label="Est. special cap (4Q avg)")
        for i, x in enumerate(top):
            ax.text(max((x.cap or 0), (x.reciprocal or 0)) / 1e6, i, f"  {_pct(x.used)}", va="center", fontsize=9, color=INK)
        bank_axis(ax, top)
        ax.set_yticklabels([f"{i}. {short(x.bank.name, 24)}" for i, x in enumerate(top, 1)], fontsize=9.5, color=INK)
        ax.set_xlabel(f"{D}B (label = % of cap used)", fontsize=10.5)
        ax.set_xlim(0, max(max((x.cap or 0), (x.reciprocal or 0)) for x in top) / 1e6 * 1.16)
        ax.legend(loc="lower right", frameon=False, fontsize=9)
        save(fig, "top")

    # 5. Nearest or over the cap: lollipop of % of cap used
    near = net.near_cap()[:15]
    if near:
        fig, ax = axes(7.4, 3.8)
        lim = 200
        for i, x in enumerate(near):
            v = min(x.used, lim)
            col = RED if x.used > 100 else AMBER
            ax.plot([0, v], [i, i], color=col, linewidth=1.6, zorder=2)
            ax.scatter([v], [i], s=55, color=col, zorder=3)
            ax.text(v + 3, i, f"{x.used:.0f}%" + (" ▶" if x.used > lim else ""), va="center", fontsize=9, color=INK)
        ax.axvline(100, color=RED, linewidth=1.0, linestyle=(0, (4, 3)))
        ax.axvline(NEAR_CAP, color=AMBER, linewidth=1.0, linestyle=(0, (4, 3)))
        bank_axis(ax, near)
        ax.set_xlim(0, lim * 1.12)
        ax.set_xlabel("Reciprocal cap used, % (dashed lines at 80% and 100%)", fontsize=10.5)
        save(fig, "nearcap")

    # 6. Special cap: current reciprocal vs. estimated special cap
    spec = net.special_users()[:15]
    if spec:
        fig, ax = axes(7.4, 3.6)
        for i, x in enumerate(spec):
            cur, est = x.reciprocal / 1e3, (x.special_cap_est or 0) / 1e3
            if x.special_cap_est:
                ax.plot([est, cur], [i, i], color=RED if cur > est else GRID, linewidth=2.4, zorder=2)
                ax.scatter([est], [i], s=60, color=INK, marker="|", linewidths=2.4, zorder=3)
            ax.scatter([cur], [i], s=60, color=RED if x.special_cap_est and cur > est else BLUE, zorder=4)
        ax.scatter([], [], s=50, color=BLUE, label="Reciprocal deposits now")
        ax.scatter([], [], s=50, color=INK, marker="|", linewidths=2.4, label="Est. special cap (4Q avg)")
        ax.plot([], [], color=RED, linewidth=2.4, label="Above it: would count as brokered")
        bank_axis(ax, spec)
        from matplotlib.ticker import FuncFormatter
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{D}{v / 1e3:,.0f}B" if v >= 1e3 else f"{D}{v:,.0f}M"))
        ax.set_xlabel("Log scale", fontsize=10.5)
        ax.legend(loc="lower right", frameon=False, fontsize=9)
        save(fig, "special")

    # 7. Closest to the special cap: capital cushion
    close = net.closest_to_special()[:15]
    if close:
        fig, ax = axes(7.4, 3.7)
        for i, x in enumerate(close):
            col = RED if x.cushion < 0 else AMBER if x.cushion < 0.5 else BLUE
            ax.plot([0, x.cushion], [i, i], color=col, linewidth=1.6, zorder=2)
            ax.scatter([x.cushion], [i], s=55, color=col, zorder=3)
            ax.text(max(x.cushion, 0) + 0.05, i, f"{x.cushion:+.2f}  {x.binding}", va="center", fontsize=8.5, color=MUTED)
        ax.axvline(0, color=INK, linewidth=1.0)
        bank_axis(ax, close)
        lo = min(0.0, min(x.cushion for x in close))
        hi = max(0.0, max(x.cushion for x in close))
        span = max(hi - lo, 0.5)
        ax.set_xlim(lo - span * 0.08, hi + span * 0.9)
        ax.set_xlabel("Points above the nearest well-capitalized line (0 = the line)", fontsize=10.5)
        save(fig, "cushion")

    # 8. Uninsured: distribution by segment, and the largest uninsured balances with room under the cap
    segs = [(lab, [x.uninsured_share for x in net.banks if lo <= (x.assets or 0) < hi and x.uninsured_share is not None])
            for lab, lo, hi in SEGMENTS]
    segs = [(lab, v) for lab, v in segs if v]
    if segs:
        fig, axs = plt.subplots(1, len(segs), figsize=(7.4, 2.8), sharey=False)
        fig.patch.set_facecolor(SURFACE)
        axs = axs if len(segs) > 1 else [axs]
        bins = list(range(0, 105, 10))
        for ax, (lab, vals) in zip(axs, segs):
            ax.set_facecolor(SURFACE)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            n, _, patches = ax.hist([min(v, 99.9) for v in vals], bins=bins, color=BLUE, edgecolor=SURFACE, zorder=3)
            for p_, lo_ in zip(patches, bins):
                if lo_ >= HIGH_UNINSURED:
                    p_.set_facecolor("#eb6834")
            ax.yaxis.grid(True, color=GRID, zorder=0)
            ax.tick_params(length=0, labelsize=8.5)
            hi_n = sum(1 for v in vals if v >= HIGH_UNINSURED)
            ax.set_title(lab.replace("$", D), fontsize=10, color=INK, loc="left", fontweight="bold", pad=20)
            ax.text(0, 1.02, f"median {statistics.median(vals):.0f}% · {hi_n:,} at {HIGH_UNINSURED:.0f}%+",
                    transform=ax.transAxes, fontsize=8.5, color=MUTED, va="bottom")
        axs[0].set_ylabel("Banks", fontsize=10)
        fig.text(0.5, 0.01, "Uninsured deposits, % of deposits (orange = 40%+)", ha="center", fontsize=10, color=MUTED)
        fig.tight_layout(rect=(0, 0.05, 1, 1))
        save(fig, "uninsured", tight=False)
    pro = net.prospects()[:12]
    if pro:
        fig, ax = axes(7.4, 3.0)
        ax.barh(range(len(pro)), [(x.uninsured or 0) / 1e6 for x in pro], color="#eb6834", height=0.6, zorder=3)
        for i, x in enumerate(pro):
            ax.text((x.uninsured or 0) / 1e6, i, f"  {_pct(x.uninsured_share)} uninsured · {_pct(x.used or 0)} of cap",
                    va="center", fontsize=8.5, color=MUTED)
        bank_axis(ax, pro)
        ax.set_xlim(0, max((x.uninsured or 0) for x in pro) / 1e6 * 1.6)
        ax.set_xlabel(f"Uninsured deposits, {D}B", fontsize=10.5)
        save(fig, "prospects")

    # 0. Highest-risk banks: one row per bank, one colored cell per measure (green = fine ... red = worst)
    rk = net.risk()
    if rk:
        from matplotlib.patches import FancyBboxPatch
        level_color = {0: "#e8f3ec", 1: "#fdf1cf", 2: "#fbd9b8", 3: "#f6c4bd"}
        level_ink = {0: "#2f7d4f", 1: "#8a6a00", 2: "#b4541a", 3: "#a3261b"}
        cols = ["Reciprocal", "Cap\nused", "Special\ncap", "Capital\ncushion", "Uninsured\nshare", "1-yr\ntrend"]
        fig = plt.figure(figsize=(7.4, 0.36 * len(rk) + 0.75))
        fig.patch.set_facecolor(SURFACE)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, 100)
        n = len(rk)
        ax.set_ylim(n + 1.3, -0.45)
        ax.axis("off")
        x_name, x0, w = 0.5, 40.0, 10.0
        ax.text(x_name, 0.3, "BANK", fontsize=8.5, color=MUTED, fontweight="bold", va="center")
        ax.text(x0 - 1.2, 0.3, "SCORE", fontsize=8.5, color=MUTED, fontweight="bold", va="center", ha="right")
        for j, c in enumerate(cols):
            ax.text(x0 + j * w + w / 2, 0.3, c.upper(), fontsize=7.6, color=MUTED, fontweight="bold", ha="center", va="center",
                    linespacing=1.1)
        for i, b in enumerate(rk, 1):
            sc, lv = risk_score(b)
            y = i + 0.15
            ax.text(x_name, y, f"{i}. {short(b.bank.name, 24)}", fontsize=9.5, color=INK, fontweight="bold", va="center")
            ax.text(x0 - 1.2, y, str(sc), fontsize=9.5, color=INK, ha="right", va="center", fontweight="bold")
            cells = [(None, _fmt_money(b.reciprocal))] + [(lv[m][0], lv[m][1]) for m in RISK_MEASURES]
            for j, (lev, txt) in enumerate(cells):
                cx = x0 + j * w
                if lev is not None:
                    ax.add_patch(FancyBboxPatch((cx + 0.5, y - 0.38), w - 1.0, 0.76, boxstyle="round,pad=0,rounding_size=0.25",
                                                facecolor=level_color[lev], edgecolor="none", mutation_aspect=0.1))
                ax.text(cx + w / 2, y, txt, fontsize=8.6, ha="center", va="center",
                        color=INK if lev is None else level_ink[lev], fontweight="bold" if lev and lev >= 2 else "normal")
        for k, (lab, lev) in enumerate((("fine", 0), ("watch", 1), ("elevated", 2), ("high", 3))):
            cx = x0 + 10.4 + k * 12.5
            ax.add_patch(FancyBboxPatch((cx, n + 0.85), 2.2, 0.36, boxstyle="round,pad=0,rounding_size=0.2",
                                        facecolor=level_color[lev], edgecolor="none", mutation_aspect=0.1))
            ax.text(cx + 3.0, n + 1.03, lab, fontsize=8, color=MUTED, va="center")
        ax.text(x_name, n + 1.03, "Score = points across the five measures", fontsize=8, color=MUTED, va="center")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, facecolor=SURFACE)
        plt.close(fig)
        out["risk"] = buf.getvalue()

    # 9. Five-quarter trend: is the gap getting worse? Small multiples, one bank per panel.
    def multiples(key, banks, draw, nrows=2, ncols=3):
        banks = banks[: nrows * ncols]
        if not banks:
            return
        fig, axs = plt.subplots(nrows, ncols, figsize=(7.4, 2.25 * nrows + 0.3), squeeze=False)
        fig.patch.set_facecolor(SURFACE)
        for k, ax in enumerate(axs.flat):
            ax.set_facecolor(SURFACE)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.tick_params(length=0, labelsize=8)
            if k >= len(banks):
                ax.axis("off")
                continue
            draw(ax, banks[k])
            ax.yaxis.grid(True, color=GRID, zorder=0)
        fig.tight_layout(pad=0.5, h_pad=1.2, w_pad=0.8)
        save(fig, key, tight=False)

    def qlab(d):
        return f"Q{(d.month - 1) // 3 + 1}\n{str(d.year)[2:]}"

    def money_axis(ax, vals):
        top = max(v for v in vals if v is not None) or 1
        div, unit = (1e6, "B") if top >= 1e6 else (1e3, "M")
        from matplotlib.ticker import FuncFormatter
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{D}{v / div:,.1f}{unit}" if div == 1e6 else f"{D}{v / div:,.0f}{unit}"))

    def draw_cap(ax, x):
        ser = [h for h in x.series() if h[2]]
        qs = [qlab(h[0]) for h in ser]
        rec, cap = [h[1] for h in ser], [h[2] for h in ser]
        xs = range(len(ser))
        ax.fill_between(xs, cap, rec, where=[r > c for r, c in zip(rec, cap)], color=RED, alpha=0.15, interpolate=True, zorder=1)
        ax.plot(xs, cap, color=MUTED, linewidth=1.4, linestyle=(0, (3, 2)), zorder=2)
        ax.plot(xs, rec, color=RED if rec[-1] > cap[-1] else AMBER, linewidth=2.2, marker="o", markersize=3.5, zorder=3)
        ax.set_xticks(list(xs))
        ax.set_xticklabels(qs)
        money_axis(ax, rec + cap)
        ax.set_ylim(0, max(rec + cap) * 1.12)
        g = x.gap_change()
        ax.set_title(short(x.bank.name, 26), fontsize=9.5, color=INK, loc="left", fontweight="bold", pad=15)
        ax.text(0, 1.02, f"{x.used:.0f}% of cap · gap {'+' if g >= 0 else '-'}{_fmt_money(abs(g))} "
                + ("wider" if g > 0 else "narrower"), transform=ax.transAxes, fontsize=8,
                color=RED if g > 0 else BLUE, va="bottom")

    multiples("trend_cap", [x for x in net.trend() if len([h for h in x.series() if h[2]]) >= 3], draw_cap)

    def draw_special(ax, x):
        ser = x.series()
        xs = range(len(ser))
        rec = [h[1] for h in ser]
        est = x.special_cap_est
        if est:
            ax.axhline(est, color=INK, linewidth=1.3, linestyle=(0, (3, 2)), zorder=2)
            ax.fill_between(xs, [est] * len(rec), rec, where=[r > est for r in rec], color=RED, alpha=0.15,
                            interpolate=True, zorder=1)
        col = RED if est and rec[-1] > est else BLUE
        ax.plot(xs, rec, color=col, linewidth=2.2, marker="o", markersize=3.5, zorder=3)
        ax.set_xticks(list(xs))
        ax.set_xticklabels([qlab(h[0]) for h in ser])
        money_axis(ax, rec + [est or 0])
        ax.set_ylim(0, max(rec + [est or 0]) * 1.15)
        g = x.special_gap_change()
        ax.set_title(short(x.bank.name, 26), fontsize=9.5, color=INK, loc="left", fontweight="bold", pad=15)
        ax.text(0, 1.02, (f"est. cap {_fmt_money(est)} · " if est else "") + f"{'+' if g >= 0 else '-'}{_fmt_money(abs(g))} in a year",
                transform=ax.transAxes, fontsize=8, color=RED if g > 0 else BLUE, va="bottom")

    multiples("trend_special", [x for x in net.special_users() if len(x.series()) >= 3], draw_special)
    return out


def build(net: Network) -> Network:
    net.summary, net.summary_by = write_summary(net)
    try:
        net.images = charts(net)
    except Exception as e:
        log.warning("network charts failed: %s", e)
    return net
