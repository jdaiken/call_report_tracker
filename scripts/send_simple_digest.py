"""Send the plain daily table email for filings FFIEC shows as submitted in a time window
(Eastern), from what the daily monitor has already stored -- no FFIEC API call, no DB writes.

  python scripts/send_simple_digest.py START END LABEL
  e.g. "2026-10-27 00:00" "2026-10-29 00:00" "Oct 27-28, 2026"
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report, simple_email
from ffiec_monitor.config import EmailConfig, get_duckdb_path


def main(start: str, end: str, label: str) -> None:
    conn = db.connect(get_duckdb_path())
    filings = simple_email.filings_submitted_between(conn, datetime.fromisoformat(start), datetime.fromisoformat(end))
    rows = simple_email.build_rows(conn, filings)
    text = simple_email.render_text(rows, label)
    print(simple_email.subject(rows, label))
    print(text)
    email_report.send_email(EmailConfig.from_env(), simple_email.subject(rows, label), text,
                            html_body=simple_email.render_html(rows, label))


if __name__ == "__main__":
    main(*sys.argv[1:4])
