"""Send the daily-style email for a stretch of time (a weekend, a week), with every
filing re-pulled so the new-filing screen and the capital check run on real ratios,
and every bank checked against public Fed, OCC and FinCEN enforcement actions.
Read-only: nothing is written to the DB.

  python scripts/send_window_digest.py MODE START END LABEL
    MODE  logged     = filings the monitor logged between START and END (UTC)
          submitted  = filings FFIEC shows as submitted between START and END (Eastern)

  e.g. logged "2026-10-18 21:00" "2026-10-26 00:00" "Week ending Sun Oct 25"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import analytics, db, email_report, enforcement, narrative, ratios
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient


def _filings(conn, mode: str, t0: datetime, t1: datetime) -> list[tuple[str, object, str, dict]]:
    """[(rssd, period_end, 'new'|'refiling', detail)] -- latest classification per filing."""
    if mode == "logged":
        rows = conn.execute(
            "SELECT id_rssd, period_end, trigger_type, detail FROM alert_log WHERE logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at", [t0, t1]).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.id_rssd, s.period_end, a.trigger_type, a.detail FROM call_report_snapshots s "
            "LEFT JOIN alert_log a ON a.id_rssd = s.id_rssd AND a.period_end = s.period_end "
            "WHERE s.last_seen_ts >= ? AND s.last_seen_ts < ? ORDER BY a.logged_at", [t0, t1]).fetchall()
    out: dict[tuple, tuple] = {}
    for rssd, period_end, kind, detail in rows:
        k = "refiling" if (kind or "").startswith("RESTATEMENT") else "new"
        out[(rssd, period_end)] = (rssd, period_end, k, json.loads(detail) if detail else {})
    return list(out.values())


def main(mode: str, start: str, end: str, label: str) -> None:
    t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    conn = db.connect(get_duckdb_path())
    filings = _filings(conn, mode, t0, t1)
    client = FFIECClient(FFIECConfig.from_env())
    refilings, new, failed = [], [], 0
    for rssd, period_end, kind, detail in filings:
        meta = db.get_institution_meta(conn, rssd)
        if meta is None:
            continue
        try:
            r = ratios.extract_ratios(client.retrieve_facsimile(rssd, period_end), period_end)
        except Exception:
            r, failed = None, failed + 1
        if kind == "refiling":
            refilings.append(email_report.RestatementEvent(meta, period_end, detail.get("reason", "amended"), r))
        else:
            new.append(email_report.NewFilingEvent(meta, period_end, r))
    run_date = datetime.now(timezone.utc).date()
    src = enforcement.fetch_all(run_date)
    rep, html, pdf = email_report.compose([], [], [], [], refilings, new, run_date, analytics.compute(conn, run_date),
                                          enforcement_sources=src, edition=label)
    body = email_report.build_email_body([], [], [], [], refilings, new, run_date) or "No filings in this window."
    msg = (f"{len(filings)} filings: {len(new)} new, {len(refilings)} refilings ({failed} pulls failed); "
           f"{rep.counts.get('ltwc', 0)} below well capitalized; {rep.counts.get('enforcement', 0)} enforcement matches; "
           f"{rep.counts['flagged']} flagged. Enforcement sources: {src.loaded} errors: {src.errors}")
    print(msg)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Window digest::{msg[:900]}")
        print(f"::notice title=Daily summary::{narrative.STATUS[:500]}")
        for b, rs in rep.ltwc[:10]:
            print(f"::notice title=Below well capitalized::{b.name} ({b.location}) {b.period}: {'; '.join(rs)}")
        for m in rep.enforcement[:10]:
            print(f"::notice title=Enforcement match::{m.bank.name} ({m.bank.location}) - {m.action.agency} "
                  f"{m.action.action} ({m.action.name}, {m.how})")
    slug = label.replace(" ", "-")
    email_report.send_email(
        EmailConfig.from_env(), rep.subject, f"WHAT'S GOING ON\n{rep.summary}\n\n{body}", html_body=html,
        attachment_filename=f"call_report_changes_{slug}.csv",
        attachment_content=email_report.events_csv(refilings, new),
        attachments=[(f"FFIEC-Call-Report-Monitor-{slug}.pdf", pdf, "pdf")], images=rep.images)


if __name__ == "__main__":
    main(*sys.argv[1:5])
