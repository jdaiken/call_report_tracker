"""The newsletter: essential rates and markets, each with a bottom line, what
it is, what it means right now, the risk to watch, and the ranges to know,
plus a chart (with a plain-English takeaway) for every section.

"Ranges to know" are general rules of thumb for context, not official
thresholds. All analysis text is rules-based: fixed sentences filled in from
the numbers, so it never invents anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .analysis import Reading
from .charts import Band, Line, index_to_100, line_chart
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


def means_sofr(r: Reading, b: Bundle) -> str:
    gap = _r(b, "sofr_vs_iorb")
    if not gap:
        return ""
    if gap.value > 0:
        return (f"SOFR is {gap.value:.2f} points above what the Fed pays on reserves: overnight cash is getting tighter.")
    return f"SOFR is {abs(gap.value):.2f} points below what the Fed pays on reserves: overnight funding is calm."


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


def means_stablecoins(r: Reading, b: Bundle) -> str:
    small = _r(b, "DPSSCBW027SBOG")
    parts = []
    if small:
        parts.append(f"That is equal to about {r.value / small.value * 100:.1f}% of all deposits at small banks.")
        if r.changes.get("year") is not None and small.changes.get("year") is not None:
            parts.append(f"Over the past year stablecoins grew {r.change_size('year'):+.0f}% while small-bank deposits "
                         f"changed {small.change_size('year'):+.1f}%.")
    return " ".join(parts)


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
    Item("SOFR", means=means_sofr),
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
    Item("STABLECOINS", [_z(None, 0.0, "Shrinking"), _z(0.0, 25.0, "0% to 25%: moderate growth"),
                         _z(25.0, 75.0, "25% to 75%: fast growth", "watch"), _z(75.0, None, "Above 75%: very fast growth", "risk")],
         "yoy", "growth over the past year", means_stablecoins),
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
def _zone_short(z: Zone) -> str:
    return z.label.split(": ")[-1]


def _pct_since(r: Optional[Reading], window: str) -> Optional[float]:
    if not r or r.changes.get(window) is None or not r.bases.get(window):
        return None
    return r.changes[window] / r.bases[window] * 100


def _zone_line(key: str, b: Bundle) -> str:
    r = _r(b, key)
    item = ITEMS.get(key)
    if not r or not item:
        return ""
    z = current_zone(item, r)
    return f"{r.name} is {r.display_value}" + (f": {_zone_short(z)}." if z else ".")


# One plain-English headline per chart: the takeaway, stated up front.
def take_fed(b: Bundle) -> str:
    top, bot, sofr, gap = _r(b, "DFEDTARU"), _r(b, "DFEDTARL"), _r(b, "SOFR"), _r(b, "sofr_vs_iorb")
    if not (top and bot):
        return "The Fed's target range and overnight rates"
    s = f"The Fed's range is {bot.value:.2f}% to {top.value:.2f}%"
    if gap is not None and sofr:
        s += (f"; SOFR ({sofr.display_value}) is above what the Fed pays on reserves, so overnight cash is getting tighter."
              if gap.value > 0 else "; overnight rates sit inside it and funding is calm.")
    return s + ("" if s.endswith(".") else ".")


def take_treasuries(b: Bundle) -> str:
    two, ten, effr = _r(b, "DGS2"), _r(b, "DGS10"), _r(b, "EFFR")
    if not (two and effr):
        return "Treasury yields"
    d = two.value - effr.value
    view = ("markets expect Fed cuts" if d <= -0.25 else "markets expect higher rates" if d >= 0.25
            else "markets expect rates to hold")
    s = f"The 2-year ({two.display_value}) is {abs(d):.2f} points {'below' if d < 0 else 'above'} fed funds: {view}."
    if ten and ten.changes.get("month") is not None:
        s += f" The 10-year is {ten.display_value}, {ten.change_text('month')} this month."
    return s


def take_curve(b: Bundle) -> str:
    c = _r(b, "curve_10y_2y")
    if not c:
        return "The yield curve"
    z = current_zone(ITEMS["curve_10y_2y"], c)
    return f"The curve is {c.display_value}: {_zone_short(z) if z else ''}."


def take_deposit_rates(b: Bundle) -> str:
    gap, mm, bill = _r(b, "bill_vs_mm"), _r(b, "MMNDR"), _r(b, "DGS3MO")
    if not (gap and mm and bill):
        return "Bank deposit rates vs. Treasuries"
    return (f"T-bills pay {bill.display_value}; the average bank money market account pays {mm.display_value}, "
            f"a gap of {gap.value:.2f} points.")


def take_gap(b: Bundle) -> str:
    g = _r(b, "bill_vs_mm")
    if not g:
        return "The T-bill vs. bank gap"
    z = current_zone(ITEMS["bill_vs_mm"], g)
    trend = g.change_text("month")
    return f"The gap is {g.value:.2f} points ({_zone_short(z) if z else ''}), {trend} over the past month."


def take_deposits(b: Bundle) -> str:
    parts = []
    for k, label in (("DPSSCBW027SBOG", "small banks"), ("DPSLCBW027SBOG", "large banks"), ("WRMFNS", "money funds")):
        p = _pct_since(_r(b, k), "year")
        if p is not None:
            parts.append(f"{label} {p:+.1f}%")
    return ("Past year: " + ", ".join(parts) + ".") if parts else "Deposits and money funds"


def take_stablecoins(b: Bundle) -> str:
    r = _r(b, "STABLECOINS")
    if not r:
        return "Stablecoins in circulation"
    p = _pct_since(r, "year")
    z = current_zone(ITEMS["STABLECOINS"], r)
    return f"Stablecoins total {r.display_value}" + (f", {p:+.0f}% in a year: {_zone_short(z)}." if p is not None and z else ".")


def take_single(key: str) -> Callable[[Bundle], str]:
    def f(b: Bundle) -> str:
        r = _r(b, key)
        if not r:
            return ""
        z = current_zone(ITEMS.get(key, Item(key)), r)
        s = f"{r.name} is {r.display_value}"
        if z:
            s += f": {_zone_short(z)}"
        m = r.change_text("month")
        if m not in ("n/a",):
            s += f" ({m} over the past month)"
        return s + "."
    return f


def take_stocks(b: Bundle) -> str:
    parts = []
    for k, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        r = _r(b, k)
        if r and r.high_1y:
            parts.append(f"{label} {(1 - r.value / r.high_1y) * 100:.1f}% below")
    return ("Distance from past-year highs: " + ", ".join(parts) + ".") if parts else "Stocks"


def take_economy(b: Bundle) -> str:
    cpi, un = _r(b, "CPIAUCSL"), _r(b, "UNRATE")
    if not (cpi and un):
        return "Inflation and unemployment"
    goal = "above" if cpi.value > 2.05 else "at or below"
    return f"Inflation is {cpi.display_value}, {goal} the Fed's 2% goal; unemployment is {un.display_value}."


@dataclass
class ChartSpec:
    takeaway: Callable[[Bundle], str]
    subtitle: str                     # what the chart shows, in small print
    lines: list[tuple[str, str]]      # (label, reading key)
    kind: str                         # pct | points | billions | index | level
    years: float
    index: bool = False
    zero: bool = False
    ref: Optional[tuple[float, str]] = None
    step: bool = False
    zones_key: Optional[str] = None   # shade this item's Watch/Risk ranges on the chart
    fed_band: bool = False            # shade the Fed's target range
    daily: bool = True                # False = only in the full Monday edition


@dataclass
class Section:
    title: str
    intro: str
    charts: list[ChartSpec]
    items: list[str]


SECTIONS = [
    Section("The Fed and overnight funding", "Where the Fed has set rates and whether overnight markets are behaving.",
            [ChartSpec(take_fed, "Gray band: the Fed's target range. Lines: what banks actually paid overnight. Past 2 years.",
                       [("Fed funds", "EFFR"), ("SOFR", "SOFR")], "pct", 2, fed_band=True)],
            ["DFEDTARU", "EFFR", "SOFR", "sofr_vs_iorb"]),
    Section("Treasury yields", "The safe-asset yields every bank rate is measured against.",
            [ChartSpec(take_treasuries, "3-month, 2-year and 10-year Treasury yields. Past 2 years.",
                       [("3-month", "DGS3MO"), ("2-year", "DGS2"), ("10-year", "DGS10")], "pct", 2)],
            ["DGS3MO", "DGS2", "DGS10"]),
    Section("The yield curve", "Long rates minus short rates: the shape that drives bank margins.",
            [ChartSpec(take_curve, "10-year minus 2-year Treasury yield. Below the black line = inverted. Past 5 years.",
                       [("10-yr minus 2-yr", "curve_10y_2y")], "points", 5, zero=True, zones_key="curve_10y_2y")],
            ["curve_10y_2y"]),
    Section("What banks pay depositors vs. Treasuries",
            "How competitive bank deposits are. This is the backdrop for ICS and CDARS pricing.",
            [ChartSpec(take_deposit_rates, "3-month T-bill vs. FDIC national average bank rates. Past 3 years.",
                       [("T-bill", "DGS3MO"), ("12-mo CD", "NDR12MCD"), ("Money market", "MMNDR")], "pct", 3, daily=False),
             ChartSpec(take_gap, "T-bill yield minus the average bank money market rate, in percentage points. Past 4 years.",
                       [("Gap", "bill_vs_mm")], "points", 4, zones_key="bill_vs_mm")],
            ["MMNDR", "NDR12MCD", "bill_vs_mm"]),
    Section("Where deposits are going", "The pool IntraFi's network moves, and the competition for it.",
            [ChartSpec(take_deposits, "Each line starts at 100 one year ago, so you can compare growth. Above 100 = grew.",
                       [("Small banks", "DPSSCBW027SBOG"), ("Large banks", "DPSLCBW027SBOG"), ("Money funds", "WRMFNS")],
                       "index", 1, index=True, ref=(100.0, ""))],
            ["DPSSCBW027SBOG", "DPSLCBW027SBOG", "WRMFNS"]),
    Section("Stablecoins", "Digital dollars: the newest competitor for bank deposits.",
            [ChartSpec(take_stablecoins, "Total dollar stablecoins in circulation. Past 3 years.",
                       [("Stablecoins", "STABLECOINS")], "billions", 3)],
            ["STABLECOINS"]),
    Section("Bank funding stress", "Early warnings that banks are short of cash. Usually quiet; they matter when they move.",
            [ChartSpec(take_single("H8B3094NSMA"), "Money small banks have borrowed, mostly FHLB advances. Past 3 years.",
                       [("Borrowings", "H8B3094NSMA")], "billions", 3),
             ChartSpec(take_single("WLCFLPCL"), "Emergency loans from the Fed's discount window. Past 4 years.",
                       [("Discount window", "WLCFLPCL")], "billions", 4, zones_key="WLCFLPCL")],
            ["H8B3094NSMA", "WLCFLPCL"]),
    Section("Credit risk", "Whether borrowers are getting into trouble.",
            [ChartSpec(take_single("BAMLH0A0HYM2"), "Extra yield on junk bonds over Treasuries. Past 5 years.",
                       [("High-yield spread", "BAMLH0A0HYM2")], "pct", 5, zones_key="BAMLH0A0HYM2"),
             ChartSpec(take_single("DRCRELEXFACBS"), "Share of bank CRE loans 30+ days past due, by quarter. Past 12 years.",
                       [("CRE past due", "DRCRELEXFACBS")], "pct", 12, step=True, zones_key="DRCRELEXFACBS", daily=False)],
            ["BAMLH0A0HYM2", "DRCRELEXFACBS"]),
    Section("Stocks and volatility", "Market mood. Sharp drops send depositors looking for safety.",
            [ChartSpec(take_stocks, "Each index starts at 100 one year ago. Above 100 = up over the year.",
                       [("S&P 500", "SP500"), ("Dow", "DJIA"), ("Nasdaq", "NASDAQCOM")], "index", 1, index=True, ref=(100.0, "")),
             ChartSpec(take_single("VIXCLS"), "Expected stock market volatility. Past 2 years.",
                       [("VIX", "VIXCLS")], "level", 2, zones_key="VIXCLS")],
            ["SP500", "DJIA", "NASDAQCOM", "VIXCLS"]),
    Section("The economy", "What drives the Fed's next move.",
            [ChartSpec(take_economy, "Inflation (CPI, past 12 months) and the unemployment rate. Past 5 years.",
                       [("Inflation", "CPIAUCSL"), ("Unemployment", "UNRATE")], "pct", 5, daily=False)],
            ["CPIAUCSL", "UNRATE"]),
]


STABLECOIN_WHAT = ("Total value of dollar-pegged stablecoins (such as USDT and USDC) in circulation, from DeFiLlama. "
                   "They are crypto tokens backed mostly by Treasuries and cash, used like digital dollars.")
STABLECOIN_RISK = ("Every dollar in a stablecoin is a dollar not sitting in a bank deposit. The GENIUS Act made them mainstream; "
                   "if they start paying yield (directly or through loopholes), deposits could leak from community banks, "
                   "shrinking the pool IntraFi's network moves.")


def stablecoin_reading(points: list[tuple[date, float]], today: date, error: Optional[str] = None) -> Reading:
    from .analysis import reading_from_points
    return reading_from_points("STABLECOINS", "Stablecoins in circulation", "level", "$B", STABLECOIN_WHAT,
                               STABLECOIN_RISK, points, today, "daily", notable_month_move=5.0, error=error)


def _meta(key: str) -> tuple[str, str]:
    """(what it is, risk) for a series or spread."""
    if key == "STABLECOINS":
        return STABLECOIN_WHAT, STABLECOIN_RISK
    s = SERIES_BY_ID.get(key)
    if s:
        return s.what_it_is, s.risk
    sp = next((x for x in SPREADS if x.key == key), None)
    return (sp.what_it_is, sp.reading) if sp else ("", "")


# ----------------------------------------------------------- the insights
def bottom_line(key: str, r: Reading, b: Bundle) -> tuple[str, Optional[str]]:
    """(one-sentence takeaway, tone) shown first on each card."""
    item = ITEMS.get(key, Item(key))
    z = current_zone(item, r)
    if z:
        m = metric_value(item, r)
        if item.metric == "drawdown":
            return f"{m:.1f}% below its past-year high: {_zone_short(z).lower()}.", z.tone
        if item.metric == "yoy":
            return f"{m:+.1f}% over the past year: {_zone_short(z).lower()}.", z.tone
        short = _zone_short(z)
        main, _, why = short.partition(" (")
        return f"{r.display_value}: in the \u201c{main}\u201d range" + (f" ({why}" if why else "") + ".", z.tone
    if item.means:
        first = item.means(r, b).split(". ")[0].rstrip(".")
        if first:
            return first + ".", None
    return "", None


def means_text(key: str, r: Reading, b: Bundle) -> str:
    """The detail under the bottom line: context, then how it has moved."""
    item = ITEMS.get(key, Item(key))
    parts = []
    if item.means:
        extra = item.means(r, b)
        bl, _ = bottom_line(key, r, b)
        if extra and extra.split(". ")[0].rstrip(".") + "." != bl:
            parts.append(extra)
        elif extra and ". " in extra:
            parts.append(extra.split(". ", 1)[1])
    moves = []
    for w, label in (("last", "since the previous reading"), ("month", "over the past month"), ("year", "over the past year")):
        if r.changes.get(w) is not None:
            moves.append(f"{r.change_text(w)} {label}")
    if moves:
        parts.append(moves[0].capitalize() + "".join(f", {m}" for m in moves[1:]) + ".")
    if r.stale and r.as_of:
        parts.append(f"Latest data is from {r.as_of:%b %-d, %Y}.")
    return " ".join(p for p in parts if p)


# -------------------------------------------------------------------- render
def _chip(text: str, tone: str, current: bool) -> str:
    color = TONE_COLOR[tone]
    if current:
        return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 8px;border-radius:4px;'
                f'border:1.5px solid {color};color:{color};font-size:12.5px;font-weight:700">'
                f'&#9654; {_esc(text)} (now)</span>')
    return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 8px;border-radius:4px;'
            f'border:1px solid {LINE};color:{QUIET};font-size:12.5px">{_esc(text)}</span>')


def _status_badge(tone: str) -> str:
    color = TONE_COLOR[tone]
    icon = {"ok": "&#10003;", "watch": "&#9888;", "risk": "&#9888;"}[tone]
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{color};color:#fff;'
            f'font-size:11px;font-weight:700;letter-spacing:.03em;vertical-align:middle">{icon} {TONE_WORD[tone].upper()}</span>')


def ranges_inline(item: Item, current: Optional[Zone]) -> str:
    """One line of ranges, current one in bold color: 'Ranges: Below 15: calm · **20 to 30: elevated** · ...'"""
    parts = []
    for z in item.zones:
        if z is current:
            parts.append(f'<b style="color:{TONE_COLOR[z.tone]}">&#9654; {_esc(z.label)}</b>')
        else:
            parts.append(_esc(z.label))
    on = f" ({_esc(item.metric_label)})" if item.metric_label else ""
    return f"Ranges{on}: " + " &middot; ".join(parts)


def item_card_short(key: str, b: Bundle) -> str:
    r = b.readings.get(key)
    if r is None:
        return ""
    if not r.ok:
        return (f'<div style="padding:10px 0;border-top:1px solid {LINE};font-size:13px;color:{QUIET}">'
                f'<b style="color:{INK}">{_esc(r.name)}</b>: data didn\'t load today.</div>')
    item = ITEMS.get(key, Item(key))
    z = current_zone(item, r)
    bl, tone = bottom_line(key, r, b)
    bl_color = TONE_COLOR[tone] if tone in ("watch", "risk") else INK
    changes = " &middot; ".join(f"{lbl} {_esc(r.compact_change(w))}"
                                for lbl, w in (("Last:", "last"), ("1 mo:", "month"), ("1 yr:", "year"))
                                if r.changes.get(w) is not None)
    small = f"font-size:12.5px;line-height:1.5;color:{QUIET};margin:3px 0 0"
    return f"""
