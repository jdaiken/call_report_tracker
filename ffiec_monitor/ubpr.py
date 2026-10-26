"""Links to a bank's Uniform Bank Performance Report (UBPR) on FFIEC CDR.

The public UBPR page needs FFIEC's internal report-cycle id, which isn't derived from
the date (ids skip numbers). Known ids were read off the live site (scripts/probe_ubpr.py,
2026-10-26); a period not in the table is looked up once per run by probing the next few
ids, and falls back to the CDR search page if that fails.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

import requests

log = logging.getLogger(__name__)

URL = "https://cdr.ffiec.gov/Public/Reports/UbprReport.aspx?rptCycleIds={cycle}&rptid=283&idrssd={rssd}"
SEARCH = "https://cdr.ffiec.gov/public/ManageFacsimiles.aspx"
PROBE_RSSD = "913856"          # any long-lived filer works for reading a cycle's period

KNOWN: dict[date, int] = {
    date(2024, 3, 31): 141, date(2024, 6, 30): 142, date(2024, 9, 30): 144, date(2024, 12, 31): 145,
    date(2025, 3, 31): 146, date(2025, 6, 30): 147, date(2025, 9, 30): 149, date(2025, 12, 31): 150,
    date(2026, 3, 31): 151, date(2026, 6, 30): 152, date(2026, 9, 30): 154, date(2026, 12, 31): 155,
}
_discovered: dict[date, Optional[int]] = {}


def _discover(period: date) -> Optional[int]:
    if period in _discovered:
        return _discovered[period]
    want = f"{period:%B} {period.day}, {period.year}"
    found = None
    try:
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0"
        for cyc in range(max(KNOWN.values()) + 1, max(KNOWN.values()) + 10):
            t = s.get(URL.format(cycle=cyc, rssd=PROBE_RSSD), timeout=20).text
            m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) Uniform Bank Performance Report", t)
            if m and m.group(1) == want:
                found = cyc
                break
    except Exception as e:                                   # offline / blocked: fall back to the search page
        log.info("UBPR cycle lookup failed: %s", e)
    _discovered[period] = found
    return found


def ubpr_url(rssd: str, period: Optional[date], discover: bool = True) -> str:
    if not rssd or period is None:
        return SEARCH
    cyc = KNOWN.get(period) or (_discover(period) if discover else None)
    return URL.format(cycle=cyc, rssd=str(rssd).strip()) if cyc else SEARCH
