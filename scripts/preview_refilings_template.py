"""TEST: build a real (not synthetic) preview of the new REFILINGS email section,
using this week's already-processed RESTATEMENT_SUB_FLOOR alert_log rows run
through the current build_email_body -- confirms the new template renders
correctly against real data without waiting for a fresh restatement to occur
(everything currently in alert_log was already marked seen by prior runs).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report
from ffiec_monitor.config import EmailConfig, get_duckdb_path


def run_preview(conn, email_config: Optional[EmailConfig], days: int = 7, send: bool = True) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        "SELECT id_rssd, period_end, detail FROM alert_log "
        "WHERE tier = 'TIER3' AND trigger_type = 'RESTATEMENT_SUB_FLOOR' AND logged_at >= ? "
        "ORDER BY logged_at ASC",
        [since],
    ).fetchall()

    events = []
    for id_rssd, period_end, detail in rows:
        meta = db.get_institution_meta(conn, id_rssd)
        if meta is None:
            continue
        # `detail` is a JSON string; older rows (logged before the "reason" field
        # was added) only have the generic "note" key, so no real reason exists.
        parsed = json.loads(detail) if detail else {}
        reason = parsed.get("reason", "reason not available (logged before per-ratio detail was tracked)")
        events.append(email_report.RestatementEvent(meta, period_end, reason))

    body = email_report.build_email_body([], [], [], [], events, datetime.now(timezone.utc).date())
    assert body is not None, f"expected a body from {len(events)} real restatement event(s)"

    if send and email_config is not None:
        email_report.send_email(
            email_config, f"[FFIEC Monitor] TEST -- new REFILINGS template preview ({len(events)} entries)", body)

    return body


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    conn = db.connect(get_duckdb_path())
    email_config = EmailConfig.from_env()
    print(run_preview(conn, email_config, days=days))
