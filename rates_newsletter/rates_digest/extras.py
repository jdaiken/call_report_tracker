"""Data that doesn't come from FRED: bank failures (FDIC), stablecoin supply
(DeFiLlama) and the economic calendar (FRED release dates + the Fed's published FOMC schedule).

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
    notes: list[str] = field(default_factory=list)          # partial problems (one source down)


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


# The BankFind API can lag the FDIC's published failed bank list by weeks or
# months, so the published list is read too and the two are merged.
FDIC_LIST_CSV_URLS = (
    "https://www.fdic.gov/bank-failures/download-data.csv",
    "https://www.fdic.gov/bank/individual/failed/banklist.csv",
)


def _parse_list_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_failed_bank_list(text: str) -> list[Failure]:
    """The FDIC's failed bank list CSV: Bank Name, City, State, Cert, Acquiring Institution, Closing Date, Fund."""
    import csv
    import io
    out = []
    rows = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for rec in rows:
        rec = {(k or "").strip().lower().replace("\xa0", " "): (v or "").strip() for k, v in rec.items()}
        d = _parse_list_date(rec.get("closing date", ""))
        name = rec.get("bank name") or rec.get("bank")
        if not d or not name:
            continue
        city, state = rec.get("city", ""), rec.get("state", "") or rec.get("st", "")
        out.append(Failure(name, f"{city}, {state}".strip(", "), d, None, None))
    return out


def _key(name: str) -> str:
    import re
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    n = re.sub(r"\b(the|of|and|national|association|bank|trust|savings|loan|company|co|fs|la|b|t|n|a)\b", " ", n)
    return "".join(n.split())[:12]


def merge_failures(api: list[Failure], listed: list[Failure]) -> list[Failure]:
    """API records (which carry deposits) win; the published list adds anything the API hasn't caught up on."""
    out = list(api)
    for f in listed:
        if not any(abs((f.fail_date - g.fail_date).days) <= 3 and
                   (_key(f.name) == _key(g.name) or f.city_state.split(",")[0].lower() == g.city_state.split(",")[0].lower())
                   for g in api):
            out.append(f)
    out.sort(key=lambda f: f.fail_date, reverse=True)
    return out


def fetch_failures() -> FailureResult:
    api, errors = [], []
    try:
        r = _get(_session(), FDIC_FAILURES_URL, {
            "fields": "NAME,CITYST,FAILDATE,QBFDEP,QBFASSET",
            "sort_by": "FAILDATE", "sort_order": "DESC", "limit": 25, "format": "json",
        })
        api = parse_failures(r.json())
    except Exception as e:
        errors.append(f"BankFind API: {e}")
    listed = []
    for url in FDIC_LIST_CSV_URLS:
        try:
            r = _get(_session(), url, None)
            r.encoding = r.encoding or "latin-1"
            listed = parse_failed_bank_list(r.text)
            if listed:
                break
            errors.append(f"{url}: no rows ({r.headers.get('content-type')}, {r.text[:80]!r})")
        except Exception as e:
            errors.append(f"{url}: {e}")
    if not api and not listed:
        log.warning("FDIC failures unavailable: %s", errors)
        return FailureResult(error="; ".join(errors) or "no records")
    return FailureResult(merge_failures(api, listed), notes=errors)


def deposits_note(f: Failure) -> str:
    """' · $210 million' or '' when the published list hasn't given deposits."""
    return f" · {fmt_thousands(f.deposits_thousands)}" if f.deposits_thousands is not None else ""


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
    """DeFiLlama: [{"date": unix seconds, "totalCirculatingUSD": {"peggedUSD": n}}, ...]"""
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
