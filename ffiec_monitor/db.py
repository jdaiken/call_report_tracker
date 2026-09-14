"""DuckDB storage layer -- schema per spec §3, scaled for the full institution universe.

The *.duckdb file itself is gitignored (see spec §9); in CI it is persisted across
daily runs via actions/cache (see .github/workflows/daily.yml), not committed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import duckdb

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS call_report_snapshots (
    id_rssd        VARCHAR,
    period_end     DATE,
    last_seen_ts   TIMESTAMP,
    ratios         JSON,
    checked_at     TIMESTAMP,
    PRIMARY KEY (id_rssd, period_end)
);

CREATE TABLE IF NOT EXISTS watch_list (
    id_rssd        VARCHAR PRIMARY KEY,
    reason         VARCHAR,
    added_at       DATE,
    removed_at     DATE
);

-- Reference table for enforcement orders. The system of record for this lives in a
-- separate, already-built project (per spec §3/§4) that is out of scope for this repo.
-- This table exists so the watch-list join has something to query against; populate it
-- via scripts/import_enforcement_orders.py from whatever the source system exports.
CREATE TABLE IF NOT EXISTS enforcement_orders (
    id_rssd          VARCHAR,
    fdic_cert        VARCHAR,
    order_type       VARCHAR,   -- 'SAFETY_SOUNDNESS' | 'AML_BSA' | 'CMP' | other
    status           VARCHAR,   -- 'ACTIVE' | 'TERMINATED'
    effective_date   DATE,
    terminated_date  DATE
);

-- Tracks the lastUpdateDateTime cursor per reporting period for
-- RetrieveFilersSubmissionDateTime (SIS611 §3.1's recommended usage: pass the
-- last time you polled a period so FFIEC returns only what changed since).
CREATE TABLE IF NOT EXISTS poll_state (
    period_end       DATE PRIMARY KEY,
    last_polled_at   TIMESTAMP
);

-- Third-party risk-score ranking (name/state/assets/percentile/tier) -- NOT the same
-- thing as enforcement_orders above. This has no RSSD ID and no S&S/AML/CMP order
-- data, so it does NOT feed the spec §4 Watch List qualification logic on its own.
-- It exists as a reference/seed list until each row is resolved to an id_rssd (the
-- FFIEC join key), at which point id_rssd can be backfilled here.
CREATE SEQUENCE IF NOT EXISTS risk_ranking_id_seq START 1;
CREATE TABLE IF NOT EXISTS risk_rankings (
    id             BIGINT PRIMARY KEY DEFAULT nextval('risk_ranking_id_seq'),
    rank           INTEGER,
    bank_name      VARCHAR,
    state          VARCHAR,
    peer_group     VARCHAR,
    total_assets   DOUBLE,      -- dollars, parsed from e.g. "$68.4M" / "$1.59B"
    pctl           DOUBLE,
    score          DOUBLE,
    tier           VARCHAR,     -- 'CRITICAL' | 'HIGH' | ... as supplied by the source
    id_rssd        VARCHAR,     -- NULL until resolved against FFIEC
    loaded_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS institution_meta (
    id_rssd        VARCHAR PRIMARY KEY,
    name           VARCHAR,
    city           VARCHAR,
    state          VARCHAR,
    fdic_cert      VARCHAR,
    total_assets   DOUBLE,
    as_of_period   DATE,
    updated_at     TIMESTAMP
);

-- Every alert decision, all tiers, so Tier 3 ("logged only, no email") is auditable.
CREATE SEQUENCE IF NOT EXISTS alert_log_id_seq START 1;
CREATE TABLE IF NOT EXISTS alert_log (
    id             BIGINT PRIMARY KEY DEFAULT nextval('alert_log_id_seq'),
    id_rssd        VARCHAR,
    period_end     DATE,
    tier           VARCHAR,   -- 'TIER1' | 'TIER2' | 'TIER3'
    trigger_type   VARCHAR,
    detail         JSON,
    logged_at      TIMESTAMP
);
"""


