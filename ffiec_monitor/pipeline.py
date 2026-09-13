"""Daily run orchestration -- spec §2 (two-tier check) tying together the client,
storage, ratio diffing, watch list, alert classification, and email modules.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional

import duckdb

from . import alerts, db, email_report, ratios, watch_list
from .config import EmailConfig
from .ffiec_client import FFIECAuthError, FFIECClient

logger = logging.getLogger(__name__)

# How many most-recent reporting periods to ping -- catches on-time filers for the
# current quarter as well as stragglers still filing the prior one.
PING_PERIOD_LOOKBACK = 2
# Prior consecutive quarters of ROAA history to pull for the sustained-low check.
ROAA_HISTORY_QUARTERS = 2


def run_daily(
    conn: duckdb.DuckDBPyConnection,
    client: FFIECClient,
    email_config: Optional[EmailConfig],
    universe: Optional[set[str]] = None,
    run_date: Optional[date] = None,
    send: bool = True,
) -> str:
    """Runs one full daily cycle. Returns the email body that was (or would have
    been) sent, or a short "nothing to report" message.

    `universe`, when given, restricts which institutions get the expensive full
    pull + diff step (spec §10.1's small-list testing phase). The cheap ping
    always covers whatever the ping endpoint returns.
    """
    run_date = run_date or date.today()

    try:
        periods = sorted(client.retrieve_reporting_periods())[-PING_PERIOD_LOOKBACK:]
    except FFIECAuthError:
        raise  # spec §1: hard failure, must surface as an alert, never a silent skip

    changed: list[tuple[str, date, datetime]] = []
    for period_end in periods:
        for status in client.retrieve_filer_statuses(period_end):
            if universe is not None and status.id_rssd not in universe:
                continue
            last_ts = db.get_last_seen_ts(conn, status.id_rssd)
            existing = db.get_snapshot(conn, status.id_rssd, status.period_end)
            if existing is None or existing.last_seen_ts != status.submission_ts:
                changed.append((status.id_rssd, status.period_end, status.submission_ts))

    watch_list_changes: list[email_report.WatchListChangeEvent] = []
    tier1_events: list[email_report.Tier1Event] = []
    tier2_events: list[email_report.Tier2Event] = []

    for id_rssd, period_end, submission_ts in changed:
        _process_one(conn, client, id_rssd, period_end, submission_ts, run_date,
                      watch_list_changes, tier1_events, tier2_events)

    snapshot_entries = _build_watch_list_snapshot(conn)

    body = email_report.build_email_body(
        watch_list_changes, tier1_events, snapshot_entries, tier2_events, run_date,
    )
    if body is None:
        return "Nothing to report today."

    if send and email_config is not None:
        subject = _subject_for(watch_list_changes, tier1_events)
        email_report.send_email(email_config, subject, body)

    return body


def _process_one(
    conn: duckdb.DuckDBPyConnection,
    client: FFIECClient,
    id_rssd: str,
    period_end: date,
    submission_ts: datetime,
    run_date: date,
    watch_list_changes: list,
    tier1_events: list,
    tier2_events: list,
) -> None:
    is_restatement = db.get_snapshot(conn, id_rssd, period_end) is not None

    facsimile = client.retrieve_facsimile(id_rssd, period_end)
    ubpr = client.retrieve_ubpr_facsimile(id_rssd, period_end)
    new_ratios = ratios.extract_ratios(ubpr)
    new_ratios["total_assets"] = ratios.extract_total_assets(facsimile)

    meta = _meta_from_facsimile(id_rssd, period_end, facsimile)
    db.upsert_institution_meta(conn, meta)

    if is_restatement:
        stored = db.get_snapshot(conn, id_rssd, period_end)
        triggers = alerts.classify_restatement(stored.ratios, new_ratios)
        if not triggers:
            db.log_alert(conn, id_rssd, period_end, "TIER3", "RESTATEMENT_SUB_FLOOR",
                         {"note": "restatement with no capital ratio down >50bp"})
    else:
        prior_snapshot = _latest_prior_snapshot(conn, id_rssd, period_end)
        prior_ratios = prior_snapshot.ratios if prior_snapshot else {}
        roaa_history = _roaa_history(conn, id_rssd, period_end)
        triggers = alerts.classify_new_period(
            prior_ratios, new_ratios, roaa_history,
            prior_ratios.get("total_assets"), new_ratios.get("total_assets"),
        )
        if not triggers:
            db.log_alert(conn, id_rssd, period_end, "TIER3", "NEW_PERIOD_NO_TRIGGER", {})

    active_orders = db.get_active_order_types(conn, id_rssd)
    wl_change = watch_list.recompute_membership(conn, id_rssd, new_ratios, active_orders, run_date)
    on_watch_list = db.get_watch_list_entry(conn, id_rssd) is not None

    if wl_change is not None:
        watch_list_changes.append(email_report.WatchListChangeEvent(meta, wl_change))

    triggers = alerts.escalate_for_watch_list(triggers, on_watch_list)

    tier1 = [t for t in triggers if t.tier == alerts.TIER1]
    tier2 = [t for t in triggers if t.tier == alerts.TIER2]

    for t in tier1 + tier2:
        db.log_alert(conn, id_rssd, period_end, t.tier, t.trigger_type, {"description": t.description})

    if tier1:
        full_snapshot = ratios.full_snapshot_rows(new_ratios) if on_watch_list else None
        tier1_events.append(email_report.Tier1Event(meta, period_end, tier1, on_watch_list, full_snapshot))
    if tier2:
        tier2_events.append(email_report.Tier2Event(meta, tier2))

    db.upsert_snapshot(conn, db.Snapshot(id_rssd, period_end, submission_ts, new_ratios, datetime.now(timezone.utc)))


def _latest_prior_snapshot(conn, id_rssd: str, before: date):
    row = conn.execute(
        "SELECT id_rssd, period_end, last_seen_ts, ratios, checked_at FROM call_report_snapshots "
        "WHERE id_rssd = ? AND period_end < ? ORDER BY period_end DESC LIMIT 1",
        [id_rssd, before],
    ).fetchone()
    if row is None:
        return None
    return db.Snapshot(row[0], row[1], row[2], json.loads(row[3]), row[4])


def _roaa_history(conn, id_rssd: str, before: date) -> list[Optional[float]]:
    rows = conn.execute(
        "SELECT ratios FROM call_report_snapshots WHERE id_rssd = ? AND period_end < ? "
        "ORDER BY period_end DESC LIMIT ?",
        [id_rssd, before, ROAA_HISTORY_QUARTERS],
    ).fetchall()
    values = [json.loads(r[0]).get("roaa") for r in rows]
    return list(reversed(values))


def _meta_from_facsimile(id_rssd: str, period_end: date, facsimile: dict) -> db.InstitutionMeta:
    info = facsimile.get("institution", facsimile)
    return db.InstitutionMeta(
        id_rssd=id_rssd,
        name=info.get("institutionName", f"RSSD {id_rssd}"),
        city=info.get("city", ""),
        state=info.get("state", ""),
        fdic_cert=info.get("fdicCertNumber", ""),
        total_assets=ratios.extract_total_assets(facsimile),
        as_of_period=period_end,
    )


def _build_watch_list_snapshot(conn) -> list[email_report.WatchListSnapshotEntry]:
    entries = []
    for wl in db.get_active_watch_list(conn):
        snap = db.get_latest_snapshot(conn, wl.id_rssd)
        meta = db.get_institution_meta(conn, wl.id_rssd)
        if snap is None or meta is None:
            continue
        entries.append(email_report.WatchListSnapshotEntry(meta, ratios.full_snapshot_rows(snap.ratios)))
    return entries


def _subject_for(watch_list_changes: list, tier1_events: list) -> str:
    if tier1_events:
        return f"[FFIEC Monitor] Tier 1 alert -- {len(tier1_events)} institution(s)"
    if watch_list_changes:
        return "[FFIEC Monitor] Watch List changed"
    return "[FFIEC Monitor] Daily digest"
