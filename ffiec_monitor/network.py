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
        return sorted([b for b in self.users if b.special_cap], key=lambda b: -(b.reciprocal or 0))

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
            if on_progress and net.pulled % 250 == 0:
                on_progress(net.pulled)
            merged = {**(json.loads(raw) if raw else {}), **{k: v for k, v in r.items() if v is not None}}
            conn.execute("UPDATE call_report_snapshots SET ratios = ? WHERE id_rssd = ? AND period_end = ?",
                         [json.dumps(merged), rssd, period_end])
            r = merged
        meta = db.get_institution_meta(conn, rssd)
        if meta is None:
            continue
        b = _bank(meta, period_end)
        rec, liab = _num(r, RECIPROCAL_RAW_KEY), _num(r, LIABILITIES_RAW_KEY)
        cap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * liab) if liab else None
        unins, deps = _num(r, UNINSURED_RAW_KEY), _num(r, DEPOSITS_RAW_KEY)
        nb = NetBank(b, period_end, rec, cap, (rec / cap * 100) if (rec is not None and cap) else None, unins,
                     (unins / deps * 100) if (unins is not None and deps) else None, deps, meta.total_assets or 0)
        if capital_status(r):
            nb.special_cap.append("below well capitalized")
        net.banks.append(nb)
    # enforcement -> likely special cap
    if enforcement_sources is not None:
        from .enforcement import match
        for m in match([nb.bank for nb in net.banks], enforcement_sources.actions):
            if not m.action.stale:
                for nb in net.banks:
                    if nb.bank.rssd == m.bank.rssd and "under an enforcement order" not in nb.special_cap:
                        nb.special_cap.append("under an enforcement order")
    # quarter-over-quarter for the banks in the lists
    if prior_lookup is not None:
        focus = {id(b): b for b in net.near_cap()[:25] + net.special()[:25] + net.prospects()[:15]}
        for nb in focus.values():
            try:
                pr = prior_lookup(nb.bank.rssd, prior_quarter_end(nb.period)) or {}
            except Exception:
                pr = {}
            prec, pliab = _num(pr, RECIPROCAL_RAW_KEY), _num(pr, LIABILITIES_RAW_KEY)
            if prec is not None:
                nb.prior_reciprocal = prec
                pcap = min(RECIPROCAL_CAP_DOLLARS_000, 0.2 * pliab) if pliab else None
                nb.prior_used = prec / pcap * 100 if pcap else None
    return net


