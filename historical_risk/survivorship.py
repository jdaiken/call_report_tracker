"""Survivorship-bias handling (spec §2): cross-reference every RSSD that ever
had an active charter in the 2012-present window against FDIC BankFind/SDI's
historical institution list, so failed and acquired banks aren't silently
dropped just because they don't exist today.

Populates `bank_universe` (historical_risk/db.py) from three public,
unauthenticated FDIC BankFind Suite API endpoints:
  - /api/institutions  -- all institutions FDIC has ever chartered (ACTIVE flag
    distinguishes currently-open from closed)
  - /api/failures      -- bank failures, with failure date
  - /api/history       -- structural changes (mergers/acquisitions), with effective date

**Field names below are UNCONFIRMED against FDIC's live schema** -- same caveat as
cleanup.py: this build sandbox's network policy blocks banks.data.fdic.gov
(confirmed via a live connection attempt, not assumed), so `_FIELDS_*` below are
written from the documented API shape but never validated against a real
response. Check https://banks.data.fdic.gov/docs/ against the field lists below
before trusting a real pull, and fix `_FIELDS_*` / `_parse_*` if any have
drifted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterator, Optional

import duckdb
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://banks.data.fdic.gov/api"
PAGE_LIMIT = 10_000  # FDIC API's documented max page size

# UNCONFIRMED (see module docstring) -- best-effort per FDIC's documented schema.
_FIELDS_INSTITUTIONS = "CERT,RSSD,NAME,ACTIVE,ENDEFYMD"
_FIELDS_FAILURES = "CERT,FAILDATE"
_FIELDS_HISTORY = "CERT,EFFDATE,CHANGECODE,RESTYPE,RESTYPE1"


def _get_paginated(session: requests.Session, endpoint: str, fields: str, filters: str = "") -> Iterator[dict]:
    offset = 0
    while True:
        params = {
            "fields": fields,
            "limit": PAGE_LIMIT,
            "offset": offset,
            "format": "json",
        }
        if filters:
            params["filters"] = filters
        resp = session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", [])
        if not rows:
            return
        for row in rows:
            yield row.get("data", row)
        if len(rows) < PAGE_LIMIT:
            return
        offset += PAGE_LIMIT


def _parse_fdic_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    logger.warning("Unrecognized FDIC date format: %r", s)
    return None


@dataclass(frozen=True)
class UniverseRow:
    id_rssd: str
    fdic_cert: Optional[str]
    name: Optional[str]
    status: str  # 'ACTIVE' | 'ACQUIRED' | 'FAILED'
    status_date: Optional[date]
    source: str


def fetch_universe(session: Optional[requests.Session] = None) -> list[UniverseRow]:
    """Builds the full survivorship-safe universe: every institution FDIC has on
    file, defaulted to ACTIVE/ACQUIRED per the institutions endpoint's ACTIVE
    flag, then overridden to FAILED for any CERT appearing in /api/failures
    (failure takes precedence over a generic "inactive" status, since spec §2's
    whole point is that failed banks need to be identifiable, not just dropped
    into an undifferentiated "gone" bucket alongside ordinary M&A)."""
    session = session or requests.Session()

    universe: dict[str, UniverseRow] = {}
    for row in _get_paginated(session, "institutions", _FIELDS_INSTITUTIONS):
        id_rssd = str(row.get("RSSD") or "").strip()
        if not id_rssd:
            continue
        active = str(row.get("ACTIVE")) in {"1", "true", "True"}
        universe[id_rssd] = UniverseRow(
            id_rssd=id_rssd,
            fdic_cert=str(row.get("CERT") or "") or None,
            name=row.get("NAME"),
            status="ACTIVE" if active else "ACQUIRED",
            status_date=None if active else _parse_fdic_date(row.get("ENDEFYMD")),
            source="FDIC_INSTITUTIONS",
        )

    cert_to_rssd = {u.fdic_cert: u.id_rssd for u in universe.values() if u.fdic_cert}

    for row in _get_paginated(session, "failures", _FIELDS_FAILURES):
        cert = str(row.get("CERT") or "").strip()
        id_rssd = cert_to_rssd.get(cert)
        if not id_rssd:
            logger.warning("Failure record for CERT %s has no matching RSSD in institutions pull", cert)
            continue
        existing = universe[id_rssd]
        universe[id_rssd] = UniverseRow(
            id_rssd=id_rssd,
            fdic_cert=existing.fdic_cert,
            name=existing.name,
            status="FAILED",
            status_date=_parse_fdic_date(row.get("FAILDATE")),
            source="FDIC_FAILURES",
        )

    return list(universe.values())


def load_universe(conn: duckdb.DuckDBPyConnection, rows: list[UniverseRow]) -> int:
    conn.executemany(
        """
        INSERT INTO bank_universe (id_rssd, fdic_cert, name, status, status_date, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (id_rssd) DO UPDATE SET
            fdic_cert = excluded.fdic_cert,
            name = excluded.name,
            status = excluded.status,
            status_date = excluded.status_date,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        [(r.id_rssd, r.fdic_cert, r.name, r.status, r.status_date, r.source) for r in rows],
    )
    return len(rows)
