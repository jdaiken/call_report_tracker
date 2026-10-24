"""IntraFi risk monitor: the essential rate and market measures, grouped by the
risk they signal for IntraFi, each with a status, a plain-English insight, why
IntraFi cares, what it is, and rule-of-thumb ranges.

`build_report()` turns the day's data into a `Report` (pure content). The email
(`render_html`, `render_text`) and the PDF (pdf.py) are both drawn from it, so
they always say the same thing.

Ranges are general rules of thumb for context, not official thresholds. All
text is rules-based: fixed sentences filled in from the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .analysis import Reading, fmt_value, reading_from_points
from .charts import Band, Line, index_to_100, line_chart
from .email_report import INK, LINE, QUIET, Bundle, _esc, page
from .extras import FOMC_DECISION_DAYS, Event, fmt_thousands

OK_C, WATCH_C, RISK_C, INFO_C = "#1b6e3c", "#8a5a00", "#b3261e", "#4a5058"
TONE_COLOR = {"ok": OK_C, "watch": WATCH_C, "risk": RISK_C, "info": INFO_C}
TONE_WORD = {"ok": "Normal", "watch": "Watch", "risk": "Risk", "info": "Info"}


# --------------------------------------------------------------------- zones
@dataclass(frozen=True)
class Zone:
    lo: Optional[float]
    hi: Optional[float]
    label: str
    tone: str           # ok | watch | risk

    def contains(self, x: float) -> bool:
        return (self.lo is None or x >= self.lo) and (self.hi is None or x < self.hi)

    @property
    def short(self) -> str:
        return self.label.split(": ")[-1]


def _z(lo, hi, label, tone="ok") -> Zone:
    return Zone(lo, hi, label, tone)


@dataclass
class Item:
    key: str
    name: str
    what: str                       # what it is (one sentence)
    why: str                        # why IntraFi cares (the risk)
    zones: list[Zone] = field(default_factory=list)
    metric: str = "value"           # value | yoy
    basis: str = ""                 # what the ranges are measured on, if not the level itself
    insight: Optional[Callable[[Reading, Bundle], str]] = None


def metric_value(item: Item, r: Reading) -> Optional[float]:
    if not r.ok:
        return None
    if item.metric == "yoy":
        return r.change_size("year") if r.changes.get("year") is not None else None
    return r.value


def current_zone(item: Item, r: Reading) -> Optional[Zone]:
    m = metric_value(item, r)
    return None if m is None else next((z for z in item.zones if z.contains(m)), None)


# -------------------------------------------------------------- insights
def _r(b: Bundle, key: str) -> Optional[Reading]:
    r = b.readings.get(key)
    return r if r and r.ok else None


def _pct(r: Optional[Reading], window: str) -> Optional[float]:
    if not r or r.changes.get(window) is None or not r.bases.get(window):
        return None
    return r.changes[window] / r.bases[window] * 100


def ins_fed(r: Reading, b: Bundle) -> str:
    bottom = _r(b, "DFEDTARL")
    rng = f"{bottom.value:.2f}% to {r.value:.2f}%" if bottom else f"top {r.value:.2f}%"
    yr = r.changes.get("year")
    trend = ("unchanged from a year ago" if yr is None or abs(yr) < 0.01
             else f"{'up' if yr > 0 else 'down'} {abs(yr):.2f} points in a year")
    s = f"Fed range is {rng}, {trend}."
    wk = r.changes.get("week")
    if wk and abs(wk) >= 0.01:
        s = f"The Fed {'raised' if wk > 0 else 'cut'} rates this week. " + s + " Expect deposit repricing across the network."
    nxt = next((d for d in FOMC_DECISION_DAYS if d >= b.today), None)
    if nxt:
        s += f" Next decision {nxt:%a %b %-d}."
    return s


def ins_2y(r: Reading, b: Bundle) -> str:
    effr = _r(b, "EFFR")
    if not effr:
        return ""
    d = r.value - effr.value
    if d <= -0.25:
        return f"{abs(d):.2f} points below fed funds: markets expect cuts, so deposit rates are likely to drift down."
    if d >= 0.25:
        return f"{d:.2f} points above fed funds: markets expect hikes, so funding costs are likely to rise."
    return "Close to fed funds: markets expect rates to hold."


def ins_gap(r: Reading, b: Bundle) -> str:
    bill, mm = _r(b, "DGS3MO"), _r(b, "MMNDR")
    s = f"T-bills pay {bill.display_value} vs. {mm.display_value} at the average bank." if bill and mm else ""
    m = r.changes.get("month")
    if m is not None and abs(m) >= 0.10:
        s += f" Gap {'widened' if m > 0 else 'narrowed'} {abs(m):.2f} points this month."
    return s.strip()


def ins_small_deposits(r: Reading, b: Bundle) -> str:
    sg, lg = _pct(r, "year"), _pct(_r(b, "DPSLCBW027SBOG"), "year")
    if sg is None or lg is None:
        return ""
    if sg < lg - 1:
        return f"Growing slower than the largest banks ({sg:+.1f}% vs {lg:+.1f}%): deposits are drifting to big banks."
    if sg > lg + 1:
        return f"Outgrowing the largest banks ({sg:+.1f}% vs {lg:+.1f}%)."
    return f"Growing at about the same pace as the largest banks ({sg:+.1f}% vs {lg:+.1f}%)."


def ins_stablecoins(r: Reading, b: Bundle) -> str:
    small = _r(b, "DPSSCBW027SBOG")
    return f"Now equal to {r.value / small.value * 100:.1f}% of all small-bank deposits." if small else ""


def ins_sofr_iorb(r: Reading, b: Bundle) -> str:
    sofr = _r(b, "SOFR")
    return f"SOFR is {sofr.display_value}." if sofr else ""


def ins_cre(r: Reading, b: Bundle) -> str:
    return f"Q{(r.as_of.month - 1) // 3 + 1} {r.as_of.year} data; updates quarterly." if r.as_of else ""


GROWTH = "growth over the past year"

ITEMS: dict[str, Item] = {i.key: i for i in [
    Item("DFEDTARU", "Fed funds rate",
         "The Fed's target for overnight lending between banks. Every deposit rate keys off it.",
         "Rate moves reprice every deposit in the network. Cuts lower what depositors earn and push large depositors to shop "
         "around; hikes raise member banks' funding costs.", insight=ins_fed),
    Item("DGS2", "Rate outlook (2-year Treasury)",
         "The market's best guess of where the Fed is heading over the next two years.",
         "Shows which way deposit pricing is headed, so you can anticipate repricing pressure in ICS and CDARS.",
         insight=ins_2y),
    Item("bill_vs_mm", "Treasuries vs. bank deposits",
         "How much more a 3-month T-bill pays than the average bank money market account (percentage points).",
         "The wider the gap, the stronger the pull for a corporate treasurer to move large balances out of banks and into "
         "Treasuries: direct competition for the dollars IntraFi places.",
         [_z(None, 2.0, "Under 2: deposits competitive"), _z(2.0, 3.5, "2 to 3.5: typical when rates are high"),
          _z(3.5, 4.5, "3.5 to 4.5: wide", "watch"), _z(4.5, None, "Over 4.5: very wide (2023 levels)", "risk")],
         insight=ins_gap),
    Item("WRMFNS", "Money market funds",
         "Money individuals hold in retail money market funds.",
         "Fast growth means cash is leaving bank deposits for funds, shrinking the pool member banks can place.",
         [_z(None, 0.0, "Shrinking: money returning to banks"), _z(0.0, 8.0, "0% to 8%: normal"),
          _z(8.0, 15.0, "8% to 15%: money leaving banks", "watch"), _z(15.0, None, "Over 15%: fast outflow from banks", "risk")],
         "yoy", GROWTH),
    Item("DPSSCBW027SBOG", "Small-bank deposits",
         "Deposits at all U.S. banks outside the 25 largest: most of IntraFi's network.",
         "Shrinking deposits, or deposits moving to the biggest banks, is the 2023 deposit-flight risk. It squeezes member "
         "banks' funding and raises demand for insured-deposit products.",
         [_z(None, 0.0, "Shrinking", "risk"), _z(0.0, 3.0, "0% to 3%: slow", "watch"),
          _z(3.0, 7.0, "3% to 7%: normal"), _z(7.0, None, "Over 7%: strong")],
         "yoy", GROWTH, ins_small_deposits),
    Item("STABLECOINS", "Stablecoins",
         "Dollar-pegged crypto tokens (such as USDT and USDC) in circulation, used like digital dollars.",
         "Every dollar in a stablecoin is a dollar not in a bank deposit. If they start paying yield, even through loopholes "
         "in the GENIUS Act, deposits could leak from community banks and the pool IntraFi moves shrinks.",
         [_z(None, 0.0, "Shrinking"), _z(0.0, 25.0, "0% to 25%: moderate"),
          _z(25.0, 75.0, "25% to 75%: fast", "watch"), _z(75.0, None, "Over 75%: very fast", "risk")],
         "yoy", GROWTH, ins_stablecoins),
    Item("sofr_vs_iorb", "Overnight funding pressure",
         "SOFR (the overnight cost of cash) minus what the Fed pays banks on reserves.",
         "The first sign of a cash squeeze in the banking system. Squeezes hit weaker member banks first.",
         [_z(None, 0.0, "Below 0: cash plentiful"), _z(0.0, 0.10, "0 to +0.10: tightening", "watch"),
          _z(0.10, None, "Over +0.10: funding stress", "risk")], insight=ins_sofr_iorb),
    Item("WLCFLPCL", "Emergency Fed borrowing",
         "Loans banks took from the Fed's discount window, reported weekly.",
         "Banks borrow here only when they need cash fast. A jump flagged the March 2023 bank runs.",
         [_z(None, 10.0, "Under $10B: normal"), _z(10.0, 50.0, "$10B to $50B: elevated", "watch"),
          _z(50.0, None, "Over $50B: stress (2023 peak about $150B)", "risk")]),
    Item("H8B3094NSMA", "Small-bank borrowing",
         "Money small banks have borrowed, mostly FHLB advances. The closest public read on FHLB demand.",
         "Rising borrowing means member banks need more funding than their deposits provide. That need can also be met "
         "with one-way placements.",
         [_z(None, -5.0, "Falling: deposits covering needs"), _z(-5.0, 10.0, "-5% to +10%: stable"),
          _z(10.0, 25.0, "+10% to +25%: rising reliance", "watch"), _z(25.0, None, "Over +25%: rapid rise", "risk")],
         "yoy", "change over the past year"),
    Item("DGS10", "Long-term rates (10-year)",
         "The yield on 10-year Treasuries. Sets the value of the bonds banks hold.",
         "Higher long rates mean bigger paper losses on bank bond portfolios, the problem that sank SVB. That weakens "
         "member-bank capital.",
         [_z(None, 3.0, "Under 3%: low"), _z(3.0, 4.5, "3% to 4.5%: moderate"),
          _z(4.5, 5.0, "4.5% to 5%: high", "watch"), _z(5.0, None, "Over 5%: highest since 2007", "risk")]),
    Item("curve_10y_2y", "Yield curve",
         "10-year minus 2-year Treasury yield (percentage points).",
         "A flat or inverted curve squeezes member banks' lending margins and has often come before recessions.",
         [_z(None, 0.0, "Below 0: inverted", "risk"), _z(0.0, 0.5, "0 to 0.5: flat", "watch"),
          _z(0.5, 1.5, "0.5 to 1.5: normal"), _z(1.5, None, "Over 1.5: steep")]),
    Item("DRCRELEXFACBS", "Commercial real estate loans past due",
         "Share of bank CRE loans 30+ days past due.",
         "The biggest credit risk for community banks. Rising delinquencies lead to losses, capital strain and failures "
         "inside the network.",
         [_z(None, 1.0, "Under 1%: low"), _z(1.0, 2.0, "1% to 2%: moderate"),
          _z(2.0, 4.0, "2% to 4%: elevated", "watch"), _z(4.0, None, "Over 4%: high (2010 peak near 9%)", "risk")],
         insight=ins_cre),
    Item("BAMLH0A0HYM2", "Credit stress",
         "Extra yield investors demand on risky corporate bonds (the high-yield spread).",
         "Widening spreads signal more defaults ahead, usually before bank loan losses show up.",
         [_z(None, 3.5, "Under 3.5%: calm"), _z(3.5, 5.0, "3.5% to 5%: normal"),
          _z(5.0, 7.0, "5% to 7%: default worries", "watch"), _z(7.0, None, "Over 7%: credit stress", "risk")]),
    Item("VIXCLS", "Market fear (VIX)",
         "Expected stock market volatility over the next month.",
         "Market panics trigger flights to safety. Depositors check whether balances are fully insured, raising demand "
         "for IntraFi's products and run risk at weak banks.",
         [_z(None, 15.0, "Under 15: calm"), _z(15.0, 20.0, "15 to 20: normal"),
          _z(20.0, 30.0, "20 to 30: elevated", "watch"), _z(30.0, None, "Over 30: panic", "risk")]),
]}


def insight_line(key: str, r: Reading, b: Bundle) -> str:
    """The one-line insight: where it sits in its ranges, plus the item's own context."""
    item = ITEMS[key]
    z = current_zone(item, r)
    bits = []
    if z:
        m = metric_value(item, r)
        if item.metric == "yoy":
            bits.append(f"{r.display_value}, {m:+.1f}% in a year: {z.short.lower()}.")
        else:
            bits.append(f"{r.display_value}: {z.short.lower()}.")
    if item.insight:
        extra = item.insight(r, b)
        if extra:
            bits.append(extra)
    return " ".join(bits) or f"{r.display_value}."