<div style="padding:11px 0 9px;border-top:1px solid {LINE}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:15px;font-weight:700;color:{INK}">{_esc(r.name)} {_status_badge(z.tone) if z else ''}</td>
    <td align="right" style="font-size:17px;font-weight:700;color:{INK};white-space:nowrap">{_esc(r.display_value)}</td>
  </tr></table>
  {f'<p style="margin:4px 0 0;font-size:14px;font-weight:600;line-height:1.45;color:{bl_color}">{_esc(bl)}</p>' if bl else ''}
  <p style="{small}">{changes}</p>
  {f'<p style="{small}">{ranges_inline(item, z)}</p>' if item.zones else ''}
</div>"""


def item_card(key: str, b: Bundle) -> str:
    r = b.readings.get(key)
    if r is None:
        return ""
    item = ITEMS.get(key, Item(key))
    what, risk = _meta(key)
    if not r.ok:
        return (f'<div style="padding:14px 0;border-top:1px solid {LINE}"><b style="color:{INK}">{_esc(r.name)}</b>'
                f'<p style="margin:4px 0 0;font-size:13px;color:{QUIET}">Data didn\'t load today; it retries next run.</p></div>')
    z = current_zone(item, r)
    badge = _status_badge(z.tone) if z else ""
    bl, tone = bottom_line(key, r, b)
    bl_color = TONE_COLOR.get(tone, INK) if tone in ("watch", "risk") else INK
    changes = " &middot; ".join(f"{lbl}: {_esc(r.compact_change(w))}"
                                for lbl, w in (("Last", "last"), ("1 mo", "month"), ("1 yr", "year"))
                                if r.changes.get(w) is not None)
    rng = ""
    if r.low_1y is not None and r.high_1y is not None:
        from .analysis import fmt_value
        rng = f" &middot; Past-year range: {_esc(fmt_value(r.low_1y, r.kind, r.unit))} to {_esc(fmt_value(r.high_1y, r.kind, r.unit))}"
    as_of = f"as of {r.as_of:%b %-d}" if r.as_of else ""
    ranges = ""
    if item.zones:
        on = f" (based on {item.metric_label})" if item.metric_label else ""
        ranges = (f'<p style="margin:10px 0 4px;font-size:12.5px;font-weight:700;color:{INK}">Ranges to know{_esc(on)}</p>'
                  + "".join(_chip(zz.label, zz.tone, zz is z) for zz in item.zones))
    p = 'style="margin:0 0 7px;font-size:14px;line-height:1.55;color:%s"' % INK
    return f"""
