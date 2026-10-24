"""Turn raw observations into plain-English trend readings.

Nothing here touches the network, so it is fully unit-testable.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .fetch import Observations
from .series import SERIES, SPREADS, Series, Spread

STALE_AFTER_DAYS = {"daily": 7, "weekly": 21, "monthly": 75, "quarterly": 240}  # FRED dates a quarter by its first day; data lands months later
LOOKBACKS = [("week", 7), ("month", 30), ("year", 365)]


# ----------------------------------------------------------------- helpers
def value_on_or_before(points: list[tuple[date, float]], d: date) -> Optional[tuple[date, float]]:
    idx = bisect_right([p[0] for p in points], d)
    return points[idx - 1] if idx else None


def to_billions(points: list[tuple[date, float]], units: Optional[str], unit: str,
                known_millions: bool = False) -> list[tuple[date, float]]:
    """H.8 deposits are reported in billions; some Fed balance-sheet series are in
    millions. Use FRED's own units text when we have it, otherwise fall back to
    magnitude (no U.S. aggregate here is anywhere near $100 trillion)."""
    if unit != "$B" or not points:
        return points
    millions = known_millions
    if units:
        millions = "million" in units.lower()
    elif not known_millions and points[-1][1] > 100_000:
        millions = True
    return [(d, v / 1000.0) for d, v in points] if millions else points


def yoy_series(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Monthly price index -> year-over-year percent change."""
    out = []
    by_month = {(d.year, d.month): v for d, v in points}
    for d, v in points:
        prior = by_month.get((d.year - 1, d.month))
        if prior:
            out.append((d, (v / prior - 1) * 100))
    return out


def fmt_value(v: float, s_kind: str, unit: str) -> str:
    if s_kind == "spread":
        return f"{v:+.2f} points"
    if unit == "%" or s_kind == "rate":
        return f"{v:.2f}%"
    if unit == "$B":
        return f"${v / 1000:,.2f} trillion" if abs(v) >= 1000 else f"${v:,.0f} billion"
    if unit == "$":
        return f"${v:,.2f}"
    if s_kind == "points":
        return f"{v:.1f}"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def fmt_change(delta: float, kind: str, base: Optional[float] = None) -> str:
    """'up 0.25 points' / 'down 1.2%' / 'unchanged'."""
    if kind in ("rate", "yoy", "spread"):
        if abs(delta) < 0.005:
            return "unchanged"
        n = abs(delta)
        word = "point" if abs(n - 1) < 0.005 else "points"
        return f"{'up' if delta > 0 else 'down'} {n:.2f} {word}"
    if kind == "points":
        if abs(delta) < 0.05:
            return "unchanged"
        return f"{'up' if delta > 0 else 'down'} {abs(delta):.1f} points"
    if kind == "level_abs":   # dollar amounts that can sit near zero: say "$40 billion", not "-95%"
        if abs(delta) < 0.5:
            return "unchanged"
        return f"{'up' if delta > 0 else 'down'} ${abs(delta):,.0f} billion"
    # level: percent change
    if not base:
        return "n/a"
    pct = (delta / base) * 100
    if abs(pct) < 0.05:
        return "unchanged"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%"


def change_size(delta: float, kind: str, base: Optional[float]) -> float:
    """Comparable magnitude for 'is this notable': points for rates, % for levels."""
    if kind in ("rate", "yoy", "spread", "points", "level_abs"):
        return delta
    return (delta / base) * 100 if base else 0.0


def range_phrase(value: float, lo: float, hi: float, kind: str, unit: str) -> str:
    span = hi - lo
    if span <= 1e-9 or (kind in ("rate", "spread", "yoy") and span < 0.05):
        return "It has been essentially flat all year."
    pos = (value - lo) / span
    where = ("at its lowest level of the past year" if pos <= 0.02 else
             "at its highest level of the past year" if pos >= 0.98 else
             "near the low end of its past-year range" if pos < 0.2 else
             "near the high end of its past-year range" if pos > 0.8 else
             "in the middle of its past-year range")
    return f"It is {where} ({fmt_value(lo, kind, unit)} to {fmt_value(hi, kind, unit)})."


