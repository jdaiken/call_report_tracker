"""Tests against the *actual* FFIEC SIS611 documentation's request/response shapes
-- these exist because the first implementation guessed wrong on three load-bearing
details (query params instead of headers, ISO dates instead of MM/DD/YYYY, and
RetrieveFacsimile's shape) and failed instantly against the live API as a result.
"""
import base64
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from ffiec_monitor.config import FFIECConfig
from ffiec_monitor.ffiec_client import (
    FFIECAPIError,
    FFIECAuthError,
    FFIECClient,
    FFIECRateLimitError,
)


def _client(session):
    return FFIECClient(FFIECConfig(user_id="PWSUser101", token="tok123"), session=session)


def _fake_response(json_body=None, status_code=200, text=""):
    resp = MagicMock()
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = text
    return resp


def test_all_parameters_sent_as_headers_not_query_params():
    session = MagicMock()
    session.get.return_value = _fake_response(["12/31/2002", "03/31/2003"])
    client = _client(session)

    client.retrieve_reporting_periods()

    _, kwargs = session.get.call_args
    assert "params" not in kwargs or not kwargs.get("params")
    assert kwargs["headers"]["dataSeries"] == "Call"
    assert kwargs["headers"]["UserID"] == "PWSUser101"
    assert kwargs["headers"]["Authentication"] == "Bearer tok123"


def test_dates_formatted_as_mm_dd_yyyy_not_iso():
    session = MagicMock()
    session.get.return_value = _fake_response([])
    client = _client(session)

    client.retrieve_panel_of_reporters(date(2025, 3, 31))

    _, kwargs = session.get.call_args
    assert kwargs["headers"]["reportingPeriodEndDate"] == "03/31/2025"


def test_retrieve_reporting_periods_parses_sample_dates():
    session = MagicMock()
    session.get.return_value = _fake_response(["12/31/2002", "03/31/2003", "06/30/2003"])
    client = _client(session)

    periods = client.retrieve_reporting_periods()
    assert periods == [date(2002, 12, 31), date(2003, 3, 31), date(2003, 6, 30)]


def test_retrieve_panel_of_reporters_returns_raw_list_with_real_keys():
    sample = [{
        "ID_RSSD": 724904, "FDICCertNumber": 27417, "Name": "SHOREHAM BANK",
        "State": "RI", "City": "WARWICK", "HasFiledForReportingPeriod": True,
    }]
    session = MagicMock()
    session.get.return_value = _fake_response(sample)
    client = _client(session)

    panel = client.retrieve_panel_of_reporters(date(2025, 3, 31))
    assert panel == sample


def test_retrieve_filer_statuses_parses_real_field_names_and_sends_since_header():
    sample = [
        {"ID_RSSD": 723158, "DateTime": "7/1/2025 6:17:33 PM"},
        {"ID_RSSD": 807432, "DateTime": "7/9/2025 11:42:45 AM"},
    ]
    session = MagicMock()
    session.get.return_value = _fake_response(sample)
    client = _client(session)

    statuses = client.retrieve_filer_statuses(date(2025, 3, 31), datetime(2025, 4, 15))

    _, kwargs = session.get.call_args
    assert kwargs["headers"]["lastUpdateDateTime"] == "04/15/2025"
    assert len(statuses) == 2
    assert statuses[0].id_rssd == "723158"
    assert statuses[0].submission_ts == datetime(2025, 7, 1, 18, 17, 33)
    assert statuses[0].period_end == date(2025, 3, 31)


def test_retrieve_facsimile_decodes_base64_sdf_into_flat_dict():
    sdf_text = '"IDRSSD"\tRCON2170\tRCONB529\n37\t85210\t16787\n'
    encoded = base64.b64encode(sdf_text.encode()).decode()
    session = MagicMock()
    session.get.return_value = _fake_response({"FacsimileFile": encoded})
    client = _client(session)

    facsimile = client.retrieve_facsimile("37", date(2026, 6, 30))

    _, kwargs = session.get.call_args
    assert kwargs["headers"]["fiIdType"] == "ID_RSSD"
    assert kwargs["headers"]["fiId"] == "37"
    assert kwargs["headers"]["facsimileFormat"] == "SDF"
    assert facsimile["RCON2170"] == "85210"
    assert facsimile["RCONB529"] == "16787"