def connect(path: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(path)
    conn.execute(SCHEMA_SQL)
    return conn


# --- call_report_snapshots -------------------------------------------------

@dataclass
class Snapshot:
    id_rssd: str
    period_end: date
    last_seen_ts: datetime
    ratios: dict[str, Any]
    checked_at: datetime


def upsert_snapshot(conn: duckdb.DuckDBPyConnection, snap: Snapshot) -> None:
    conn.execute(
        """
        INSERT INTO call_report_snapshots (id_rssd, period_end, last_seen_ts, ratios, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (id_rssd, period_end) DO UPDATE SET
            last_seen_ts = excluded.last_seen_ts,
            ratios = excluded.ratios,
            checked_at = excluded.checked_at
        """,
        [snap.id_rssd, snap.period_end, snap.last_seen_ts, json.dumps(snap.ratios), snap.checked_at],
    )


def get_snapshot(conn: duckdb.DuckDBPyConnection, id_rssd: str, period_end: date) -> Optional[Snapshot]:
    row = conn.execute(
        "SELECT id_rssd, period_end, last_seen_ts, ratios, checked_at "
        "FROM call_report_snapshots WHERE id_rssd = ? AND period_end = ?",
        [id_rssd, period_end],
    ).fetchone()
    if row is None:
        return None
    return Snapshot(row[0], row[1], row[2], json.loads(row[3]), row[4])


def get_latest_snapshot(conn: duckdb.DuckDBPyConnection, id_rssd: str) -> Optional[Snapshot]:
    row = conn.execute(
        "SELECT id_rssd, period_end, last_seen_ts, ratios, checked_at "
        "FROM call_report_snapshots WHERE id_rssd = ? ORDER BY period_end DESC LIMIT 1",
        [id_rssd],
    ).fetchone()
    if row is None:
        return None
    return Snapshot(row[0], row[1], row[2], json.loads(row[3]), row[4])


def get_last_seen_ts(conn: duckdb.DuckDBPyConnection, id_rssd: str) -> Optional[datetime]:
    row = conn.execute(
        "SELECT MAX(last_seen_ts) FROM call_report_snapshots WHERE id_rssd = ?",
        [id_rssd],
    ).fetchone()
    return row[0] if row else None


# --- institution_meta -------------------------------------------------------

@dataclass
class InstitutionMeta:
    id_rssd: str
    name: str
    city: str
    state: str
    fdic_cert: str
    total_assets: Optional[float]
    as_of_period: Optional[date]


def upsert_institution_meta(conn: duckdb.DuckDBPyConnection, meta: InstitutionMeta) -> None:
    conn.execute(
        """
        INSERT INTO institution_meta (id_rssd, name, city, state, fdic_cert, total_assets, as_of_period, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (id_rssd) DO UPDATE SET
            name = excluded.name,
            city = excluded.city,
            state = excluded.state,
            fdic_cert = excluded.fdic_cert,
            total_assets = excluded.total_assets,
            as_of_period = excluded.as_of_period,
            updated_at = excluded.updated_at
        """,
        [meta.id_rssd, meta.name, meta.city, meta.state, meta.fdic_cert, meta.total_assets, meta.as_of_period],
    )


def get_institution_meta(conn: duckdb.DuckDBPyConnection, id_rssd: str) -> Optional[InstitutionMeta]:
    row = conn.execute(
        "SELECT id_rssd, name, city, state, fdic_cert, total_assets, as_of_period "
        "FROM institution_meta WHERE id_rssd = ?",
        [id_rssd],
    ).fetchone()
    if row is None:
        return None
    return InstitutionMeta(*row)


# --- watch_list --------------------------------------------------------------

@dataclass
class WatchListEntry:
    id_rssd: str
    reason: str
    added_at: date
    removed_at: Optional[date]


def get_active_watch_list(conn: duckdb.DuckDBPyConnection) -> list[WatchListEntry]:
    rows = conn.execute(
        "SELECT id_rssd, reason, added_at, removed_at FROM watch_list WHERE removed_at IS NULL"
    ).fetchall()
    return [WatchListEntry(*r) for r in rows]


def get_watch_list_entry(conn: duckdb.DuckDBPyConnection, id_rssd: str) -> Optional[WatchListEntry]:
    row = conn.execute(
        "SELECT id_rssd, reason, added_at, removed_at FROM watch_list "
        "WHERE id_rssd = ? AND removed_at IS NULL",
        [id_rssd],
    ).fetchone()
    return WatchListEntry(*row) if row else None


def add_to_watch_list(conn: duckdb.DuckDBPyConnection, id_rssd: str, reason: str, as_of: date) -> None:
    conn.execute(
        """
        INSERT INTO watch_list (id_rssd, reason, added_at, removed_at)
        VALUES (?, ?, ?, NULL)
        ON CONFLICT (id_rssd) DO UPDATE SET
            reason = excluded.reason,
            added_at = watch_list.added_at,
            removed_at = NULL
        """,
        [id_rssd, reason, as_of],
    )


def update_watch_list_reason(conn: duckdb.DuckDBPyConnection, id_rssd: str, reason: str) -> None:
    conn.execute("UPDATE watch_list SET reason = ? WHERE id_rssd = ? AND removed_at IS NULL", [reason, id_rssd])


def remove_from_watch_list(conn: duckdb.DuckDBPyConnection, id_rssd: str, as_of: date) -> None:
    conn.execute(
        "UPDATE watch_list SET removed_at = ? WHERE id_rssd = ? AND removed_at IS NULL",
        [as_of, id_rssd],
    )


# --- enforcement_orders (reference) ------------------------------------------

def get_active_order_types(conn: duckdb.DuckDBPyConnection, id_rssd: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT order_type FROM enforcement_orders WHERE id_rssd = ? AND status = 'ACTIVE'",
        [id_rssd],
    ).fetchall()
    return [r[0] for r in rows]


# --- risk_rankings (reference, RSSD-less until resolved) ---------------------

@dataclass
class RiskRankingRow:
    rank: Optional[int]
    bank_name: str
    state: str
    peer_group: Optional[str]
    total_assets: Optional[float]
    pctl: Optional[float]
    score: Optional[float]
    tier: str


def replace_risk_rankings(conn: duckdb.DuckDBPyConnection, rows: list[RiskRankingRow]) -> int:
    """Full replace, mirroring import_enforcement_orders.py's behavior -- this is a
    re-load of the source file, not an incremental merge."""
    conn.execute("DELETE FROM risk_rankings")
    conn.executemany(
        """
        INSERT INTO risk_rankings
            (rank, bank_name, state, peer_group, total_assets, pctl, score, tier, loaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
        """,
        [(r.rank, r.bank_name, r.state, r.peer_group, r.total_assets, r.pctl, r.score, r.tier) for r in rows],
    )
    return len(rows)


def unresolved_risk_rankings(conn: duckdb.DuckDBPyConnection) -> list[tuple[int, str, str]]:
    """(id, bank_name, state) for every risk_rankings row not yet matched to an id_rssd."""
    return conn.execute(
        "SELECT id, bank_name, state FROM risk_rankings WHERE id_rssd IS NULL ORDER BY id"
    ).fetchall()


def unresolved_risk_rankings_with_assets(
    conn: duckdb.DuckDBPyConnection,
) -> list[tuple[int, str, str, Optional[float]]]:
    """(id, bank_name, state, total_assets) -- lets a resolver break a name
    collision by comparing to each candidate's actual reported assets."""
    return conn.execute(
        "SELECT id, bank_name, state, total_assets FROM risk_rankings WHERE id_rssd IS NULL ORDER BY id"
    ).fetchall()


# --- poll_state ----------------------------------------------------------------

def get_last_polled_at(conn: duckdb.DuckDBPyConnection, period_end: date) -> Optional[datetime]:
    row = conn.execute("SELECT last_polled_at FROM poll_state WHERE period_end = ?", [period_end]).fetchone()
    return row[0] if row else None


def set_last_polled_at(conn: duckdb.DuckDBPyConnection, period_end: date, polled_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO poll_state (period_end, last_polled_at) VALUES (?, ?)
        ON CONFLICT (period_end) DO UPDATE SET last_polled_at = excluded.last_polled_at
        """,
        [period_end, polled_at],
    )


# --- alert_log -----------------------------------------------------------------

def log_alert(
    conn: duckdb.DuckDBPyConnection,
    id_rssd: str,
    period_end: date,
    tier: str,
    trigger_type: str,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO alert_log (id_rssd, period_end, tier, trigger_type, detail, logged_at)
        VALUES (?, ?, ?, ?, ?, now())
        """,
        [id_rssd, period_end, tier, trigger_type, json.dumps(detail)],
    )
