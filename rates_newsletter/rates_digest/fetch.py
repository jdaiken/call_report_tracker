"""Pull observations from FRED.

FRED access, in order of preference:
  1. The official API (needs a free key in FRED_API_KEY). Most reliable from
     GitHub's servers, and it tells us each series' units.
  2. The public fredgraph.csv download (no key). Works most of the time, but
     FRED can rate-limit or block cloud IPs, so the key is recommended.

A series that fails to load is reported as unavailable in the email; it never
stops the whole digest.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = "rates-digest/1.0 (personal research newsletter; github actions)"
API_URL = "https://api.stlouisfed.org/fred/series/observations"
META_URL = "https://api.stlouisfed.org/fred/series"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"



@dataclass
class Observations:
    series_id: str
    points: list[tuple[date, float]]   # sorted oldest -> newest, missing values dropped
    units: Optional[str] = None        # FRED's units text, when known
    error: Optional[str] = None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(session: requests.Session, url: str, params: dict, tries: int = 3) -> requests.Response:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # noqa: PERF203
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last))


def parse_value(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if raw in ("", ".", "NA", "#N/A"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    """fredgraph.csv: first column is the date (header has been both DATE and
    observation_date over the years), second is the value ('.' when missing)."""
    rows = list(csv.reader(io.StringIO(text)))
    out: list[tuple[date, float]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            d = date.fromisoformat(row[0].strip())
        except ValueError:
            continue
        v = parse_value(row[1])
        if v is not None:
            out.append((d, v))
    out.sort()
    return out


def fetch_series(series_id: str, start: date, session: Optional[requests.Session] = None,
                 api_key: Optional[str] = None) -> Observations:
    session = session or _session()
    api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY", "").strip()
    try:
        if api_key:
            r = _get(session, API_URL, {
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "observation_start": start.isoformat(),
            })
            pts = []
            for o in r.json().get("observations", []):
                v = parse_value(o.get("value", ""))
                if v is not None:
                    pts.append((date.fromisoformat(o["date"]), v))
            pts.sort()
            units = None
            try:
                m = _get(session, META_URL, {"series_id": series_id, "api_key": api_key, "file_type": "json"})
                units = (m.json().get("seriess") or [{}])[0].get("units")
            except Exception:  # units are a nice-to-have
                pass
            return Observations(series_id, pts, units)
        r = _get(session, CSV_URL, {"id": series_id, "cosd": start.isoformat()})
        return Observations(series_id, parse_fred_csv(r.text))
    except Exception as e:
        log.warning("Could not load %s: %s", series_id, e)
        return Observations(series_id, [], error=str(e))


def fetch_all(series_ids: list[str], start: date, fetcher=None) -> dict[str, Observations]:
    """Fetch every series. If the first few all fail, FRED itself is down or
    blocking us: stop instead of retrying every series for most of an hour."""
    session = _session()
    fetcher = fetcher or (lambda sid: fetch_series(sid, start, session=session))
    out: dict[str, Observations] = {}
    for i, sid in enumerate(series_ids):
        if i == 3 and all(out[x].error for x in series_ids[:3]):
            reason = f"skipped: FRED unreachable ({out[series_ids[0]].error})"
            log.error("First 3 series all failed; skipping the remaining %d.", len(series_ids) - 3)
            for rest in series_ids[3:]:
                out[rest] = Observations(rest, [], error=reason)
            break
        out[sid] = fetcher(sid)
        time.sleep(0.25)  # stay well under FRED's 120 requests/minute
    return out


def today_eastern() -> date:
    # Good enough without a tz database: Eastern is UTC-4 or UTC-5; the digest
    # runs mid-morning, so either offset gives the same calendar date.
    return (datetime.now(timezone.utc) - timedelta(hours=5)).date()