<div style="padding:16px 0 12px;border-top:1px solid {LINE}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:16px;font-weight:700;color:{INK}">{_esc(r.name)} {badge}</td>
    <td align="right" style="font-size:20px;font-weight:700;color:{INK};white-space:nowrap">{_esc(r.display_value)}</td>
  </tr></table>
  <p style="margin:2px 0 10px;font-size:12.5px;color:{QUIET}">{_esc(as_of)}{(' &middot; ' + changes) if changes else ''}{rng}</p>
  {f'<p style="margin:0 0 9px;padding:8px 10px;background:#f4f5f7;border-left:4px solid {bl_color};font-size:15px;font-weight:700;line-height:1.45;color:{bl_color}">Bottom line: {_esc(bl)}</p>' if bl else ''}
  <p {p}><b>What it is:</b> {_esc(what)}</p>
  <p {p}><b>What it means now:</b> {_esc(means_text(key, r, b))}</p>
  {f'<p {p}><b>Risk to watch:</b> {_esc(risk)}</p>' if risk else ''}
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
    bands = []
    if spec.zones_key and spec.zones_key in ITEMS and ITEMS[spec.zones_key].metric == "value":
        for z in ITEMS[spec.zones_key].zones:
            if z.tone in ("watch", "risk"):
                bands.append(Band(z.lo, z.hi, z.tone, f"{TONE_WORD[z.tone]}: {_zone_short(z)}"))
    fed = None
    if spec.fed_band:
        top, bot = b.readings.get("DFEDTARU"), b.readings.get("DFEDTARL")
        if top and bot and top.ok and bot.ok:
            fed = (bot.points, top.points, "Fed target range")
    try:
        return line_chart(lines, spec.kind, start, zero_line=spec.zero, ref_line=spec.ref, step=spec.step,
                          bands=bands, range_band=fed)
    except Exception:
        return None


