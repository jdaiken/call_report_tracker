"""One-off: print id_rssd alongside name/city/state/period for every
RESTATEMENT_SUB_FLOOR alert_log row in the last N days -- the same set
preview_refilings_template.py emails, but with the RSSD identifier included.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path


def print_restatement_rssds(conn, days: int = 7) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        "SELECT id_rssd, period_end FROM alert_log "
        "WHERE tier = 'TIER3' AND trigger_type = 'RESTATEMENT_SUB_FLOOR' AND logged_at >= ? "
        "ORDER BY logged_at ASC",
        [since],
    ).fetchall()

    for id_rssd, period_end in rows:
        meta = db.get_institution_meta(conn, id_rssd)
        name = meta.name if meta else "?"
        city = meta.city if meta else "?"
        state = meta.state if meta else "?"
        fdic_cert = meta.fdic_cert if meta else "?"
        print(f"RSSD {id_rssd} | FDIC Cert {fdic_cert or 'n/a'} | {name} ({city}, {state}) | period {period_end}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    conn = db.connect(get_duckdb_path())
    print_restatement_rssds(conn, days=days)
