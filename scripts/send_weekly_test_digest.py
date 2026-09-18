"""One-off test: report every institution/period change seen in the last N days,
regardless of alert tier -- unlike the daily pipeline, which only emails Tier 1/
Tier 2 events and otherwise sends just a heartbeat. alert_log records all three
tiers (including TIER3 "logged only, no email" decisions -- see db.py's schema
comment), so this is a query over already-collected data, not a new API pull.

Meant to answer "did anything change this week" directly, bypassing the ratio
thresholds that decide what's alert-worthy.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report
from ffiec_monitor.config import EmailConfig, get_duckdb_path


def run_weekly_test_digest(conn, email_config: Optional[EmailConfig], days: int = 7, send: bool = True) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT id_rssd, period_end, tier, trigger_type, detail, logged_at
        FROM alert_log
        WHERE logged_at >= ?
        ORDER BY logged_at ASC
        """,
        [since],
    ).fetchall()

    if not rows:
        body = f"No institution/period changes logged in the last {days} day(s)."
        if send and email_config is not None:
            email_report.send_email(
                email_config, f"[FFIEC Monitor] TEST -- last {days}d digest", body)
        return body

    lines = [f"All institution/period changes logged in the last {days} day(s), "
             f"every tier (unlike the daily digest, which only emails Tier 1/2):", ""]
    for id_rssd, period_end, tier, trigger_type, detail, logged_at in rows:
        meta = db.get_institution_meta(conn, id_rssd)
        name = meta.name if meta else f"RSSD {id_rssd}"
        loc = f"{meta.city}, {meta.state}" if meta else "n/a"
        lines.append(f"[{tier}] {name} ({loc}) -- period {period_end} -- {trigger_type} "
                      f"(logged {logged_at.isoformat()})")
        if detail and detail != "{}":
            lines.append(f"    {detail}")

    body = "\n".join(lines)

    if send and email_config is not None:
        email_report.send_email(
            email_config, f"[FFIEC Monitor] TEST -- last {days}d digest ({len(rows)} change(s))", body)

    return body


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    conn = db.connect(get_duckdb_path())
    email_config = EmailConfig.from_env()
    print(run_weekly_test_digest(conn, email_config, days=days))
