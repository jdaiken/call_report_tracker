"""Weekly Network Exposure report: reciprocal cap use and uninsured deposits for every bank
(ffiec_monitor/network.py). Updates stored snapshots in place with the new fields (re-pulling
Call Reports filed before those fields were captured), then emails the highlights with a ten-page
PDF and a spreadsheet.

  python scripts/send_network_report.py [max_pulls]
"""
from __future__ import annotations

import os
import sys
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report, enforcement, network, network_render
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient

XLSX = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def main(max_pulls: int) -> None:
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not network.due(today):
        print(f"{today}: outside the filing window and not the first Monday of the month; skipping")
        return
    conn = db.connect(get_duckdb_path())
    client = FFIECClient(FFIECConfig.from_env())
    run_date = today
    net = network.collect(conn, client, run_date, max_pulls=max_pulls,
                          enforcement_sources=enforcement.fetch_all(run_date),
                          prior_lookup=network.make_history_lookup(conn, client),
                          on_progress=lambda n: print(f"pulled {n} Call Reports", flush=True))
    conn.execute("CHECKPOINT")
    net = network.build(net)
    s = net.stats()
    msg = (f"{s['banks']} banks ({net.pulled} pulled, {net.pull_failed} failed, {net.missing} not loaded); "
           f"{s['users']} use reciprocal; {s['near_cap']} near cap, {s['over_cap']} over; {s['special']} special cap; "
           f"{s['high_uninsured']} with {network.HIGH_UNINSURED:.0f}%+ uninsured; summary by {net.summary_by}")
    print(msg)
    if gha:
        print(f"::notice title=Network report::{msg}")
    pdf = network_render.render_pdf(net)
    pages = len(re.findall(rb"/Type /Page[^s]", pdf))
    print(f"PDF {pages} pages; charts: {', '.join(sorted(net.images))}")
    big = sorted(net.banks, key=lambda b: -(b.assets or 0))[:3]
    info = (f"left out {len(net.excluded)} banks of ${network.EXCLUDE_ASSETS // 1_000_000}B+; largest kept: "
            + "; ".join(f"{b.bank.name} {b.bank.assets}" for b in big)
            + "; highest risk: " + "; ".join(b.bank.name for b in net.risk(5)))
    print(info)
    multi = net.treasury_multi()
    tinfo = (f"treasury list {net.treasury_loaded}: not found {', '.join(net.treasury_unmatched) or 'none'}; ambiguous "
             + ("; ".join(f"{k} -> " + ", ".join(f"{b.bank.name} ({b.bank.location}, {b.bank.assets})" for b in v)
                          for k, v in multi.items()) or "none"))
    print(tinfo)
    if gha:
        print(f"::notice title=Treasury list::{tinfo[:3900]}")
    for name in [x.strip() for x in os.environ.get("INSPECT", "").split(";") if x.strip()]:
        found = [b for b in net.banks if name.upper() in b.bank.name.upper()]
        if found:
            msg = "; ".join(f"{b.bank.name} ({b.bank.location}, RSSD {b.bank.rssd}, cert {b.bank.cert}) period {b.period} "
                            f"orders {len(b.orders)} in report for: {', '.join(net.reasons(b)) or 'nothing'}" for b in found)
        else:
            rows = conn.execute("SELECT m.id_rssd, m.name, m.city, m.state, m.fdic_cert, m.total_assets, "
                                "(SELECT string_agg(CAST(period_end AS VARCHAR), ',') FROM call_report_snapshots s "
                                "WHERE s.id_rssd = m.id_rssd) FROM institution_meta m WHERE upper(m.name) LIKE ?",
                                [f"%{name.upper()}%"]).fetchall()
            msg = ("not in the report's bank list; stored: " + "; ".join(
                f"{r[1]} ({r[2]}, {r[3]}, RSSD {r[0]}, cert {r[4]}, assets {r[5]}) snapshots [{r[6] or 'none'}]" for r in rows)
                   if rows else "not in the report and no stored Call Report or institution record at all")
        print(f"INSPECT {name}: {msg}")
        if gha:
            print(f"::notice title=Inspect {name}::{msg[:3500]}")
    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("dry run: no email sent")
        return
    if gha:
        print(f"::notice title=Network scope::{info}")
    if gha:
        print(f"::notice title=Network PDF::{pages} pages; charts: {', '.join(sorted(net.images))}")
    stamp = run_date.isoformat()
    email_report.send_email(
        EmailConfig.from_env(), os.environ.get("SUBJECT_PREFIX", "") + network_render.subject(net), net.summary, html_body=network_render.render_html(net),
        attachments=[(f"Network-Exposure-{stamp}.pdf", pdf, "pdf"),
                     (f"Network-Exposure-{stamp}.xlsx", network_render.build_workbook(net), XLSX)],
        images={f"net_{k}": v for k, v in net.images.items() if k in network_render.EMAIL_IMAGES})


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 5000)
