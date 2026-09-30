"""Build and send the daily email -- spec §7 (structure) and §8 (delivery).

Spec §7 describes one email, four ordered sections, each omitted entirely when
empty. In practice this pipeline runs once daily (spec §2), so "Tier 1 sends
immediately" and "Tier 2 sends once per day" both resolve to: everything that
fired *in this run* goes out together, in one message, in the order below --
which is also exactly what keeps a bad day across many banks from turning into
dozens of separate emails (spec §8).
"""
from __future__ import annotations

import csv
import html
import io
import smtplib
from dataclasses import dataclass
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .alerts import Trigger
from .config import EmailConfig
from .db import InstitutionMeta
from .ratios import RatioChange
from .watch_list import WatchListChange


@dataclass
class Tier1Event:
    meta: InstitutionMeta
    period_end: date
    triggers: list[Trigger]
    on_watch_list: bool
    full_snapshot: Optional[list[RatioChange]] = None  # populated only if on_watch_list


@dataclass
class Tier2Event:
    meta: InstitutionMeta
    triggers: list[Trigger]


@dataclass
class WatchListChangeEvent:
    meta: InstitutionMeta
    change: WatchListChange


@dataclass
class WatchListSnapshotEntry:
    meta: InstitutionMeta
    ratios: list[RatioChange]


@dataclass
class RestatementEvent:
    """A refiling of an already-seen period where no capital ratio moved >50bp
    (alerts.classify_restatement's Tier 3 case -- see pipeline.py's
    RESTATEMENT_SUB_FLOOR log). Real capital-ratio restatements are Tier 1 and
    go through Tier1Event instead; this is purely "something changed, here's a
    one-liner" for the rest, per user request 2026-09-18."""
    meta: InstitutionMeta
    period_end: date
    reason: str


@dataclass
class NewFilingEvent:
    """A genuinely new quarter (never seen before for this institution) that
    crossed no Tier 1/2 threshold -- alerts.classify_new_period's Tier 3 case
    (pipeline.py's NEW_PERIOD_NO_TRIGGER log). Per user request 2026-09-18:
    no longer silent -- every new filing is reported, threshold or not, since
    the user reviews the ones they care about themselves via the report link."""
    meta: InstitutionMeta
    period_end: date


@dataclass
class TreasuryDeskEvent:
    """Anything that happened this run to a bank on the Treasury Desk list
    (see treasury_desk.py) -- surfaced in its own section at the top of the
    email regardless of tier, per user request 2026-09-30. `what` is a one-line
    summary of the underlying event (Tier 1/2 trigger, refiling, new filing,
    Watch List change), which still also appears in its normal section."""
    meta: InstitutionMeta
    period_end: Optional[date]
    desk_name: str
    what: str


def _fmt_assets(total_assets: Optional[float]) -> str:
    if total_assets is None:
        return "n/a"
    # Call Report dollar figures are reported in thousands.
    dollars = total_assets * 1000
    if dollars >= 1e9:
        return f"${dollars / 1e9:.1f}B"
    return f"${dollars / 1e6:.1f}M"


def _identity_block(meta: InstitutionMeta) -> str:
    period = meta.as_of_period.isoformat() if meta.as_of_period else "n/a"
    return (
        f"{meta.name} — {meta.city}, {meta.state}\n"
        f"FDIC Cert #{meta.fdic_cert or 'n/a'} · RSSD {meta.id_rssd} · "
        f"Total Assets: {_fmt_assets(meta.total_assets)} (as of {period})"
    )


def _fmt_value(v: Optional[float]) -> str:
    return f"{v:.2f}%" if v is not None else "n/a"


def _ratio_table(changes: list[RatioChange]) -> str:
    lines = []
    for c in changes:
        if c.old_value is None:
            row = f"  {c.label}: {_fmt_value(c.new_value)}"
        else:
            row = f"  {c.label}: {_fmt_value(c.old_value)} -> {_fmt_value(c.new_value)}"
        if c.peer_value is not None:
            row += f"  (peer avg: {_fmt_value(c.peer_value)})"
        lines.append(row)
        if c.paired is not None:
            p = c.paired
            if p.old_value is None:
                lines.append(f"    Capital Growth: {_fmt_value(p.new_value)}")
            else:
                lines.append(f"    Capital Growth: {_fmt_value(p.old_value)} -> {_fmt_value(p.new_value)}")
    return "\n".join(lines)


