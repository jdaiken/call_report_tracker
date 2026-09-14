#!/usr/bin/env python3
"""Apply a pre-resolved name+state -> id_rssd mapping to risk_rankings.

Run this after import_risk_rankings.py (which always loads id_rssd as NULL --
the raw source CSV has no RSSD). data/risk-rankings-rssd-map.csv was produced
once by resolving the full 200-bank list against a real FFIEC bulk Call Report
download (see scripts/resolve_from_bulk_por.py) plus one manual confirmation
(Increase Bank, WA -> Twin City Bank, RSSD 2905761 -- a fintech-brand/DBA match
on assets, not a name match). Committing the resolved mapping means CI doesn't
need the ~30MB bulk zip (which isn't committed) or a live API round-trip just to
reproduce a resolution that's already been done and reviewed once.

If the risk-rankings source list changes, re-run resolve_from_bulk_por.py (or
resolve_risk_rankings.py against the live API) with a fresh bulk file/token and
re-export data/risk-rankings-rssd-map.csv.

Usage:
    python scripts/apply_rssd_map.py [--db path.duckdb] [--map data/risk-rankings-rssd-map.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path

DEFAULT_MAP = Path(__file__).resolve().parent.parent / "data" / "risk-rankings-rssd-map.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    args = parser.parse_args()

    conn = db.connect(args.db)
    with args.map.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        conn.execute(
            "UPDATE risk_rankings SET id_rssd = ? WHERE bank_name = ? AND state = ? AND id_rssd IS NULL",
            [row["id_rssd"], row["bank_name"], row["state"]],
        )

    remaining = conn.execute("SELECT COUNT(*) FROM risk_rankings WHERE id_rssd IS NULL").fetchone()[0]
    print(f"Applied {len(rows)} mapping rows from {args.map}. {remaining} risk_rankings rows still unresolved.")


if __name__ == "__main__":
    main()