def stablecoin_reading(points: list[tuple[date, float]], today: date, error: Optional[str] = None) -> Reading:
    it = ITEMS["STABLECOINS"]
    return reading_from_points("STABLECOINS", it.name, "level", "$B", it.what, it.why, points, today, "daily",
                               notable_month_move=5.0, error=error)


# ------------------------------------------------------------- risk areas
@dataclass
class ChartSpec:
    takeaway: Callable[[Bundle], str]
    subtitle: str
    lines: list[tuple[str, str]]
    kind: str
    years: float
    index: bool = False
    zero: bool = False
    ref: Optional[tuple[float, str]] = None
    step: bool = False
    zones_key: Optional[str] = None
    fed_band: bool = False


@dataclass
class Area:
    title: str
    question: str                      # the risk question this area answers
    charts: list[ChartSpec]
    items: list[str]


def _take_item(key: str) -> Callable[[Bundle], str]:
    """Chart headline: level, range and recent trend (the item card below has the detail)."""
    def f(b: Bundle) -> str:
        r = _r(b, key)
        if not r:
            return ""
        item = ITEMS[key]
        z = current_zone(item, r)
        s = f"{item.name} is {r.display_value}" + (f" ({z.short.lower()})" if z else "")
        w = "year" if item.metric == "yoy" or r.changes.get("month") is None else "month"
        if r.changes.get(w) is not None:
            s += f", {r.change_text(w)} over the past {w}"
        return s + "."
    return f


