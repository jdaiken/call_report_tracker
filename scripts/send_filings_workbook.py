"""Email the Call Report workbook (ffiec_monitor/filings_workbook.py) for filings FFIEC shows
as submitted in a window (Eastern), from the monitor's stored data plus a fresh facsimile pull
per filing for the ratios. No DB writes.

  python scripts/send_filings_workbook.py START END LABEL
  e.g. "2026-10-28 00:00" "2026-10-30 00:00" "Oct 28-29, 2026"
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, filings_workbook, simple_email
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient


def main(start: str, end: str, label: str) -> None:
    conn = db.connect(get_duckdb_path())
    filings = simple_email.filings_submitted_between(conn, datetime.fromisoformat(start), datetime.fromisoformat(end))
    records, failed = filings_workbook.collect(conn, FFIECClient(FFIECConfig.from_env()), filings)
    print(filings_workbook.send(EmailConfig.from_env(), records, label, failed))


if __name__ == "__main__":
    main(*sys.argv[1:4])
