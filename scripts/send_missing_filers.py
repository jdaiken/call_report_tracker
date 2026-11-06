"""Banks that filed a Call Report last quarter but have not filed this quarter yet, emailed as a
table (+ CSV). Read-only.

Who filed comes straight from FFIEC (RetrieveFilersSubmissionDateTime for each quarter), not from
the monitor's stored snapshots, so a missed or capped pull can't make a bank look late. Names
come from last quarter's FFIEC panel of reporters; total assets from the monitor's stored
snapshot when it has one. Each bank is checked against FDIC BankFind: no longer active there
usually means it merged or closed, rather than being late.

  python scripts/send_missing_filers.py [--no-send]
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ffiec_monitor import db, email_report
from ffiec_monitor.config import EmailConfig, FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient

log = logging.getLogger("missing")


def _quarter(p: date) -> str:
    return f"Q{(p.month - 1) // 3 + 1} {p.year}"


def fdic_status(certs: list[str]) -> dict[str, str]:
    """FDIC cert -> "Active" / "Inactive since YYYY-MM-DD"; {} if BankFind is unreachable."""
    out: dict[str, str] = {}
    try:
        for i in range(0, len(certs), 50):
            chunk = certs[i:i + 50]
            r = requests.get("https://api.fdic.gov/banks/institutions", timeout=120, params={
                "filters": "CERT:(" + " OR ".join(chunk) + ")", "fields": "CERT,ACTIVE,ENDEFYMD",
                "limit": 100, "format": "json"})
            r.raise_for_status()
            for d in r.json().get("data", []):
                x = d.get("data", d)
                cert = str(x.get("CERT", "")).lstrip("0")
                end = str(x.get("ENDEFYMD") or "")
                out[cert] = "Active" if str(x.get("ACTIVE")) == "1" else (
                    f"Inactive since {end[:10]}" if end and not end.startswith("9999") else "Inactive")
    except Exception as e:
        log.warning("FDIC BankFind lookup failed: %s", e)
        return {}
    return out


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
        r["status"] = status.get(r["cert"], "Not an FDIC-insured bank" if r["cert"] in ("", "0") else
                                 ("Not found" if status else "Unknown (FDIC lookup failed)"))
    rows.sort(key=lambda r: (r["status"] != "Active", -(r["assets"] or 0), r["name"]))
    active = [r for r in rows if r["status"] == "Active"]

    subject = (f"Call Reports not yet filed for {_quarter(cur)}: {len(rows)} banks that filed {_quarter(prev)} "
               f"({len(active)} still active)")
    intro = (f"{len(rows)} institutions filed a {_quarter(prev)} Call Report but have no {_quarter(cur)} filing "
             f"as of {date.today():%b %-d, %Y} ({len(filed[cur])} of {len(filed[prev])} {_quarter(prev)} filers have "
             f"filed). {len(active)} are still active per FDIC BankFind, so their filing is late; the rest are no "
             f"longer active (usually merged or closed) or are not FDIC-insured. Deadlines: 30 days after quarter "
             f"end, 35 for banks with foreign offices.")
    heads = ["Institution", "City", "State", "RSSD", "FDIC cert", f"Total assets {_quarter(prev)} ($M)",
             "FDIC status", f"{_quarter(prev)} filed"]
    lines = [subject, "", intro, ""]
    lines += [f"{r['name']} ({r['city']}, {r['state']}) RSSD {r['rssd']} -- {r['status']}" for r in rows]
    text = "\n".join(lines)
    cells = "".join(
        "<tr>" + "".join(f"<td style='padding:3px 8px;border-bottom:1px solid #ddd'>{html.escape(str(v))}</td>" for v in (
            r["name"], r["city"], r["state"], r["rssd"], r["cert"],
            f"{r['assets'] / 1000:,.1f}" if r["assets"] else "", r["status"],
            f"{r['last']:%Y-%m-%d}" if r["last"] else "")) + "</tr>" for r in rows)
    body = (f"<div style='font-family:Arial,sans-serif;font-size:13px'><p>{html.escape(intro)}</p>"
            "<table style='border-collapse:collapse;font-size:12px'><tr>"
            + "".join(f"<th style='text-align:left;padding:3px 8px;background:#1F3A5F;color:#fff'>{h}</th>" for h in heads)
            + f"</tr>{cells}</table></div>")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(heads)
    for r in rows:
        w.writerow([r["name"], r["city"], r["state"], r["rssd"], r["cert"],
                    round(r["assets"] / 1000, 1) if r["assets"] else "", r["status"],
                    f"{r['last']:%Y-%m-%d}" if r["last"] else ""])
    print(text)
    if "--no-send" in argv:
        return
    fname = f"Call-Report-Not-Filed-{cur.year}Q{(cur.month - 1) // 3 + 1}-{date.today().isoformat()}.csv"
    email_report.send_email(EmailConfig.from_env(), subject, text, html_body=body,
                            attachments=[(fname, buf.getvalue().encode(), "csv")])
    print(f"Sent {fname}")


if __name__ == "__main__":
    main(*sys.argv[1:])