# ------------------------------------------------------------------ narrative
PROMPT = """You write the opening analysis of a weekly report for a risk manager at a firm that runs a reciprocal deposit
network. Plain English, ONE paragraph of 4 to 6 sentences, at most 130 words. Cover: how widely banks use reciprocal
deposits and how close they are to the reciprocal cap (lesser of $5 billion or 20% of liabilities); the banks nearest or
over the cap and what that means (more placements would count as brokered); banks likely held to the special cap
(not well capitalized/rated, so limited to their four-quarter average); and uninsured-deposit exposure, including banks
with high uninsured deposits and room under the cap. Name at most three banks.

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
    for title, lst in (("NEAREST OR OVER THE CAP", net.near_cap()[:8]), ("LIKELY ON THE SPECIAL CAP", net.special()[:8]),
                       ("HIGH UNINSURED, ROOM UNDER THE CAP", net.prospects()[:8])):
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
                if t and len(t) < 1100 and _numbers(t) <= (_numbers(data) | {str(i) for i in range(0, 13)}):
                    return t, "Claude"
        except Exception as e:
            log.warning("network summary unavailable: %s", e)
    return rule_summary(net), "rules"


# ------------------------------------------------------------------ charts
def charts(net: Network) -> dict[str, bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .analytics import AMBER, BLUE, GRID, INK, MUTED, RED, SURFACE
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    out = {}

    def save(fig, key):
        buf = io.BytesIO()
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

    # 2. Uninsured share vs. cap used: the exposure map
    pts = [x for x in net.users if x.used is not None and x.uninsured_share is not None]
    if pts:
        fig, ax = axes(7.2, 4.0)
        ax.spines["left"].set_visible(True)
        size = lambda a: max(8, min(220, (a or 0) / 15000))                 # noqa: E731
        normal = [p for p in pts if not p.special_cap]
        spec = [p for p in pts if p.special_cap]
        ax.scatter([p.uninsured_share for p in normal], [min(p.used, 140) for p in normal], s=[size(p.assets) for p in normal],
                   color=BLUE, alpha=0.45, edgecolors=SURFACE, linewidths=0.6, label="Reciprocal user", zorder=3)
        if spec:
            ax.scatter([p.uninsured_share for p in spec], [min(p.used, 140) for p in spec], s=[size(p.assets) for p in spec],
                       color=RED, alpha=0.85, edgecolors=SURFACE, linewidths=0.6, label="Likely on special cap", zorder=4)
        ax.axhline(100, color=RED, linewidth=1.2, linestyle=(0, (4, 3)), zorder=2)
        ax.axhline(NEAR_CAP, color=AMBER, linewidth=1.0, linestyle=(0, (4, 3)), zorder=2)
        ax.axvline(HIGH_UNINSURED, color=MUTED, linewidth=0.8, linestyle=(0, (2, 3)), zorder=2)
        ax.text(0.995, 100, "cap ", transform=ax.get_yaxis_transform(), ha="right", va="bottom", fontsize=10.5, color=RED)
        ax.text(0.995, NEAR_CAP, f"{NEAR_CAP:.0f}% ", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
                fontsize=10.5, color=AMBER)
        ax.set_xlabel("Uninsured deposits, % of deposits", fontsize=11)
        ax.set_ylabel("Reciprocal cap used, %", fontsize=11)
        ax.yaxis.grid(True, color=GRID, zorder=0)
        ax.set_xlim(0, max(100, max(p.uninsured_share for p in pts) * 1.05))
        ax.set_ylim(0, 145)
        ax.legend(loc="upper left", frameon=False, fontsize=10.5)
        save(fig, "map")

    # 3. By asset size: share using reciprocal deposits, and median uninsured share
    from .workbook import SIZE_BUCKETS
    labels, use_pct, unins = [], [], []
    for lab, lo, hi in SIZE_BUCKETS:
        grp = [x for x in net.banks if lo <= (x.assets or 0) < hi]
        if not grp:
            continue
        # "$" starts math mode in matplotlib text, so escape it
        labels.append(lab.replace(" to ", "-").replace("Under ", "<").replace(" and over", "+").replace("$", r"\$"))
        use_pct.append(sum(1 for x in grp if (x.reciprocal or 0) > 0) / len(grp) * 100)
        u = [x.uninsured_share for x in grp if x.uninsured_share is not None]
        unins.append(statistics.median(u) if u else 0)
    if labels:
        fig, ax = axes(7.2, 3.0)
        xs = list(range(len(labels)))
        w = 0.36
        ax.bar([x - w / 2 for x in xs], use_pct, width=w, color=BLUE, label="% of banks using reciprocal",
               edgecolor=SURFACE, linewidth=1.0, zorder=3)
        ax.bar([x + w / 2 for x in xs], unins, width=w, color="#eb6834", label="Median uninsured share, %",
               edgecolor=SURFACE, linewidth=1.0, zorder=3)
        for x, a, c in zip(xs, use_pct, unins):
            ax.text(x - w / 2, a, f"{a:.0f}", ha="center", va="bottom", fontsize=10.5, color=INK)
            ax.text(x + w / 2, c, f"{c:.0f}", ha="center", va="bottom", fontsize=10.5, color=INK)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels)
        ax.yaxis.grid(True, color=GRID, zorder=0)
        ax.set_ylim(0, max(use_pct + unins + [1]) * 1.2)
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, fontsize=10.5, ncol=2)
        save(fig, "size")
    return out


def build(net: Network) -> Network:
    net.summary, net.summary_by = write_summary(net)
    try:
        net.images = charts(net)
    except Exception as e:
        log.warning("network charts failed: %s", e)
    return net
