"""DuckDB schema additions for the historical risk analysis (spec §2, §6, §10).

Lives in the same DuckDB file as ffiec_monitor (see package docstring) -- call
ensure_schema() with a connection already opened via ffiec_monitor.db.connect()
or duckdb.connect() directly; it's additive (CREATE TABLE IF NOT EXISTS) and
safe to call repeatedly.
"""
from __future__ import annotations

import duckdb

SCHEMA_SQL = """
-- Survivorship-safe bank universe (spec §2): every RSSD that ever existed in the
-- 2012-present window, not just banks that exist today. status/status_date let the
-- field-stats and survival work distinguish failed/acquired banks from active ones
-- instead of silently dropping them.
CREATE TABLE IF NOT EXISTS bank_universe (
    id_rssd        VARCHAR PRIMARY KEY,
    fdic_cert      VARCHAR,
    name           VARCHAR,
    status         VARCHAR,   -- 'ACTIVE' | 'ACQUIRED' | 'FAILED'
    status_date    DATE,      -- acquisition/failure date; NULL for ACTIVE
    source         VARCHAR,   -- 'FDIC_SDI' | 'FDIC_FAILURES' | 'FDIC_INSTITUTIONS'
    updated_at     TIMESTAMP
);

-- Clean, logically-named UBPR ratio history (spec §10) -- the ONLY table the
-- field-stats and survival work reads ratios from. Populated exclusively by
-- historical_risk.cleanup from the raw bulk pull + config/ubpr_field_mapping.csv;
-- nothing downstream should ever need a raw UBPR field code.
CREATE TABLE IF NOT EXISTS ubpr_ratios (
    id_rssd                          VARCHAR,
    period_end                       DATE,
    bank_name                        VARCHAR,
    peer_group_id                    VARCHAR,
    tier1_leverage_ratio             DOUBLE,
    tier1_rbc_ratio                  DOUBLE,
    total_rbc_ratio                  DOUBLE,
    cet1_ratio                       DOUBLE,   -- NULL pre-2015, spec §11 -- not zero-filled
    roaa                             DOUBLE,
    net_interest_margin              DOUBLE,
    noncurrent_loans_oreo_ratio      DOUBLE,
    efficiency_ratio                 DOUBLE,
    net_loans_leases_to_deposits     DOUBLE,
    net_noncore_funding_dependence   DOUBLE,
    charge_off_ratio                 DOUBLE,
    loan_growth_rate                 DOUBLE,
    capital_growth_rate              DOUBLE,
    camels_composite                 INTEGER,  -- 1-5, usually NULL -- not public data (spec §3)
    source_file                      VARCHAR,  -- raw bulk filename this row came from, for audit
    loaded_at                        TIMESTAMP,
    PRIMARY KEY (id_rssd, period_end)
);

-- Audit trail of the field-mapping config actually used for a given cleanup run
-- (spec §10) -- config/ubpr_field_mapping.csv is the source of truth Claude/humans
-- edit; this table is what a specific ubpr_ratios row was actually built from, so
-- "what did logical_name X map to on date Y" is answerable without git-blame.
CREATE TABLE IF NOT EXISTS ubpr_field_mapping_log (
    raw_field_id   VARCHAR,
    logical_name   VARCHAR,
    ubpr_page      VARCHAR,
    active_from    DATE,
    active_to      DATE,
    verified       BOOLEAN,   -- spec §10: checked against the real UBPR User's Guide
    loaded_at      TIMESTAMP
);
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_SQL)