# ---------------------------------------------------------------- readings
@dataclass
class Reading:
    key: str
    name: str
    section: str
    kind: str                 # rate | level | yoy | points | spread
    unit: str
    what_it_is: str
    why_it_matters: str
    as_of: Optional[date] = None
    value: Optional[float] = None
    prev_date: Optional[date] = None
    changes: dict[str, Optional[float]] = field(default_factory=dict)   # 'last','week','month','year'
    bases: dict[str, Optional[float]] = field(default_factory=dict)
    compare_dates: dict[str, date] = field(default_factory=dict)
    low_1y: Optional[float] = None
    high_1y: Optional[float] = None
    stale: bool = False
    error: Optional[str] = None
    notable_month_move: float = 0.25
    history: list[tuple[date, float]] = field(default_factory=list)      # last ~1y, for sparklines
    points: list[tuple[date, float]] = field(default_factory=list)       # everything fetched, for charts

    @property
    def ok(self) -> bool:
        return self.value is not None

    @property
    def display_value(self) -> str:
        if not self.ok:
            return "unavailable"
        return fmt_value(self.value, self.kind, self.unit)

    def change_text(self, window: str) -> str:
        d = self.changes.get(window)
        if d is None:
            return "n/a"
        return fmt_change(d, self.kind, self.bases.get(window))

    def compact_change(self, window: str) -> str:
        """Short form for tables: '-0.23 pts', '+0.3%', '+$4B', 'no chg'."""
        d = self.changes.get(window)
        if d is None:
            return "—"
        text = self.change_text(window)
        if text == "unchanged":
            return "no chg"
        sign = "+" if d > 0 else "−"
        if self.kind in ("rate", "yoy", "spread"):
            return f"{sign}{abs(d):.2f} pts"
        if self.kind == "points":
            return f"{sign}{abs(d):.1f}"
        if self.kind == "level_abs":
            return f"{sign}${abs(d):,.0f}B"
        base = self.bases.get(window)
        return f"{sign}{abs(d / base * 100):.1f}%" if base else "—"

    def change_size(self, window: str) -> float:
        d = self.changes.get(window)
        if d is None:
            return 0.0
        return change_size(d, self.kind, self.bases.get(window))

    def summary(self) -> str:
        """One or two plain-English sentences on how the reading compares with its recent past."""
        if not self.ok:
            return "No data came through today; it will retry on the next run."
        parts = []
        last = self.change_text("last")
        if last != "n/a" and self.prev_date:
            parts.append(f"{last.capitalize()} since the previous reading ({self.prev_date:%b %-d})")
        trend_bits = []
        for w in ("month", "year"):
            # For monthly data the "previous reading" already is last month; don't say it twice.
            if parts and self.compare_dates.get(w) == self.compare_dates.get("last"):
                continue
            t = self.change_text(w)
            if t != "n/a":
                trend_bits.append(f"{t} over the past {w}")
        sentence = ""
        if parts:
            sentence = parts[0] + (", " + " and ".join(trend_bits) if trend_bits else "") + "."
        elif trend_bits:
            sentence = " and ".join(trend_bits).capitalize() + "."
        if self.low_1y is not None and self.high_1y is not None:
            sentence += " " + range_phrase(self.value, self.low_1y, self.high_1y, self.kind, self.unit)
        if self.stale and self.as_of:
            sentence += f" Latest available data is from {self.as_of:%b %-d, %Y}."
        return sentence.strip()


