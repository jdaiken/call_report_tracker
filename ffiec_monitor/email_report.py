"""Build and send the daily email -- spec §7 (structure) and §8 (delivery).

Spec §7 describes one email, four ordered sections, each omitted entirely when
empty. In practice this pipeline runs once daily (spec §2), so "Tier 1 sends
immediately" and "Tier 2 sends once per day" both resolve to: everything that
fired *in this run* goes out together, in one message, in the order below --
which is also exactly what keeps a bad day across many banks from turning into
dozens of separate emails (spec §8).
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import date
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


def _restatement_line(evt: RestatementEvent) -> str:
    return (f"{evt.meta.name} ({evt.meta.city}, {evt.meta.state}) -- "
            f"{_quarter_label(evt.period_end)} Call Report amended, no material capital change")


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

    if not sections:
        return None

    header = f"FFIEC Call Report Monitor — Daily Run: {run_date.isoformat()}\n"
    return header + "\n\n" + "\n\n---\n\n".join(sections)


def send_email(config: EmailConfig, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = config.address
    msg["To"] = config.recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(msg)