def _take_rates(b: Bundle) -> str:
    r = _r(b, "DGS2")
    return f"2-year yield is {ins_2y(r, b)}" if r else ""


def _take_flight(b: Bundle) -> str:
    parts = []
    for k, label in (("DPSSCBW027SBOG", "small banks"), ("DPSLCBW027SBOG", "largest banks"), ("WRMFNS", "money funds")):
        p = _pct(_r(b, k), "year")
        if p is not None:
            parts.append(f"{label} {p:+.1f}%")
    return ("Past year: " + ", ".join(parts) + ".") if parts else ""


AREAS = [
    Area("Rate path", "Which way are deposit rates heading?",
         [ChartSpec(_take_rates, "Gray band: the Fed's target range. Lines: fed funds and the 2-year yield. Past 2 years.",
                    [("Fed funds", "EFFR"), ("2-year", "DGS2")], "pct", 2, fed_band=True)],
         ["DFEDTARU", "DGS2"]),
    Area("Deposit competition", "Can bank deposits compete with Treasuries and money funds?",
         [ChartSpec(_take_item("bill_vs_mm"), "3-month T-bill minus the average bank money market rate. Past 4 years.",
                    [("Gap", "bill_vs_mm")], "points", 4, zones_key="bill_vs_mm")],
         ["bill_vs_mm", "WRMFNS"]),
    Area("Deposit flight", "Are deposits leaving small banks?",
         [ChartSpec(_take_flight, "Each line starts at 100 one year ago. Above 100 = grew.",
                    [("Small banks", "DPSSCBW027SBOG"), ("Largest banks", "DPSLCBW027SBOG"), ("Money funds", "WRMFNS")],
                    "index", 1, index=True, ref=(100.0, ""))],
         ["DPSSCBW027SBOG"]),
    Area("Stablecoins", "How fast are digital dollars pulling money outside banks?",
         [ChartSpec(_take_item("STABLECOINS"), "Total dollar stablecoins in circulation. Past 3 years.",
                    [("Stablecoins", "STABLECOINS")], "billions", 3)],
         ["STABLECOINS"]),
    Area("Funding and liquidity stress", "Are banks short of cash?",
         [ChartSpec(_take_item("sofr_vs_iorb"), "SOFR minus interest on reserves, in percentage points. Past 2 years.",
                    [("SOFR minus IORB", "sofr_vs_iorb")], "points", 2, zero=True, zones_key="sofr_vs_iorb"),
          ChartSpec(_take_item("WLCFLPCL"), "Discount window loans outstanding. Past 4 years.",
                    [("Discount window", "WLCFLPCL")], "billions", 4, zones_key="WLCFLPCL")],
         ["sofr_vs_iorb", "WLCFLPCL", "H8B3094NSMA"]),
    Area("Member-bank balance sheets", "Are bond losses, margins or CRE loans straining banks?",
         [ChartSpec(_take_item("DGS10"), "10-year Treasury yield. Past 5 years.",
                    [("10-year", "DGS10")], "pct", 5, zones_key="DGS10"),
          ChartSpec(_take_item("curve_10y_2y"), "10-year minus 2-year yield. Below the black line = inverted. Past 5 years.",
                    [("Curve", "curve_10y_2y")], "points", 5, zero=True, zones_key="curve_10y_2y"),
          ChartSpec(_take_item("DRCRELEXFACBS"), "Share of bank CRE loans 30+ days past due, by quarter. Past 12 years.",
                    [("CRE past due", "DRCRELEXFACBS")], "pct", 12, step=True, zones_key="DRCRELEXFACBS")],
         ["DGS10", "curve_10y_2y", "DRCRELEXFACBS"]),
    Area("Credit and market stress", "Are investors bracing for trouble?",
         [ChartSpec(_take_item("BAMLH0A0HYM2"), "High-yield spread over Treasuries. Past 5 years.",
                    [("High-yield spread", "BAMLH0A0HYM2")], "pct", 5, zones_key="BAMLH0A0HYM2"),
          ChartSpec(_take_item("VIXCLS"), "VIX. Past 2 years.", [("VIX", "VIXCLS")], "level", 2, zones_key="VIXCLS")],
         ["BAMLH0A0HYM2", "VIXCLS"]),
]


