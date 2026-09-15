#!/usr/bin/env python3
"""Bulk UBPR ratio history pull (historical-risk-spec.md §1).

Downloads one UBPR_RATIO_FOUR file per available quarter from 2012 Q1 through
whatever the latest filed period actually is, into --download-dir. Run
cleanup via scripts/clean_ubpr_ratios.py afterward -- this script only pulls
raw files, it doesn't touch the DuckDB.

Requires real network access to cdr.ffiec.gov -- not available in the sandbox
this was built in (see historical_risk/pull.py's docstring). Run from an
environment with internet access.

Usage:
    python scripts/pull_ubpr_history.py --sanity-check-only
    python scripts/pull_ubpr_history.py --download-dir data/ubpr_raw
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_data_collector import FFIECDownloader

from historical_risk.pull import pull_all_quarters, sanity_check_one_quarter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download-dir", default="data/ubpr_raw", type=Path)
    parser.add_argument("--sanity-check-only", action="store_true",
                         help="Pull just the latest quarter and report its size (spec §1) -- "
                              "do this once before looping every quarter since 2012.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    args.download_dir.mkdir(parents=True, exist_ok=True)

    downloader = FFIECDownloader(download_dir=args.download_dir)

    if args.sanity_check_only:
        result = sanity_check_one_quarter(downloader, args.download_dir)
        print(f"Sanity check OK: {result.file_path} ({result.size_bytes / 1_000_000:.1f} MB)")
        return 0

    failures = 0
    for result in pull_all_quarters(downloader, args.download_dir):
        if not result.success:
            failures += 1
    if failures:
        print(f"{failures} quarterly pull(s) failed -- see log above", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
