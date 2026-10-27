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
    history: list = field(default_factory=list)            # [(quarter end, reciprocal $000, cap $000, below WC?)] newest first
    state: str = ""
    ratios: dict = field(default_factory=dict)             # the filing's ratios (capital, asset quality, earnings, funding)
    order_date: Optional[date] = None                      # earliest active enforcement order, if any
    special_hist: list = field(default_factory=list)       # [(quarter end, reciprocal)]: the 4 quarters the special cap averages
    special_basis: str = ""                                # plain-English note on which quarters, and why

    @property
    def special_cap_est(self) -> Optional[float]:
        '''Estimated special cap: average quarter-end reciprocal deposits over the four quarters before this one
        (what the bank would be held to if it lost well-capitalized/well-rated status now). With fewer than two of
        those on hand, the quarters available including this one; None if under two in all.'''
        if self.special_hist:
            vals = [v for _, v in self.special_hist if v is not None]
            return sum(vals) / len(vals) if vals else None
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

    def year_ago_used(self) -> Optional[float]:
        """% of cap used at the oldest quarter on hand (up to four quarters back)."""
        ser = [h for h in self.series() if h[2]]
        return ser[0][1] / ser[0][2] * 100 if len(ser) >= 2 else None

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
        """Watch list: reciprocal users with two or more network issues, largest reciprocal balance first."""
        return sorted([b for b in self.users if len(issues(b)) >= 2], key=lambda b: -(b.reciprocal or 0))[:n]

    def watch_count(self) -> int:
        return sum(1 for b in self.users if len(issues(b)) >= 2)

    def distressed(self) -> list[NetBank]:
        """Reciprocal users with weak capital plus bad loans or losses, or any three distress signs; largest first."""
        out = []
        for b in self.users:
            f = distress_flags(b)
            if ("Capital" in f and ("Asset quality" in f or "Earnings" in f)) or len(f) >= 3:
                out.append(b)
        return sorted(out, key=lambda b: -(b.reciprocal or 0))

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


def _size(meta, r: dict) -> float:
    """Best figure for a bank's size ($000): stored assets, the filing's total assets, or total liabilities (a floor).
    institution_meta can lack assets for some filers, so the largest of the three is used."""
    return max(meta.total_assets or 0, _num(r, "total_assets") or 0, _num(r, LIABILITIES_RAW_KEY) or 0)


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
        r = json.loads(raw) if raw else {}
        if excluded_bank(meta.name, _size(meta, r)):            # known large: skip before any pull
            net.excluded.append(meta.name)
            continue
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
        size = _size(meta, r)
        if excluded_bank(meta.name, size):
            net.excluded.append(meta.name)
            continue
        b = _bank(meta, period_end)
        if not meta.total_assets and size:
            from .report import fmt_assets
            b.assets, b.assets_raw = fmt_assets(size), size
        rec, liab = _num(r, RECIPROCAL_RAW_KEY), _num(r, LIABILITIES_RAW_KEY)
        cap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * liab) if liab else None
        unins, deps = _num(r, UNINSURED_RAW_KEY), _num(r, DEPOSITS_RAW_KEY)
        nb = NetBank(b, period_end, rec, cap, (rec / cap * 100) if (rec is not None and cap) else None, unins,
                     (unins / deps * 100) if (unins is not None and deps) else None, deps, size)
        if capital_status(r):
            nb.special_cap.append("below well capitalized")
        nb.state = b.location.rsplit(",", 1)[-1].strip()
        nb.capital = {k: _num(r, k) for k in ("tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio", "cet1_ratio")}
        nb.cushion, nb.binding = capital_cushion(nb.capital)
        nb.ratios = {k: _num(r, k) for k in ("noncurrent_loans_oreo_ratio", "roaa", "net_noncore_funding_dependence_ratio",
                                             "net_interest_margin", "net_loans_leases_to_deposits")}
        net.banks.append(nb)
    # enforcement -> likely special cap
    if enforcement_sources is not None:
        from .enforcement import match
        for m in match([nb.bank for nb in net.banks], enforcement_sources.actions):
            if not m.action.stale:
                for nb in net.banks:
                    if nb.bank.rssd == m.bank.rssd:
                        if "under an enforcement order" not in nb.special_cap:
                            nb.special_cap.append("under an enforcement order")
                        if m.action.start and (nb.order_date is None or m.action.start < nb.order_date):
                            nb.order_date = m.action.start
    # Five quarters of history for every bank the report features: reciprocal deposits and the cap each quarter,
    # for the estimated special cap and the trend pages. Pulled once and cached (network_history).
    if prior_lookup is not None:
        focus = {}
        for b in (net.special() + net.top_exposure() + net.near_cap()[:20] + net.closest_to_special()[:15]
                  + net.prospects()[:10] + net.risk(25)):
            focus[id(b)] = b
        for nb in focus.values():
            nb.history = [(nb.period, nb.reciprocal, nb.cap, "below well capitalized" in nb.special_cap)]
            q = nb.period
            for i in range(QUARTERS - 1):
                q = prior_quarter_end(q)
                try:
                    pr = prior_lookup(nb.bank.rssd, q) or {}
                except Exception:
                    pr = {}
                prec, pliab = _num(pr, RECIPROCAL_RAW_KEY), _num(pr, LIABILITIES_RAW_KEY)
                pcap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * pliab) if pliab else None
                nb.history.append((q, prec, pcap, pr.get("_below_wc")))
                if i == 0 and prec is not None:
                    nb.prior_reciprocal = prec
                    nb.prior_used = prec / pcap * 100 if pcap else None
        for nb in net.special():
            special_quarters(nb, prior_lookup)
    return net