def _quarter_label(period_end: date) -> str:
    quarter = (period_end.month - 1) // 3 + 1
    return f"Q{quarter} {period_end.year}"


def cdr_report_url(id_rssd: str, period_end: date) -> str:
    """FFIEC CDR's public facsimile viewer -- confirmed URL shape via a live
    example (https://cdr.ffiec.gov/public/ViewFacsimileDirect.aspx?date=09302015
    &ds=call&id=3719648&idtype=id_rssd), NOT verified end-to-end against a real
    RSSD/period from this codebase (cdr.ffiec.gov is unreachable from this dev
    environment's network) -- flag to the user if a real link ever 404s."""
    return (f"https://cdr.ffiec.gov/public/ViewFacsimileDirect.aspx"
            f"?date={period_end:%m%d%Y}&ds=call&id={id_rssd}&idtype=id_rssd")


def _restatement_line(evt: RestatementEvent) -> str:
    return (f"{evt.meta.name} ({evt.meta.city}, {evt.meta.state}) -- RSSD {evt.meta.id_rssd} · "
            f"FDIC Cert {evt.meta.fdic_cert or 'n/a'} -- "
            f"{_quarter_label(evt.period_end)} Call Report amended: {evt.reason}\n"
            f"    Report: {cdr_report_url(evt.meta.id_rssd, evt.period_end)}")


def _new_filing_line(evt: NewFilingEvent) -> str:
    return (f"{evt.meta.name} ({evt.meta.city}, {evt.meta.state}) -- RSSD {evt.meta.id_rssd} · "
            f"FDIC Cert {evt.meta.fdic_cert or 'n/a'} -- "
            f"{_quarter_label(evt.period_end)} Call Report filed\n"
            f"    Report: {cdr_report_url(evt.meta.id_rssd, evt.period_end)}")


def _treasury_desk_line(evt: TreasuryDeskEvent) -> str:
    period = f"{_quarter_label(evt.period_end)} -- " if evt.period_end else ""
    line = (f"{evt.meta.name} ({evt.meta.city}, {evt.meta.state}) -- RSSD {evt.meta.id_rssd} · "
            f"FDIC Cert {evt.meta.fdic_cert or 'n/a'} -- {period}{evt.what}")
    if evt.period_end:
        line += f"\n    Report: {cdr_report_url(evt.meta.id_rssd, evt.period_end)}"
    return line


def _watch_list_change_line(evt: WatchListChangeEvent) -> str:
    verb = "Added" if evt.change.change_type == "ADDED" else (
        "Removed" if evt.change.change_type == "REMOVED" else "Reason changed"
    )
    reason = evt.change.reason or evt.change.previous_reason or "n/a"
    return f"{verb}: {evt.meta.name} — {reason}"


def build_email_body(
    watch_list_changes: list[WatchListChangeEvent],
    tier1_events: list[Tier1Event],
    watch_list_snapshot: list[WatchListSnapshotEntry],
    tier2_events: list[Tier2Event],
    restatement_events: list[RestatementEvent],
    new_filing_events: list[NewFilingEvent],
    run_date: date,
    treasury_desk_events: Optional[list[TreasuryDeskEvent]] = None,
) -> Optional[str]:
    """Returns the full email body, or None if there is nothing to report at all."""
    sections: list[str] = []

    if treasury_desk_events:
        lines = [_treasury_desk_line(e) for e in treasury_desk_events]
        sections.append("TREASURY DESK\n\n" + "\n".join(lines))

    if watch_list_changes:
        lines = [_watch_list_change_line(e) for e in watch_list_changes]
        sections.append("WATCH LIST CHANGES\n" + "\n".join(lines))

    if tier1_events:
        blocks = []
        for evt in tier1_events:
            block = [_identity_block(evt.meta)]
            block.append("Triggers:")
            for t in evt.triggers:
                tag = " [escalated from Tier 2 -- Watch List member]" if t.escalated else ""
                block.append(f"  - {t.description}{tag}")
            fired_changes = [c for t in evt.triggers for c in t.ratio_changes]
            if fired_changes:
                block.append(_ratio_table(fired_changes))
            if evt.on_watch_list and evt.full_snapshot:
                block.append("\nFull current ratio snapshot (Watch List member):")
                block.append(_ratio_table(evt.full_snapshot))
            blocks.append("\n".join(block))
        sections.append("TIER 1 ALERTS\n\n" + "\n\n".join(blocks))

    if watch_list_snapshot:
        blocks = []
        for entry in watch_list_snapshot:
            blocks.append(_identity_block(entry.meta) + "\n" + _ratio_table(entry.ratios))
        sections.append("WATCH LIST — DAILY RATIO SNAPSHOT\n\n" + "\n\n".join(blocks))

    if tier2_events:
        lines = []
        for evt in tier2_events:
            lines.append(_identity_block(evt.meta))
            for t in evt.triggers:
                lines.append(f"  - {t.description}")
        sections.append("TIER 2 DIGEST\n\n" + "\n".join(lines))

    if restatement_events:
        lines = [_restatement_line(e) for e in restatement_events]
        sections.append("REFILINGS (no material capital change)\n\n" + "\n".join(lines))

    if new_filing_events:
        lines = [_new_filing_line(e) for e in new_filing_events]
        sections.append("NEW QUARTERLY FILINGS (no threshold trigger)\n\n" + "\n".join(lines))

    if not sections:
        return None

    header = f"FFIEC Call Report Monitor — Daily Run: {run_date.isoformat()}\n"
    return header + "\n\n" + "\n\n---\n\n".join(sections)


