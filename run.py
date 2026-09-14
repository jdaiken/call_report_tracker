#!/usr/bin/env python3
"""CLI entrypoint for the daily FFIEC Call Report Monitor run -- spec §9/§10.

Examples:
    # Small known list first: enforcement-orders banks + risk-ranking banks, §10.1
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
    """Every RSSD worth tracking on a small-list run: the enforcement-orders bank
    list (spec §10.1) union the risk-ranking reference list (§4's LTWC/order tests
    don't depend on which list a bank came from -- both are just "institutions to
    watch"). Rows in either table with no id_rssd yet resolved are silently
    skipped here, not an error -- scripts/resolve_from_bulk_por.py or
    scripts/apply_rssd_map.py handle resolving those.
    """
    rows = conn.execute(
        "SELECT id_rssd FROM enforcement_orders WHERE id_rssd IS NOT NULL "
        "UNION SELECT id_rssd FROM risk_rankings WHERE id_rssd IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe", choices=["small", "full"], default="small",
                        help="small = enforcement-orders + risk-ranking bank lists, resolved RSSDs only "
                             "(default, spec §10.1); full = every FDIC-insured filer")
    parser.add_argument("--db", default=get_duckdb_path())
    parser.add_argument("--no-send", action="store_true", help="build the email but do not send it")
    parser.add_argument("--fake-diff", action="store_true",
                        help="synthesize one Tier 1 event to prove the email path works end to end (spec §10.2), "
                             "without calling the FFIEC API")
    parser.add_argument("--limit", type=int, default=None,
                        help="DEBUG/SMOKE-TEST ONLY: cap how many changed institution/period pairs get the "
                             "full facsimile pull this run, for a quick live-pipeline check without waiting on "
                             "the whole universe. See run_daily's docstring for the coverage caveat -- never use "
                             "this for the real daily run.")
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
        print("Small universe requested but enforcement_orders and risk_rankings are "
              "both empty (or unresolved) -- run scripts/import_enforcement_orders.py "
              "and/or scripts/import_risk_rankings.py + scripts/apply_rssd_map.py first.",
              file=sys.stderr)
        return 1

    try:
        client = FFIECClient(FFIECConfig.from_env())
    except Exception:
        logging.exception("Failed to build FFIEC client (check FFIEC_USER_ID / FFIEC_TOKEN)")
        raise

    email_config = None if args.no_send else EmailConfig.from_env()

    try:
        body = run_daily(conn, client, email_config, universe=universe, send=not args.no_send, limit=args.limit)
    except FFIECAuthError:
        # Spec §1: an expired/invalid token must surface loudly, not be swallowed.
        logging.exception("FFIEC auth failure -- token likely expired (90-day, no auto-refresh)")
        raise

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
