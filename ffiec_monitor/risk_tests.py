"""The quarterly risk-test workbook: every bank's Call Report run through point-in-time screens,
quarter-over-quarter trend tests, size-peer outliers and a composite risk score, plus data-quality
and monitored-list checks (scripts/send_risk_tests.py builds and emails it).

Input is one dict per bank (see `Bank`): this quarter's stored ratios, last quarter's, and the
reciprocal-deposit history the special-cap estimate needs. Ratios are percents, as
ratios.extract_ratios / filings_workbook.extra_ratios store them; dollars are $000.

The composite score is a ranking aid, not a validated failure model: each risk area is the bank's
average percentile against banks of its size (100 = riskiest), weighted, plus points for trend
hits. Its weights are judgement calls to backtest (see the reviewer guide), not estimates.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Callable, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from . import ratios as R
from .filings_workbook import FLAG_FILL, _header, _link, _pct
from .pipeline_fields import KEY as PF_KEY, capital_category

# --- thresholds (percent unless noted) -------------------------------------------------------
WELL_CAP = {"tier1_leverage_ratio": 5.0, "tier1_rbc_ratio": 8.0, "total_rbc_ratio": 10.0, "cet1_ratio": 6.5}
CBLR_LINE = 9.0
BUFFER_PP = 1.0                 # "thin buffer": well capitalized but within 1 point of a line
DEPOSIT_OUTFLOW_PCT = -10.0     # total deposits, quarter over quarter
UNINSURED_OUTFLOW_PCT = -15.0
LEVERAGE_DROP_PP = 0.5
NIM_DROP_PP = 0.25
FUNDING_SHIFT_PP = 5.0          # brokered + reciprocal share of deposits
CORE_DROP_PP = 5.0
LTD_RISE_PP = 10.0
SEC_LOSS_RISE_PP = 10.0         # unrealized securities losses / Tier 1
FAST_GROWTH_PCT = 6.0           # assets in one quarter (~25% a year)
PEER_TAIL = 5.0                 # worst 5% of the size group
PEER_MIN_N = 20
TREND_POINTS, TREND_CAP = 5.0, 20.0
SIZE_BUCKETS = [(250_000, "Under $250M"), (1_000_000, "$250M-$1B"), (10_000_000, "$1B-$10B"),
                (100_000_000, "$10B-$100B"), (float("inf"), "Over $100B")]

# Composite: (area, weight, [(ratio key, higher_is_riskier)]).
AREAS = [
    ("Capital", 25, [("tier1_leverage_ratio", False), ("total_rbc_ratio", False), ("aoci_leverage", False)]),
    ("Asset quality", 20, [("noncurrent_loans_oreo_ratio", True), ("npa_to_assets", True), ("texas_ratio", True),
                           ("charge_off_ratio", True)]),
    ("Earnings", 15, [("roaa", False), ("net_interest_margin", False), ("efficiency_ratio", True)]),
    ("Liquidity & funding", 20, [("uninsured_deposit_share", True), ("liquid_to_uninsured", False),
                                 ("brokered_reciprocal_deposits_to_total_deposits", True),
                                 ("net_loans_leases_to_deposits", True), ("core_deposits_to_total_deposits", False)]),
    ("Rate sensitivity", 10, [("unrealized_loss_to_tier1", True)]),
    ("Concentration", 10, [("cre_to_capital", True), ("construction_to_capital", True)]),
]
PEER_METRICS = [(k, hi) for _a, _w, ms in AREAS for k, hi in ms]
METRIC_NAMES = {
    "tier1_leverage_ratio": "Tier 1 leverage", "total_rbc_ratio": "Total risk-based capital",
    "aoci_leverage": "Leverage after securities losses", "noncurrent_loans_oreo_ratio": "Noncurrent loans + OREO",
    "npa_to_assets": "NPA / assets", "texas_ratio": "Texas ratio", "charge_off_ratio": "Net charge-offs",
    "roaa": "ROA", "net_interest_margin": "Net interest margin", "efficiency_ratio": "Efficiency ratio",
    "uninsured_deposit_share": "Uninsured deposit share", "liquid_to_uninsured": "Liquid assets / uninsured",
    "brokered_reciprocal_deposits_to_total_deposits": "Brokered + reciprocal share",
    "net_loans_leases_to_deposits": "Net loans / deposits", "core_deposits_to_total_deposits": "Core deposit share",
    "unrealized_loss_to_tier1": "Securities losses / Tier 1", "cre_to_capital": "CRE / capital",
    "construction_to_capital": "Construction / capital",
}
# Ratios whose formula changed in pipeline_fields VERSION 2: trend tests on them need both quarters
# computed the same way, or a formula fix would read as a quarter-over-quarter move.
V2_KEYS = {"net_interest_margin", "net_loans_leases_to_deposits", "core_deposits_to_total_deposits", "charge_off_ratio"}
# Impossible or implausible values, for the Data Quality tab: (key, low, high).
RANGES = [("tier1_leverage_ratio", 0, 100), ("cet1_ratio", 0, 100), ("total_rbc_ratio", 0, 100),
          ("net_interest_margin", -5, 25), ("roaa", -50, 50), ("efficiency_ratio", 0, 500),
          ("core_deposits_to_total_deposits", 0, 100), ("uninsured_deposit_share", 0, 100),
          ("net_loans_leases_to_deposits", 0, 1000), ("charge_off_ratio", -20, 50)]


@dataclass
class Bank:
    rssd: str
    name: str
    city: str
    state: str
    cert: str
    cur: dict[str, Any]
    prev: Optional[dict[str, Any]] = None
    recip_hist: list[float] = field(default_factory=list)     # quarter-end reciprocal, up to 4 prior quarters ($000)
    lists: list[str] = field(default_factory=list)            # "FDIC order", "Treasury Desk", "Watch List", ...
    refiled: bool = False
    call_report_url: str = ""
    ubpr_url: str = ""
    # filled in by run()
    screens: list[str] = field(default_factory=list)
    trends: list[str] = field(default_factory=list)
    outliers: list[str] = field(default_factory=list)
    pctl: dict[str, float] = field(default_factory=dict)
    areas: dict[str, Optional[float]] = field(default_factory=dict)
    score: Optional[float] = None
    rank: Optional[int] = None

    @property
    def assets(self) -> Optional[float]:
        return _f(self.cur.get("total_assets"))

    @property
    def size(self) -> str:
        a = self.assets or 0
        return next(name for cap, name in SIZE_BUCKETS if a < cap)

    @property
    def specialty(self) -> bool:
        """Trust companies, card banks and the like: deposits under 25% of assets or no loan book.
        Their ratios are real but not comparable with a community bank's, so they are left out of
        peer percentiles and the composite."""
        deposits, assets = _f(self.cur.get(R.DEPOSITS_RAW_KEY)), self.assets
        loans = _f(self.cur.get("net_loans_leases_to_deposits"))
        return not deposits or not assets or deposits < 0.25 * assets or not loans


def _f(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def _v2(r: Optional[dict]) -> bool:
    pf = (r or {}).get(PF_KEY)
    return isinstance(pf, dict) and (pf.get("_v") or 0) >= 2


def aoci_leverage(r: dict) -> Optional[float]:
    """Leverage ratio if unrealized AFS + HTM losses came out of Tier 1 (a stress, not the rule)."""
    lev, loss = _f(r.get("tier1_leverage_ratio")), _f(r.get("unrealized_loss_to_tier1"))
    if lev is None or loss is None:
        return None
    return lev * (1 - max(loss, 0) / 100)


def metric(r: dict, key: str) -> Optional[float]:
    return aoci_leverage(r) if key == "aoci_leverage" else _f(r.get(key))


def buffer_pp(r: dict) -> Optional[float]:
    """Smallest distance above a well-capitalized line, in points (CBLR banks: the 9% line)."""
    lev = _f(r.get("tier1_leverage_ratio"))
    if lev is None:
        return None
    if r.get("tier1_rbc_ratio") is None and r.get("total_rbc_ratio") is None:
        return lev - CBLR_LINE
    gaps = [v - m for k, m in WELL_CAP.items() if (v := _f(r.get(k))) is not None]
    return min(gaps) if gaps else None


def special_cap_est(b: Bank) -> Optional[float]:
    vals = [v for v in b.recip_hist if v is not None]
    return mean(vals) if vals else None


# --- screens: (label, rule text, test returning a short detail or None) ------------------------
def _cat(r):
    return capital_category(_f(r.get("tier1_leverage_ratio")), _f(r.get("tier1_rbc_ratio")),
                            _f(r.get("total_rbc_ratio")), _f(r.get("cet1_ratio")))


def _below_wc(r, b):
    c = _cat(r)
    return {"AC": "Adequately capitalized", "UC": "Undercapitalized"}.get(c)


def _thin(r, b):
    g = buffer_pp(r)
    if _cat(r) != "WC" or g is None or g >= BUFFER_PP:
        return None
    if g < 0:                                   # CBLR bank between 8% and 9%
        return f"CBLR grace band, leverage {r['tier1_leverage_ratio']:.2f}% (9% line)"
    return f"{g:.2f} pts above the line"


def _aoci(r, b):
    a, lev = aoci_leverage(r), _f(r.get("tier1_leverage_ratio"))
    return f"{lev:.2f}% -> {a:.2f}%" if a is not None and lev is not None and lev >= 5 and a < 5 else None


def _gte(key, line, fmt="{:.1f}%"):
    return lambda r, b: fmt.format(r[key]) if (_f(r.get(key)) or 0) >= line else None


def _liquid(r, b):
    v = _f(r.get("liquid_to_uninsured"))
    return f"{v:.0f}%" if v is not None and v < 100 else None


def _loss(r, b):
    v = _f(r.get("roaa"))
    return f"ROA {v:.2f}%" if v is not None and v < 0 else None


def _special(r, b):
    exposed = _cat(r) in ("AC", "UC") or (buffer_pp(r) or 99) < BUFFER_PP or "FDIC order" in b.lists
    recip, est = _f(r.get(R.RECIPROCAL_RAW_KEY)), special_cap_est(b)
    if exposed and recip and est is not None and recip > est:
        return f"${recip / 1000:,.0f}M reciprocal vs ~${est / 1000:,.0f}M special cap"
    return None


SCREENS: list[tuple[str, str, Callable]] = [
    ("Below well capitalized", "Any capital ratio under its well-capitalized line (CBLR banks: leverage under 8%, "
     "after the 8-9% grace band).", _below_wc),
    ("Thin capital buffer", f"Well capitalized, but within {BUFFER_PP:g} point of a line (CBLR banks: of 9%).", _thin),
    ("Leverage under 5% after securities losses", "Tier 1 leverage x (1 - unrealized AFS+HTM losses / Tier 1) "
     "under 5%: well capitalized only because losses are excluded.", _aoci),
    ("Reciprocal cap 80%+ used", "Reciprocal deposits at 80%+ of the lesser of $5B or 20% of liabilities.",
     _gte("reciprocal_cap_used", 80, "{:.0f}%")),
    ("Special-cap exposure", "Below or near well capitalized, or under an FDIC order, and holding more reciprocal "
     "deposits than its average of the prior quarter-ends (the special cap estimate).", _special),
    ("40%+ uninsured deposits", "Estimated uninsured deposits 40%+ of deposits.", _gte("uninsured_deposit_share", 40)),
    ("Liquid assets < uninsured deposits", "Cash, fed funds, repo, AFS and HTM at fair value below uninsured "
     "deposits.", _liquid),
    ("Securities losses 25%+ of Tier 1", "Unrealized AFS + HTM losses 25%+ of Tier 1 capital.",
     _gte("unrealized_loss_to_tier1", 25)),
    ("CRE 300%+ of capital", "Non-owner-occupied CRE 300%+ of total capital (interagency guidance).",
     _gte("cre_to_capital", 300, "{:.0f}%")),
    ("Construction 100%+ of capital", "Construction and land loans 100%+ of total capital.",
     _gte("construction_to_capital", 100, "{:.0f}%")),
    ("Texas ratio 50%+", "Noncurrent loans + OREO over tangible equity + reserves, 50%+.",
     _gte("texas_ratio", 50, "{:.0f}%")),
    ("Losing money", "Year-to-date ROA below zero.", _loss),
]


# --- trends: (label, rule text, test(prev, cur, bank)) -----------------------------------------
def _pct_change(p, c, key):
    a, b_ = _f(p.get(key)), _f(c.get(key))
    return (b_ - a) / a * 100 if a and b_ is not None and a > 0 else None


def _pp_change(p, c, key):
    if key in V2_KEYS and not (_v2(p) and _v2(c)):
        return None
    a, b_ = metric(p, key), metric(c, key)
    return b_ - a if a is not None and b_ is not None else None


def _drop(key, line, unit="%"):
    def t(p, c, b):
        d = _pct_change(p, c, key) if unit == "%" else _pp_change(p, c, key)
        return f"{d:+.1f}{'%' if unit == '%' else ' pts'}" if d is not None and d <= line else None
    return t


def _rise(key, line, unit="pp"):
    def t(p, c, b):
        d = _pct_change(p, c, key) if unit == "%" else _pp_change(p, c, key)
        return f"{d:+.1f}{'%' if unit == '%' else ' pts'}" if d is not None and d >= line else None
    return t


def _npl_doubled(p, c, b):
    a, n = _f(p.get("noncurrent_loans_oreo_ratio")), _f(c.get("noncurrent_loans_oreo_ratio"))
    return f"{a:.2f}% -> {n:.2f}%" if a is not None and n is not None and n >= 1 and n >= 2 * a else None


def _two_losses(p, c, b):
    a, n = _f(p.get("roaa")), _f(c.get("roaa"))
    return f"{a:.2f}%, {n:.2f}%" if a is not None and n is not None and a < 0 and n < 0 else None


TRENDS: list[tuple[str, str, Callable]] = [
    ("Deposit outflow 10%+", "Total deposits down 10%+ from last quarter-end.",
     _drop(R.DEPOSITS_RAW_KEY, DEPOSIT_OUTFLOW_PCT)),
    ("Uninsured outflow 15%+", "Estimated uninsured deposits down 15%+ from last quarter-end.",
     _drop(R.UNINSURED_RAW_KEY, UNINSURED_OUTFLOW_PCT)),
    ("Leverage down 0.5 pts+", "Tier 1 leverage down half a point or more in the quarter.",
     _drop("tier1_leverage_ratio", -LEVERAGE_DROP_PP, "pp")),
    ("Noncurrent loans doubled", "Noncurrent loans + OREO at least doubled, to 1%+ of loans + OREO.", _npl_doubled),
    ("Second quarter of losses", "ROA below zero this quarter and last.", _two_losses),
    ("Margin down 0.25 pts+", "Net interest margin down a quarter point or more.",
     _drop("net_interest_margin", -NIM_DROP_PP, "pp")),
    ("Shift to wholesale funding", "Brokered + reciprocal share of deposits up 5+ points.",
     _rise("brokered_reciprocal_deposits_to_total_deposits", FUNDING_SHIFT_PP)),
    ("Core deposits down 5 pts+", "Core deposit share down 5+ points.",
     _drop("core_deposits_to_total_deposits", -CORE_DROP_PP, "pp")),
    ("Loans / deposits up 10 pts+", "Net loans / deposits up 10+ points.",
     _rise("net_loans_leases_to_deposits", LTD_RISE_PP)),
    ("Securities losses up 10 pts+", "Unrealized losses / Tier 1 up 10+ points.",
     _rise("unrealized_loss_to_tier1", SEC_LOSS_RISE_PP)),
    ("Fast asset growth", f"Total assets up {FAST_GROWTH_PCT:g}%+ in the quarter (~25% a year).",
     _rise("total_assets", FAST_GROWTH_PCT, "%")),
]


def screens_for(r: dict, b: Bank) -> list[str]:
    return [f"{label}: {d}" for label, _rule, t in SCREENS if (d := t(r, b))]


def trends_for(b: Bank) -> list[str]:
    if not b.prev:
        return []
    return [f"{label}: {d}" for label, _rule, t in TRENDS if (d := t(b.prev, b.cur, b))]


def _percentiles(values: list[tuple[Bank, float]], higher_is_riskier: bool) -> dict[str, float]:
    """Risk percentile per bank (100 = riskiest in the group), ties averaged."""
    ordered = sorted(values, key=lambda t: t[1], reverse=not higher_is_riskier)
    n, out, i = len(ordered), {}, 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        p = ((i + j) / 2 + 1) / n * 100
        for k in range(i, j + 1):
            out[ordered[k][0].rssd] = p
        i = j + 1
    return out


def run(banks: list[Bank]) -> list[Bank]:
    """Score every bank in place and return them ranked (riskiest first, specialty banks last)."""
    for b in banks:
        b.screens, b.trends = screens_for(b.cur, b), trends_for(b)
    peers = [b for b in banks if not b.specialty]
    for size in {b.size for b in peers}:
        group = [b for b in peers if b.size == size]
        for key, hi in PEER_METRICS:
            vals = [(b, v) for b in group if (v := metric(b.cur, key)) is not None]
            if len(vals) < PEER_MIN_N:
                continue
            by_rssd = {b.rssd: b for b, _v in vals}
            for rssd, p in _percentiles(vals, hi).items():
                by_rssd[rssd].pctl[key] = p
            worst = sorted(vals, key=lambda t: t[1], reverse=hi)
            tail = max(1, int(len(vals) * PEER_TAIL / 100))
            for pos, (bank, v) in enumerate(worst[:tail], 1):
                bank.outliers.append(f"{METRIC_NAMES[key]} {v:,.2f}% (#{pos} of {len(vals)})")
    for b in peers:
        weighted, weights = 0.0, 0
        for area, w, ms in AREAS:
            ps = [b.pctl[k] for k, _ in ms if k in b.pctl]
            b.areas[area] = mean(ps) if ps else None
            if ps:
                weighted += w * mean(ps)
                weights += w
        if weights:
            b.score = min(100.0, weighted / weights + min(TREND_CAP, TREND_POINTS * len(b.trends)))
    ranked = sorted(peers, key=lambda b: -(b.score or -1))
    for i, b in enumerate(ranked, 1):
        b.rank = i if b.score is not None else None
    return ranked + sorted((b for b in banks if b.specialty), key=lambda b: b.name)


def range_problems(banks: list[Bank]) -> list[tuple[Bank, str, float]]:
    out = []
    for b in banks:
        for key, lo, hi in RANGES:
            v = _f(b.cur.get(key))
            if v is not None and not lo <= v <= hi:
                out.append((b, key, v))
    return out


# --- workbook ----------------------------------------------------------------------------------
def _bank_cells(ws, row: int, b: Bank) -> int:
    for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 1):
        ws.cell(row=row, column=col, value=v)
    return 7


def _links(ws, row: int, col: int, b: Bank) -> None:
    if b.call_report_url:
        _link(ws.cell(row=row, column=col), b.call_report_url, "Call Report")
    if b.ubpr_url:
        _link(ws.cell(row=row, column=col + 1), b.ubpr_url, "UBPR")


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i) if i < 26 else "A" + chr(65 + i - 26)].width = w


BANK_HEAD = ["Institution", "City", "State", "RSSD", "FDIC cert", "Size group"]
BANK_W = [38, 16, 6, 10, 9, 13]


def build(banks: list[Bank], label: str, missing: list[tuple[str, str, str]], new_actions: list[tuple[Bank, str]],
          prev_counts: Optional[dict[str, int]] = None) -> bytes:
    """`banks` as returned by run(). `missing` = (rssd, name, state) filers last quarter with no filing this
    quarter; `new_actions` = (bank, action text) enforcement actions issued in the quarter."""
    prev_counts = prev_counts or {}
    wb = Workbook()
    scored = [b for b in banks if b.score is not None]

    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"Call Report risk tests {label}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"{len(banks)} banks: {len(scored)} scored against size peers, "
                f"{len(banks) - len(scored)} specialty (trust, card and other banks with deposits under 25% of "
                f"assets or no loan book) left out of the scores.")
    _header(ws, 4, ["Test", "Kind", "Banks this quarter", "Last quarter"])
    row = 5
    for kind, tests, field_ in (("Screen", SCREENS, "screens"), ("Trend", TRENDS, "trends")):
        for label_, _rule, _t in tests:
            n = sum(1 for b in banks if any(s.startswith(label_ + ":") for s in getattr(b, field_)))
            ws.cell(row=row, column=1, value=label_)
            ws.cell(row=row, column=2, value=kind)
            ws.cell(row=row, column=3, value=n)
            if label_ in prev_counts:
                ws.cell(row=row, column=4, value=prev_counts[label_])
            row += 1
    ws.cell(row=row, column=1, value="Peer outliers (any metric in the worst 5% of its size group)")
    ws.cell(row=row, column=2, value="Peer")
    ws.cell(row=row, column=3, value=sum(1 for b in banks if b.outliers))
    row += 2
    ws.cell(row=row, column=1, value="Top 25 by composite score").font = Font(bold=True, size=12)
    _header(ws, row + 1, BANK_HEAD + ["Score", "Screens", "Trends", "Lists"])
    for i, b in enumerate(scored[:25], row + 2):
        c = _bank_cells(ws, i, b)
        ws.cell(row=i, column=c, value=round(b.score, 1))
        ws.cell(row=i, column=c + 1, value=len(b.screens))
        ws.cell(row=i, column=c + 2, value=len(b.trends))
        ws.cell(row=i, column=c + 3, value=", ".join(b.lists))
    _widths(ws, [44, 10, 18, 14, 9, 13, 8, 9, 8, 30])

    ws = wb.create_sheet("Risk Ranking")
    head = ["Rank"] + BANK_HEAD + ["Total assets ($M)", "Score"] + [a for a, _w, _m in AREAS] + \
        ["Screens", "Trends", "Peer outliers", "Lists", "Refiled", "Call Report", "UBPR"]
    _header(ws, 1, head)
    for i, b in enumerate(banks, 2):
        ws.cell(row=i, column=1, value=b.rank)
        for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 2):
            ws.cell(row=i, column=col, value=v)
        c = 8
        ws.cell(row=i, column=c, value=round(b.assets / 1000, 1) if b.assets else None)
        ws.cell(row=i, column=c + 1, value=round(b.score, 1) if b.score is not None else "Specialty")
        for j, (a, _w, _m) in enumerate(AREAS):
            v = b.areas.get(a)
            ws.cell(row=i, column=c + 2 + j, value=round(v) if v is not None else None)
        c += 2 + len(AREAS)
        ws.cell(row=i, column=c, value="; ".join(b.screens))
        ws.cell(row=i, column=c + 1, value="; ".join(b.trends))
        ws.cell(row=i, column=c + 2, value="; ".join(b.outliers))
        ws.cell(row=i, column=c + 3, value=", ".join(b.lists))
        ws.cell(row=i, column=c + 4, value="Yes" if b.refiled else None)
        _links(ws, i, c + 5, b)
        if b.lists:
            for col in range(1, len(head) + 1):
                ws.cell(row=i, column=col).fill = FLAG_FILL
    _widths(ws, [6] + BANK_W + [11, 9] + [10] * len(AREAS) + [50, 50, 50, 24, 8, 12, 8])

    def flag_sheet(title: str, field_: str, tests):
        ws = wb.create_sheet(title)
        labels = [t[0] for t in tests]
        _header(ws, 1, ["Rank", "Score"] + BANK_HEAD + labels + ["Lists", "Call Report", "UBPR"])
        row = 2
        for b in banks:
            hits = {s.split(":", 1)[0]: s.split(":", 1)[1].strip() for s in getattr(b, field_)}
            if not hits:
                continue
            ws.cell(row=row, column=1, value=b.rank)
            ws.cell(row=row, column=2, value=round(b.score, 1) if b.score is not None else "Specialty")
            for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 3):
                ws.cell(row=row, column=col, value=v)
            for j, lab in enumerate(labels):
                if lab in hits:
                    cell = ws.cell(row=row, column=9 + j, value=hits[lab])
                    cell.fill = FLAG_FILL
            ws.cell(row=row, column=9 + len(labels), value=", ".join(b.lists))
            _links(ws, row, 10 + len(labels), b)
            row += 1
        _widths(ws, [6, 9] + BANK_W + [18] * len(labels) + [24, 12, 8])
        for col in range(9, 9 + len(labels)):
            ws.cell(row=1, column=col).alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 48

    flag_sheet("Screens", "screens", SCREENS)
    flag_sheet("Trends", "trends", TRENDS)

    ws = wb.create_sheet("Peer Outliers")
    _header(ws, 1, ["Rank", "Score"] + BANK_HEAD + ["Worst-5% metrics", "Lists", "Call Report", "UBPR"])
    row = 2
    for b in banks:
        if b.outliers:
            ws.cell(row=row, column=1, value=b.rank)
            ws.cell(row=row, column=2, value=round(b.score, 1) if b.score is not None else None)
            for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 3):
                ws.cell(row=row, column=col, value=v)
            ws.cell(row=row, column=9, value="; ".join(b.outliers))
            ws.cell(row=row, column=10, value=", ".join(b.lists))
            _links(ws, row, 11, b)
            row += 1
    _widths(ws, [6, 9] + BANK_W + [90, 24, 12, 8])

    ws = wb.create_sheet("Data Quality")
    ws["A1"] = "Data quality checks"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A3"] = f"Filed last quarter, no filing this quarter: {len(missing)}"
    ws["A3"].font = Font(bold=True)
    _header(ws, 4, ["RSSD", "Institution", "State"])
    row = 5
    for rssd, name, state in missing:
        for col, v in enumerate([rssd, name, state], 1):
            ws.cell(row=row, column=col, value=v)
        row += 1
    row += 1
    refiled = [b for b in banks if b.refiled]
    ws.cell(row=row, column=1, value=f"Refiled this quarter: {len(refiled)}").font = Font(bold=True)
    _header(ws, row + 1, ["RSSD", "Institution", "State", "Score"])
    row += 2
    for b in refiled:
        for col, v in enumerate([b.rssd, b.name, b.state, round(b.score, 1) if b.score is not None else None], 1):
            ws.cell(row=row, column=col, value=v)
        row += 1
    row += 1
    probs = range_problems(banks)
    ws.cell(row=row, column=1, value=f"Values outside a plausible range: {len(probs)} (check the Call Report before "
            "relying on them)").font = Font(bold=True)
    _header(ws, row + 1, ["RSSD", "Institution", "State", "Ratio", "Value (%)", "Specialty"])
    row += 2
    for b, key, v in probs:
        for col, x in enumerate([b.rssd, b.name, b.state, METRIC_NAMES.get(key, key), round(v, 2),
                                 "Yes" if b.specialty else None], 1):
            ws.cell(row=row, column=col, value=x)
        row += 1
    row += 1
    ws.cell(row=row, column=1, value="Coverage: banks with a value, by ratio").font = Font(bold=True)
    _header(ws, row + 1, ["Ratio", "Banks with a value", "Share of banks"])
    row += 2
    for key, _hi in PEER_METRICS:
        n = sum(1 for b in banks if metric(b.cur, key) is not None)
        ws.cell(row=row, column=1, value=METRIC_NAMES[key])
        ws.cell(row=row, column=2, value=n)
        c = ws.cell(row=row, column=3, value=n / len(banks) if banks else None)
        c.number_format = "0%"
        row += 1
    _widths(ws, [12, 44, 8, 34, 12, 10])

    ws = wb.create_sheet("Lists")
    ws["A1"] = "Every bank on a monitored list, with its score and hits"
    ws["A1"].font = Font(bold=True, size=13)
    _header(ws, 2, ["Rank", "Score"] + BANK_HEAD + ["Lists", "Screens", "Trends", "Call Report", "UBPR"])
    row = 3
    for b in sorted((b for b in banks if b.lists), key=lambda b: (b.rank or 10**6)):
        ws.cell(row=row, column=1, value=b.rank)
        ws.cell(row=row, column=2, value=round(b.score, 1) if b.score is not None else "Specialty")
        for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 3):
            ws.cell(row=row, column=col, value=v)
        ws.cell(row=row, column=9, value=", ".join(b.lists))
        ws.cell(row=row, column=10, value="; ".join(b.screens))
        ws.cell(row=row, column=11, value="; ".join(b.trends))
        _links(ws, row, 12, b)
        row += 1
    row += 1
    ws.cell(row=row, column=1, value=f"FDIC enforcement actions issued this quarter: {len(new_actions)}").font = \
        Font(bold=True)
    _header(ws, row + 1, ["Rank", "Score"] + BANK_HEAD + ["Action"])
    row += 2
    for b, action in new_actions:
        ws.cell(row=row, column=1, value=b.rank)
        ws.cell(row=row, column=2, value=round(b.score, 1) if b.score is not None else None)
        for col, v in enumerate([b.name, b.city, b.state, b.rssd, b.cert, b.size], 3):
            ws.cell(row=row, column=col, value=v)
        ws.cell(row=row, column=9, value=action)
        row += 1
    _widths(ws, [6, 9] + BANK_W + [30, 60, 60, 12, 8])

    ws = wb.create_sheet("Test Guide")
    _header(ws, 1, ["Test", "Kind", "Rule"])
    row = 2
    for kind, tests in (("Screen", SCREENS), ("Trend", TRENDS)):
        for label_, rule, _t in tests:
            for col, v in enumerate([label_, kind, rule], 1):
                ws.cell(row=row, column=col, value=v).alignment = Alignment(wrap_text=True, vertical="top")
            row += 1
    notes = [
        ("Peer outlier", "Peer", f"A ratio in the worst {PEER_TAIL:g}% of the bank's size group "
         f"({', '.join(n for _c, n in SIZE_BUCKETS)}); groups under {PEER_MIN_N} banks are skipped. Specialty "
         "banks are left out."),
        ("Composite score", "Score", "Each area = the bank's average risk percentile in its size group (100 = "
         "riskiest): " + "; ".join(f"{a} {w}% ({', '.join(METRIC_NAMES[k] for k, _ in ms)})" for a, w, ms in AREAS)
         + f". Score = weighted average + {TREND_POINTS:g} points per trend hit (up to {TREND_CAP:g}), capped at "
         "100. A ranking aid with judgement weights, not a validated failure probability."),
        ("Trend tests on changed formulas", "Note", "Margin, loans / deposits, core deposits and charge-off trends "
         "run only when both quarters were computed with the current formulas (from the Q4 2026 run on)."),
    ]
    for label_, kind, rule in notes:
        for col, v in enumerate([label_, kind, rule], 1):
            ws.cell(row=row, column=col, value=v).alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    _widths(ws, [34, 8, 120])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def prior_counts(banks: list[Bank]) -> dict[str, int]:
    """How many banks hit each screen last quarter (screens re-run on last quarter's ratios)."""
    out: dict[str, int] = {}
    for label, _rule, t in SCREENS:
        out[label] = sum(1 for b in banks if b.prev and t(b.prev, b))
    return out
