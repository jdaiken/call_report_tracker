"""Quarterly risk-test workbook (ffiec_monitor/risk_tests.py): screens, trend tests, size-peer
outliers, a composite risk ranking, data-quality and monitored-list checks for every bank that has
filed the quarter, emailed as one .xlsx. Read-only: works from the monitor's stored snapshots (the
quarterly filings workbook re-pulls them first), never writes the database, never calls FFIEC.

  python scripts/send_risk_tests.py [PERIOD_END] [--out PATH]    PERIOD_END e.g. 2026-09-30 (default: latest)

--out writes the workbook to PATH instead of emailing it.
"""
from __future__ import annotations

import html
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, email_report, enforcement, performance, ratios, risk_tests, simple_email
from ffiec_monitor.config import EmailConfig, get_duckdb_path
from ffiec_monitor.ubpr import ubpr_url

REFILING_TYPES = ("RESTATEMENT_SUB_FLOOR", "CAPITAL_RATIO_RESTATED_DOWN")


def _quarter_start(p: date) -> date:
    return date(p.year, p.month - 2, 1)


def load(conn, period: date) -> tuple[list[risk_tests.Bank], list[tuple[str, str, str]], list]:
    prev_end = performance.prior_quarter_end(period)
    history = [prev_end]
    for _ in range(3):
        history.append(performance.prior_quarter_end(history[-1]))

    def snaps(p):
        return {rssd: json.loads(raw) for rssd, raw in conn.execute(
            "SELECT id_rssd, ratios FROM call_report_snapshots WHERE period_end = ?", [p]).fetchall()}

    cur, hist = snaps(period), [snaps(p) for p in history]
    refiled = {r[0] for r in conn.execute(
        "SELECT DISTINCT id_rssd FROM alert_log WHERE period_end = ? AND trigger_type IN (?, ?)",
        [period, *REFILING_TYPES]).fetchall()}
    checker = simple_email.ListChecker(conn)
    banks = []
    for rssd, r in cur.items():
        meta = db.get_institution_meta(conn, rssd) or db.InstitutionMeta(rssd, f"RSSD {rssd}", "", "", "", None, period)
        if r.get("total_assets") is None:
            r["total_assets"] = meta.total_assets
        prev = hist[0].get(rssd)
        banks.append(risk_tests.Bank(
            rssd=rssd, name=meta.name.strip(), city=(meta.city or "").strip(), state=(meta.state or "").strip(),
            cert=(meta.fdic_cert or "").strip(), cur=r, prev=prev,
            recip_hist=[h[rssd].get(ratios.RECIPROCAL_RAW_KEY) for h in hist if rssd in h],
            lists=checker.lists_for(meta), refiled=rssd in refiled,
            call_report_url=email_report.cdr_report_url(rssd, period), ubpr_url=ubpr_url(rssd, period)))
    missing = []
    for rssd in sorted(set(hist[0]) - set(cur)):
        meta = db.get_institution_meta(conn, rssd)
        missing.append((rssd, meta.name.strip() if meta else "", (meta.state or "").strip() if meta else ""))
    by_cert = {b.cert.lstrip("0"): b for b in banks if b.cert}
    try:
        actions = enforcement.load_fdic()[0]
    except Exception:
        actions = []
    new_actions = [(by_cert[a.cert.lstrip("0")], f"{a.action} ({a.start:%Y-%m-%d})") for a in actions
                   if a.cert and a.cert.lstrip("0") in by_cert and a.start and _quarter_start(period) <= a.start <= period]
    return banks, missing, new_actions


def main(*argv: str) -> None:
    args = [a for a in argv if not a.startswith("--")]
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    if out in args:
        args.remove(out)
    conn = db.connect(get_duckdb_path())
    period = (date.fromisoformat(args[0]) if args else
              conn.execute("SELECT max(period_end) FROM call_report_snapshots").fetchone()[0])
    quarter = f"Q{(period.month - 1) // 3 + 1} {period.year}"
    banks, missing, new_actions = load(conn, period)
    prev = risk_tests.prior_counts(banks)
    ranked = risk_tests.run(banks)
    data = risk_tests.build(ranked, quarter, missing, new_actions, prev)

    def hits(label):
        return sum(1 for b in ranked if any(x.startswith(label + ":") for x in b.screens + b.trends))

    scored = [b for b in ranked if b.score is not None]
    subject = (f"Call Report risk tests {quarter}: {len(scored)} banks ranked, {hits('Below well capitalized')} "
               f"below well capitalized, {hits('Deposit outflow 10%+')} deposit outflows")
    lines = [f"Call Report risk tests {quarter}", "",
             f"Banks: {len(ranked)} ({len(scored)} scored, {len(ranked) - len(scored)} specialty)",
             f"Filed last quarter, not yet this quarter: {len(missing)}", ""]
    lines += [f"{label}: {hits(label)}" for label, _r, _t in risk_tests.SCREENS + risk_tests.TRENDS]
    lines += ["", "Top 10 by composite score:"]
    lines += [f"  {b.rank}. {b.name} ({b.state}) {b.score:.0f}" + (f" [{', '.join(b.lists)}]" if b.lists else "")
              for b in scored[:10]]
    text = "\n".join(lines)
    print(subject)
    print(text)
    fname = f"Call-Report-Risk-Tests-{period.year}Q{(period.month - 1) // 3 + 1}.xlsx"
    if out:
        Path(out).write_bytes(data)
        print(f"Wrote {out} ({len(data):,} bytes)")
        return
    email_report.send_email(EmailConfig.from_env(), subject, text,
                            html_body="<div style='font-family:Arial,sans-serif;font-size:13px;'>"
                                      + html.escape(text).replace("\n", "<br>") + "</div>",
                            attachments=[(fname, data, "vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
    print(f"Sent {fname} ({len(data):,} bytes)")


if __name__ == "__main__":
    main(*sys.argv[1:])