# ------------------------------------------------------------ the report
@dataclass
class RangeChip:
    label: str
    tone: str
    current: bool


@dataclass
class ItemContent:
    key: str
    name: str
    reading: str
    as_of: str
    tone: str                 # ok | watch | risk | info
    insight: str
    why: str
    what: str
    changes: str
    past_year: str
    ranges: list[RangeChip]
    basis: str
    loaded: bool = True


@dataclass
class ChartContent:
    cid: str
    takeaway: str
    subtitle: str
    png: bytes


@dataclass
class AreaContent:
    title: str
    question: str
    tone: str
    charts: list[ChartContent]
    items: list[ItemContent]


@dataclass
class Report:
    today: date
    subject: str
    alerts: list[tuple[str, str]]          # (tone, text) at the very top
    areas: list[AreaContent]
    failures_line: str
    coming_up: list[Event]
    sample: bool
    missing: list[str]


def _worst(tones: list[str]) -> str:
    return next((t for t in ("risk", "watch", "ok") if t in tones), "info")


def item_content(key: str, b: Bundle) -> ItemContent:
    item = ITEMS[key]
    r = b.readings.get(key)
    if r is None or not r.ok:
        return ItemContent(key, item.name, "n/a", "", "info", "Data didn't load today; it retries next run.",
                           item.why, item.what, "", "", [], "", loaded=False)
    z = current_zone(item, r)
    changes = " · ".join(f"{lbl} {r.compact_change(w)}" for lbl, w in (("Last:", "last"), ("1 mo:", "month"), ("1 yr:", "year"))
                         if r.changes.get(w) is not None)
    past = (f"{fmt_value(r.low_1y, r.kind, r.unit)} to {fmt_value(r.high_1y, r.kind, r.unit)}"
            if r.low_1y is not None and r.high_1y is not None else "")
    insight = insight_line(key, r, b)
    if r.stale and r.as_of:
        insight += f" (Latest data: {r.as_of:%b %-d, %Y}.)"
    return ItemContent(key, item.name, r.display_value, f"{r.as_of:%b %-d}" if r.as_of else "",
                       z.tone if z else "info", insight, item.why, item.what, changes, past,
                       [RangeChip(zz.label, zz.tone, zz is z) for zz in item.zones], item.basis)