def _fill(reading: Reading, points: list[tuple[date, float]], today: date, frequency: str) -> Reading:
    if not points:
        return reading
    as_of, value = points[-1]
    reading.as_of, reading.value = as_of, value
    if len(points) >= 2:
        reading.prev_date, prev = points[-2]
        reading.changes["last"], reading.bases["last"] = value - prev, prev
        reading.compare_dates["last"] = reading.prev_date
    for name, days in LOOKBACKS:
        then = value_on_or_before(points, as_of - timedelta(days=days))
        # Allow a little slack for weekends/holidays/publication gaps, but don't let
        # a monthly series pass off last month's value as "a week ago".
        tolerance = 4 if days < 30 else 45
        if then is not None and then[0] != as_of and (as_of - then[0]).days <= days + tolerance:
            reading.changes[name], reading.bases[name] = value - then[1], then[1]
            reading.compare_dates[name] = then[0]
    window = [v for d, v in points if d >= as_of - timedelta(days=365)]
    if window:
        reading.low_1y, reading.high_1y = min(window), max(window)
    reading.history = [(d, v) for d, v in points if d >= as_of - timedelta(days=365)]
    reading.points = list(points)
    reading.stale = (today - as_of).days > STALE_AFTER_DAYS.get(frequency, 7)
    return reading


def build_reading(s: Series, obs: Optional[Observations], today: date) -> Reading:
    r = Reading(s.id, s.name, s.section, s.kind, s.unit, s.what_it_is, s.why_it_matters,
                notable_month_move=s.notable_month_move)
    if obs is None or obs.error or not obs.points:
        r.error = (obs.error if obs else None) or "no data"
        return r
    pts = to_billions(obs.points, obs.units, s.unit, known_millions=s.millions)
    if s.kind == "yoy":
        pts = yoy_series(pts)
    return _fill(r, pts, today, s.frequency)


def build_spread(sp: Spread, data: dict[str, Observations], today: date) -> Reading:
    r = Reading(sp.key, sp.name, "spreads", "spread", "%", sp.what_it_is, sp.reading,
                notable_month_move=sp.notable_month_move)
    a, b = data.get(sp.a), data.get(sp.b)
    if not a or not b or not a.points or not b.points:
        r.error = "missing input"
        return r
    pts = []
    for d, v in a.points:
        bv = value_on_or_before(b.points, d)
        if bv is not None and (d - bv[0]).days <= sp.max_b_age_days:
            pts.append((d, v - bv[1]))
    if not pts:
        r.error = "no overlapping dates"
        return r
    return _fill(r, pts, today, "daily")


# ------------------------------------------------------------ watch flags
@dataclass
class Flag:
    level: str    # 'watch' | 'info'
    text: str
    keys: tuple[str, ...] = ()   # readings this flag already covers


def _get(readings: dict[str, Reading], key: str) -> Optional[Reading]:
    r = readings.get(key)
    return r if r and r.ok else None


