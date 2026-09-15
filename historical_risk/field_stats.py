"""Descriptive field stats (spec §4) -- frequency counts, before/after ratio
snapshots around enforcement orders, and an order-type x ratio cross-tab. No
control group, no regression -- spec §7's explicit scope boundary. Everything
here is a group-by/aggregate over ubpr_ratios joined to enforcement_orders.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .field_mapping import EXPECTED_RATIO_LOGICAL_NAMES as RATIO_COLUMNS


def order_frequency_by_year_and_type(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Orders per year x order_type, 2012-present (spec §4). Not broken out "by
    regulator" as the spec table asks -- enforcement_orders (ffiec_monitor/db.py)
    has no regulator column; add one to the source export (README's "Populating
    the enforcement-orders reference table") if that cut is wanted."""
    return conn.execute(
        """
        SELECT date_part('year', effective_date) AS year,
               order_type,
               count(*) AS order_count
        FROM enforcement_orders
        WHERE effective_date >= DATE '2012-01-01'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).df()


def before_after_snapshots(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """For each order, the closest ubpr_ratios row at T-4, T-1, T, T+4 quarters
    relative to effective_date -- pure description of what the ratios looked
    like around the event, no control group (spec §4)."""
    ratio_select = ", ".join(RATIO_COLUMNS)
    return conn.execute(
        f"""
        WITH orders AS (
            SELECT id_rssd, order_type, effective_date, status
            FROM enforcement_orders
            WHERE effective_date IS NOT NULL
        ),
        offsets AS (
            SELECT * FROM (VALUES (-4, 'T-4'), (-1, 'T-1'), (0, 'T'), (4, 'T+4')) AS t(q_offset, label)
        ),
        targets AS (
            SELECT o.id_rssd, o.order_type, o.effective_date, off.label,
                   o.effective_date + INTERVAL (off.q_offset * 3) MONTH AS target_period
            FROM orders o CROSS JOIN offsets off
        ),
        nearest AS (
            SELECT t.id_rssd, t.order_type, t.effective_date, t.label, t.target_period,
                   r.period_end, {ratio_select},
                   row_number() OVER (
                       PARTITION BY t.id_rssd, t.effective_date, t.label
                       ORDER BY abs(date_diff('day', t.target_period, r.period_end))
                   ) AS rn
            FROM targets t
            JOIN ubpr_ratios r ON r.id_rssd = t.id_rssd
                AND abs(date_diff('day', t.target_period, r.period_end)) <= 60
        )
        SELECT id_rssd, order_type, effective_date, label, period_end, {ratio_select}
        FROM nearest
        WHERE rn = 1
        ORDER BY id_rssd, effective_date, label
        """
    ).df()


def order_type_ratio_crosstab(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Means/medians of each ratio at order-effective-quarter (T), grouped by
    order_type -- descriptive only (spec §4: "do AML orders cluster around
    different ratio profiles than S&S orders... no regression")."""
    agg_exprs = []
    for col in RATIO_COLUMNS:
        agg_exprs.append(f"avg({col}) AS {col}_mean")
        agg_exprs.append(f"median({col}) AS {col}_median")
    agg_sql = ", ".join(agg_exprs)

    return conn.execute(
        f"""
        WITH t_snapshot AS (
            SELECT o.order_type, r.*
            FROM enforcement_orders o
            JOIN ubpr_ratios r ON r.id_rssd = o.id_rssd
            QUALIFY row_number() OVER (
                PARTITION BY o.id_rssd, o.effective_date
                ORDER BY abs(date_diff('day', o.effective_date, r.period_end))
            ) = 1
        )
        SELECT order_type, count(*) AS n, {agg_sql}
        FROM t_snapshot
        GROUP BY order_type
        ORDER BY order_type
        """
    ).df()


def watch_list_network_cross_section(conn: duckdb.DuckDBPyConnection) -> None:
    """Spec §4's "what share of currently-LTWC/ordered banks are on IntraFi's own
    network" -- deliberately NOT implemented here. Doing it correctly needs
    network-membership data, and this repo only has `risk_rankings` (a *different*
    third-party risk-score list, not network membership -- see README) as a
    candidate proxy. Blurring those together to produce a number would cross
    exactly the boundary spec §8 says needs its own data-governance conversation
    before any IntraFi-network data (even used just for cross-referencing) enters
    this pipeline. Leave this as a documented gap until that conversation happens
    and a real, approved network-membership source is wired in.
    """
    raise NotImplementedError(
        "Intentionally unimplemented -- see this function's docstring and spec §8."
    )
