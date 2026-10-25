"""TEST: send the designed daily email built from REAL recent activity: refilings and
new quarterly filings logged in the last N days. New filings get a fresh facsimile pull
(capped at MAX_PULLS, largest banks first) so the level screen runs on real ratios.
Read-only: no snapshots or alerts are written. Subject starts with "TEST".
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report, ratios
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient

MAX_PULLS = 150


def main(days: int) -> None:
    conn = db.connect(get_duckdb_path())
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute("SELECT id_rssd, period_end, trigger_type, detail FROM alert_log WHERE tier = 'TIER3' "
                        "AND trigger_type IN ('RESTATEMENT_SUB_FLOOR', 'NEW_PERIOD_NO_TRIGGER') AND logged_at >= ?",
                        [since]).fetchall()
    refilings, new = [], []
    for rssd, period_end, kind, detail in rows:
        meta = db.get_institution_meta(conn, rssd)
        if meta is None:
            continue
        if kind == "RESTATEMENT_SUB_FLOOR":
            reason = (json.loads(detail) if detail else {}).get("reason", "reason not recorded")
            refilings.append(email_report.RestatementEvent(meta, period_end, reason))
        else:
            new.append((meta, period_end))
    new.sort(key=lambda t: t[0].total_assets or 0, reverse=True)
    client = FFIECClient(FFIECConfig.from_env())
    events, failed = [], 0
    for i, (meta, period_end) in enumerate(new):
        r = None
        if i < MAX_PULLS:
            try:
                r = ratios.extract_ratios(client.retrieve_facsimile(meta.id_rssd, period_end), period_end)
            except Exception:
                failed += 1
        events.append(email_report.NewFilingEvent(meta, period_end, r))
    run_date = datetime.now(timezone.utc).date()
    rep, html, pdf = email_report.compose([], [], [], [], refilings, events, run_date)
    body = email_report.build_email_body([], [], [], [], refilings, events, run_date) or "Nothing logged."
    msg = (f"{len(refilings)} refilings, {len(new)} new filings ({min(len(new), MAX_PULLS)} screened, "
           f"{failed} pulls failed), {rep.counts['flagged']} flagged")
    print(msg)
    from ffiec_monitor import narrative
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Preview::{msg}")
        print(f"::notice title=Daily summary::{narrative.STATUS[:500]}")
    email_report.send_email(
        EmailConfig.from_env(), f"TEST · {rep.subject}", f"WHAT'S GOING ON\n{rep.summary}\n\n{body}", html_body=html,
        attachment_filename=f"call_report_changes_{run_date.isoformat()}.csv",
        attachment_content=email_report.events_csv(refilings, events),
        attachments=[(f"FFIEC-Call-Report-Monitor-{run_date.isoformat()}.pdf", pdf, "pdf")])


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