def watch_flags(readings: dict[str, Reading]) -> list[Flag]:
    flags: list[Flag] = []
    g = lambda k: _get(readings, k)  # noqa: E731

    top, bottom = g("DFEDTARU"), g("DFEDTARL")
    if top and top.changes.get("week") and abs(top.changes["week"]) >= 0.01:
        verb = "raised" if top.changes["week"] > 0 else "cut"
        flags.append(Flag("watch",
            f"The Fed {verb} rates this week by {abs(top.changes['week']):.2f} points. "
            f"Expect banks to reprice deposits, including ICS and CDARS rates, over the coming days.",
            # Everything that mechanically follows a Fed move, so it isn't repeated as a separate "move".
            ("DFEDTARU", "DFEDTARL", "IORB", "DPRIME", "EFFR", "SOFR", "bill_vs_effr", "bill_vs_iorb", "sofr_vs_iorb")))

    effr = g("EFFR")
    if effr and top and bottom and not (bottom.value - 0.001 <= effr.value <= top.value + 0.001):
        flags.append(Flag("watch", f"The effective fed funds rate ({effr.display_value}) is outside the Fed's target range. "
                                   "That is rare and signals stress in overnight funding.", ("EFFR",)))

    sofr_iorb = g("sofr_vs_iorb")
    if sofr_iorb and sofr_iorb.value > 0.02:
        flags.append(Flag("watch", f"SOFR is {sofr_iorb.value:.2f} points above what the Fed pays on reserves. "
                                   "Cash is getting tighter in funding markets, which tends to raise what banks must pay for deposits.",
                                   ("sofr_vs_iorb", "SOFR")))

    bill_iorb = g("bill_vs_iorb")
    if bill_iorb and bill_iorb.value >= 0.10:
        flags.append(Flag("info", f"3-month T-bills pay {bill_iorb.value:.2f} points more than banks earn on reserves. "
                                  "Large depositors have a strong reason to compare bank rates with Treasuries.", ("bill_vs_iorb",)))

    bill_mm = g("bill_vs_mm")
    # National average deposit rates sit far below T-bills almost all the time (they
    # include big banks paying near zero), so the level alone isn't news. A widening gap is.
    if bill_mm and (bill_mm.changes.get("month") or 0) >= 0.25:
        flags.append(Flag("watch", f"The gap between 3-month T-bills and the average bank money market rate widened "
                                   f"{bill_mm.changes['month']:.2f} points over the past month, to {bill_mm.value:.2f}. "
                                   "Bank deposits are getting less competitive with Treasuries.", ("bill_vs_mm",)))

    curve = g("curve_10y_2y")
    if curve and curve.value < 0:
        flags.append(Flag("watch", f"The yield curve is inverted (10-year minus 2-year is {curve.value:.2f}). "
                                   "This squeezes bank margins and has historically come before recessions.", ("curve_10y_2y",)))
    elif curve and curve.changes.get("month") is not None and curve.changes["month"] >= 0.25:
        flags.append(Flag("info", f"The yield curve steepened {curve.changes['month']:.2f} points over the past month, "
                                  "which generally helps bank lending margins.", ("curve_10y_2y",)))

    hy = g("BAMLH0A0HYM2")
    if hy and ((hy.changes.get("month") or 0) >= 0.5 or hy.value >= 5.0):
        flags.append(Flag("watch", f"High-yield credit spreads are {hy.display_value}, "
                                   f"{hy.change_text('month')} over the past month. Investors are pricing in more default risk.", ("BAMLH0A0HYM2",)))

    vix = g("VIXCLS")
    if vix and vix.value >= 25:
        flags.append(Flag("watch", f"The VIX is {vix.value:.1f}, a stressed level. Stress often sends depositors looking for fully insured options.", ("VIXCLS",)))

    small, large = g("DPSSCBW027SBOG"), g("DPSLCBW027SBOG")
    if small and large and small.changes.get("month") is not None and large.changes.get("month") is not None:
        if small.changes["month"] < 0 < large.changes["month"]:
            flags.append(Flag("watch", "Over the past month, deposits fell at small banks while rising at large banks. "
                                       "Money may be moving toward the biggest banks, which is the risk IntraFi's products address.",
                                       ("DPSSCBW027SBOG", "DPSLCBW027SBOG")))
    if small and small.changes.get("year") is not None and small.changes["year"] < 0:
        flags.append(Flag("watch", f"Small-bank deposits are {small.change_text('year')} from a year ago.", ("DPSSCBW027SBOG",)))

    mmf = g("WRMFNS")
    if mmf and mmf.change_size("year") >= 10:
        flags.append(Flag("info", f"Retail money market fund assets are {mmf.change_text('year')} over the past year. "
                                  "Savers keep choosing funds over bank deposits.", ("WRMFNS",)))

    loans, deps = g("TOTLL"), g("DPSACBW027SBOG")
    if loans and deps and loans.changes.get("year") is not None and deps.changes.get("year") is not None:
        lg, dg = loans.change_size("year"), deps.change_size("year")
        if lg - dg >= 1.5:
            flags.append(Flag("info", f"Bank loans grew {lg:.1f}% over the past year but deposits only {dg:.1f}%. "
                                      "Banks need more funding, which can lift demand for one-way placements.", ("TOTLL", "DPSACBW027SBOG")))

    # ---- bank stress signals
    dw = g("WLCFLPCL")
    if dw and not dw.stale:
        jump = dw.changes.get("week") or 0
        base = dw.bases.get("week") or 0
        if jump >= 3 and (base <= 0 or jump / base >= 0.5):
            flags.append(Flag("watch", f"Banks' emergency borrowing from the Fed's discount window jumped ${jump:,.0f} billion "
                                       f"in a week, to {dw.display_value}. Some banks needed cash quickly.", ("WLCFLPCL",)))
        elif dw.value >= 20:
            flags.append(Flag("watch", f"Discount window borrowing is {dw.display_value}, well above normal levels.", ("WLCFLPCL",)))

    bor = g("H8B3094NSMA")
    if bor and not bor.stale and bor.change_size("month") >= 5:
        flags.append(Flag("watch", f"Small banks' borrowings are {bor.change_text('month')} over the past month, to {bor.display_value}. "
                                   "They are leaning harder on FHLB and other borrowed funding.", ("H8B3094NSMA",)))

    tga = g("WTREGEN")
    if tga and not tga.stale and (tga.changes.get("month") or 0) >= 150:
        flags.append(Flag("info", f"The Treasury's cash account rose ${tga.changes['month']:,.0f} billion over the past month. "
                                  "That money comes out of bank reserves and deposits.", ("WTREGEN",)))

    cre = g("DRCRELEXFACBS")
    if cre and (cre.changes.get("last") or 0) >= 0.15:
        flags.append(Flag("watch", f"Commercial real estate delinquencies rose to {cre.display_value} last quarter "
                                   f"({cre.change_text('last')}). CRE is the main credit risk for community banks.", ("DRCRELEXFACBS",)))
    return flags


