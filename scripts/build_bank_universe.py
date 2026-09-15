#!/usr/bin/env python3
"""Build the survivorship-safe bank universe from FDIC BankFind/SDI (historical-
risk-spec.md §2). Populates bank_universe -- run before field_stats/survival so
failed/acquired banks aren't silently excluded from the analysis.

Requires real network access to banks.data.fdic.gov -- not available in the
sandbox this was built in (see historical_risk/survivorship.py's docstring).

Usage:
    python scripts/build_bank_universe.py [--db path.duckdb]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor.config import get_duckdb_path
from historical_risk import db as hdb
from historical_risk.survivorship import fetch_universe, load_universe

import duckdb


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    conn = duckdb.connect(args.db)
    hdb.ensure_schema(conn)

    rows = fetch_universe()
    n = load_universe(conn, rows)
    n_failed = sum(1 for r in rows if r.status == "FAILED")
    n_acquired = sum(1 for r in rows if r.status == "ACQUIRED")
    n_active = n - n_failed - n_acquired
    print(f"Loaded {n} institutions into bank_universe "
          f"({n_active} active, {n_acquired} acquired, {n_failed} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