def build_chart(spec: ChartSpec, b: Bundle) -> Optional[bytes]:
    start = b.today - timedelta(days=int(365 * spec.years))
    lines = []
    for label, key in spec.lines:
        r = b.readings.get(key)
        pts = r.points if r and r.ok else []
        lines.append(Line(label, index_to_100(pts, start) if spec.index else pts))
    bands = []
    if spec.zones_key and ITEMS[spec.zones_key].metric == "value":
        bands = [Band(z.lo, z.hi, z.tone, f"{TONE_WORD[z.tone]}: {z.short}") for z in ITEMS[spec.zones_key].zones
                 if z.tone in ("watch", "risk")]
    fed = None
    if spec.fed_band and _r(b, "DFEDTARU") and _r(b, "DFEDTARL"):
        fed = (b.readings["DFEDTARL"].points, b.readings["DFEDTARU"].points, "Fed target range")
    try:
        return line_chart(lines, spec.kind, start, zero_line=spec.zero, ref_line=spec.ref, step=spec.step,
                          bands=bands, range_band=fed)
    except Exception:
        return None


def build_report(b: Bundle) -> Report:
    areas, n = [], 0
    for a in AREAS:
        items = [item_content(k, b) for k in a.items]
        charts = []
        for spec in a.charts:
            png = build_chart(spec, b)
            if png:
                n += 1
                charts.append(ChartContent(f"chart{n}", spec.takeaway(b), spec.subtitle, png))
        areas.append(AreaContent(a.title, a.question, _worst([i.tone for i in items]), charts, items))

    alerts: list[tuple[str, str]] = []
    for f in b.failures.failures:
        if (b.today - f.fail_date).days <= 7:
            alerts.append(("risk", f"Bank failure: the FDIC closed {f.name} ({f.city_state}) on {f.fail_date:%b %-d} "
                                   f"with {fmt_thousands(f.deposits_thousands)} in deposits. Check network exposure."))
    top = _r(b, "DFEDTARU")
    if top and top.changes.get("week") and abs(top.changes["week"]) >= 0.01:
        alerts.append(("watch", f"The Fed {'raised' if top.changes['week'] > 0 else 'cut'} rates by "
                                f"{abs(top.changes['week']):.2f} points this week. Expect deposit repricing, including ICS "
                                "and CDARS rates."))
    for a in areas:
        for i in a.items:
            if i.tone in ("risk", "watch"):
                alerts.append((i.tone, f"{i.name}: {i.insight}"))
    for f in b.flags:   # sudden moves not captured by a range
        if f.level == "watch" and any(k in f.text for k in ("discount window jumped", "small banks while")):
            alerts.append(("watch", f.text))
    alerts.sort(key=lambda t: 0 if t[0] == "risk" else 1)

    if b.failures.error:
        failures_line = "Bank failure data didn't load today."
    else:
        ytd = [f for f in b.failures.failures if f.fail_date.year == b.today.year]
        failures_line = (f"Bank failures in {b.today.year}: {len(ytd)}"
                         + (f" (most recent: {ytd[0].name}, {ytd[0].fail_date:%b %-d})." if ytd else "."))

    n_risk = sum(1 for t, _ in alerts if t == "risk")
    n_watch = sum(1 for t, _ in alerts if t == "watch")
    status = (f"{n_risk} risk, {n_watch} watch" if n_risk
              else f"{n_watch} watch item{'s' if n_watch != 1 else ''}" if n_watch else "all normal")
    missing = [r.name for r in b.readings.values() if not r.ok]
    missing += [lbl for lbl, err in (("bank failures", b.failures.error), ("release calendar", b.calendar.error)) if err]
    return Report(b.today, f"IntraFi Risk Monitor, {b.today:%a %b %-d}: {status}", alerts, areas, failures_line,
                  b.calendar.events, b.sample, missing)


