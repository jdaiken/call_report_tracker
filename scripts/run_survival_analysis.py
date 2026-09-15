#!/usr/bin/env python3
"""Kaplan-Meier survival analysis (historical-risk-spec.md §5) -- Python/lifelines,
see historical_risk/survival.py's docstring for why not R. Only needs
enforcement_orders, not ubpr_ratios.

Usage:
    python scripts/run_survival_analysis.py [--db path.duckdb] [--out-dir reports]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from ffiec_monitor.config import get_duckdb_path
from historical_risk.survival import fit_by_order_type, load_order_durations, plot_survival_curves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--out-dir", default="reports", type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(args.db, read_only=True)

    durations = load_order_durations(conn)
    if durations.empty:
        print("No enforcement_orders rows with an effective_date -- nothing to fit.", file=sys.stderr)
        return 1

    fitters, summaries = fit_by_order_type(durations)

    import pandas as pd
    summary_df = pd.DataFrame([s.__dict__ for s in summaries])
    summary_df.to_csv(args.out_dir / "survival_summary.csv", index=False)
    print(summary_df.to_string(index=False))

    plot_path = args.out_dir / "survival_curves.png"
    plot_survival_curves(fitters, str(plot_path))
    print(f"\nWrote {args.out_dir / 'survival_summary.csv'} and {plot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
