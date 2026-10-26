"""Send the designed daily email for every Call Report SUBMITTED in a time window
(e.g. a weekend), using FFIEC's own submission timestamps (call_report_snapshots.
last_seen_ts, Eastern time as FFIEC reports it). Each filing is re-pulled so the
new-filing screen runs on real ratios. Read-only: nothing is written to the DB.

  python scripts/send_window_digest.py "2026-10-23 17:00" "2026-10-26 00:00" "Weekend Oct 23-25"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import analytics, db, email_report, narrative, ratios
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient


def main(start: str, end: str, label: str) -> None:
    t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    conn = db.connect(get_duckdb_path())
    rows = conn.execute("SELECT id_rssd, period_end, last_seen_ts FROM call_report_snapshots "
                        "WHERE last_seen_ts >= ? AND last_seen_ts < ?", [t0, t1]).fetchall()
    client = FFIECClient(FFIECConfig.from_env())
    refilings, new, failed = [], [], 0
    for rssd, period_end, _ts in rows:
        meta = db.get_institution_meta(conn, rssd)
        if meta is None:
            continue
        kinds = [k for (k,) in conn.execute("SELECT trigger_type FROM alert_log WHERE id_rssd = ? AND period_end = ? "
                                             "ORDER BY logged_at DESC", [rssd, period_end]).fetchall()]
        if kinds and kinds[0].startswith("RESTATEMENT"):
            detail = conn.execute("SELECT detail FROM alert_log WHERE id_rssd = ? AND period_end = ? AND trigger_type "
                                  "LIKE 'RESTATEMENT%' ORDER BY logged_at DESC LIMIT 1", [rssd, period_end]).fetchone()
            reason = (json.loads(detail[0]) if detail and detail[0] else {}).get("reason", "amended")
            refilings.append(email_report.RestatementEvent(meta, period_end, reason))
            continue
        try:
            r = ratios.extract_ratios(client.retrieve_facsimile(rssd, period_end), period_end)
        except Exception:
            r, failed = None, failed + 1
        new.append(email_report.NewFilingEvent(meta, period_end, r))
    run_date = datetime.now(timezone.utc).date()
    rep, html, pdf = email_report.compose([], [], [], [], refilings, new, run_date, analytics.compute(conn, run_date))
    rep.subject = rep.subject.replace(f"{run_date:%a %b %-d}", label)
    html = html.replace(f"{run_date:%A, %B %-d, %Y} · ", f"{label} (filed {t0:%a %-I%p} to {t1:%a %-I%p} ET) · ")
    body = email_report.build_email_body([], [], [], [], refilings, new, run_date) or "No filings in this window."
    msg = f"{len(rows)} submissions in window: {len(new)} new ({failed} pulls failed), {len(refilings)} refilings, " \
          f"{rep.counts['flagged']} flagged"
    print(msg)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Window digest::{msg}")
        print(f"::notice title=Daily summary::{narrative.STATUS[:500]}")
    email_report.send_email(
        EmailConfig.from_env(), rep.subject, f"WHAT'S GOING ON\n{rep.summary}\n\n{body}", html_body=html,
        attachment_filename=f"call_report_changes_{label.replace(' ', '_')}.csv",
        attachment_content=email_report.events_csv(refilings, new),
        attachments=[(f"FFIEC-Call-Report-Monitor-{label.replace(' ', '-')}.pdf", pdf, "pdf")], images=rep.images)


if __name__ == "__main__":
    main(*sys.argv[1:4])
