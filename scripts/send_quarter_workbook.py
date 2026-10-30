"""Sunday-evening quarter spreadsheet: every Call Report filed so far for the latest quarter, in
the same three-tab workbook the daily email sends (Summary, Filings with Call Report + UBPR
links, Ratios -- ffiec_monitor/filings_workbook.py), plus the pipeline-fields sheet with the IDC
rank model's exact column headers and its notes (ffiec_monitor/pipeline_fields.py).

Filings come from the daily monitor's stored snapshots. Any snapshot stored before the extra
ratios / pipeline fields existed is re-pulled from FFIEC once and written back (ratios only; the
submission timestamp is left alone), so the first run is long and later Sundays only pull what
is new. No Claude summary.

  python scripts/send_quarter_workbook.py [PERIOD_END]     e.g. 2026-09-30 (default: latest stored)
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ffiec_monitor import db, email_report, performance, pipeline_fields, ratios, simple_email
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.report import capital_status
from ffiec_monitor.ffiec_client import FFIECAuthError, FFIECClient, FFIECRateLimitError
from ffiec_monitor.ubpr import ubpr_url
from ffiec_monitor.filings_workbook import build, extra_ratios

log = logging.getLogger("quarter")
REFILING_TYPES = ("RESTATEMENT_SUB_FLOOR", "CAPITAL_RATIO_RESTATED_DOWN")


def _needs_pull(r: dict) -> bool:
    pf = r.get(pipeline_fields.KEY)
    return (not isinstance(pf, dict) or pf.get("_v") != pipeline_fields.VERSION
            or "roe" not in r or "efficiency_ratio" not in r)


def refresh(conn, client, period: date) -> tuple[int, int, int]:
    """Re-pull snapshots missing the current fields. Returns (pulled, failed, left for next time)."""
    rows = conn.execute("SELECT id_rssd, last_seen_ts, ratios, checked_at FROM call_report_snapshots "
                        "WHERE period_end = ?", [period]).fetchall()
    todo = [r for r in rows if _needs_pull(json.loads(r[2]))]
    log.info("%d of %d snapshots need a pull", len(todo), len(rows))
    pulled = failed = 0
    for rssd, seen, raw, checked in todo:
        try:
            fac = client.retrieve_facsimile(rssd, period)
        except (FFIECAuthError, FFIECRateLimitError):
            log.exception("Stopping pulls")
            break
        except Exception:
            failed += 1
            continue
        old = json.loads(raw)
        new = {**old, **ratios.extract_ratios(fac, period),
               **extra_ratios(fac.get("concepts", fac), period),
               pipeline_fields.KEY: pipeline_fields.extract(fac, period)}
        for k in ("loan_growth_rate", "capital_growth_rate"):     # set from the prior quarter; keep them
            if old.get(k) is not None:
                new[k] = old[k]
        db.upsert_snapshot(conn, db.Snapshot(rssd, period, seen, new, checked))
        pulled += 1
        if pulled % 250 == 0:
            log.info("pulled %d / %d", pulled, len(todo))
    return pulled, failed, len(todo) - pulled - failed


def parents_by_cert() -> dict[str, int]:
    """FDIC cert -> holding company RSSD (FDIC BankFind RSSDHCR; 0 = none). {} if unreachable."""
    try:
        r = requests.get("https://api.fdic.gov/banks/institutions", timeout=120, params={
            "filters": "ACTIVE:1", "fields": "CERT,RSSDHCR", "limit": 10000, "format": "json"})
        r.raise_for_status()
        out = {}
        for d in r.json().get("data", []):
            x = d.get("data", d)
            if x.get("CERT") is not None:
                out[str(x["CERT"]).lstrip("0")] = int(x.get("RSSDHCR") or 0)
        return out
    except Exception as e:
        log.warning("FDIC BankFind parent lookup failed: %s", e)
        return {}


def main(period_arg: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    conn = db.connect(get_duckdb_path())
    period = (date.fromisoformat(period_arg) if period_arg else
              conn.execute("SELECT max(period_end) FROM call_report_snapshots").fetchone()[0])
    qtag = f"{period.year}Q{(period.month - 1) // 3 + 1}"
    quarter = f"Q{(period.month - 1) // 3 + 1} {period.year}"
    pulled = failed = left = 0
    if os.environ.get("FFIEC_TOKEN"):
        pulled, failed, left = refresh(conn, FFIECClient(FFIECConfig.from_env()), period)

    snaps = conn.execute(
        "SELECT s.id_rssd, s.ratios, s.last_seen_ts, EXISTS (SELECT 1 FROM alert_log a WHERE a.id_rssd = s.id_rssd "
        "AND a.period_end = s.period_end AND a.trigger_type IN (?, ?)) FROM call_report_snapshots s "
        "WHERE s.period_end = ?", [*REFILING_TYPES, period]).fetchall()
    prior_pf = {rssd: json.loads(raw).get(pipeline_fields.KEY) for rssd, raw in conn.execute(
        "SELECT id_rssd, ratios FROM call_report_snapshots WHERE period_end = ?",
        [performance.prior_quarter_end(period)]).fetchall()}
    parents = parents_by_cert()
    checker = simple_email.ListChecker(conn)

    records, pipeline_rows = [], []
    for rssd, raw, seen, is_refiling in snaps:
        r = json.loads(raw)
        meta = db.get_institution_meta(conn, rssd) or db.InstitutionMeta(rssd, f"RSSD {rssd}", "", "", "", None, period)
        if r.get("total_assets") is None:
            r["total_assets"] = meta.total_assets
        lists = checker.lists_for(meta)
        cert = (meta.fdic_cert or "").strip()
        records.append({
            "name": meta.name.strip(), "city": meta.city.strip(), "state": meta.state.strip(),
            "rssd": rssd, "cert": cert, "period": period,
            "kind": "Refiling" if is_refiling else "New filing",
            "submitted": f"{seen:%Y-%m-%d %I:%M %p}" if seen else "",
            "enforcement": "FDIC order" in lists, "treasury": "Treasury Desk" in lists,
            "other_lists": [x for x in lists if x not in ("FDIC order", "Treasury Desk")],
            "call_report_url": email_report.cdr_report_url(rssd, period),
            "ubpr_url": ubpr_url(rssd, period),
            "ratios": r,
        })
        pf = pipeline_fields.with_prior(dict(r.get(pipeline_fields.KEY) or {}), prior_pf.get(rssd))
        c = cert.lstrip("0")
        parent = parents.get(c) if (c and parents) else None
        pf.update({"FRS ID": int(rssd) if rssd.isdigit() else rssd, "CERTIFICATE NUMBER": int(c) if c.isdigit() else None,
                   "INSTITUTION NAME": meta.name.strip(), "STATE": meta.state.strip(),
                   "PARENT NUMBER": parent, "HOLD TYPE": None if parent is None else (2 if parent else 0),
                   "YEAR PERIOD": qtag})
        pipeline_rows.append(pf)
    records.sort(key=lambda x: (not (x["enforcement"] or x["treasury"]), x["name"].upper()))
    pipeline_rows.sort(key=lambda x: -(x.get("TOTAL ASSETS DOLLARS IN MILLIONS") or 0))

    run_date = datetime.now(timezone.utc).date()
    label = f"{quarter}, all filings through {run_date:%b %-d, %Y}"
    new = sum(1 for x in records if x["kind"] == "New filing")
    enf = sum(1 for x in records if x["enforcement"])
    tsy = sum(1 for x in records if x["treasury"])
    ltwc = sum(1 for x in records if capital_status(x["ratios"]))
    subject = f"Call Report workbook {label}: {len(records)} filings, {enf} enforcement, {ltwc} LTWC"
    text = (f"Call Report filings {label}\n\n"
            f"Total filings: {len(records)} ({new} new, {len(records) - new} refilings)\n"
            f"On the enforcement list: {enf}\nLess than well capitalized: {ltwc}\nOn the Treasury Desk list: {tsy}\n\n"
            f"The attached workbook has the Summary, Filings (Call Report + UBPR links) and Ratios tabs, plus "
            f"\"{quarter.split()[0]} Pipeline Fields\" with the IDC model's exact column headers (IDC-calculated "
            f"columns left blank; see its notes tab).")
    stale = sum(1 for x in pipeline_rows if x.get("_v") is None)
    if stale or failed or left:
        text += (f"\n\n{stale} rows have no pipeline fields yet ({failed} pulls failed, {left} not reached); "
                 f"they fill in next Sunday.")
    if not parents:
        text += "\n\nParent numbers unavailable this week (FDIC BankFind did not respond)."
    print(subject)
    print(text)
    print(f"pulled {pulled}, failed {failed}, left {left}")
    data = build(records, label, failed, pipeline_rows, f"{quarter.split()[0]} Pipeline Fields")
    fname = f"Call-Report-Filings-{qtag}-through-{run_date.isoformat()}.xlsx"
    email_report.send_email(EmailConfig.from_env(), subject, text,
                            html_body="<div style='font-family:Arial,sans-serif;font-size:13px;'>"
                                      + html.escape(text).replace("\n", "<br>") + "</div>",
                            attachments=[(fname, data, "vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
    print(f"Sent {fname} ({len(data):,} bytes)")


if __name__ == "__main__":
    main(*sys.argv[1:2])
