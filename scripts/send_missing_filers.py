"""Banks that filed a Call Report last quarter but have not filed this quarter yet, emailed as a
table + a formatted Excel workbook. Read-only.

Who filed comes straight from FFIEC (RetrieveFilersSubmissionDateTime for each quarter), not from
the monitor's stored snapshots, so a missed or capped pull can't make a bank look late. Names
come from last quarter's FFIEC panel of reporters; total assets from the monitor's stored
snapshot when it has one. Each bank is checked against FDIC BankFind: no longer active there
usually means it merged or closed, rather than being late.

  python scripts/send_missing_filers.py [--no-send] [--out PATH]
"""
from __future__ import annotations

import html
import io
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ffiec_monitor import db, email_report
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient
from ffiec_monitor.filings_workbook import HEAD_FILL, LINK_FONT

log = logging.getLogger("missing")

LATE, GONE, UNINSURED, UNKNOWN = "Late (still active)", "No longer active", "Not FDIC-insured", "FDIC status unknown"
GROUP_ORDER = [LATE, UNKNOWN, GONE, UNINSURED]
LATE_FILL = PatternFill("solid", fgColor="FDECEC")
GROUP_FILL = PatternFill("solid", fgColor="E8EDF4")
QUIET = Font(color="6B6B6B")
BANKFIND_URL = "https://banks.data.fdic.gov/bankfind-suite/bankfind/details/{cert}"


def _quarter(p: date) -> str:
    return f"Q{(p.month - 1) // 3 + 1} {p.year}"


def _parse_day(text: str) -> Optional[date]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def fdic_status(certs: list[str]) -> dict[str, tuple[bool, Optional[date]]]:
    """FDIC cert -> (active, date it became inactive); {} if BankFind is unreachable."""
    out: dict[str, tuple[bool, Optional[date]]] = {}
    try:
        for i in range(0, len(certs), 50):
            chunk = certs[i:i + 50]
            r = requests.get("https://api.fdic.gov/banks/institutions", timeout=120, params={
                "filters": "CERT:(" + " OR ".join(chunk) + ")", "fields": "CERT,ACTIVE,ENDEFYMD",
                "limit": 100, "format": "json"})
            r.raise_for_status()
            for d in r.json().get("data", []):
                x = d.get("data", d)
                active = str(x.get("ACTIVE")) == "1"
                end = _parse_day(str(x.get("ENDEFYMD") or ""))
                out[str(x.get("CERT", "")).lstrip("0")] = (active, None if active or (end and end.year >= 9999) else end)
    except Exception as e:
        log.warning("FDIC BankFind lookup failed: %s", e)
        return {}
    return out


