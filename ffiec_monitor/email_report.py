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
    ratios: Optional[dict] = None   # the filing's ratios, for the level screen (ffiec_monitor/screen.py)


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
) -> Optional[str]:
    """Returns the full email body, or None if there is nothing to report at all."""
    sections: list[str] = []

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


def compose(
    watch_list_changes: list[WatchListChangeEvent],
    tier1_events: list[Tier1Event],
    watch_list_snapshot: list[WatchListSnapshotEntry],
    tier2_events: list[Tier2Event],
    restatement_events: list[RestatementEvent],
    new_filing_events: list[NewFilingEvent],
    run_date: date,
    analytics=None,
):
    """(Report, html, pdf bytes) for one day's events: the designed email body and the
    same report as a PDF (ffiec_monitor/report.py, pdf_report.py). Builds the summary
    paragraph once (one Claude call) and shares it between the two."""
    from . import pdf_report, report
    rep = report.build_report(watch_list_changes, tier1_events, watch_list_snapshot, tier2_events,
                              restatement_events, new_filing_events, run_date, analytics)
    return rep, report.render_html(rep), pdf_report.render_pdf(rep)


def build_email_html(
    watch_list_changes: list[WatchListChangeEvent],
    tier1_events: list[Tier1Event],
    watch_list_snapshot: list[WatchListSnapshotEntry],
    tier2_events: list[Tier2Event],
    restatement_events: list[RestatementEvent],
    new_filing_events: list[NewFilingEvent],
    run_date: date,
) -> Optional[str]:
    """HTML counterpart to build_email_body -- same sections and ordering; None when
    build_email_body has nothing to report."""
    if build_email_body(watch_list_changes, tier1_events, watch_list_snapshot, tier2_events,
                        restatement_events, new_filing_events, run_date) is None:
        return None
    return compose(watch_list_changes, tier1_events, watch_list_snapshot, tier2_events,
                   restatement_events, new_filing_events, run_date)[1]


def events_csv(restatement_events: list[RestatementEvent], new_filing_events: list[NewFilingEvent]) -> str:
    """Institution/period changes (refilings + new filings) as CSV -- Excel/
    Sheets open this directly, and it sorts/filters the way a table in an
    email body never can. Per user request 2026-09-18."""
    out = io.StringIO()
    writer = csv.writer(out)
    from .screen import screen
    writer.writerow(["Type", "Institution", "City", "State", "RSSD", "FDIC Cert", "Period", "Report URL", "Flags"])
    for kind, events in (("Refiling", restatement_events), ("New Filing", new_filing_events)):
        for evt in events:
            m = evt.meta
            flags = screen(getattr(evt, "ratios", None)) if kind == "New Filing" else []
            writer.writerow([kind, m.name, m.city, m.state, m.id_rssd, m.fdic_cert or "",
                              _quarter_label(evt.period_end), cdr_report_url(m.id_rssd, evt.period_end),
                              "; ".join(f"{f.level.upper()}: {f.text}" for f in flags)])
    return out.getvalue()


def send_email(
    config: EmailConfig, subject: str, body: str, html_body: Optional[str] = None,
    attachment_filename: Optional[str] = None, attachment_content: Optional[str] = None,
    attachments: Optional[list[tuple[str, bytes, str]]] = None,
    images: Optional[dict[str, bytes]] = None,
) -> None:
    """`attachments` = extra (filename, bytes, mime subtype) files, e.g. the PDF.
    `images` = {content-id: PNG} shown inline in the HTML via src="cid:<id>"."""
    if html_body is not None:
        alt = MIMEMultipart("alternative")
        # Plain text first: email clients render the LAST alternative part they
        # support, and most that support HTML prefer it -- this order gives
        # HTML-capable clients the table/link version, others the plain digest.
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        content: MIMEMultipart | MIMEText = alt
        if images:
            from email.mime.image import MIMEImage
            related = MIMEMultipart("related")
            related.attach(alt)
            for cid, png in images.items():
                img = MIMEImage(png, "png")
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
                related.attach(img)
            content = related
    else:
        content = MIMEText(body, "plain")

    files = list(attachments or [])
    if attachment_content is not None:
        files.insert(0, (attachment_filename, attachment_content.encode("utf-8"), "octet-stream"))
    if files:
        msg = MIMEMultipart("mixed")
        msg.attach(content)
        for name, data, subtype in files:
            part = MIMEApplication(data, _subtype=subtype, Name=name)
            part["Content-Disposition"] = f'attachment; filename="{name}"'
            msg.attach(part)
    else:
        msg = content

    msg["Subject"] = subject
    msg["From"] = config.address
    msg["To"] = config.recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(msg)