# ------------------------------------------------------------------ email
def _badge(tone: str) -> str:
    color = TONE_COLOR[tone]
    icon = {"ok": "&#10003;", "watch": "&#9888;", "risk": "&#9888;", "info": "&#9679;"}[tone]
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{color};color:#fff;'
            f'font-size:11px;font-weight:700;letter-spacing:.03em;white-space:nowrap;vertical-align:middle">'
            f'{icon} {TONE_WORD[tone].upper()}</span>')


def _dashboard_html(rep: Report) -> str:
    rows = ""
    for a in rep.areas:
        rows += (f'<tr><td colspan="3" style="padding:12px 6px 4px;font-size:12px;font-weight:700;text-transform:uppercase;'
                 f'letter-spacing:.04em;color:{QUIET}">{_esc(a.title)}</td></tr>')
        for i in a.items:
            rows += (f'<tr><td style="padding:7px 6px;border-bottom:1px solid {LINE};vertical-align:top;width:86px">{_badge(i.tone)}</td>'
                     f'<td style="padding:7px 6px;border-bottom:1px solid {LINE};vertical-align:top;font-size:13.5px;line-height:1.45;color:{INK}">'
                     f'<b>{_esc(i.name)}</b><br><span style="color:{QUIET}">{_esc(i.insight)}</span></td>'
                     f'<td style="padding:7px 6px;border-bottom:1px solid {LINE};vertical-align:top;text-align:right;'
                     f'font-size:14px;font-weight:700;color:{INK};white-space:nowrap">{_esc(i.reading)}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows}</table>'


def _item_html(i: ItemContent) -> str:
    if not i.loaded:
        return (f'<div style="padding:12px 0;border-top:1px solid {LINE}"><b style="color:{INK}">{_esc(i.name)}</b>'
                f'<p style="margin:4px 0 0;font-size:13px;color:{QUIET}">{_esc(i.insight)}</p></div>')
    color = TONE_COLOR[i.tone] if i.tone in ("watch", "risk") else INK
    ranges = ""
    if i.ranges:
        parts = [(f'<b style="color:{TONE_COLOR[c.tone]}">&#9654; {_esc(c.label)}</b>' if c.current else _esc(c.label))
                 for c in i.ranges]
        basis = f" ({_esc(i.basis)})" if i.basis else ""
        ranges = (f'<p style="margin:6px 0 0;font-size:12.5px;line-height:1.55;color:{QUIET}"><b style="color:{INK}">'
                  f'Ranges{basis}:</b> ' + " &middot; ".join(parts) + "</p>")
    meta = " &middot; ".join(x for x in (f"as of {i.as_of}" if i.as_of else "", _esc(i.changes),
                                          f"Past year: {_esc(i.past_year)}" if i.past_year else "") if x)
    lab = f'style="margin:0 0 6px;font-size:13.5px;line-height:1.5;color:{INK}"'
    return f"""
<div style="padding:14px 0 10px;border-top:1px solid {LINE}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:15.5px;font-weight:700;color:{INK}">{_esc(i.name)} {_badge(i.tone)}</td>
    <td align="right" style="font-size:18px;font-weight:700;color:{INK};white-space:nowrap">{_esc(i.reading)}</td>
  </tr></table>
  <p style="margin:2px 0 8px;font-size:12px;color:{QUIET}">{meta}</p>
  <p style="margin:0 0 8px;padding:8px 10px;background:#f4f5f7;border-left:4px solid {color};font-size:14.5px;font-weight:700;
     line-height:1.45;color:{color}">Insight: {_esc(i.insight)}</p>
  <p {lab}><b>Why IntraFi cares:</b> {_esc(i.why)}</p>
  <p {lab}><b>What it is:</b> {_esc(i.what)}</p>
  {ranges}
</div>"""


def render_html(rep: Report, b: Bundle, image_src: Callable[[str], str] = lambda cid: f"cid:{cid}") -> str:
    color = {"risk": RISK_C, "watch": WATCH_C}
    inner = '<h2 style="margin:22px 0 8px;font-size:19px;color:%s">Today\'s alerts</h2>' % INK
    if rep.alerts:
        inner += ('<ul style="margin:0;padding-left:20px;font-size:14.5px;line-height:1.55">'
                  + "".join(f'<li style="margin:0 0 8px;color:{color[t]}"><b>{TONE_WORD[t]}:</b> {_esc(x)}</li>' for t, x in rep.alerts)
                  + "</ul>")
    else:
        inner += f'<p style="margin:0;font-size:14.5px;color:{OK_C}"><b>&#10003; All clear:</b> nothing is in a Watch or Risk range.</p>'
    inner += f'<p style="margin:8px 0 0;font-size:13.5px;color:{INK}">{_esc(rep.failures_line)}</p>'
    inner += ('<h2 style="margin:28px 0 4px;font-size:19px;color:%s">Risk dashboard</h2>' % INK
              + f'<p style="margin:0 0 4px;font-size:12.5px;color:{QUIET}">Green = Normal, amber = Watch, red = Risk, gray = '
              "for information. Ranges are rules of thumb, not official thresholds. The same report is attached as a PDF.</p>"
              + _dashboard_html(rep))
    for a in rep.areas:
        inner += (f'<h2 style="margin:36px 0 2px;font-size:20px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  f'{_esc(a.title)} {_badge(a.tone)}</h2>'
                  f'<p style="margin:2px 0 8px;font-size:13.5px;font-style:italic;color:{QUIET}">{_esc(a.question)}</p>')
        for c in a.charts:
            inner += (f'<p style="margin:12px 0 2px;font-size:15px;font-weight:700;line-height:1.4;color:{INK}">{_esc(c.takeaway)}</p>'
                      f'<p style="margin:0 0 4px;font-size:12px;color:{QUIET}">{_esc(c.subtitle)}</p>'
                      f'<img src="{image_src(c.cid)}" width="624" alt="{_esc(c.takeaway)}" '
                      f'style="display:block;width:100%;max-width:624px;height:auto;border:0;margin:0 0 6px">')
        for i in a.items:
            inner += _item_html(i)
    if rep.coming_up:
        inner += (f'<h2 style="margin:36px 0 8px;font-size:20px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  "Coming up</h2><ul style=\"margin:0;padding-left:20px;font-size:14px;line-height:1.6\">"
                  + "".join(f"<li><b>{e.when:%a %b %-d}</b> &middot; {_esc(e.label)}</li>" for e in rep.coming_up) + "</ul>")
    return page("IntraFi Risk Monitor · Rates & Markets", f"{rep.today:%A, %B %-d, %Y}", inner, b)


def render_text(rep: Report) -> str:
    lines = []
    if rep.sample:
        lines.append("*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n")
    lines += [f"INTRAFI RISK MONITOR - {rep.today:%A, %B %-d, %Y}", "(Charts are in the HTML email and the attached PDF.)", "",
              "TODAY'S ALERTS"]
    lines += [f"  - {TONE_WORD[t].upper()}: {x}" for t, x in rep.alerts] or ["  All clear: nothing in a Watch or Risk range."]
    lines += [f"  {rep.failures_line}", "", "RISK DASHBOARD"]
    for a in rep.areas:
        lines.append(f"  {a.title.upper()}")
        lines += [f"    [{TONE_WORD[i.tone]:6}] {i.name}: {i.reading} - {i.insight}" for i in a.items]
    for a in rep.areas:
        lines += ["", f"{a.title.upper()}: {a.question}"]
        lines += [f"  > {c.takeaway}" for c in a.charts if c.takeaway]
        for i in a.items:
            if not i.loaded:
                continue
            lines += [f"  {i.name}: {i.reading} [{TONE_WORD[i.tone]}]", f"    Insight: {i.insight}",
                      f"    Why IntraFi cares: {i.why}", f"    What it is: {i.what}"]
            if i.ranges:
                lines.append("    Ranges: " + "; ".join(("> " if c.current else "") + c.label for c in i.ranges))
    if rep.coming_up:
        lines += ["", "COMING UP"] + [f"  {e.when:%a %b %-d}: {e.label}" for e in rep.coming_up]
    lines += ["", "Ranges are rules of thumb, not official thresholds. Data: FRED, FDIC, DeFiLlama. Not investment advice."]
    return "\n".join(lines)