_HTML_STYLE = (
    "font-family: -apple-system, Arial, sans-serif; font-size: 13px; color: #1a1a1a;"
)
_TABLE_STYLE = "border-collapse: collapse; width: 100%; margin: 8px 0 16px 0;"
_TH_STYLE = "text-align: left; padding: 4px 10px; border-bottom: 2px solid #ccc; white-space: nowrap;"
_TD_STYLE = "padding: 4px 10px; border-bottom: 1px solid #eee; vertical-align: top;"


def _e(text: str) -> str:
    return html.escape(str(text))


def _events_table_html(events: list) -> str:
    """Shared table renderer for RestatementEvent and NewFilingEvent -- both
    expose .meta and .period_end and render identically (Institution, Location,
    RSSD, FDIC Cert, Period, Link), so one function covers both event types."""
    rows = []
    for evt in events:
        url = cdr_report_url(evt.meta.id_rssd, evt.period_end)
        rows.append(
            "<tr>"
            f'<td style="{_TD_STYLE}">{_e(evt.meta.name)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.city)}, {_e(evt.meta.state)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.id_rssd)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.fdic_cert or "n/a")}</td>'
            f'<td style="{_TD_STYLE}">{_e(_quarter_label(evt.period_end))}</td>'
            f'<td style="{_TD_STYLE}"><a href="{_e(url)}">Call Report</a></td>'
            "</tr>"
        )
    header = "".join(
        f'<th style="{_TH_STYLE}">{col}</th>'
        for col in ("Institution", "Location", "RSSD", "FDIC Cert", "Period", "Link")
    )
    return (
        f'<table style="{_TABLE_STYLE}"><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _treasury_desk_table_html(events: list[TreasuryDeskEvent]) -> str:
    rows = []
    for evt in events:
        period = _quarter_label(evt.period_end) if evt.period_end else ""
        link = (f'<a href="{_e(cdr_report_url(evt.meta.id_rssd, evt.period_end))}">Call Report</a>'
                if evt.period_end else "")
        rows.append(
            "<tr>"
            f'<td style="{_TD_STYLE}">{_e(evt.meta.name)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.city)}, {_e(evt.meta.state)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.id_rssd)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.meta.fdic_cert or "n/a")}</td>'
            f'<td style="{_TD_STYLE}">{_e(period)}</td>'
            f'<td style="{_TD_STYLE}">{_e(evt.what)}</td>'
            f'<td style="{_TD_STYLE}">{link}</td>'
            "</tr>"
        )
    header = "".join(
        f'<th style="{_TH_STYLE}">{col}</th>'
        for col in ("Institution", "Location", "RSSD", "FDIC Cert", "Period", "What happened", "Link")
    )
    return (
        f'<table style="{_TABLE_STYLE}"><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def build_email_html(
    watch_list_changes: list[WatchListChangeEvent],
    tier1_events: list[Tier1Event],
    watch_list_snapshot: list[WatchListSnapshotEntry],
    tier2_events: list[Tier2Event],
    restatement_events: list[RestatementEvent],
    new_filing_events: list[NewFilingEvent],
    run_date: date,
    treasury_desk_events: Optional[list[TreasuryDeskEvent]] = None,
) -> Optional[str]:
    """HTML counterpart to build_email_body -- same section set and ordering, so
    the two must be called with identical arguments (build_email_body's None
    check is the single source of truth for "is there anything to report").
    REFILINGS renders as a real <table> (the one section structured enough to
    warrant it); everything else stays a <pre> block of the plain-text
    rendering rather than a parallel implementation to keep in sync.
    """
    sections: list[str] = []

    if treasury_desk_events:
        sections.append("<h3>Treasury Desk</h3>" + _treasury_desk_table_html(treasury_desk_events))

    if watch_list_changes:
        lines = [_watch_list_change_line(e) for e in watch_list_changes]
        sections.append("<h3>Watch List Changes</h3><pre>" + _e("\n".join(lines)) + "</pre>")

    if tier1_events:
        blocks = []
        for evt in tier1_events:
            block = [_identity_block(evt.meta)]
            block.append("Triggers:")
            for t in evt.triggers:
                tag = " [escalated from Tier 2 -- Watch List member]" if t.escalated else ""
                block.append(f"  - {t.description}{tag}")
            fired_changes = [c for t in evt.triggers for c in t.ratio_changes]
            if fired_changes:
                block.append(_ratio_table(fired_changes))
            if evt.on_watch_list and evt.full_snapshot:
                block.append("\nFull current ratio snapshot (Watch List member):")
                block.append(_ratio_table(evt.full_snapshot))
            blocks.append("\n".join(block))
        sections.append("<h3>Tier 1 Alerts</h3><pre>" + _e("\n\n".join(blocks)) + "</pre>")

    if watch_list_snapshot:
        blocks = []
        for entry in watch_list_snapshot:
            blocks.append(_identity_block(entry.meta) + "\n" + _ratio_table(entry.ratios))
        sections.append("<h3>Watch List — Daily Ratio Snapshot</h3><pre>" + _e("\n\n".join(blocks)) + "</pre>")

    if tier2_events:
        lines = []
        for evt in tier2_events:
            lines.append(_identity_block(evt.meta))
            for t in evt.triggers:
                lines.append(f"  - {t.description}")
        sections.append("<h3>Tier 2 Digest</h3><pre>" + _e("\n".join(lines)) + "</pre>")

    if restatement_events:
        sections.append(
            "<h3>Refilings (no material capital change)</h3>"
            + _events_table_html(restatement_events)
        )

    if new_filing_events:
        sections.append(
            "<h3>New Quarterly Filings (no threshold trigger)</h3>"
            + _events_table_html(new_filing_events)
        )

    if not sections:
        return None

    header = f"<p><strong>FFIEC Call Report Monitor — Daily Run: {run_date.isoformat()}</strong></p>"
    body = header + "<hr>".join(sections)
    return f'<html><body style="{_HTML_STYLE}">{body}</body></html>'


def events_csv(restatement_events: list[RestatementEvent], new_filing_events: list[NewFilingEvent]) -> str:
    """Institution/period changes (refilings + new filings) as CSV -- Excel/
    Sheets open this directly, and it sorts/filters the way a table in an
    email body never can. Per user request 2026-09-18."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Type", "Institution", "City", "State", "RSSD", "FDIC Cert", "Period", "Report URL"])
    for kind, events in (("Refiling", restatement_events), ("New Filing", new_filing_events)):
        for evt in events:
            m = evt.meta
            writer.writerow([kind, m.name, m.city, m.state, m.id_rssd, m.fdic_cert or "",
                              _quarter_label(evt.period_end), cdr_report_url(m.id_rssd, evt.period_end)])
    return out.getvalue()


def send_email(
    config: EmailConfig, subject: str, body: str, html_body: Optional[str] = None,
    attachment_filename: Optional[str] = None, attachment_content: Optional[str] = None,
) -> None:
    if html_body is not None:
        alt = MIMEMultipart("alternative")
        # Plain text first: email clients render the LAST alternative part they
        # support, and most that support HTML prefer it -- this order gives
        # HTML-capable clients the table/link version, others the plain digest.
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        content: MIMEMultipart | MIMEText = alt
    else:
        content = MIMEText(body, "plain")

    if attachment_content is not None:
        msg = MIMEMultipart("mixed")
        msg.attach(content)
        part = MIMEApplication(attachment_content.encode("utf-8"), Name=attachment_filename)
        part["Content-Disposition"] = f'attachment; filename="{attachment_filename}"'
        msg.attach(part)
    else:
        msg = content

    msg["Subject"] = subject
    msg["From"] = config.address
    msg["To"] = config.recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(msg)
