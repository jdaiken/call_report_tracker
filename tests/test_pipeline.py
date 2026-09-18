"""Coverage for run_daily's resumable poll_state cursor -- a period's watermark
must only advance once every changed institution/period pair found for it this
run has actually been processed, so a capped or partially-failed run never
silently drops coverage (see pipeline.py's run_daily docstring)."""
from datetime import date, datetime
from unittest.mock import patch

from ffiec_monitor import db
from ffiec_monitor.config import EmailConfig
from ffiec_monitor.ffiec_client import FilerStatus
from ffiec_monitor.pipeline import run_daily

PERIOD = date(2026, 6, 30)
_EMAIL_CONFIG = EmailConfig(address="a@example.com", app_password="x", recipient="a@example.com")


class FakeClient:
    """Minimal stand-in for FFIECClient covering exactly what run_daily calls."""

    def __init__(self, id_rssds, failing_id_rssd=None):
        self._id_rssds = id_rssds
        self._failing_id_rssd = failing_id_rssd

    def retrieve_reporting_periods(self):
        return [PERIOD]

    def retrieve_filer_statuses(self, period_end, since):
        return [
            FilerStatus(id_rssd=rssd, period_end=period_end, submission_ts=datetime(2026, 7, 1))
            for rssd in self._id_rssds
        ]

    def retrieve_panel_of_reporters(self, period_end):
        return [{"ID_RSSD": int(rssd), "Name": f"Bank {rssd}", "City": "", "State": "", "FDICCertNumber": ""}
                for rssd in self._id_rssds]

    def retrieve_facsimile(self, id_rssd, period_end):
        if id_rssd == self._failing_id_rssd:
            raise ValueError("simulated unexpected facsimile shape")
        return {}


def test_max_facsimile_pulls_does_not_advance_cursor_when_coverage_incomplete():
    conn = db.connect(":memory:")
    client = FakeClient(["1", "2", "3"])

    run_daily(conn, client, None, run_date=date(2026, 7, 15), send=False, max_facsimile_pulls=1)

    # Only 1 of 3 processed -- the period must stay uncovered by the cursor.
    assert db.get_last_polled_at(conn, PERIOD) is None
    assert db.get_snapshot(conn, "1", PERIOD) is not None
    assert db.get_snapshot(conn, "2", PERIOD) is None

    # A follow-up uncapped run re-sees all 3 (redundant reprocessing of "1" is
    # an acceptable cost) and this time fully covers the period.
    run_daily(conn, client, None, run_date=date(2026, 7, 16), send=False)
    assert db.get_last_polled_at(conn, PERIOD) is not None
    assert db.get_snapshot(conn, "2", PERIOD) is not None
    assert db.get_snapshot(conn, "3", PERIOD) is not None


def test_isolated_institution_failure_does_not_sink_the_run_or_advance_cursor():
    conn = db.connect(":memory:")
    client = FakeClient(["1", "2", "3"], failing_id_rssd="2")

    body = run_daily(conn, client, None, run_date=date(2026, 7, 15), send=False)

    # Run completes despite institution "2" failing.
    assert body is not None
    assert db.get_snapshot(conn, "1", PERIOD) is not None
    assert db.get_snapshot(conn, "2", PERIOD) is None
    assert db.get_snapshot(conn, "3", PERIOD) is not None
    # Cursor must not advance -- "2" was never actually covered this run.
    assert db.get_last_polled_at(conn, PERIOD) is None


def test_clean_full_run_advances_cursor():
    conn = db.connect(":memory:")
    client = FakeClient(["1", "2"])

    run_daily(conn, client, None, run_date=date(2026, 7, 15), send=False)

    assert db.get_last_polled_at(conn, PERIOD) is not None
    assert db.get_snapshot(conn, "1", PERIOD) is not None
    assert db.get_snapshot(conn, "2", PERIOD) is not None


def test_nothing_to_report_stays_silent_without_heartbeat():
    conn = db.connect(":memory:")
    client = FakeClient([])  # nothing changed -- alert body is None

    with patch("ffiec_monitor.pipeline.email_report.send_email") as mock_send:
        body = run_daily(conn, client, _EMAIL_CONFIG, run_date=date(2026, 7, 15))

    assert body == "Nothing to report today."
    mock_send.assert_not_called()


