"""Data that doesn't come from FRED: bank failures (FDIC), stablecoin supply
(DeFiLlama), the economic calendar (FRED release dates + the Fed's published
FOMC schedule) and regional bank stocks (Finviz, via the unofficial
finvizfinance package).

Every function here returns a result object with an `error` field instead of
raising. A source being down only removes its section from the email.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .fetch import _get, _session

log = logging.getLogger(__name__)


# ------------------------------------------------------------ bank failures
FDIC_FAILURES_URL = "https://api.fdic.gov/banks/failures"


@dataclass
class Failure:
    name: str
    city_state: str
    fail_date: date
    deposits_thousands: Optional[float]
    assets_thousands: Optional[float]


@dataclass
class FailureResult:
    failures: list[Failure] = field(default_factory=list)   # newest first
    error: Optional[str] = None


def _parse_fdic_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_failures(payload: dict) -> list[Failure]:
    """BankFind wraps each record as {"data": {...}}; accept bare records too."""
    out = []
    for row in payload.get("data", []) or []:
        rec = row.get("data", row) if isinstance(row, dict) else {}
        d = _parse_fdic_date(str(rec.get("FAILDATE", "")))
        if not d:
            continue
        out.append(Failure(
            name=str(rec.get("NAME", "")).strip() or "Unknown bank",
            city_state=str(rec.get("CITYST", "")).strip(),
            fail_date=d,
            # BankFind reports dollar amounts in thousands.
            deposits_thousands=_num(rec.get("QBFDEP")),
            assets_thousands=_num(rec.get("QBFASSET")),
        ))
    out.sort(key=lambda f: f.fail_date, reverse=True)
    return out


def fetch_failures() -> FailureResult:
    try:
        r = _get(_session(), FDIC_FAILURES_URL, {
            "fields": "NAME,CITYST,FAILDATE,QBFDEP,QBFASSET",
            "sort_by": "FAILDATE", "sort_order": "DESC", "limit": 25, "format": "json",
        })
        return FailureResult(parse_failures(r.json()))
    except Exception as e:
        log.warning("FDIC failures unavailable: %s", e)
        return FailureResult(error=str(e))


def fmt_thousands(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    dollars = v * 1000
    if dollars >= 1e9:
        return f"${dollars / 1e9:,.1f} billion"
    return f"${dollars / 1e6:,.0f} million"


# --------------------------------------------------------------- stablecoins
STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoincharts/all"


@dataclass
class StablecoinResult:
    points: list[tuple[date, float]] = field(default_factory=list)   # (date, $ billions)
    error: Optional[str] = None


def parse_stablecoins(payload: list) -> list[tuple[date, float]]:
    out = []
    for row in payload or []:
        try:
            d = datetime.fromtimestamp(int(row["date"]), tz=timezone.utc).date()
            usd = float(row["totalCirculatingUSD"]["peggedUSD"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((d, usd / 1e9))
    out.sort()
    return out


def fetch_stablecoins() -> StablecoinResult:
    try:
        r = _get(_session(), STABLECOIN_URL, {})
        pts = parse_stablecoins(r.json())
        if not pts:
            return StablecoinResult(error="no data in response")
        return StablecoinResult(pts)
    except Exception as e:
        log.warning("Stablecoin data unavailable: %s", e)
        return StablecoinResult(error=str(e))


# ------------------------------------------------------------------ calendar
# From federalreserve.gov/monetarypolicy/fomccalendars.htm (decision day = 2nd day).
# The Fed marks 2027 dates as tentative. Add the next year's dates each fall;
# the email warns when this list is about to run out.
FOMC_DECISION_DAYS = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28), date(2027, 6, 9),
    date(2027, 7, 28), date(2027, 9, 15), date(2027, 10, 27), date(2027, 12, 8),
]
FOMC_SEP_DAYS = {date(2026, 3, 18), date(2026, 6, 17), date(2026, 9, 16), date(2026, 12, 9),
                 date(2027, 3, 17), date(2027, 6, 9), date(2027, 9, 15), date(2027, 12, 8)}

# FRED release names worth knowing about (matched case-insensitively, by prefix
# substring), with a plain-English label.
KEY_RELEASES = [
    ("consumer price index", "Inflation report (CPI)"),
    ("employment situation", "Jobs report"),
    ("gross domestic product", "GDP report"),
    ("personal income and outlays", "Fed's preferred inflation gauge (PCE)"),
    ("producer price index", "Wholesale inflation (PPI)"),
    ("advance monthly sales for retail", "Retail sales"),
    ("h.8 assets and liabilities of commercial banks", "Fed's weekly bank deposits report (H.8)"),
    ("senior loan officer opinion survey", "Fed's bank lending survey (SLOOS)"),
    ("fomc meeting minutes", "Minutes of the last Fed meeting"),
]


@dataclass
class Event:
    when: date
    label: str


@dataclass
class CalendarResult:
    events: list[Event] = field(default_factory=list)
    warning: Optional[str] = None
    error: Optional[str] = None


def fomc_events(start: date, end: date) -> list[Event]:
    out = []
    for d in FOMC_DECISION_DAYS:
        if start <= d <= end:
            extra = " with new economic projections" if d in FOMC_SEP_DAYS else ""
            out.append(Event(d, f"Fed interest rate decision, 2:00pm ET{extra}"))
    return out


def fomc_list_warning(today: date) -> Optional[str]:
    if today >= FOMC_DECISION_DAYS[-1] - timedelta(days=60):
        return ("The list of Fed meeting dates in rates_digest/extras.py runs out soon. "
                "Add next year's dates from federalreserve.gov/monetarypolicy/fomccalendars.htm.")
    return None


def parse_release_dates(payload: dict) -> list[Event]:
    out, seen = [], set()
    for row in payload.get("release_dates", []) or []:
        name = str(row.get("release_name", "")).lower()
        try:
            d = date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            continue
        for needle, label in KEY_RELEASES:
            if needle in name and (d, label) not in seen:
                seen.add((d, label))
                out.append(Event(d, label))
                break
    return out


def fetch_calendar(today: date, days: int = 8) -> CalendarResult:
    end = today + timedelta(days=days)
    res = CalendarResult(events=fomc_events(today, end), warning=fomc_list_warning(today))
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        res.error = "FRED_API_KEY not set (data release dates need it)"
    else:
        try:
            r = _get(_session(), "https://api.stlouisfed.org/fred/releases/dates", {
                "api_key": key, "file_type": "json",
                "realtime_start": today.isoformat(), "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true", "limit": 1000,
                "sort_order": "asc",
            })
            res.events += [e for e in parse_release_dates(r.json()) if today <= e.when <= end]
        except Exception as e:
            log.warning("Release calendar unavailable: %s", e)
            res.error = str(e)
    res.events.sort(key=lambda e: (e.when, e.label))
    return res


# ------------------------------------------------------------ regional banks
@dataclass
class BankQuote:
    ticker: str
    company: str
    market_cap: Optional[float]     # dollars
    change_pct: Optional[float]     # today's % change, e.g. -1.2
    perf_week_pct: Optional[float] = None
    perf_month_pct: Optional[float] = None


@dataclass
class RegionalBanks:
    quotes: list[BankQuote] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return len(self.quotes) >= 20

    def _w_avg(self, attr: str) -> Optional[float]:
        rows = [(q.market_cap, getattr(q, attr)) for q in self.quotes
                if q.market_cap and getattr(q, attr) is not None]
        total = sum(c for c, _ in rows)
        return sum(c * v for c, v in rows) / total if total else None

    @property
    def cap_weighted_change(self) -> Optional[float]:
        return self._w_avg("change_pct")

    @property
    def cap_weighted_week(self) -> Optional[float]:
        return self._w_avg("perf_week_pct")

    @property
    def cap_weighted_month(self) -> Optional[float]:
        return self._w_avg("perf_month_pct")

    @property
    def share_down(self) -> Optional[float]:
        vals = [q.change_pct for q in self.quotes if q.change_pct is not None]
        return 100 * sum(1 for v in vals if v < 0) / len(vals) if vals else None

    def worst(self, n: int = 5, attr: str = "change_pct") -> list[BankQuote]:
        rows = [q for q in self.quotes if getattr(q, attr) is not None]
        return sorted(rows, key=lambda q: getattr(q, attr))[:n]


def _col(row, *names):
    for n in names:
        if n in row and row[n] is not None and row[n] == row[n]:   # skip NaN
            return row[n]
    return None


def _pct(v) -> Optional[float]:
    """finvizfinance turns '-1.23%' into -0.0123; show it as -1.23."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%")
        try:
            return float(v)
        except ValueError:
            return None
    try:
        return float(v) * 100
    except (TypeError, ValueError):
        return None


