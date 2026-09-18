"""One-off: print a period_end/tier breakdown of alert_log for the last N days,
instead of the full row-by-row digest (send_weekly_test_digest.py) -- useful to
sanity-check what's actually in a large digest without dumping thousands of lines.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path


def print_period_stats(conn, days: int = 7) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT period_end, tier, trigger_type, count(*) AS n,
               min(logged_at) AS first_logged, max(logged_at) AS last_logged
        FROM alert_log
        WHERE logged_at >= ?
        GROUP BY period_end, tier, trigger_type
        ORDER BY period_end, tier, trigger_type
        """,
        [since],
    ).fetchall()
    total = sum(r[3] for r in rows)
    print(f"alert_log entries in the last {days} day(s): {total} total")
    for period_end, tier, trigger_type, n, first_logged, last_logged in rows:
        print(f"  period {period_end} [{tier}] {trigger_type}: {n} "
              f"(logged {first_logged.isoformat()} .. {last_logged.isoformat()})")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    conn = db.connect(get_duckdb_path())
    print_period_stats(conn, days=days)
