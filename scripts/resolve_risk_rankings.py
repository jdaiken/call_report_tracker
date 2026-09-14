#!/usr/bin/env python3
"""Resolve risk_rankings rows (name + state, no RSSD) to an id_rssd using the LIVE
FFIEC panel of reporters. Prefer scripts/resolve_from_bulk_por.py when you have a
recent bulk POR file on hand (faster, no API round-trips) -- use this one when you
don't, e.g. next quarter after the bulk file's period has aged out.

See ffiec_monitor/matching.py for the matching rules (conservative: only exact
normalized name+state matches auto-assign; everything else is reported for
manual review, never guessed).

Usage:
    python scripts/resolve_risk_rankings.py [--db path.duckdb]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient
from ffiec_monitor.matching import PanelEntry, print_report, resolve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=get_duckdb_path())
    args = parser.parse_args()

    conn = db.connect(args.db)
    client = FFIECClient(FFIECConfig.from_env())

    latest_period = sorted(client.retrieve_reporting_periods())[-1]
    print(f"Using panel of reporters for period {latest_period.isoformat()}")
    raw_panel = client.retrieve_panel_of_reporters(latest_period)
    panel = [
        PanelEntry(id_rssd=str(p["ID_RSSD"]), name=str(p["Name"]), state=str(p["State"]))
        for p in raw_panel
    ]
    print(f"Panel has {len(panel)} institutions")

    result = resolve(db.unresolved_risk_rankings(conn), panel)
    for row_id, rssd in result.resolved:
        conn.execute("UPDATE risk_rankings SET id_rssd = ? WHERE id = ?", [rssd, row_id])
    print_report(result)


if __name__ == "__main__":
    main()
