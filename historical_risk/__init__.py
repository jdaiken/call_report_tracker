"""Historical enforcement & failure-risk analysis (spec: historical-risk-spec.md).

Pulls UBPR ratio history back to 2012 from FFIEC's public bulk data, joins it
against the existing `enforcement_orders` table, and produces descriptive field
stats + Kaplan-Meier survival curves. See historical-risk-spec.md §6/§7 for the
explicit scope boundary -- this is a descriptive pass, not a causal/matched-control
study.

Shares the same DuckDB file as ffiec_monitor (spec §6) -- there is no separate
DUCKDB_PATH for this package, see ffiec_monitor.config.get_duckdb_path().
"""
