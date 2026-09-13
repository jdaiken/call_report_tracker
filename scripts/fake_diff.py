"""Spec §10.2: force a fake diff to confirm the email-sending path works end to end,
without touching the FFIEC API or persisting anything to the real snapshot table.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ffiec_monitor import alerts, db, email_report, ratios
from ffiec_monitor.config import EmailConfig

FAKE_RSSD = "TEST0000001"


def run_fake_diff(conn, email_config: Optional[EmailConfig], send: bool = True) -> str:
    meta = db.InstitutionMeta(
        id_rssd=FAKE_RSSD, name="Test Bank, N.A.", city="Testville", state="CA",
        fdic_cert="99999", total_assets=1_250_000, as_of_period=date.today(),
    )

    old_ratios = {"tier1_leverage_ratio": 5.8, "total_rbc_ratio": 11.2, "roaa": 0.15}
    new_ratios = {"tier1_leverage_ratio": 4.6, "total_rbc_ratio": 11.0, "roaa": -0.20}

    triggers = alerts.classify_new_period(old_ratios, new_ratios, [], None, None)
    assert triggers, "fake diff should always produce at least one trigger"

    tier1 = [t for t in triggers if t.tier == alerts.TIER1]
    tier2 = [t for t in triggers if t.tier == alerts.TIER2]

    tier1_events = [email_report.Tier1Event(meta, date.today(), tier1, False, None)] if tier1 else []
    tier2_events = [email_report.Tier2Event(meta, tier2)] if tier2 else []

    body = email_report.build_email_body([], tier1_events, [], tier2_events, date.today())
    assert body is not None

    if send and email_config is not None:
        email_report.send_email(email_config, "[FFIEC Monitor] TEST -- fake diff", body)

    return body