def _qend(d: date) -> date:
    """Quarter end of the quarter containing d."""
    m = ((d.month - 1) // 3 + 1) * 3
    return date(d.year, m, 30 if m in (6, 9) else 31)


def special_quarters(nb: "NetBank", lookup) -> None:
    """Which four quarters the special cap averages, per 12 CFR 337.6(e): the four quarter-ends BEFORE the quarter the
    bank stopped being well capitalized and well rated. That date isn't public, so it is estimated as the earlier of
    (a) the quarter of its earliest active enforcement order and (b) the first quarter of its current run below well
    capitalized in the Call Reports on hand."""
    from .performance import prior_quarter_end
    trig, why = None, ""
    if "below well capitalized" in nb.special_cap:
        run = nb.history[0][0]
        for h in nb.history:
            if len(h) > 3 and h[3]:
                run = h[0]
            elif len(h) > 3 and h[3] is False:
                break
        trig, why = run, f"first below well capitalized in {_quarter(run)}"
        if run == nb.history[-1][0] and len(nb.history) >= 5:
            why = f"below well capitalized since at least {_quarter(run)}"
    if nb.order_date:
        oq = _qend(nb.order_date)
        if trig is None or oq < trig:
            trig, why = oq, f"enforcement order dated {nb.order_date:%b %Y}"
    if trig is None:
        return
    have = {h[0]: h[1] for h in nb.history}
    q, quarters = trig, []
    for _ in range(4):
        q = prior_quarter_end(q)
        if q not in have:
            try:
                have[q] = _num(lookup(nb.bank.rssd, q) or {}, RECIPROCAL_RAW_KEY)
            except Exception:
                have[q] = None
        quarters.append((q, have[q]))
    nb.special_hist = quarters
    got = [v for _, v in quarters if v is not None]
    span = f"{_quarter(quarters[-1][0])} to {_quarter(quarters[0][0])}"
    nb.special_basis = (f"average of {span} ({why})" if len(got) == 4 else
                        f"average of {len(got)} of the 4 quarters {span} on file ({why})" if got else
                        f"no reciprocal figures on file for {span} ({why})")


# ------------------------------------------------------------------ watch list and distress (plain flags, no scores)
def issues(b: "NetBank") -> list[str]:
    """Network issues, in plain words."""
    out = []
    if (b.used or 0) > 100:
        out.append("over the cap")
    elif (b.used or 0) >= NEAR_CAP:
        out.append("near the cap")
    if b.special_cap:
        out.append("over its special cap" if (b.over_special or 0) > 0 else "likely on the special cap")
    elif b.cushion is not None and b.cushion < 0.5:
        out.append("under the 9% CBLR line" if b.cushion < 0 else "within 0.5 pts of a capital minimum")
    if (b.used or 0) >= NEAR_CAP and (b.gap_change() or 0) > 0:
        out.append("gap to the cap widening")
    if (b.uninsured_share or 0) >= 50:
        out.append("50%+ uninsured")
    return out


DISTRESS_RULES = [
    ("Capital", "below well capitalized, under the 9% CBLR line, or within 0.5 pts of a minimum"),
    ("Asset quality", "noncurrent loans + foreclosed real estate at 3% or more of loans"),
    ("Earnings", "losing money (annualized return on assets below 0)"),
    ("Funding", "net non-core funding dependence of 30% or more"),
]


def distress_flags(b: "NetBank") -> dict[str, str]:
    f = {}
    if "below well capitalized" in b.special_cap:
        f["Capital"] = "below well capitalized"
    elif b.cushion is not None and b.cushion < 0.5:
        f["Capital"] = f"{b.cushion:+.2f} pts"
    r = b.ratios or {}
    if (r.get("noncurrent_loans_oreo_ratio") or 0) >= 3:
        f["Asset quality"] = f"{r['noncurrent_loans_oreo_ratio']:.1f}% noncurrent"
    if r.get("roaa") is not None and r["roaa"] < 0:
        f["Earnings"] = f"ROA {r['roaa']:.2f}%"
    if (r.get("net_noncore_funding_dependence_ratio") or 0) >= 30:
        f["Funding"] = f"{r['net_noncore_funding_dependence_ratio']:.0f}% non-core"
    return f


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
    '''A past quarter's reciprocal deposits, total liabilities and whether the bank was below well capitalized: cached
    in network_history, else the stored snapshot, else a Call Report pull (cached, including "nothing filed").'''
    from datetime import datetime
    from . import db, ratios as ratios_mod
    from .report import capital_status
    conn.execute(HISTORY_SQL)
    conn.execute("ALTER TABLE network_history ADD COLUMN IF NOT EXISTS below_wc BOOLEAN")
    conn.execute("ALTER TABLE network_history ADD COLUMN IF NOT EXISTS checked_capital BOOLEAN")

    def lookup(rssd: str, period_end: date) -> Optional[dict]:
        row = conn.execute("SELECT reciprocal, liabilities, below_wc, checked_capital FROM network_history "
                           "WHERE id_rssd = ? AND period_end = ?", [rssd, period_end]).fetchone()
        if row is not None and (row[3] or client is None):
            return {RECIPROCAL_RAW_KEY: row[0], LIABILITIES_RAW_KEY: row[1], "_below_wc": row[2]}
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
        has_cap = any(isinstance(r.get(k), (int, float)) for k in ("tier1_leverage_ratio", "total_rbc_ratio"))
        below = bool(capital_status(r)) if has_cap else None
        conn.execute("INSERT OR REPLACE INTO network_history (id_rssd, period_end, reciprocal, liabilities, fetched_at, "
                     "below_wc, checked_capital) VALUES (?, ?, ?, ?, ?, ?, TRUE)",
                     [rssd, period_end, rec, liab, datetime.now(), below])
        return {RECIPROCAL_RAW_KEY: rec, LIABILITIES_RAW_KEY: liab, "_below_wc": below}
    return lookup


# ------------------------------------------------------------------ narrative
PROMPT = """You write the opening summary of a weekly risk report for a firm that runs a reciprocal deposit network.
The reader is its risk team. Plain English, ONE paragraph of 4 to 6 sentences, up to 130 words, risk only. Cover, in order:
the watch list (banks with two or more network issues) and the largest names on it; banks showing signs of financial
distress; banks over or near the reciprocal cap (lesser of $5 billion or 20% of liabilities; over it,
more placements count as brokered) and whether those gaps widened over the past year; banks likely on the special cap
(enforcement order or below well capitalized, held to their four-quarter average), especially any above their estimate;
and the users closest to slipping onto the special cap. Name at most four banks.

Wrap the 2 to 4 most important points in **double asterisks** (a short phrase each, e.g. **197 banks are over their cap**).

Rules: use ONLY facts and numbers in the data; copy numbers exactly; no predictions, no advice, no other markdown.

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
                     + (f"special cap basis: {b.special_basis}; " if b.special_basis else "")
                     + (f"estimated special cap {_fmt_money(est)}; "
                        + (f"{_fmt_money(over)} above it (would count as brokered)" if over and over > 0 else "within it")
                        if est else "special cap not estimated (history unavailable)"))
    lines.append(f"WATCH LIST (reciprocal users with two or more network issues): {net.watch_count()} banks. Largest:")
    for b in net.risk(6):
        lines.append(f"  {b.bank.name} ({b.bank.location}): reciprocal {_fmt_money(b.reciprocal)}; issues: {', '.join(issues(b))}")
    ds = net.distressed()
    lines.append(f"SIGNS OF FINANCIAL DISTRESS (weak capital plus bad loans or losses, or three distress signs): {len(ds)} reciprocal users.")
    for b in ds[:5]:
        lines.append(f"  {b.bank.name}: reciprocal {_fmt_money(b.reciprocal)}; " + "; ".join(f"{k}: {v}" for k, v in distress_flags(b).items()))
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
    parts = []
    rk = net.risk(2)
    if rk:
        parts.append(f"**{net.watch_count()} reciprocal users are on the watch list** with two or more issues; the largest is "
                     f"{short(rk[0].bank.name)} ({', '.join(issues(rk[0]))}).")
    ds = net.distressed()
    if ds:
        parts.append(f"**{len(ds)} show signs of financial distress** (weak capital plus bad loans or losses).")
    parts.append(f"**{s['over_cap']} banks are over their reciprocal cap** and {s['near_cap']} more are at {NEAR_CAP:.0f}-100%; "
                 "above the cap, more placements count as brokered.")
    if s["special_all"]:
        over = sum(1 for b in net.special() if (b.over_special or 0) > 0)
        parts.append(f"**{s['special_all']} banks are likely on the special cap**, {s['special']} of them holding reciprocal deposits"
                     + (f"; {over} hold more than their estimated four-quarter average." if over else "."))
    close = net.closest_to_special()[:1]
    if close:
        b = close[0]
        parts.append(f"Closest to the special cap: {short(b.bank.name)}, {b.cushion:+.2f} pts on {b.binding}.")
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
                t = re.sub(r"\s+", " ", t).strip()
                plain = t.replace("**", "")
                if t.count("**") % 2:
                    t = plain
                if plain and len(plain) < 1200 and _numbers(plain) <= (_numbers(data) | {str(i) for i in range(0, 13)}):
                    return t, "Claude"
        except Exception as e:
            log.warning("network summary unavailable: %s", e)
    return rule_summary(net), "rules"


# ------------------------------------------------------------------ key takeaways (page 1)
def takeaways(net: Network) -> list[str]:
    """Risk points for page 1. **...** marks the phrase to bold."""
    s = net.stats()
    out = []
    rk = net.risk(3)
    if rk:
        out.append(f"**{net.watch_count()} banks are on the watch list** (two or more issues). Largest: "
                   + "; ".join(f"{short(b.bank.name)} ({_fmt_money(b.reciprocal)})" for b in rk) + ".")
    ds = net.distressed()
    if ds:
        out.append(f"**{len(ds)} reciprocal users show signs of financial distress**, holding "
                   f"{_fmt_money(sum(b.reciprocal or 0 for b in ds))} of reciprocal deposits. Largest: {short(ds[0].bank.name)}.")
    if s["over_cap"] or s["near_cap"]:
        top = net.near_cap()[0]
        out.append(f"**{s['over_cap']} banks are over their cap**, where more placements count as brokered; {s['near_cap']} more "
                   f"are at {NEAR_CAP:.0f}-100%. Highest: {short(top.bank.name)} at {top.used:.0f}%.")
    tr = net.trend()
    if tr:
        wid = [b for b in tr if b.gap_change() > 0]
        out.append(f"**The gap to the cap widened at {len(wid)} of {len(tr)}** banks near or over it over the past year"
                   + (f"; most at {short(wid[0].bank.name)} (+{_fmt_money(wid[0].gap_change())})." if wid else "."))
    if s["special_all"]:
        over = [b for b in net.special() if (b.over_special or 0) > 0]
        out.append(f"**{s['special_all']} banks are likely on the special cap** (enforcement order or below well capitalized)"
                   + (f"; {len(over)} hold more than their estimated four-quarter average." if over else "."))
    close = net.closest_to_special()
    neg = sum(1 for b in close if b.cushion < 0)
    thin = sum(1 for b in close if 0 <= b.cushion < 0.5)
    if close:
        out.append(f"**{neg + thin} reciprocal users are within 0.5 pts of losing well-capitalized status** or already under the "
                   f"9% CBLR line; the next candidates for the special cap.")
    return out


def short(name: str, n: int = 28) -> str:
    t = re.sub(r",? (NATIONAL ASSOCIATION|N\.A\.|THE)$", "", (name or "").upper()).title()
    t = t.replace("And Trust", "& Trust").replace("Company", "Co.")
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


GROUP_NOTES = [
    ("Likely on the special cap", "enforcement order or below well capitalized; held to the four-quarter average."),
    ("Largest reciprocal users", "biggest balances; past 100% of the cap, new placements count as brokered."),
    ("Thinnest capital cushion", "not yet on the special cap, but closest to it."),
]
GROUP_N = 7


def watch_groups(net: "Network") -> list[tuple[str, str, list]]:
    return [
        ("Likely on the special cap", "Enforcement order or below well capitalized: held to their four-quarter average",
         net.special_users()[:GROUP_N]),
        ("Largest reciprocal users", "Biggest balances, and how much of each bank's cap they use",
         net.top_exposure(GROUP_N)),
        ("Thinnest capital cushion", "Not yet on the special cap, closest to slipping below well capitalized",
         net.closest_to_special()[:GROUP_N]),
    ]


# ------------------------------------------------------------------ charts
# Built on network_style: navy/blue data, grey context, red only for a breach, Inter throughout, direct labels.
DRIVER_WORDS = {"Cap used": {3: "over cap", 2: "near cap"}, "Special cap": {3: "special cap"},
                "Capital cushion": {3: "below well capitalized", 2: "thin capital"},
                "Uninsured": {3: "60%+ uninsured", 2: "50%+ uninsured"}, "1-yr trend": {2: "gap rising fast"}}


def drivers(b: "NetBank") -> str:
    lv = risk_levels(b)
    words = [DRIVER_WORDS.get(m, {}).get(v[0]) for m, v in lv.items()]
    if lv["Special cap"][1] == "over est.":
        words[1] = "over special cap"
    return " · ".join(w for w in words if w) or "several moderate flags"


def charts(net: Network) -> dict[str, bytes]:
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    from . import network_style as S
    plt = S.mpl()
    out = {}
    M = S.dollars
    W = 7.3                                                     # inches: the PDF text width

    def hbar(ax, banks, vals, colors, labels, xmax, ticks=None, ref=None, fmt=None, bold=None):
        n = len(banks)
        ax.barh(range(n), vals, color=colors, height=0.6, zorder=3)
        if ref is not None:
            ax.axvline(ref, color=S.INK, linewidth=0.8, linestyle=(0, (2, 2)), zorder=2)
        for i, t in enumerate(ticks or []):
            if t:
                ax.plot([t, t], [i - 0.4, i + 0.4], color=S.NAVY, linewidth=1.8, solid_capstyle="butt", zorder=4)
        for i, (v, lab) in enumerate(zip(vals, labels)):
            x = max(v, 0, (ticks[i] if ticks and ticks[i] else 0)) + xmax * 0.012
            if bold:
                t = ax.text(x, i, M(bold[i]), va="center", fontsize=8.8, color=S.INK, fontweight="semibold", zorder=5)
                ax.set_xlim(min(0, min(vals) * 1.2), xmax)
                bb = t.get_window_extent(renderer=ax.figure.canvas.get_renderer())
                x = ax.transData.inverted().transform((bb.x1, bb.y0))[0] + xmax * 0.012
            if lab:
                ax.text(x, i, M(lab), va="center", fontsize=8.4, color=S.MUTED, zorder=5)
        ax.set_yticks(range(n))
        ax.set_yticklabels([short(b.bank.name, 30) for b in banks])
        ax.set_ylim(n - 0.45, -0.55)
        ax.set_xlim(min(0, min(vals) * 1.2), xmax)
        ax.xaxis.set_major_locator(MaxNLocator(5))
        if fmt:
            ax.xaxis.set_major_formatter(FuncFormatter(fmt))
        S.clean(ax, "x")

    def figure(n, extra=0.3):
        fig, ax = plt.subplots(figsize=(W, 0.27 * n + extra))
        return fig, ax

    musd = lambda v, _: M(f"${v:,.0f}M")                                   # noqa: E731
    busd = lambda v, _: M(f"${v:,.1f}B" if v else "$0")                    # noqa: E731
    pctf = lambda v, _: f"{v:.0f}%"                                         # noqa: E731

    # Watch list and distress: reciprocal dollars at stake, with the issues written out (no scores)
    def issue_chart(key, banks, words):
        if not banks:
            return
        fig, ax = figure(len(banks))
        v = [(x.reciprocal or 0) / 1e3 for x in banks]
        hbar(ax, banks, v, [S.NAVY] * len(banks), [words(x) for x in banks], max(v) * 2.6, fmt=musd,
             bold=[_fmt_money(x.reciprocal) for x in banks])
        out[key] = S.png(fig)

    issue_chart("risk", net.risk(), lambda x: " · ".join(issues(x)))
    issue_chart("distress", net.distressed()[:12], lambda x: " · ".join(distress_flags(x).values()))

    # Cap use by bank size: 100% stacked bar per segment
    rows = [(lab, [x for x in net.banks if lo <= (x.assets or 0) < hi]) for lab, lo, hi in SEGMENTS]
    rows = [(lab, g) for lab, g in rows if g] + [("All banks", net.banks)]
    if net.banks:
        fig, ax = plt.subplots(figsize=(W, 0.55 * len(rows) + 0.75))
        names = ["No reciprocal", "Under 25%", "25-50%", "50-80%", "80-100%", "Over 100%"]
        for i, (lab, grp) in enumerate(rows):
            sub = Network(net.run_date, grp, net.quarter_label)
            pc = [n / len(grp) * 100 for _, n in sub.buckets()]
            left = 0
            for j, v in enumerate(pc):
                ax.barh(i, v, left=left, color=S.SEQ[j], height=0.62, edgecolor="white", linewidth=1.2, zorder=3)
                if v >= 6:
                    ax.text(left + v / 2, i, f"{v:.0f}%", ha="center", va="center", fontsize=8,
                            color="white" if j >= 3 else S.INK, fontweight="medium", zorder=4)
                left += v
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([M(f"{lab}\n{len(g):,} banks") for lab, g in rows], fontsize=8.8, linespacing=1.35)
        ax.set_ylim(len(rows) - 0.45, -1.15)
        ax.set_xlim(0, 100)
        ax.set_xticks([])
        S.clean(ax, None)
        x = 0
        for j, nm in enumerate(names):
            ax.add_patch(plt.Rectangle((x, -1.02), 1.8, 0.28, color=S.SEQ[j], clip_on=False, zorder=4))
            ax.text(x + 2.5, -0.88, nm, fontsize=8, color=S.MUTED, va="center")
            x += 2.5 + 1.25 * len(nm) + 3.5
        out["segments"] = S.png(fig)

    # Exposure map: the 20 largest holders, labeled directly
    top = [x for x in net.top_exposure() if x.used is not None and x.uninsured_share is not None]
    if top:
        fig, ax = plt.subplots(figsize=(W, 6.0))
        ymax = max(150, min(400, max(x.used for x in top) * 1.08))
        ax.axhspan(100, ymax + 15, color=S.RED, alpha=0.045, zorder=0, linewidth=0)
        ax.axhline(100, color=S.RED, linewidth=0.8, linestyle=(0, (2, 2)), zorder=1)
        ax.axvline(HIGH_UNINSURED, color=S.MUTED, linewidth=0.7, linestyle=(0, (2, 2)), zorder=1)
        ax.text(0.5, ymax + 12, "Over the cap: new placements count as brokered", fontsize=7.8, color=S.RED, va="top")
        ax.text(HIGH_UNINSURED + 0.6, ymax + 12, f"{HIGH_UNINSURED:.0f}% uninsured", fontsize=7.8, color=S.MUTED, va="top")
        xs = [x.uninsured_share for x in top]
        ys = [min(x.used, ymax) for x in top]
        cols = [S.RED if x.special_cap else S.BLUE for x in top]
        ax.scatter(xs, ys, s=[30 + 140 * (x.reciprocal / top[0].reciprocal) for x in top], color=cols, alpha=0.9,
                   edgecolors="white", linewidths=0.8, zorder=3)
        texts = [ax.text(px, py, short(x.bank.name, 30) + (f" ({x.used:.0f}%)" if x.used > ymax else ""), fontsize=7.4,
                         color=S.INK, zorder=4) for px, py, x in zip(xs, ys, top)]
        try:
            from adjustText import adjust_text
            ax.set_xlim(0, max(60, max(xs) * 1.12))
            ax.set_ylim(0, ymax + 15)
            adjust_text(texts, x=xs, y=ys, ax=ax, expand=(1.2, 1.6), force_text=(0.4, 0.8), max_move=(40, 40),
                        arrowprops=dict(arrowstyle="-", color=S.CONTEXT, lw=0.6))
        except Exception:
            for t in texts:
                t.set_position((t.get_position()[0] + 0.8, t.get_position()[1] + 2.5))
        ax.set_xlim(0, max(60, max(xs) * 1.12))
        ax.set_ylim(0, ymax + 15)
        ticks = [t for t in (0, 50, 100, 150, 200, 300, 400) if t <= ymax]
        ax.set_yticks(ticks)
        ax.set_yticklabels([f"{t}%" for t in ticks[:-1]] + [f"{ticks[-1]}%" + ("+" if max(x.used for x in top) > ymax else "")])
        ax.xaxis.set_major_formatter(FuncFormatter(pctf))
        ax.set_xlabel("Uninsured deposits, % of deposits")
        ax.set_ylabel("Reciprocal cap used")
        S.clean(ax, "y")
        ax.tick_params(axis="y", colors=S.MUTED)
        out["map"] = S.png(fig)

    # Top 20 holders: reciprocal deposits, tick = the bank's cap
    top = net.top_exposure()
    if top:
        fig, ax = figure(len(top))
        v = [(x.reciprocal or 0) / 1e6 for x in top]
        hbar(ax, top, v, [S.RED if (x.used or 0) > 100 else S.BLUE for x in top], [f"{_pct(x.used)} of cap" for x in top],
             max(max(v), max((x.cap or 0) / 1e6 for x in top)) * 1.28, ticks=[(x.cap or 0) / 1e6 for x in top], fmt=busd,
             bold=[_fmt_money(x.reciprocal) for x in top])
        out["top"] = S.png(fig)

    # Nearest or over the cap
    near = net.near_cap()[:15]
    if near:
        fig, ax = figure(len(near))
        v = [min(x.used, 200) for x in near]
        hbar(ax, near, v, [S.RED if x.used > 100 else S.AMBER for x in near], [_fmt_money(x.reciprocal) for x in near],
             300, ref=100, fmt=pctf, bold=[f"{x.used:.0f}%" for x in near])
        ax.set_xticks([0, 50, 100, 150, 200])
        out["nearcap"] = S.png(fig)

    # Special cap: reciprocal deposits, tick = estimated special cap
    spec = net.special_users()[:12]
    if spec:
        fig, ax = figure(len(spec))
        v = [(x.reciprocal or 0) / 1e3 for x in spec]
        hbar(ax, spec, v, [S.RED if (x.over_special or 0) > 0 else S.BLUE for x in spec],
             [f"est. cap {_fmt_money(x.special_cap_est)}" if x.special_cap_est else "est. cap n/a" for x in spec],
             max(max(v), max(((x.special_cap_est or 0) / 1e3 for x in spec), default=0)) * 1.6,
             ticks=[(x.special_cap_est or 0) / 1e3 for x in spec], fmt=musd, bold=[_fmt_money(x.reciprocal) for x in spec])
        out["special"] = S.png(fig)

    # Closest to the special cap: capital cushion
    close = net.closest_to_special()[:12]
    if close:
        fig, ax = figure(len(close))
        v = [x.cushion for x in close]
        span = max(0.5, max(v) - min(0, min(v)))
        hbar(ax, close, v, [S.RED if c < 0 else S.BLUE for c in v], [""] * len(close),
             max(v + [0]) + span * 1.6, ref=0, fmt=lambda t, _: f"{t:+.1f}", bold=[f"{c:+.2f} pts" for c in v])
        out["cushion"] = S.png(fig)

    # Uninsured deposits: distribution across all banks
    un = [x.uninsured_share for x in net.banks if x.uninsured_share is not None]
    if un:
        fig, ax = plt.subplots(figsize=(W, 2.5))
        bins = list(range(0, 105, 5))
        n, _, patches = ax.hist([min(v, 99.9) for v in un], bins=bins, color=S.SKY, edgecolor="white", linewidth=0.8, zorder=3)
        for p_, lo in zip(patches, bins):
            if lo >= HIGH_UNINSURED:
                p_.set_facecolor(S.NAVY)
        ax.axvline(statistics.median(un), color=S.INK, linewidth=0.8, linestyle=(0, (2, 2)), zorder=4)
        ax.text(statistics.median(un) + 0.8, max(n) * 0.96, f"median {statistics.median(un):.0f}%", fontsize=8, color=S.INK)
        ax.text(HIGH_UNINSURED + 1, max(n) * 0.6, f"{sum(1 for v in un if v >= HIGH_UNINSURED):,} banks at\n"
                f"{HIGH_UNINSURED:.0f}% or more", fontsize=8, color=S.NAVY, fontweight="semibold")
        ax.xaxis.set_major_formatter(FuncFormatter(pctf))
        ax.set_xlim(0, 100)
        ax.set_xlabel("Uninsured deposits, % of deposits")
        ax.set_ylabel("Banks")
        S.clean(ax, "y")
        ax.tick_params(axis="y", colors=S.MUTED)
        out["uninsured"] = S.png(fig)

    # Quarter-over-quarter movers
    mv = net.movers()
    if mv:
        up = sorted([x for x in mv if x.reciprocal > x.prior_reciprocal], key=lambda x: x.prior_reciprocal - x.reciprocal)[:8]
        dn = sorted([x for x in mv if x.reciprocal < x.prior_reciprocal], key=lambda x: x.reciprocal - x.prior_reciprocal)[:8]
        rows_ = up + dn[::-1]
        if rows_:
            fig, ax = figure(len(rows_), 0.4)
            d = [(x.reciprocal - x.prior_reciprocal) / 1e3 for x in rows_]
            m = max(abs(v) for v in d)
            ax.barh(range(len(rows_)), d, color=[S.BLUE if v > 0 else S.CONTEXT for v in d], height=0.6, zorder=3)
            ax.axvline(0, color=S.INK, linewidth=0.8, zorder=4)
            for i, (x, v) in enumerate(zip(rows_, d)):
                pc = (x.reciprocal / x.prior_reciprocal - 1) * 100 if x.prior_reciprocal else None
                lab = M(f"{'+' if v > 0 else '-'}{_fmt_money(abs(v) * 1e3)}" + (f"  ({pc:+.0f}%)" if pc is not None and abs(pc) < 1000 else ""))
                ax.text(v + (m * 0.02 if v > 0 else -m * 0.02), i, lab, va="center", ha="left" if v > 0 else "right",
                        fontsize=8.4, color=S.MUTED, zorder=5)
            ax.set_yticks(range(len(rows_)))
            ax.set_yticklabels([short(x.bank.name, 30) for x in rows_])
            ax.set_ylim(len(rows_) - 0.45, -0.55)
            ax.set_xlim(-m * 1.7 if dn else -m * 0.1, m * 1.7 if up else m * 0.1)
            ax.xaxis.set_major_formatter(FuncFormatter(musd))
            S.clean(ax, "x")
            out["movers"] = S.png(fig)
    return out


def build(net: Network) -> Network:
    net.summary, net.summary_by = write_summary(net)
    try:
        net.images = charts(net)
    except Exception as e:
        log.warning("network charts failed: %s", e)
    return net