def notable_moves(readings: dict[str, Reading], limit: int = 5,
                  covered: frozenset[str] = frozenset()) -> list[Reading]:
    """Readings whose one-month move is large relative to that series' normal size,
    skipping anything a watch flag already talks about."""
    scored = []
    for r in readings.values():
        # Fed target moves get their own flag; listing them again as "moves" is noise.
        if not r.ok or r.stale or r.key in ("DFEDTARU", "DFEDTARL") or r.key in covered:
            continue
        size = abs(r.change_size("month"))
        if r.notable_month_move and size >= r.notable_month_move:
            scored.append((size / r.notable_month_move, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def notable_today(readings: dict[str, Reading], today: date, limit: int = 3,
                  covered: frozenset[str] = frozenset()) -> list[Reading]:
    """What moved since the previous reading, for the short daily email. Only
    fresh data counts, so a big move doesn't repeat every day for a month."""
    scored = []
    for r in readings.values():
        if (not r.ok or r.stale or r.as_of is None or (today - r.as_of).days > 4
                or r.key in ("DFEDTARU", "DFEDTARL") or r.key in covered):
            continue
        size = abs(r.change_size("last"))
        threshold = r.notable_month_move * 0.5
        if threshold and size >= threshold:
            scored.append((size / threshold, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def notable_week(readings: dict[str, Reading], limit: int = 6,
                 covered: frozenset[str] = frozenset()) -> list[Reading]:
    """Biggest moves over the past week, for the weekly look book."""
    scored = []
    for r in readings.values():
        if not r.ok or r.stale or r.key in ("DFEDTARL",) or r.key in covered:
            continue
        size = abs(r.change_size("week"))
        threshold = r.notable_month_move * 0.6
        if threshold and size >= threshold:
            scored.append((size / threshold, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def build_all(data: dict[str, Observations], today: date) -> dict[str, Reading]:
    readings: dict[str, Reading] = {}
    for s in SERIES:
        readings[s.id] = build_reading(s, data.get(s.id), today)
    for sp in SPREADS:
        readings[sp.key] = build_spread(sp, data, today)
    return readings