def test_heartbeat_sends_confirmation_email_when_nothing_to_report():
    conn = db.connect(":memory:")
    client = FakeClient([])

    with patch("ffiec_monitor.pipeline.email_report.send_email") as mock_send:
        body = run_daily(conn, client, _EMAIL_CONFIG, run_date=date(2026, 7, 15), heartbeat=True)

    assert body != "Nothing to report today."
    assert "No alerts or Watch List activity today." in body
    mock_send.assert_called_once()
    _config, subject, sent_body = mock_send.call_args[0]
    assert "Heartbeat" in subject
    assert sent_body == body


class RestatementClient(FakeClient):
    """Same institution/period on every call, but a distinct submission_ts per
    instance -- the "changed" signal run_daily's diff relies on -- so a second
    run sees the period as a restatement of the first, not a fresh one."""

    def __init__(self, id_rssds, submission_ts):
        super().__init__(id_rssds)
        self._submission_ts = submission_ts

    def retrieve_filer_statuses(self, period_end, since):
        return [FilerStatus(id_rssd=r, period_end=period_end, submission_ts=self._submission_ts)
                for r in self._id_rssds]


def test_restatement_with_no_ratio_data_gets_a_one_liner_not_silence():
    """RESTATEMENT_SUB_FLOOR used to be Tier 3 -- logged to alert_log but never
    emailed. Per user request 2026-09-18, it now surfaces as a one-line
    REFILINGS mention instead of vanishing into a heartbeat."""
    conn = db.connect(":memory:")
    run_daily(conn, RestatementClient(["1"], datetime(2026, 7, 1)), None,
              run_date=date(2026, 7, 15), send=False)  # first sight

    body = run_daily(conn, RestatementClient(["1"], datetime(2026, 7, 2)), None,
                      run_date=date(2026, 7, 16), send=False)  # refiled -- same (empty) ratios

    assert body is not None
    assert "REFILINGS" in body
    assert "Bank 1" in body
    assert "no capital ratio field present in both versions to compare" in body


class RestatedRatioClient(RestatementClient):
    """A restatement carrying a real (below-threshold) capital ratio delta,
    so describe_restatement_change has an actual number to name."""

    def __init__(self, id_rssds, submission_ts, total_rbc_ratio):
        super().__init__(id_rssds, submission_ts)
        self._total_rbc_ratio = total_rbc_ratio

    def retrieve_facsimile(self, id_rssd, period_end):
        return {"RCOA7205": str(self._total_rbc_ratio)}


def test_restatement_one_liner_names_the_actual_ratio_that_moved():
    conn = db.connect(":memory:")
    run_daily(conn, RestatedRatioClient(["1"], datetime(2026, 7, 1), 11.20), None,
              run_date=date(2026, 7, 15), send=False)  # first sight

    body = run_daily(conn, RestatedRatioClient(["1"], datetime(2026, 7, 2), 11.05), None,
                      run_date=date(2026, 7, 16), send=False)  # refiled, -0.15pp (sub-floor)

    assert body is not None
    assert "Total Risk-Based Capital Ratio 11.20% -> 11.05% (-0.15pp)" in body


def test_heartbeat_never_fires_when_there_is_a_real_alert():
    """A real alert is its own confirmation the run fired -- heartbeat must not
    also send a redundant second email."""
    conn = db.connect(":memory:")
    client = FakeClient(["1"])
    # Force a real Tier 1-worthy change: first snapshot with a critical ratio,
    # then a second period showing a qualifying capital deterioration.
    run_daily(conn, client, None, run_date=date(2026, 7, 15), send=False)

    with patch("ffiec_monitor.pipeline.email_report.build_email_body", return_value="ALERT BODY"), \
         patch("ffiec_monitor.pipeline.email_report.send_email") as mock_send:
        body = run_daily(conn, client, _EMAIL_CONFIG, run_date=date(2026, 7, 16), heartbeat=True)

    assert body == "ALERT BODY"
    mock_send.assert_called_once()
    _config, subject, _sent_body = mock_send.call_args[0]
    assert "Heartbeat" not in subject
