"""Daily run orchestration -- spec §2 (two-tier check) tying together the client,
storage, ratio diffing, watch list, alert classification, and email modules.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import duckdb

from . import alerts, db, email_report, ratios, simple_email, watch_list
from .config import EmailConfig
from .ffiec_client import FFIECAuthError, FFIECClient, FFIECRateLimitError

logger = logging.getLogger(__name__)

# How many most-recent reporting periods to ping -- catches on-time filers for the
# current quarter as well as stragglers still filing the prior one.
PING_PERIOD_LOOKBACK = 2
# Prior consecutive quarters of ROAA history to pull for the sustained-low check.
ROAA_HISTORY_QUARTERS = 2
# First-ever poll of a period has no prior cursor -- go back far enough to see
# every filer, not just "changed since an arbitrary recent date".
FIRST_POLL_SENTINEL = datetime(2000, 1, 1)


def run_daily(
    conn: duckdb.DuckDBPyConnection,
    client: FFIECClient,
    email_config: Optional[EmailConfig],
    universe: Optional[set[str]] = None,
    run_date: Optional[date] = None,
    send: bool = True,
    max_facsimile_pulls: Optional[int] = None,
    heartbeat: bool = False,
) -> str:
    """Runs one full daily cycle. Returns the email body that was (or would have
    been) sent, or a short "nothing to report" message.

    `universe`, when given, restricts which institutions get the expensive full
    pull + diff step (spec §10.1's small-list testing phase). The cheap ping
    always covers whatever the ping endpoint returns.

    `max_facsimile_pulls`, when given, caps how many changed institution/period
    pairs get the full facsimile pull *this run* -- useful for a quick live
    smoke test, or to bound a single run's wall-clock time on a very large
    universe (each full pull is its own throttled FFIEC round-trip; the full
    ~4,500-institution universe's first run is ~8,000+ pairs). This is safe to
    use for real operation, unlike an earlier version of this parameter: a
    period's poll_state cursor now only advances once *every* pair found for
    that period this run has actually been processed. Anything left over
    (past the cap, or skipped after an isolated per-institution failure) stays
    uncovered by the cursor and is picked up again on the next run -- nothing
    is silently dropped from coverage.

    `heartbeat`, when true, sends a short confirmation email even when there's
    nothing to report (normally a silent, un-emailed "Nothing to report today")
    -- so a quiet inbox can't be mistaken for "the scheduled run didn't fire".
    Temporary by design: meant to be turned off again once the live pipeline
    (esp. the full-universe rollout) is trusted to actually run daily; it never
    duplicates a real alert email, only fires on the otherwise-silent path.
    """
    run_date = run_date or date.today()

    try:
        periods = sorted(client.retrieve_reporting_periods())[-PING_PERIOD_LOOKBACK:]
    except FFIECAuthError:
        raise  # spec §1: hard failure, must surface as an alert, never a silent skip

    changed: list[tuple[str, date, datetime]] = []
    pending_watermarks: dict[date, datetime] = {}
    for period_end in periods:
        since = db.get_last_polled_at(conn, period_end) or FIRST_POLL_SENTINEL
        # Watermark captured *before* the call, not after -- so a filing that
        # lands mid-call is seen again next run rather than silently skipped.
        # Not written to poll_state yet, though: see the fully_processed_periods
        # handling below -- it's only persisted once this period's slice of
        # `changed` is confirmed fully processed, so an interrupted run (hit
        # max_facsimile_pulls, or an isolated per-institution failure) doesn't
        # advance the cursor past coverage it never actually got.
        pending_watermarks[period_end] = datetime.now(timezone.utc)
        for status in client.retrieve_filer_statuses(period_end, since):
            if universe is not None and status.id_rssd not in universe:
                continue
            existing = db.get_snapshot(conn, status.id_rssd, status.period_end)
            if existing is None or existing.last_seen_ts != status.submission_ts:
                changed.append((status.id_rssd, status.period_end, status.submission_ts))

    logger.info("Found %d changed institution/period pair(s) since last poll.", len(changed))

    fully_processed_periods = set(periods)
    if max_facsimile_pulls is not None and len(changed) > max_facsimile_pulls:
        logger.warning(
            "max_facsimile_pulls=%d: processing only the first %d of %d changed "
            "pairs this run; the rest stay uncovered by the cursor and are "
            "picked up next run.",
            max_facsimile_pulls, max_facsimile_pulls, len(changed),
        )
        for _r, period_end, _t in changed[max_facsimile_pulls:]:
            fully_processed_periods.discard(period_end)
        changed_to_process = changed[:max_facsimile_pulls]
    else:
        changed_to_process = changed

    watch_list_changes: list[email_report.WatchListChangeEvent] = []
    tier1_events: list[email_report.Tier1Event] = []
    tier2_events: list[email_report.Tier2Event] = []
    restatement_events: list[email_report.RestatementEvent] = []
    new_filing_events: list[email_report.NewFilingEvent] = []

    # RetrieveFacsimile carries no name/city/state/FDIC-cert fields (verified
    # against the real API doc -- that identity data only exists in the Panel of
    # Reporters), so fetch it once per period actually needed rather than guessing
    # at nonexistent facsimile fields.
    panel_by_period: dict[date, dict[str, Any]] = {}
    for period_end in {p for _r, p, _t in changed_to_process}:
        panel_by_period[period_end] = {
            str(entry["ID_RSSD"]): entry for entry in client.retrieve_panel_of_reporters(period_end)
        }

    failure_count = 0
    processed: list[simple_email.Filing] = []
    for id_rssd, period_end, submission_ts in changed_to_process:
        panel_entry = panel_by_period.get(period_end, {}).get(id_rssd)
        refiling = db.get_snapshot(conn, id_rssd, period_end) is not None
        try:
            _process_one(conn, client, id_rssd, period_end, submission_ts, run_date, panel_entry,
                          watch_list_changes, tier1_events, tier2_events, restatement_events, new_filing_events)
        except FFIECAuthError:
            raise  # spec §1: must propagate, never swallowed
        except FFIECRateLimitError:
            # Proactive throttling should make this rare; once retries are
            # exhausted (see ffiec_client), the quota problem is systemic, not
            # isolated to this one institution -- every remaining pull would
            # fail the same way, so stop rather than burning through the rest
            # of `changed_to_process` repeating a doomed backoff sequence.
            raise
        except Exception:
            # An isolated per-institution failure (e.g. an unexpected facsimile
            # shape) must not sink the whole run on a universe with thousands
            # of other institutions to process. Skip it, mark its period as not
            # fully covered so the cursor doesn't advance past it, and keep going.
            logger.exception(
                "Failed to process id_rssd=%s period_end=%s -- skipping, will "
                "retry next run.", id_rssd, period_end,
            )
            fully_processed_periods.discard(period_end)
            failure_count += 1
            continue
        processed.append(simple_email.Filing(id_rssd, period_end, refiling, submission_ts))

    if failure_count:
        logger.warning("%d institution/period pair(s) failed and will be retried next run.", failure_count)

    for period_end in periods:
        if period_end in fully_processed_periods:
            db.set_last_polled_at(conn, period_end, pending_watermarks[period_end])

    snapshot_entries = _build_watch_list_snapshot(conn)

    body = email_report.build_email_body(
        watch_list_changes, tier1_events, snapshot_entries, tier2_events,
        restatement_events, new_filing_events, run_date,
    )

    # The daily email is the Call Report workbook (Summary, Filings with Call Report + UBPR
    # links, Ratios) for every filing processed this run, as the only attachment, sent every
    # day -- per user request 2026-10-29. No Claude summary, charts or PDF.
    from . import filings_workbook
    label = run_date.strftime("%a %b %d, %Y")
    note = f"{failure_count} filing(s) could not be pulled and will be retried next run." if failure_count else ""
    if send and email_config is not None:
        records, failed = filings_workbook.collect(conn, client, processed)
        text = filings_workbook.send(email_config, records, label, failed, note)
    else:
        text = simple_email.render_text(simple_email.build_rows(conn, processed), label)
    if body is None:
        return text if heartbeat else "Nothing to report today."
    return body


def _process_one(
    conn: duckdb.DuckDBPyConnection,
    client: FFIECClient,
    id_rssd: str,
    period_end: date,
    submission_ts: datetime,
    run_date: date,
    panel_entry: Optional[dict[str, Any]],
    watch_list_changes: list,
    tier1_events: list,
    tier2_events: list,
    restatement_events: list,
    new_filing_events: list,
) -> None:
    is_restatement = db.get_snapshot(conn, id_rssd, period_end) is not None

    # Ratios are computed directly from the Call Report facsimile (verified MDRM
    # codes -- see ratios.py module docstring), not from UBPR.
    facsimile = client.retrieve_facsimile(id_rssd, period_end)
    new_ratios = ratios.extract_ratios(facsimile, period_end)
    new_ratios["total_assets"] = ratios.extract_total_assets(facsimile)

    meta = _meta_from_panel(id_rssd, period_end, panel_entry, new_ratios["total_assets"])
    db.upsert_institution_meta(conn, meta)

    if is_restatement:
        stored = db.get_snapshot(conn, id_rssd, period_end)
        triggers = alerts.classify_restatement(stored.ratios, new_ratios)
        if not triggers:
            reason = alerts.describe_restatement_change(stored.ratios, new_ratios)
            db.log_alert(conn, id_rssd, period_end, "TIER3", "RESTATEMENT_SUB_FLOOR", {"reason": reason})
            # Not a real alert (no capital ratio moved >50bp), but per user request
            # 2026-09-18, still surfaced as a one-line email mention rather than
            # staying invisible in alert_log.
            restatement_events.append(email_report.RestatementEvent(meta, period_end, reason, new_ratios))
    else:
        prior_snapshot = _latest_prior_snapshot(conn, id_rssd, period_end)
        prior_ratios = prior_snapshot.ratios if prior_snapshot else {}
        if prior_snapshot is not None:
            ratios.compute_growth_rates(prior_ratios, new_ratios, prior_snapshot.period_end, period_end)
        roaa_history = _roaa_history(conn, id_rssd, period_end)
        triggers = alerts.classify_new_period(
            prior_ratios, new_ratios, roaa_history,
            prior_ratios.get("total_assets"), new_ratios.get("total_assets"),
        )
        if not triggers:
            db.log_alert(conn, id_rssd, period_end, "TIER3", "NEW_PERIOD_NO_TRIGGER", {})
            # Per user request 2026-09-18: every new quarter is reported, not just
            # threshold-crossing ones -- the user reviews whichever ones they care
            # about themselves via the report link. Note this is a much higher-
            # volume category than restatements: expect a large batch whenever a
            # new quarter opens and institutions file over the following weeks
            # (comparable to the ~8,600-entry initial full-universe backfill),
            # not the small trickle restatements produce.
            new_filing_events.append(email_report.NewFilingEvent(meta, period_end, new_ratios))

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


def _meta_from_panel(
    id_rssd: str, period_end: date, panel_entry: Optional[dict[str, Any]], total_assets: Optional[float],
) -> db.InstitutionMeta:
    """Identity fields (name/city/state/FDIC cert) come from the Panel of
    Reporters, NOT the facsimile -- verified against the real API doc, which
    shows RetrieveFacsimile returning only a raw report byte-stream, no identity
    fields. `panel_entry` can be None if this RSSD wasn't in that period's panel
    (shouldn't normally happen for something that just filed, but institution_meta
    still needs a row rather than crashing the whole run over one lookup miss).
    """
    if panel_entry is None:
        return db.InstitutionMeta(
            id_rssd=id_rssd, name=f"RSSD {id_rssd}", city="", state="",
            fdic_cert="", total_assets=total_assets, as_of_period=period_end,
        )
    return db.InstitutionMeta(
        id_rssd=id_rssd,
        name=str(panel_entry.get("Name", f"RSSD {id_rssd}")),
        city=str(panel_entry.get("City", "")),
        state=str(panel_entry.get("State", "")),
        fdic_cert=str(panel_entry.get("FDICCertNumber", "")),
        total_assets=total_assets,
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
