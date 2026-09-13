#!/usr/bin/env python3
"""Load enforcement-order rows into the local enforcement_orders reference table.

The enforcement-orders table is already built and maintained in a separate project
(see spec §3/§4) that this repo does not have access to. This script is the seam:
point it at a CSV export from that system and it (re)populates the reference table
used by ffiec_monitor.watch_list. Adjust the column mapping below to match whatever
that system actually exports.

Expected CSV columns: id_rssd, fdic_cert, order_type, status, effective_date, terminated_date
  order_type must be one of: SAFETY_SOUNDNESS, AML_BSA, CMP (or another value that isn't
  treated as watch-list-qualifying -- see watch_list.py)
  status must be one of: ACTIVE, TERMINATED

Usage:
    python scripts/import_enforcement_orders.py path/to/enforcement_orders.csv [--db path.duckdb]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db", default=get_duckdb_path())
    args = parser.parse_args()

    conn = db.connect(args.db)
    conn.execute("DELETE FROM enforcement_orders")

    with args.csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = [
            (
                r["id_rssd"],
                r.get("fdic_cert") or None,
                r["order_type"],
                r["status"],
                r.get("effective_date") or None,
                r.get("terminated_date") or None,
            )
            for r in reader
        ]

    conn.executemany(
        "INSERT INTO enforcement_orders "
        "(id_rssd, fdic_cert, order_type, status, effective_date, terminated_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    print(f"Loaded {len(rows)} enforcement order rows into {args.db}")


if __name__ == "__main__":
    main()