def test_retrieve_facsimile_handles_bare_string_payload():
    """A real live call (2026-09-14) returned the base64 blob as a bare JSON
    string body, not wrapped in {"FacsimileFile": ...} as the doc's placeholder
    example implied -- the client must handle both shapes."""
    sdf_text = '"IDRSSD"\tRCON2170\tRCONB529\n37\t85210\t16787\n'
    encoded = base64.b64encode(sdf_text.encode()).decode()
    session = MagicMock()
    session.get.return_value = _fake_response(encoded)
    client = _client(session)

    facsimile = client.retrieve_facsimile("37", date(2026, 6, 30))

    assert facsimile["RCON2170"] == "85210"
    assert facsimile["RCONB529"] == "16787"


def test_retrieve_facsimile_unexpected_shape_raises_clear_error():
    session = MagicMock()
    session.get.return_value = _fake_response({"UnexpectedKey": 123})
    with pytest.raises(FFIECAPIError, match="Unexpected facsimile payload shape"):
        _client(session).retrieve_facsimile("37", date(2026, 6, 30))


def test_sends_a_real_user_agent_not_requests_default():
    """requests' default UA ("python-requests/x.y.z") is a known signature Azure
    Application Gateway's WAF (OWASP CRS "known bad UA" rules) blocks outright,
    before APIM or FFIEC's own logic runs -- symptomatically identical to every
    403 seen so far (generic gateway HTML, not FFIEC's JSON error shape),
    regardless of token validity or which runner/IP made the request."""
    session = MagicMock()
    session.get.return_value = _fake_response([])
    client = _client(session)

    client.retrieve_reporting_periods()

    _, kwargs = session.get.call_args
    user_agent = kwargs["headers"]["User-Agent"]
    assert "python-requests" not in user_agent.lower()
    assert user_agent


def test_401_raises_auth_error():
    session = MagicMock()
    session.get.return_value = _fake_response(
        {"StatusCode": 401, "Message": "Invalid JWT token"}, status_code=401,
    )
    with pytest.raises(FFIECAuthError):
        _client(session).retrieve_reporting_periods()


def test_403_raises_rate_limit_error_not_auth_error():
    """Per FFIEC's own documented error table, 403 means out-of-quota, not a bad
    token -- conflating the two was the original bug's second-order mistake."""
    session = MagicMock()
    session.get.return_value = _fake_response(
        {"StatusCode": 403, "Message": "Out of call volume quota. Quota will be replenished in 12 minutes."},
        status_code=403,
    )
    with pytest.raises(FFIECRateLimitError):
        _client(session).retrieve_reporting_periods()


def test_400_with_token_message_raises_auth_error():
    """FFIEC's error table lists 'Security Token is expired' under 400, not 401."""
    session = MagicMock()
    session.get.return_value = _fake_response(
        {"StatusCode": 400, "Message": "Security Token is expired."}, status_code=400,
    )
    with pytest.raises(FFIECAuthError):
        _client(session).retrieve_reporting_periods()


def test_400_without_token_message_raises_generic_api_error():
    session = MagicMock()
    session.get.return_value = _fake_response(
        {"StatusCode": 400, "Message": "Invalid Reporting Period End Date specified in request."},
        status_code=400,
    )
    with pytest.raises(FFIECAPIError):
        _client(session).retrieve_reporting_periods()


def test_gateway_level_403_html_body_still_raises_rate_limit_error_with_raw_body_visible():
    """A non-JSON body (e.g. an Azure Application Gateway HTML page) must not
    crash error handling -- and should still surface the raw text for diagnosis."""
    session = MagicMock()
    resp = _fake_response(status_code=403, text="<html><body>403 Forbidden</body></html>")
    resp.json.side_effect = ValueError("not JSON")
    session.get.return_value = resp

    with pytest.raises(FFIECRateLimitError, match="403 Forbidden"):
        _client(session).retrieve_reporting_periods()
