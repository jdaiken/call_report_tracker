#!/usr/bin/env python3
"""Load a third-party risk-score ranking CSV into the risk_rankings reference table.

This is NOT enforcement-orders data (see scripts/import_enforcement_orders.py) --
it has no RSSD ID and no Safety & Soundness/AML/CMP order info, just a name, state,
assets, percentile, and a CRITICAL/HIGH-style tier from whatever risk model produced
it. It does not by itself feed the spec §4 Watch List logic (LTWC + enforcement
orders only) -- it's a reference/seed list until each bank is resolved to an id_rssd.

Expected CSV columns (header row required):
    #,Bank,State,Peer Group,Assets,1Y Fail,1Y Distress,1Y Acq,Pctl,Score,Tier
Assets like "$68.4M" / "$1.59B" are parsed to dollars; "1Y Fail"/"1Y Distress"/"1Y Acq"
are currently all placeholder ('--') in the source and are not stored.

Usage:
    python scripts/import_risk_rankings.py path/to/risk-rankings.csv [--db path.duckdb]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path

ASSET_RE = re.compile(r"^\$?([\d.]+)\s*([MB])$", re.IGNORECASE)


def _parse_assets(raw: str) -> Optional[float]:
    m = ASSET_RE.match(raw.strip())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).upper()
    return value * (1e9 if unit == "B" else 1e6)


def _parse_float(raw: str) -> Optional[float]:
    raw = raw.strip()
    if not raw or raw == "'--":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db", default=get_duckdb_path())
    args = parser.parse_args()

    with args.csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [
            db.RiskRankingRow(
                rank=_parse_int(r["#"]),
                bank_name=r["Bank"].strip(),
                state=r["State"].strip(),
                peer_group=r["Peer Group"].strip() or None,
                total_assets=_parse_assets(r["Assets"]),
                pctl=_parse_float(r["Pctl"]),
                score=_parse_float(r["Score"]),
                tier=r["Tier"].strip(),
            )
            for r in reader
        ]

    conn = db.connect(args.db)
    count = db.replace_risk_rankings(conn, rows)
    unresolved = len(db.unresolved_risk_rankings(conn))
    print(f"Loaded {count} risk ranking rows into {args.db} ({unresolved} with no id_rssd yet)")


if __name__ == "__main__":
    main()
