"""The measures: each one's status ranges (rules of thumb, not official
thresholds), context for its signal line, and where to check the data.
The 5-page layout that uses them is in monitor.py.

The what/why notes are kept for a future reference guide; the daily report
itself doesn't print them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from .analysis import Reading, reading_from_points
from .email_report import Bundle
from .extras import FOMC_DECISION_DAYS

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


FRED_URL = "https://fred.stlouisfed.org/series/{}"
FDIC_FAILURES_URL = "https://www.fdic.gov/bank-failures/failed-bank-list"
DEFILLAMA_URL = "https://defillama.com/stablecoins"

# The underlying series behind each measure (spreads link to both sides).
_SOURCE_SERIES = {
    "DFEDTARU": ["DFEDTARU", "DFEDTARL", "EFFR"],
    "DGS2": ["DGS2", "EFFR"],
    "bill_vs_mm": ["DGS3MO", "MMNDR"],
    "DPSSCBW027SBOG": ["DPSSCBW027SBOG", "DPSLCBW027SBOG"],
    "sofr_vs_iorb": ["SOFR", "IORB"],
    "curve_10y_2y": ["DGS10", "DGS2"],
}


def sources_for(key: str) -> list[tuple[str, str]]:
    """(label, url) pairs where the reader can check the number."""
    from .series import SERIES_BY_ID
    if key == "STABLECOINS":
        return [("DeFiLlama: Stablecoin supply (DeFiLlama)", DEFILLAMA_URL)]
    out = []
    for sid in _SOURCE_SERIES.get(key, [key]):
        s = SERIES_BY_ID.get(sid)
        out.append((f"FRED: {s.name if s else sid} ({sid})", FRED_URL.format(sid)))
    return out


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