def headline_bullets(b: Bundle) -> list[tuple[str, str]]:
    """Today at a glance: anything in a Risk or Watch range, plus essentials."""
    out: list[tuple[str, str]] = []
    top = _r(b, "DFEDTARU")
    if top:
        out.append(("info", means_fed(top, b)))
    parts = []
    for k, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        p = _pct_since(_r(b, k), "last")
        if p is not None:
            parts.append(f"{label} {'up' if p > 0 else 'down'} {abs(p):.1f}%" if abs(p) >= 0.05 else f"{label} flat")
    ten = _r(b, "DGS10")
    if ten and ten.changes.get("last") is not None:
        parts.append(f"10-year Treasury {ten.display_value} ({ten.change_text('last')})")
    if parts:
        day = next((b.readings[k].as_of for k in ("SP500", "DGS10") if _r(b, k)), None)
        out.append(("info", f"Markets{f' on {day:%A}' if day else ''}: " + ", ".join(parts) + "."))
    risks, watches = [], []
    for f in b.flags:
        if f.level == "watch":
            watches.append(f.text)
    flagged = {k for f in b.flags for k in f.keys}
    for key, item in ITEMS.items():
        r = b.readings.get(key)
        if not r or not r.ok or key in flagged:
            continue
        z = current_zone(item, r)
        if z and z.tone in ("watch", "risk"):
            bl, _ = bottom_line(key, r, b)
            (risks if z.tone == "risk" else watches).append(f"{r.name}: {bl}")
    for f in b.failures.failures:
        if (b.today - f.fail_date).days <= 7:
            risks.append(f"The FDIC closed {f.name} ({f.city_state}) on {f.fail_date:%b %-d}, with "
                         f"{fmt_thousands(f.deposits_thousands)} in deposits. Check whether it was in IntraFi's network.")
    out += [("risk", t) for t in risks] + [("watch", t) for t in watches]
    if not b.failures.error:
        ytd = [f for f in b.failures.failures if f.fail_date.year == b.today.year]
        out.append(("info", f"Bank failures so far in {b.today.year}: {len(ytd)}"
                            + (f" (most recent: {ytd[0].name}, {ytd[0].fail_date:%b %-d})." if ytd else ".")))
    if not risks and not watches:
        out.append(("ok", "Nothing is in a Watch or Risk range today."))
    return out


