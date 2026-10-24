"""The weekly newsletter: essential rates and markets, each with what it is,
what it means right now, the risk to watch, and the ranges to know, plus
charts for every section.

"Ranges to know" are general rules of thumb for context, not official
thresholds. All analysis text is rules-based: fixed sentences filled in from
the numbers, so it never invents anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .analysis import Reading
from .charts import Line, index_to_100, line_chart
from .email_report import INK, LINE, QUIET, Bundle, _esc, page
from .extras import FOMC_DECISION_DAYS, fmt_thousands
from .series import SERIES_BY_ID, SPREADS

OK_C, WATCH_C, RISK_C = "#1b6e3c", "#8a5a00", "#b3261e"
TONE_COLOR = {"ok": OK_C, "watch": WATCH_C, "risk": RISK_C}
TONE_WORD = {"ok": "Normal", "watch": "Watch", "risk": "Risk"}


# --------------------------------------------------------------------- zones
@dataclass(frozen=True)
class Zone:
    lo: Optional[float]
    hi: Optional[float]
    label: str
    tone: str           # ok | watch | risk

    def contains(self, x: float) -> bool:
        return (self.lo is None or x >= self.lo) and (self.hi is None or x < self.hi)


@dataclass
class Item:
    key: str
    zones: list[Zone] = field(default_factory=list)
    metric: str = "value"           # value | yoy | drawdown
    metric_label: str = ""          # what the zones are measured on, if not the level itself
    means: Optional[Callable[[Reading, Bundle], str]] = None


def metric_value(item: Item, r: Reading) -> Optional[float]:
    if not r.ok:
        return None
    if item.metric == "yoy":
        return r.change_size("year") if r.changes.get("year") is not None else None
    if item.metric == "drawdown":
        if r.high_1y:
            return (1 - r.value / r.high_1y) * 100      # % below the past-year high
        return None
    return r.value


def current_zone(item: Item, r: Reading) -> Optional[Zone]:
    m = metric_value(item, r)
    if m is None:
        return None
    return next((z for z in item.zones if z.contains(m)), None)


def _z(lo, hi, label, tone="ok") -> Zone:
    return Zone(lo, hi, label, tone)


# ------------------------------------------------------- "what it means now"
def _r(b: Bundle, key: str) -> Optional[Reading]:
    r = b.readings.get(key)
    return r if r and r.ok else None


def _next_fomc(today: date) -> Optional[date]:
    return next((d for d in FOMC_DECISION_DAYS if d >= today), None)


def means_fed(r: Reading, b: Bundle) -> str:
    bottom = _r(b, "DFEDTARL")
    rng = f"{bottom.value:.2f}% to {r.value:.2f}%" if bottom else f"top of {r.value:.2f}%"
    yr = r.changes.get("year")
    if yr is None or abs(yr) < 0.01:
        trend = "unchanged from a year ago"
    else:
        trend = f"{'up' if yr > 0 else 'down'} {abs(yr):.2f} points from a year ago ({'hikes' if yr > 0 else 'cuts'})"
    s = f"The Fed's target range is {rng}, {trend}."
    nxt = _next_fomc(b.today)
    if nxt:
        s += f" Next decision: {nxt:%A, %B %-d}."
    return s


def means_effr(r: Reading, b: Bundle) -> str:
    top, bottom = _r(b, "DFEDTARU"), _r(b, "DFEDTARL")
    if not (top and bottom):
        return ""
    if r.value > top.value + 0.001 or r.value < bottom.value - 0.001:
        return f"At {r.display_value} it is outside the Fed's range, which is rare and signals stress in overnight funding."
    pos = (r.value - bottom.value) / max(0.01, top.value - bottom.value)
    where = "near the top of" if pos > 0.8 else "near the bottom of" if pos < 0.2 else "in the middle of"
    return f"At {r.display_value} it sits {where} the Fed's range, so the Fed's target is holding."


def means_bill(r: Reading, b: Bundle) -> str:
    gap = _r(b, "bill_vs_effr")
    if not gap:
        return ""
    if gap.value <= -0.15:
        return (f"Bills yield {abs(gap.value):.2f} points less than fed funds, a sign markets expect the Fed to cut "
                "within a few months.")
    if gap.value >= 0.15:
        return f"Bills yield {gap.value:.2f} points more than fed funds, a sign markets expect rates to rise."
    return "Bills yield about the same as fed funds: markets expect little change in the next few months."


def means_2y(r: Reading, b: Bundle) -> str:
    effr = _r(b, "EFFR")
    if not effr:
        return ""
    d = r.value - effr.value
    if d <= -0.25:
        return f"It is {abs(d):.2f} points below fed funds: markets expect the Fed to cut over the next two years."
    if d >= 0.25:
        return f"It is {d:.2f} points above fed funds: markets expect rates to rise."
    return "It is close to fed funds: markets expect rates to stay roughly where they are."


def means_mm(r: Reading, b: Bundle) -> str:
    bill = _r(b, "DGS3MO")
    if not bill:
        return ""
    return (f"The average bank pays {r.display_value} on money market accounts versus {bill.display_value} on "
            "3-month T-bills. National averages include big banks paying very little, so competitive banks, "
            "and IntraFi programs, typically price well above this.")


def means_cd(r: Reading, b: Bundle) -> str:
    mm = _r(b, "MMNDR")
    return (f"Banks pay {r.value - mm.value:.2f} points more on average to lock money up for a year than in a money "
            "market account." if mm else "")


def means_deposits_small(r: Reading, b: Bundle) -> str:
    large = _r(b, "DPSLCBW027SBOG")
    if not large or r.changes.get("year") is None or large.changes.get("year") is None:
        return ""
    sg, lg = r.change_size("year"), large.change_size("year")
    if sg < lg - 1:
        return f"Small banks ({sg:+.1f}%) are growing slower than large banks ({lg:+.1f}%): deposits are drifting toward the biggest banks."
    if sg > lg + 1:
        return f"Small banks ({sg:+.1f}%) are outgrowing large banks ({lg:+.1f}%)."
    return f"Small and large banks are growing at about the same pace ({sg:+.1f}% vs {lg:+.1f}%)."


def means_cre(r: Reading, b: Bundle) -> str:
    if not r.as_of:
        return ""
    q = (r.as_of.month - 1) // 3 + 1
    return f"Latest data is for Q{q} {r.as_of.year}; it updates once a quarter."


def means_vix(r: Reading, b: Bundle) -> str:
    return f"The past year ranged from {r.low_1y:.1f} to {r.high_1y:.1f}." if r.low_1y is not None else ""


ITEMS: dict[str, Item] = {i.key: i for i in [
    Item("DFEDTARU", means=means_fed),
    Item("EFFR", means=means_effr),
    Item("SOFR"),
    Item("sofr_vs_iorb", [_z(None, 0.0, "Below 0: cash is plentiful"), _z(0.0, 0.10, "0 to +0.10: tightening", "watch"),
                          _z(0.10, None, "Above +0.10: funding stress", "risk")]),
    Item("DGS3MO", means=means_bill),
    Item("DGS2", means=means_2y),
    Item("DGS10", [_z(None, 3.0, "Below 3%: low"), _z(3.0, 4.5, "3% to 4.5%: moderate"),
                   _z(4.5, 5.0, "4.5% to 5%: high", "watch"), _z(5.0, None, "Above 5%: highest since 2007", "risk")]),
    Item("curve_10y_2y", [_z(None, 0.0, "Below 0: inverted (margin squeeze, recession signal)", "risk"),
                          _z(0.0, 0.5, "0 to 0.5: flat", "watch"), _z(0.5, 1.5, "0.5 to 1.5: normal"),
                          _z(1.5, None, "Above 1.5: steep (good for bank margins)")]),
    Item("MMNDR", means=means_mm),
    Item("NDR12MCD", means=means_cd),
    # National averages include big banks paying near zero, so a gap of several points is
    # normal when rates are high; 2023's deposit flight came with a gap near 4.8 points.
    Item("bill_vs_mm", [_z(None, 2.0, "Under 2 points: bank deposits competitive"),
                        _z(2.0, 3.5, "2 to 3.5 points: typical when rates are high"),
                        _z(3.5, 4.5, "3.5 to 4.5 points: wide, pull toward Treasuries", "watch"),
                        _z(4.5, None, "Over 4.5 points: very wide (2023 deposit-flight levels)", "risk")]),
    Item("DPSSCBW027SBOG", [_z(None, 0.0, "Shrinking", "risk"), _z(0.0, 3.0, "0% to 3%: slow", "watch"),
                            _z(3.0, 7.0, "3% to 7%: normal"), _z(7.0, None, "Above 7%: fast")],
         "yoy", "growth over the past year", means_deposits_small),
    Item("DPSLCBW027SBOG", [_z(None, 0.0, "Shrinking"), _z(0.0, 3.0, "0% to 3%: slow"),
                            _z(3.0, 7.0, "3% to 7%: normal"), _z(7.0, None, "Above 7%: fast")],
         "yoy", "growth over the past year"),
    Item("WRMFNS", [_z(None, 0.0, "Shrinking (money returning to banks)"), _z(0.0, 8.0, "0% to 8%: normal growth"),
                    _z(8.0, 15.0, "8% to 15%: fast, money leaving banks", "watch"), _z(15.0, None, "Above 15%: very fast", "risk")],
         "yoy", "growth over the past year"),
    Item("H8B3094NSMA", [_z(None, -5.0, "Falling more than 5%: deposits covering needs"), _z(-5.0, 10.0, "-5% to +10%: stable"),
                         _z(10.0, 25.0, "+10% to +25%: rising reliance on borrowing", "watch"),
                         _z(25.0, None, "Above +25%: rapid rise", "risk")],
         "yoy", "change over the past year"),
    Item("WLCFLPCL", [_z(None, 10.0, "Under $10 billion: normal"), _z(10.0, 50.0, "$10 to $50 billion: elevated", "watch"),
                      _z(50.0, None, "Above $50 billion: stress (March 2023 peak: about $150 billion)", "risk")]),
    Item("BAMLH0A0HYM2", [_z(None, 3.5, "Below 3.5%: calm, possibly complacent"), _z(3.5, 5.0, "3.5% to 5%: normal"),
                          _z(5.0, 7.0, "5% to 7%: default worries", "watch"), _z(7.0, None, "Above 7%: credit stress", "risk")]),
    Item("DRCRELEXFACBS", [_z(None, 1.0, "Below 1%: low"), _z(1.0, 2.0, "1% to 2%: moderate"),
                           _z(2.0, 4.0, "2% to 4%: elevated", "watch"), _z(4.0, None, "Above 4%: high (2010 peak near 9%)", "risk")],
         means=means_cre),
    Item("SP500", [_z(None, 5.0, "Within 5% of its high"), _z(5.0, 10.0, "5% to 10% below: pullback"),
                   _z(10.0, 20.0, "10% to 20% below: correction", "watch"), _z(20.0, None, "20%+ below: bear market", "risk")],
         "drawdown", "distance below its past-year high"),
    Item("DJIA", [_z(None, 5.0, "Within 5% of its high"), _z(5.0, 10.0, "5% to 10% below: pullback"),
                  _z(10.0, 20.0, "10% to 20% below: correction", "watch"), _z(20.0, None, "20%+ below: bear market", "risk")],
         "drawdown", "distance below its past-year high"),
    Item("NASDAQCOM", [_z(None, 5.0, "Within 5% of its high"), _z(5.0, 10.0, "5% to 10% below: pullback"),
                       _z(10.0, 20.0, "10% to 20% below: correction", "watch"), _z(20.0, None, "20%+ below: bear market", "risk")],
         "drawdown", "distance below its past-year high"),
    Item("VIXCLS", [_z(None, 15.0, "Below 15: calm"), _z(15.0, 20.0, "15 to 20: normal"),
                    _z(20.0, 30.0, "20 to 30: elevated", "watch"), _z(30.0, None, "Above 30: stress", "risk")], means=means_vix),
    Item("CPIAUCSL", [_z(None, 2.0, "Below 2%: under the Fed's goal"), _z(2.0, 3.0, "2% to 3%: near the goal"),
                      _z(3.0, 5.0, "3% to 5%: elevated", "watch"), _z(5.0, None, "Above 5%: high", "risk")]),
    Item("UNRATE", [_z(None, 4.0, "Below 4%: tight job market"), _z(4.0, 5.0, "4% to 5%: normal"),
                    _z(5.0, 6.0, "5% to 6%: softening", "watch"), _z(6.0, None, "Above 6%: weak", "risk")]),
]}


# ------------------------------------------------------------------ sections
@dataclass
class ChartSpec:
    title: str
    lines: list[tuple[str, str]]      # (legend label, reading key)
    kind: str                         # pct | points | billions | index | level
    years: float
    index: bool = False
    zero: bool = False
    ref: Optional[tuple[float, str]] = None
    step: bool = False


@dataclass
class Section:
    title: str
    intro: str
    charts: list[ChartSpec]
    items: list[str]


SECTIONS = [
    Section("The Fed and overnight funding",
            "Where the Fed has set rates and whether overnight markets are behaving.",
            [ChartSpec("Fed target vs. what banks actually pay overnight",
                       [("Fed target (top)", "DFEDTARU"), ("Effective fed funds", "EFFR"), ("SOFR", "SOFR")], "pct", 2, step=False)],
            ["DFEDTARU", "EFFR", "SOFR", "sofr_vs_iorb"]),
    Section("Treasury yields",
            "The safe-asset yields every bank rate is measured against.",
            [ChartSpec("3-month, 2-year and 10-year Treasury yields",
                       [("3-month", "DGS3MO"), ("2-year", "DGS2"), ("10-year", "DGS10")], "pct", 2)],
            ["DGS3MO", "DGS2", "DGS10"]),
    Section("The yield curve", "Long rates minus short rates: the shape that drives bank margins.",
            [ChartSpec("10-year minus 2-year Treasury yield", [("10-yr minus 2-yr", "curve_10y_2y")], "points", 5, zero=True)],
            ["curve_10y_2y"]),
    Section("What banks pay depositors vs. Treasuries",
            "How competitive bank deposits are. This is the backdrop for ICS and CDARS pricing.",
            [ChartSpec("Bank deposit rates vs. the 3-month T-bill",
                       [("3-month T-bill", "DGS3MO"), ("Bank 12-mo CD (avg)", "NDR12MCD"), ("Bank money market (avg)", "MMNDR")], "pct", 3),
             ChartSpec("The gap: T-bill minus bank money market rate", [("Gap", "bill_vs_mm")], "points", 4, ref=(4.5, "very wide"))],
            ["MMNDR", "NDR12MCD", "bill_vs_mm"]),
    Section("Where deposits are going",
            "The pool IntraFi's network moves, and the competition for it.",
            [ChartSpec("Deposits and money funds (one year ago = 100)",
                       [("Small banks", "DPSSCBW027SBOG"), ("Large banks", "DPSLCBW027SBOG"), ("Money market funds", "WRMFNS")],
                       "index", 1, index=True, ref=(100.0, ""))],
            ["DPSSCBW027SBOG", "DPSLCBW027SBOG", "WRMFNS"]),
    Section("Bank funding stress",
            "Early warnings that banks are short of cash. Usually quiet; they matter when they move.",
            [ChartSpec("Small banks' borrowing (mostly FHLB advances)", [("Borrowings", "H8B3094NSMA")], "billions", 3),
             ChartSpec("Emergency borrowing from the Fed (discount window)", [("Discount window", "WLCFLPCL")], "billions", 4,
                       ref=(50.0, "stress"))],
            ["H8B3094NSMA", "WLCFLPCL"]),
    Section("Credit risk", "Whether borrowers are getting into trouble.",
            [ChartSpec("High-yield credit spread", [("High-yield spread", "BAMLH0A0HYM2")], "pct", 5, ref=(5.0, "default worries")),
             ChartSpec("Commercial real estate loans past due (quarterly)", [("CRE delinquency", "DRCRELEXFACBS")], "pct", 12,
                       step=True, ref=(2.0, "elevated"))],
            ["BAMLH0A0HYM2", "DRCRELEXFACBS"]),
    Section("Stocks and volatility", "Market mood. Sharp drops send depositors looking for safety.",
            [ChartSpec("Stocks (one year ago = 100)", [("S&P 500", "SP500"), ("Dow", "DJIA"), ("Nasdaq", "NASDAQCOM")],
                       "index", 1, index=True, ref=(100.0, "")),
             ChartSpec("VIX fear gauge", [("VIX", "VIXCLS")], "level", 2, ref=(30.0, "stress"))],
            ["SP500", "DJIA", "NASDAQCOM", "VIXCLS"]),
    Section("The economy", "What drives the Fed's next move.",
            [ChartSpec("Inflation and unemployment", [("Inflation (CPI, 12-mo)", "CPIAUCSL"), ("Unemployment", "UNRATE")], "pct", 5)],
            ["CPIAUCSL", "UNRATE"]),
]


def _meta(key: str) -> tuple[str, str]:
    """(what it is, risk) for a series or spread."""
    s = SERIES_BY_ID.get(key)
    if s:
        return s.what_it_is, s.risk
    sp = next((x for x in SPREADS if x.key == key), None)
    return (sp.what_it_is, sp.reading) if sp else ("", "")


def means_text(key: str, r: Reading, b: Bundle) -> str:
    item = ITEMS.get(key, Item(key))
    parts = []
    z = current_zone(item, r)
    if z:
        m = metric_value(item, r)
        if item.metric == "drawdown":
            parts.append(f"It is {m:.1f}% below its past-year high: {z.label.split(': ')[-1].lower()}.")
        elif item.metric == "yoy":
            parts.append(f"{item.metric_label.capitalize()}: {m:+.1f}%, which is {z.label.split(': ')[-1].lower()}.")
        else:
            parts.append(f"At {r.display_value}, it is in the “{z.label.split(': ')[-1].lower()}” range.")
    if item.means:
        extra = item.means(r, b)
        if extra:
            parts.append(extra)
    trend = []
    for w, label in (("week", "this week"), ("month", "over the past month")):
        t = r.change_text(w)
        if t not in ("n/a",):
            trend.append(f"{t} {label}")
    if trend:
        parts.append(trend[0].capitalize() + (", " + trend[1] if len(trend) > 1 else "") + ".")
    if r.stale and r.as_of:
        parts.append(f"Latest data is from {r.as_of:%b %-d, %Y}.")
    return " ".join(parts)


# -------------------------------------------------------------------- render
def _chip(text: str, tone: str, current: bool) -> str:
    color = TONE_COLOR[tone]
    if current:
        return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 8px;border-radius:4px;'
                f'border:1.5px solid {color};color:{color};font-size:12px;font-weight:700">'
                f'&#9654; {_esc(text)} (now)</span>')
    return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 8px;border-radius:4px;'
            f'border:1px solid {LINE};color:{QUIET};font-size:12px">{_esc(text)}</span>')


def _status_badge(tone: str) -> str:
    color = TONE_COLOR[tone]
    icon = {"ok": "&#10003;", "watch": "&#9888;", "risk": "&#9888;"}[tone]
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{color};color:#fff;'
            f'font-size:11px;font-weight:700;letter-spacing:.03em">{icon} {TONE_WORD[tone].upper()}</span>')


def item_card(key: str, b: Bundle) -> str:
    r = b.readings.get(key)
    if r is None:
        return ""
    item = ITEMS.get(key, Item(key))
    what, risk = _meta(key)
    if not r.ok:
        return (f'<div style="padding:14px 0;border-top:1px solid {LINE}"><b style="color:{INK}">{_esc(r.name)}</b>'
                f'<p style="margin:4px 0 0;font-size:13px;color:{QUIET}">Data didn\'t load this week; it retries next run.</p></div>')
    z = current_zone(item, r)
    badge = _status_badge(z.tone) if z else ""
    changes = " &middot; ".join(f"{lbl}: {_esc(r.compact_change(w))}" for lbl, w in (("1 wk", "week"), ("1 mo", "month"), ("1 yr", "year"))
                                if r.changes.get(w) is not None)
    rng = ""
    if r.low_1y is not None and r.high_1y is not None:
        from .analysis import fmt_value
        rng = f" &middot; Past year: {_esc(fmt_value(r.low_1y, r.kind, r.unit))} to {_esc(fmt_value(r.high_1y, r.kind, r.unit))}"
    as_of = f"as of {r.as_of:%b %-d}" if r.as_of else ""
    ranges = ""
    if item.zones:
        on = f" (based on {item.metric_label})" if item.metric_label else ""
        ranges = (f'<p style="margin:8px 0 2px;font-size:12px;font-weight:600;color:{INK}">Ranges to know{_esc(on)}</p>'
                  + "".join(_chip(zz.label, zz.tone, zz is z) for zz in item.zones))
    return f"""
