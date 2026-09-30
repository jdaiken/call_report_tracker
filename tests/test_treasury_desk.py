"""Treasury Desk special alert: name-variation matching against the committed
list, and its own section/subject in the daily email."""
from datetime import date
from unittest.mock import patch

from ffiec_monitor import db, treasury_desk
from ffiec_monitor.config import EmailConfig
from ffiec_monitor.pipeline import run_daily

from tests.test_pipeline import FakeClient

_EMAIL_CONFIG = EmailConfig(address="a@example.com", app_password="x", recipient="a@example.com")


def test_committed_list_matches_legal_name_variations():
    m = treasury_desk.load()
    assert m.match("PIONEER BANK, NATIONAL ASSOCIATION") == "Pioneer Bank National Association"
    assert m.match("Pioneer Bank, N.A.") == "Pioneer Bank National Association"
    assert m.match("FIRST MID BANK & TRUST, N.A.") == "First Mid Bank and Trust NA"
    assert m.match("APPLE BANK FOR SAVINGS") == "Apple Bank"
    assert m.match("WASHINGTON FEDERAL BANK") == "WaFd Bank"
    assert m.match("BMO BANK NATIONAL ASSOCIATION") == "BMO Bank"
    assert m.match("LIVE OAK BANKING COMPANY") == "Live Oak Banking Company"
    assert m.match("GOLDMAN SACHS BANK USA") == "Goldman Sachs Bank USA"
    assert m.match("The Provident Bank") == "Provident Bank"


def test_no_loose_matches():
    m = treasury_desk.load()
    assert m.match("Pioneer Bank") is None  # a different bank than Pioneer Bank, N.A.
    assert m.match("Bank of India Trust") is None
    assert m.match("Truist Financial Corp") is None


def test_state_pin_restricts_generic_names():
    m = treasury_desk.TreasuryDeskMatcher([treasury_desk.TreasuryDeskEntry("Pinnacle Bank", (), "TN")])
    assert m.match("Pinnacle Bank", "TN") == "Pinnacle Bank"
    assert m.match("Pinnacle Bank", "NE") is None


class _NamedClient(FakeClient):
    def retrieve_panel_of_reporters(self, period_end):
        return [{"ID_RSSD": 1, "Name": "TRUIST BANK", "City": "CHARLOTTE", "State": "NC", "FDICCertNumber": "9846"},
                {"ID_RSSD": 2, "Name": "SOME OTHER BANK", "City": "X", "State": "WI", "FDICCertNumber": "1"}]


def test_treasury_desk_bank_gets_its_own_section_and_subject():
    conn = db.connect(":memory:")
    with patch("ffiec_monitor.pipeline.email_report.send_email") as mock_send:
        body = run_daily(conn, _NamedClient(["1", "2"]), _EMAIL_CONFIG, run_date=date(2026, 7, 15))

    assert body.index("TREASURY DESK") < body.index("NEW QUARTERLY FILINGS")
    desk_section = body.split("TREASURY DESK", 1)[1].split("---", 1)[0]
    assert "TRUIST BANK" in desk_section and "Call Report filed" in desk_section
    assert "SOME OTHER BANK" not in desk_section

    _config, subject, _body = mock_send.call_args[0]
    assert subject.startswith("[FFIEC Monitor] Treasury Desk alert -- 1 institution(s)")
    assert "<h3>Treasury Desk</h3>" in mock_send.call_args.kwargs["html_body"]


def test_no_treasury_desk_section_when_no_listed_bank_changed():
    conn = db.connect(":memory:")
    body = run_daily(conn, FakeClient(["1"]), None, run_date=date(2026, 7, 15), send=False)
    assert "TREASURY DESK" not in body