def render(b: Bundle, full: bool = False) -> tuple[str, str, dict[str, bytes], str]:
    """Returns (html, text, images by content-id, subject). `full` is the Monday
    edition: every chart plus what-it-is and risk notes for each item."""
    images: dict[str, bytes] = {}
    bullets = headline_bullets(b)
    color = {"risk": RISK_C, "watch": WATCH_C}
    inner = ('<h2 style="margin:22px 0 8px;font-size:19px;color:%s">Today at a glance</h2>' % INK
             + '<ul style="margin:0;padding-left:20px;font-size:14.5px;line-height:1.55">'
             + "".join(f'<li style="margin:0 0 8px;color:{color.get(t, INK)}">'
                       f'{"<b>" + TONE_WORD[t] + ":</b> " if t in color else ""}{_esc(x)}</li>' for t, x in bullets)
             + "</ul>")
    inner += (f'<p style="margin:10px 0 0;font-size:12.5px;line-height:1.5;color:{QUIET}">'
              "Green = Normal, amber = Watch, red = Risk (shaded the same way on charts). \"Points\" = percentage points "
              "(0.25 = 25 basis points). Ranges are rules of thumb, not official thresholds. "
              + ("" if full else "Full explanations of every item are in Monday's edition.") + "</p>")
    n = 0
    for sec in SECTIONS:
        inner += (f'<h2 style="margin:36px 0 4px;font-size:20px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  f'{_esc(sec.title)}</h2><p style="margin:0 0 6px;font-size:13px;color:{QUIET}">{_esc(sec.intro)}</p>')
        for spec in sec.charts:
            if not full and not spec.daily:
                continue
            png = build_chart(spec, b)
            if not png:
                continue
            n += 1
            cid = f"chart{n}"
            images[cid] = png
            take = spec.takeaway(b) or ""
            inner += (f'<p style="margin:14px 0 2px;font-size:16px;font-weight:700;line-height:1.4;color:{INK}">{_esc(take)}</p>'
                      f'<p style="margin:0 0 4px;font-size:12.5px;color:{QUIET}">{_esc(spec.subtitle)}</p>'
                      f'<img src="cid:{cid}" width="624" alt="{_esc(take)}" '
                      f'style="display:block;width:100%;max-width:624px;height:auto;border:0;margin:0 0 6px">')
        for key in sec.items:
            inner += item_card(key, b) if full else item_card_short(key, b)
    if b.calendar.events:
        inner += (f'<h2 style="margin:36px 0 8px;font-size:20px;color:{INK};border-bottom:2px solid {INK};padding-bottom:4px">'
                  "Coming up (next 7 days)</h2><ul style=\"margin:0;padding-left:20px;font-size:14.5px;line-height:1.6\">"
                  + "".join(f"<li><b>{e.when:%a %b %-d}</b> &middot; {_esc(e.label)}</li>" for e in b.calendar.events) + "</ul>")
    html = page("Rates & Markets" + (" · Weekly edition" if full else ""), f"{b.today:%A, %B %-d, %Y}", inner, b)

    lines = []
    if b.sample:
        lines.append("*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n")
    lines += [f"RATES & MARKETS - {b.today:%A, %B %-d, %Y}", "(Charts are in the HTML version.)", "", "TODAY AT A GLANCE"]
    lines += [f"  - {TONE_WORD[t].upper() + ': ' if t in color else ''}{x}" for t, x in bullets]
    for sec in SECTIONS:
        lines += ["", sec.title.upper()] + [f"  > {spec.takeaway(b)}" for spec in sec.charts
                                            if (full or spec.daily) and spec.takeaway(b)]
        for key in sec.items:
            r = b.readings.get(key)
            if not r or not r.ok:
                continue
            what, risk = _meta(key)
            item = ITEMS.get(key, Item(key))
            bl, _ = bottom_line(key, r, b)
            lines += [f"  {r.name}: {r.display_value}"]
            if bl:
                lines.append(f"    Bottom line: {bl}")
            if full:
                lines += [f"    What it is: {what}", f"    What it means now: {means_text(key, r, b)}"]
                if risk:
                    lines.append(f"    Risk to watch: {risk}")
            if item.zones:
                lines.append("    Ranges to know: " + "; ".join(zz.label for zz in item.zones))
    if b.calendar.events:
        lines += ["", "COMING UP"] + [f"  {e.when:%a %b %-d}: {e.label}" for e in b.calendar.events]
    lines += ["", "Ranges are rules of thumb, not official thresholds. Data: FRED, FDIC, DeFiLlama. Not investment advice."]
    text = "\n".join(lines)

    top_issue = next((x for t, x in bullets if t in ("risk", "watch")), None)
    tail = (top_issue.split(". ")[0].rstrip(".")[:80] if top_issue else "nothing in a Watch or Risk range")
    subject = f"Rates & Markets{' (weekly)' if full else ''}, {b.today:%a %b %-d}: {tail}"
    return html, text, images, subject