<div style="padding:16px 0 12px;border-top:1px solid {LINE}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:15px;font-weight:700;color:{INK}">{_esc(r.name)} {badge}</td>
    <td align="right" style="font-size:18px;font-weight:700;color:{INK};white-space:nowrap">{_esc(r.display_value)}</td>
  </tr></table>
  <p style="margin:2px 0 8px;font-size:12px;color:{QUIET}">{_esc(as_of)}{(' &middot; ' + changes) if changes else ''}{rng}</p>
  <p style="margin:0 0 6px;font-size:13.5px;line-height:1.5;color:{INK}"><b>What it is:</b> {_esc(what)}</p>
  <p style="margin:0 0 6px;font-size:13.5px;line-height:1.5;color:{INK}"><b>What it means now:</b> {_esc(means_text(key, r, b))}</p>
  {f'<p style="margin:0 0 6px;font-size:13.5px;line-height:1.5;color:{INK}"><b>Risk to watch:</b> {_esc(risk)}</p>' if risk else ''}
  {ranges}
</div>"""


def build_chart(spec: ChartSpec, b: Bundle) -> Optional[bytes]:
    start = b.today - timedelta(days=int(365 * spec.years))
    lines = []
    for label, key in spec.lines:
        r = b.readings.get(key)
        pts = r.points if r and r.ok else []
        if spec.index:
            pts = index_to_100(pts, start)
        lines.append(Line(label, pts))
    try:
        return line_chart(lines, spec.kind, start, zero_line=spec.zero, ref_line=spec.ref, step=spec.step)
    except Exception:
        return None


def headline_bullets(b: Bundle) -> list[tuple[str, str]]:
    """The week in one page: anything in a risk or watch range, plus essentials."""
    out: list[tuple[str, str]] = []
    top = _r(b, "DFEDTARU")
    if top:
        out.append(("info", means_fed(top, b)))
    parts = []
    for k, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        r = _r(b, k)
        if r and r.changes.get("week") is not None:
            parts.append(f"{label} {r.change_text('week')}")
    ten = _r(b, "DGS10")
    if ten and ten.changes.get("week") is not None:
        parts.append(f"10-year Treasury {ten.display_value} ({ten.change_text('week')})")
    if parts:
        out.append(("info", "This week: " + ", ".join(parts) + "."))
    for f in b.flags:
        if f.level == "watch":
            out.append(("watch", f.text))
    flagged = {k for f in b.flags for k in f.keys}
    for key, item in ITEMS.items():
        r = b.readings.get(key)
        if not r or not r.ok or key in flagged:
            continue
        z = current_zone(item, r)
        if z and z.tone in ("watch", "risk"):
            out.append((z.tone, f"{r.name} is {r.display_value}: {z.label.split(': ')[-1]}."))
    for f in b.failures.failures:
        if (b.today - f.fail_date).days <= 7:
            out.append(("risk", f"The FDIC closed {f.name} ({f.city_state}) on {f.fail_date:%b %-d}, with "
                                f"{fmt_thousands(f.deposits_thousands)} in deposits. Check whether it was in IntraFi's network."))
    if not b.failures.error:
        ytd = [f for f in b.failures.failures if f.fail_date.year == b.today.year]
        out.append(("info", f"Bank failures so far in {b.today.year}: {len(ytd)}"
                            + (f" (most recent: {ytd[0].name}, {ytd[0].fail_date:%b %-d})." if ytd else ".")))
    if not any(t in ("watch", "risk") for t, _ in out):
        out.append(("ok", "Nothing is in a watch or risk range this week."))
    return out


def render(b: Bundle) -> tuple[str, str, dict[str, bytes], str]:
    """Returns (html, text, images by content-id, subject)."""
    images: dict[str, bytes] = {}
    bullets = headline_bullets(b)
    color = {"risk": RISK_C, "watch": WATCH_C}
    inner = ('<h2 style="margin:22px 0 8px;font-size:18px;color:%s">The week in one page</h2>' % INK
             + '<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.55">'
             + "".join(f'<li style="margin:0 0 8px;color:{color.get(t, INK)}">'
                       f'{"<b>" + TONE_WORD[t] + ":</b> " if t in color else ""}{_esc(x)}</li>' for t, x in bullets)
             + "</ul>")
    inner += (f'<p style="margin:10px 0 0;font-size:12px;line-height:1.5;color:{QUIET}">"Points" means percentage points '
              "(0.25 points = 25 basis points). Ranges to know are general rules of thumb for context, not official thresholds. "
              "Bank deposit rates are FDIC national averages.</p>")
    n = 0
    for sec in SECTIONS:
        inner += (f'<h2 style="margin:34px 0 4px;font-size:19px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  f'{_esc(sec.title)}</h2><p style="margin:0 0 10px;font-size:13px;color:{QUIET}">{_esc(sec.intro)}</p>')
        for spec in sec.charts:
            png = build_chart(spec, b)
            if png:
                n += 1
                cid = f"chart{n}"
                images[cid] = png
                inner += (f'<p style="margin:10px 0 2px;font-size:13px;font-weight:600;color:{INK}">{_esc(spec.title)}</p>'
                          f'<img src="cid:{cid}" width="624" alt="{_esc(spec.title)} chart" '
                          f'style="display:block;width:100%;max-width:624px;height:auto;border:0;margin:0 0 4px">')
        for key in sec.items:
            inner += item_card(key, b)
    if b.calendar.events:
        inner += (f'<h2 style="margin:34px 0 8px;font-size:19px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  "The week ahead</h2><ul style=\"margin:0;padding-left:20px;font-size:14px;line-height:1.55\">"
                  + "".join(f"<li><b>{e.when:%a %b %-d}</b> &middot; {_esc(e.label)}</li>" for e in b.calendar.events) + "</ul>")
    html = page("Weekly Rates & Markets", f"Week of {b.today:%B %-d, %Y}", inner, b)

    lines = []
    if b.sample:
        lines.append("*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n")
    lines += [f"WEEKLY RATES & MARKETS - week of {b.today:%B %-d, %Y}", "(Charts are in the HTML version.)", "",
              "THE WEEK IN ONE PAGE"]
    lines += [f"  - {TONE_WORD[t].upper() + ': ' if t in color else ''}{x}" for t, x in bullets]
    for sec in SECTIONS:
        lines += ["", sec.title.upper()]
        for key in sec.items:
            r = b.readings.get(key)
            if not r or not r.ok:
                continue
            what, risk = _meta(key)
            item = ITEMS.get(key, Item(key))
            lines += [f"  {r.name}: {r.display_value}",
                      f"    What it is: {what}", f"    What it means now: {means_text(key, r, b)}"]
            if risk:
                lines.append(f"    Risk to watch: {risk}")
            if item.zones:
                lines.append("    Ranges to know: " + "; ".join(z.label for z in item.zones))
    if b.calendar.events:
        lines += ["", "THE WEEK AHEAD"] + [f"  {e.when:%a %b %-d}: {e.label}" for e in b.calendar.events]
    lines += ["", "Ranges are rules of thumb, not official thresholds. Data: FRED, FDIC. Not investment advice."]
    text = "\n".join(lines)

    top_issue = next((x for t, x in bullets if t in ("risk", "watch")), None)
    tail = (top_issue.split(". ")[0].rstrip(".")[:80] if top_issue else "nothing in a watch or risk range")
    subject = f"Weekly Rates & Markets, {b.today:%b %-d}: {tail}"
    return html, text, images, subject