def build_workbook(rows: list[dict], prev: date, cur: date, intro: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Not Yet Filed"
    heads = ["Status", "Institution", "City", "State", "RSSD", "FDIC cert", f"Total assets {_quarter(prev)} ($M)",
             "Inactive since", f"{_quarter(prev)} filed", f"{_quarter(prev)} Call Report", "FDIC BankFind"]
    widths = [22, 44, 18, 7, 11, 10, 16, 14, 13, 16, 14]
    ws["A1"] = f"Call Reports not yet filed for {_quarter(cur)}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = intro
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(heads))
    ws.row_dimensions[2].height = 48
    counts = {g: sum(1 for r in rows if r["group"] == g) for g in GROUP_ORDER}
    ws["A3"] = "   ".join(f"{g}: {n}" for g, n in counts.items() if n)
    ws["A3"].font = Font(bold=True)

    head_row = 5
    for col, h in enumerate(heads, 1):
        c = ws.cell(row=head_row, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[head_row].height = 32
    row = head_row + 1
    for r in rows:
        values = [r["group"], r["name"], r["city"], r["state"], r["rssd"], r["cert"] or None,
                  r["assets"] / 1000 if r["assets"] else None, r["inactive"], r["last"].date() if r["last"] else None]
        for col, v in enumerate(values, 1):
            ws.cell(row=row, column=col, value=v)
        ws.cell(row=row, column=7).number_format = '#,##0.0'
        ws.cell(row=row, column=8).number_format = "mmm d, yyyy"
        ws.cell(row=row, column=9).number_format = "mmm d, yyyy"
        link = ws.cell(row=row, column=10, value="Call Report")
        link.hyperlink, link.font = email_report.cdr_report_url(r["rssd"], prev), LINK_FONT
        if r["cert"] and r["cert"] != "0":
            bf = ws.cell(row=row, column=11, value="BankFind")
            bf.hyperlink, bf.font = BANKFIND_URL.format(cert=r["cert"]), LINK_FONT
        for col in range(1, len(heads) + 1):
            cell = ws.cell(row=row, column=col)
            if r["group"] == LATE:
                cell.fill = LATE_FILL
                if col <= 2:
                    cell.font = Font(bold=True)
            elif col <= 9 and r["group"] in (GONE, UNINSURED):
                cell.font = QUIET
        row += 1
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=head_row + 1, column=3)
    ws.auto_filter.ref = f"A{head_row}:{get_column_letter(len(heads))}{max(row - 1, head_row)}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{head_row}:{head_row}"

    notes = wb.create_sheet("Notes")
    for i, line in enumerate([
        "How this list is built",
        f"Filers: FFIEC's own lists of institutions that submitted a {_quarter(prev)} and a {_quarter(cur)} Call Report "
        "(Central Data Repository web service), compared bank by bank.",
        f"Names, city, state and FDIC cert: FFIEC panel of reporters for {_quarter(prev)}.",
        f"Total assets: the {_quarter(prev)} Call Report as stored by the monitor (blank if it never stored one).",
        "Status: FDIC BankFind. 'Late (still active)' = an active FDIC-insured bank with no filing past the deadline. "
        "'No longer active' = FDIC shows it inactive, usually merged into another bank or closed, so no filing is "
        "expected. 'Not FDIC-insured' = no FDIC cert (e.g. some trust companies).",
        "Deadlines: 30 days after quarter end; 35 days for banks with foreign offices.",
    ], 1):
        c = notes.cell(row=i, column=1, value=line)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if i == 1:
            c.font = Font(bold=True, size=13)
    notes.column_dimensions["A"].width = 110

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main(*argv: str) -> None:
    logging.basicConfig(level=logging.INFO)
    client = FFIECClient(FFIECConfig.from_env())
    periods = sorted(client.retrieve_reporting_periods())
    cur, prev = periods[-1], periods[-2]
    filed = {p: {f.id_rssd: f.submission_ts for f in client.retrieve_filer_statuses(
        p, datetime(p.year, p.month, p.day))} for p in (prev, cur)}
    missing = sorted(set(filed[prev]) - set(filed[cur]))
    panel = {str(e.get("ID_RSSD")): e for e in client.retrieve_panel_of_reporters(prev)}
    log.info("%s filers: %d; %s filers so far: %d; missing: %d", _quarter(prev), len(filed[prev]),
             _quarter(cur), len(filed[cur]), len(missing))

    assets: dict[str, float] = {}
    if Path(get_duckdb_path()).exists():
        conn = db.connect(get_duckdb_path())
        for rssd, raw in conn.execute("SELECT id_rssd, ratios FROM call_report_snapshots WHERE period_end = ?",
                                      [prev]).fetchall():
            a = json.loads(raw).get("total_assets")
            if isinstance(a, (int, float)):
                assets[rssd] = a

    rows = []
    for rssd in missing:
        p = panel.get(rssd, {})
        rows.append({"name": str(p.get("Name") or f"RSSD {rssd}").strip(), "city": str(p.get("City") or "").strip(),
                     "state": str(p.get("State") or "").strip(), "rssd": rssd,
                     "cert": str(p.get("FDICCertNumber") or "").strip().lstrip("0"),
                     "assets": assets.get(rssd), "last": filed[prev][rssd]})
    status = fdic_status([r["cert"] for r in rows if r["cert"] and r["cert"] != "0"])
    for r in rows:
        r["inactive"] = None
        if r["cert"] in ("", "0"):
            r["group"] = UNINSURED
        elif r["cert"] not in status:
            r["group"] = UNKNOWN
        else:
            active, r["inactive"] = status[r["cert"]]
            r["group"] = LATE if active else GONE
    rows.sort(key=lambda r: (GROUP_ORDER.index(r["group"]), -(r["assets"] or 0), r["name"]))
    late = [r for r in rows if r["group"] == LATE]

    subject = (f"Call Reports not yet filed for {_quarter(cur)}: {len(late)} late, "
               f"{len(rows) - len(late)} merged/closed or other ({len(rows)} {_quarter(prev)} filers)")
    intro = (f"{len(rows)} institutions filed a {_quarter(prev)} Call Report but have no {_quarter(cur)} filing as of "
             f"{date.today():%b %-d, %Y} ({len(filed[cur]):,} of {len(filed[prev]):,} {_quarter(prev)} filers have filed). "
             f"{len(late)} {'is' if len(late) == 1 else 'are'} still active per FDIC BankFind, so "
             f"{'its filing is' if len(late) == 1 else 'their filings are'} late; the rest are no longer active "
             f"(usually merged or closed) or are not FDIC-insured. Deadlines: 30 days after quarter end, 35 for banks "
             f"with foreign offices.")

    def money(r):
        return f"${r['assets'] / 1000:,.1f}M" if r["assets"] else ""

    text = "\n".join([subject, "", intro, ""] + [
        f"{r['group']}: {r['name']} ({r['city']}, {r['state']}) RSSD {r['rssd']} {money(r)}"
        + (f", inactive since {r['inactive']:%b %-d, %Y}" if r["inactive"] else "") for r in rows])
    cells = "".join(
        f"<tr style='background:{'#FDECEC' if r['group'] == LATE else '#fff'}'>" + "".join(
            f"<td style='padding:3px 8px;border-bottom:1px solid #ddd'>{html.escape(str(v))}</td>" for v in (
                r["group"], r["name"], r["city"], r["state"], r["rssd"], money(r),
                f"{r['inactive']:%b %-d, %Y}" if r["inactive"] else "")) + "</tr>" for r in rows)
    body = (f"<div style='font-family:Arial,sans-serif;font-size:13px'><p>{html.escape(intro)}</p>"
            "<p>The attached workbook has the same list with Call Report and FDIC BankFind links.</p>"
            "<table style='border-collapse:collapse;font-size:12px'><tr>"
            + "".join(f"<th style='text-align:left;padding:3px 8px;background:#1F3A5F;color:#fff'>{h}</th>" for h in (
                "Status", "Institution", "City", "State", "RSSD", f"Assets {_quarter(prev)}", "Inactive since"))
            + f"</tr>{cells}</table></div>")
    data = build_workbook(rows, prev, cur, intro)
    print(text)
    fname = f"Call-Report-Not-Filed-{cur.year}Q{(cur.month - 1) // 3 + 1}-{date.today().isoformat()}.xlsx"
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_bytes(data)
        print(f"Wrote {fname}")
    if "--no-send" in argv or "--out" in argv:
        return
    email_report.send_email(EmailConfig.from_env(), subject, text, html_body=body,
                            attachments=[(fname, data, "vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
    print(f"Sent {fname}")


if __name__ == "__main__":
    main(*sys.argv[1:])
