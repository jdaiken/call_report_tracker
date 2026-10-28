"""The plain daily email: one table of the day's Call Report filings.

One row per filing: bank, location, quarter, new filing or refiling, when FFIEC shows it
submitted, a link to the Call Report, a link to the UBPR, and which of our monitored lists
the bank is on (Watch List, FDIC active order, Treasury Desk, risk ranking). No Claude
summary, no charts, no PDF -- per user request 2026-10-28.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import duckdb

from . import db, enforcement, treasury_desk
from .email_report import cdr_report_url
from .ubpr import ubpr_url


@dataclass
class Filing:
    id_rssd: str
    period_end: date
    refiling: bool
    submitted: Optional[datetime] = None


@dataclass
class Row:
    name: str
    location: str
    id_rssd: str
    quarter: str
    kind: str
    submitted: str
    call_report_url: str
    ubpr_url: str
    lists: list[str]


class ListChecker:
    """Which monitored lists a bank is on. Built once per email; local data only."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.watch = {r[0] for r in conn.execute(
            "SELECT id_rssd FROM watch_list WHERE removed_at IS NULL").fetchall()}
        self.ranked = {r[0] for r in conn.execute(
            "SELECT id_rssd FROM risk_rankings WHERE id_rssd IS NOT NULL").fetchall()}
        self.order_rssds = {r[0] for r in conn.execute(
            "SELECT id_rssd FROM enforcement_orders WHERE status = 'ACTIVE' AND id_rssd IS NOT NULL").fetchall()}
        try:
            self.order_certs = {a.cert for a in enforcement.load_fdic()[0] if a.cert}
        except Exception:
            self.order_certs = set()
        try:
            self.treasury = treasury_desk.load()
        except Exception:
            self.treasury = None

    def lists_for(self, meta: db.InstitutionMeta) -> list[str]:
        out = []
        if meta.id_rssd in self.watch:
            out.append("Watch List")
        cert = (meta.fdic_cert or "").strip()
        if meta.id_rssd in self.order_rssds or (cert and cert in self.order_certs):
            out.append("FDIC order")
        if self.treasury is not None and self.treasury.match(meta.name, meta.state):
            out.append("Treasury Desk")
        if meta.id_rssd in self.ranked:
            out.append("Risk ranking")
        return out


def _quarter(period_end: date) -> str:
    return f"Q{(period_end.month - 1) // 3 + 1} {period_end.year}"


def build_rows(conn: duckdb.DuckDBPyConnection, filings: list[Filing]) -> list[Row]:
    """Rows sorted with list matches first, then by bank name."""
    checker = ListChecker(conn)
    rows = []
    for f in filings:
        meta = db.get_institution_meta(conn, f.id_rssd) or db.InstitutionMeta(
            id_rssd=f.id_rssd, name=f"RSSD {f.id_rssd}", city="", state="", fdic_cert="",
            total_assets=None, as_of_period=f.period_end)
        rows.append(Row(
            name=meta.name.strip(),
            location=", ".join(p for p in (meta.city.strip(), meta.state.strip()) if p),
            id_rssd=f.id_rssd,
            quarter=_quarter(f.period_end),
            kind="Refiling" if f.refiling else "New filing",
            submitted=f"{f.submitted:%b %d %I:%M %p}" if f.submitted else "",
            call_report_url=cdr_report_url(f.id_rssd, f.period_end),
            ubpr_url=ubpr_url(f.id_rssd, f.period_end),
            lists=checker.lists_for(meta),
        ))
    rows.sort(key=lambda r: (not r.lists, r.name.upper()))
    return rows


def subject(rows: list[Row], label: str) -> str:
    hits = sum(1 for r in rows if r.lists)
    return f"Call Report filings {label}: {len(rows)} filed, {hits} on our lists"


def render_text(rows: list[Row], label: str) -> str:
    lines = [f"Call Report filings {label}", ""]
    if not rows:
        return "\n".join(lines + ["No new Call Report filings."])
    for r in rows:
        on = ", ".join(r.lists) if r.lists else "No"
        lines.append(f"{r.name} ({r.location}) | {r.quarter} {r.kind} | On our lists: {on}")
        lines.append(f"    Call Report: {r.call_report_url}")
        lines.append(f"    UBPR: {r.ubpr_url}")
    return "\n".join(lines)


_TH = "text-align:left;padding:6px 10px;border-bottom:2px solid #999;background:#f2f2f2;"
_TD = "padding:6px 10px;border-bottom:1px solid #ddd;vertical-align:top;"


def render_html(rows: list[Row], label: str) -> str:
    e = html.escape
    head = f"<p style='font-size:15px;margin:0 0 10px 0;'><b>Call Report filings {e(label)}</b></p>"
    if not rows:
        return f"<div style='font-family:Arial,sans-serif;font-size:13px;'>{head}<p>No new Call Report filings.</p></div>"
    cols = ["Bank", "Location", "Quarter", "Type", "Submitted", "Call Report", "UBPR", "On our lists?"]
    out = [f"<th style='{_TH}'>{c}</th>" for c in cols]
    body = []
    for r in rows:
        on = (f"<b style='color:#b00020;'>Yes: {e(', '.join(r.lists))}</b>" if r.lists else "No")
        bg = "background:#fff4f4;" if r.lists else ""
        cells = [e(r.name), e(r.location), e(r.quarter), e(r.kind), e(r.submitted),
                 f"<a href='{e(r.call_report_url)}'>Call Report</a>",
                 f"<a href='{e(r.ubpr_url)}'>UBPR</a>", on]
        body.append("<tr>" + "".join(f"<td style='{_TD}{bg}'>{c}</td>" for c in cells) + "</tr>")
    return (f"<div style='font-family:Arial,sans-serif;font-size:13px;color:#111;'>{head}"
            f"<table style='border-collapse:collapse;'><tr>{''.join(out)}</tr>{''.join(body)}</table></div>")


def filings_submitted_between(conn: duckdb.DuckDBPyConnection, t0: datetime, t1: datetime) -> list[Filing]:
    """Filings whose FFIEC submission time (Eastern, as FFIEC reports it) falls in [t0, t1),
    from what the daily monitor has already stored -- no FFIEC API call."""
    rows = conn.execute(
        "SELECT s.id_rssd, s.period_end, s.last_seen_ts, "
        "  EXISTS (SELECT 1 FROM alert_log a WHERE a.id_rssd = s.id_rssd AND a.period_end = s.period_end "
        "          AND a.trigger_type IN ('RESTATEMENT_SUB_FLOOR', 'CAPITAL_RATIO_RESTATED_DOWN')) "
        "FROM call_report_snapshots s WHERE s.last_seen_ts >= ? AND s.last_seen_ts < ? "
        "ORDER BY s.last_seen_ts", [t0, t1]).fetchall()
    return [Filing(r[0], r[1], bool(r[3]), r[2]) for r in rows]
