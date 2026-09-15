#!/usr/bin/env python3
"""Descriptive field stats (historical-risk-spec.md §4). Writes CSVs to
--out-dir: frequency counts, before/after snapshots, order-type x ratio
cross-tab. Requires ubpr_ratios and enforcement_orders both populated.

Usage:
    python scripts/run_field_stats.py [--db path.duckdb] [--out-dir reports]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from ffiec_monitor.config import get_duckdb_path
from historical_risk.field_stats import (
    before_after_snapshots,
    order_frequency_by_year_and_type,
    order_type_ratio_crosstab,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--out-dir", default="reports", type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(args.db, read_only=True)

    freq = order_frequency_by_year_and_type(conn)
    freq.to_csv(args.out_dir / "order_frequency_by_year_type.csv", index=False)
    print(f"order_frequency_by_year_type.csv: {len(freq)} rows")

    snapshots = before_after_snapshots(conn)
    snapshots.to_csv(args.out_dir / "before_after_snapshots.csv", index=False)
    print(f"before_after_snapshots.csv: {len(snapshots)} rows")

    crosstab = order_type_ratio_crosstab(conn)
    crosstab.to_csv(args.out_dir / "order_type_ratio_crosstab.csv", index=False)
    print(f"order_type_ratio_crosstab.csv: {len(crosstab)} rows")

    print(f"\nWrote reports to {args.out_dir}/")
    print("Note: watch_list_network_cross_section (spec §4) is intentionally not run here "
          "-- see historical_risk/field_stats.py's docstring on that function (spec §8 boundary).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
