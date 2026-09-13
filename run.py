#!/usr/bin/env python3
"""CLI entrypoint for the daily FFIEC Call Report Monitor run -- spec §9/§10.

Examples:
    # Small known list first (existing enforcement-orders banks), spec §10.1
    python run.py --universe small

    # Full ~4,500-institution universe, once the small-list run is trusted
    python run.py --universe full

    # Confirm the email-sending path works end to end without hitting the API, §10.2
    python run.py --fake-diff
"""
from __future__ import annotations

import argparse
import logging
import sys

from ffiec_monitor import db
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECAuthError, FFIECClient
from ffiec_monitor.pipeline import run_daily


def _small_universe(conn) -> set[str]:
    rows = conn.execute("SELECT DISTINCT id_rssd FROM enforcement_orders").fetchall()
    return {r[0] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe", choices=["small", "full"], default="small",
                        help="small = existing enforcement-orders bank list (default, spec §10.1); "
                             "full = every FDIC-insured filer")
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--no-send", action="store_true", help="build the email but do not send it")
    parser.add_argument("--fake-diff", action="store_true",
                        help="synthesize one Tier 1 event to prove the email path works end to end (spec §10.2), "
                             "without calling the FFIEC API")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    conn = db.connect(args.db)

    if args.fake_diff:
        from scripts.fake_diff import run_fake_diff
        email_config = None if args.no_send else EmailConfig.from_env()
        body = run_fake_diff(conn, email_config, send=not args.no_send)
        print(body)
        return 0

    universe = None if args.universe == "full" else _small_universe(conn)
    if universe is not None and not universe:
        print("Small universe requested but enforcement_orders is empty -- run "
              "scripts/import_enforcement_orders.py first.", file=sys.stderr)
        return 1

    try:
        client = FFIECClient(FFIECConfig.from_env())
    except Exception:
        logging.exception("Failed to build FFIEC client (check FFIEC_USER_ID / FFIEC_TOKEN)")
        raise

    email_config = None if args.no_send else EmailConfig.from_env()

    try:
        body = run_daily(conn, client, email_config, universe=universe, send=not args.no_send)
    except FFIECAuthError:
        # Spec §1: an expired/invalid token must surface loudly, not be swallowed.
        logging.exception("FFIEC auth failure -- token likely expired (90-day, no auto-refresh)")
        raise

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