def fetch_regional_banks(with_performance: bool = False) -> RegionalBanks:
    """Regional banks over $300M market cap from Finviz's screener. Uses the
    unofficial finvizfinance package, which reads Finviz's public pages; it can
    break when Finviz changes its site or blocks automated requests."""
    try:
        from finvizfinance.screener.overview import Overview
        filters = {"Industry": "Banks - Regional", "Market Cap.": "+Small (over $300mln)"}
        ov = Overview()
        ov.set_filter(filters_dict=filters)
        df = ov.screener_view(verbose=0, sleep_sec=1)
        if df is None or df.empty:
            return RegionalBanks(error="Finviz returned no rows")
        quotes = {}
        for _, row in df.iterrows():
            t = _col(row, "Ticker")
            if not t:
                continue
            quotes[t] = BankQuote(t, str(_col(row, "Company") or t),
                                  _num(_col(row, "Market Cap")), _pct(_col(row, "Change")))
        if with_performance:
            try:
                from finvizfinance.screener.performance import Performance
                pf = Performance()
                pf.set_filter(filters_dict=filters)
                pdf = pf.screener_view(verbose=0, sleep_sec=1)
                if pdf is not None:
                    for _, row in pdf.iterrows():
                        q = quotes.get(_col(row, "Ticker"))
                        if q:
                            q.perf_week_pct = _pct(_col(row, "Perf Week"))
                            q.perf_month_pct = _pct(_col(row, "Perf Month"))
            except Exception as e:   # weekly extras are optional
                log.warning("Finviz performance view unavailable: %s", e)
        return RegionalBanks(list(quotes.values()))
    except Exception as e:
        log.warning("Regional bank data unavailable: %s", e)
        return RegionalBanks(error=str(e))
