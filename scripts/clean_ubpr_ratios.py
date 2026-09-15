#!/usr/bin/env python3
"""Cleanup step (historical-risk-spec.md §10): raw pulled UBPR files -> clean
ubpr_ratios table. Run after scripts/pull_ubpr_history.py.

Before the first real run, verify the raw file layout against
config/ubpr_field_mapping.csv (still shipped with raw_field_id="TBD" -- see
historical_risk/field_mapping.py's docstring):

    python scripts/clean_ubpr_ratios.py --inspect data/ubpr_raw/<one-file>.zip

Once the mapping config is filled in and verified:

    python scripts/clean_ubpr_ratios.py --raw-dir data/ubpr_raw
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from ffiec_monitor.config import get_duckdb_path
from historical_risk import db as hdb
from historical_risk.cleanup import clean_one_file, inspect_raw_file
from historical_risk.field_mapping import load_mapping, validate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", default="data/ubpr_raw", type=Path)
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--inspect", type=Path, default=None,
                         help="Print one raw file's header instead of cleaning anything.")
    parser.add_argument("--allow-unverified", action="store_true",
                         help="Skip the require_verified check (spec §10) -- for a dry run "
                              "against synthetic/test data only, never real FFIEC data.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    if args.inspect:
        inspect_raw_file(args.inspect)
        return 0

    mappings = load_mapping()
    validate(mappings, require_verified=not args.allow_unverified)

    conn = duckdb.connect(args.db)
    hdb.ensure_schema(conn)

    zip_files = sorted(args.raw_dir.glob("*.zip"))
    if not zip_files:
        print(f"No .zip files found in {args.raw_dir}", file=sys.stderr)
        return 1

    total = 0
    for zip_path in zip_files:
        total += clean_one_file(conn, zip_path, mappings)
    print(f"Cleaned {total} rows from {len(zip_files)} raw file(s) into ubpr_ratios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
