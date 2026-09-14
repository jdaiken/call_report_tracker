"""Client for the FFIEC CDR Public Web Service (PWS), REST/OAuth2 front end.

Rewritten against the actual FFIEC documentation ("SIS611 Retrieve Public Data via
Web Service", CDR-PDD-SIS-611 v1.10), not guessed. The first live run against the
old (guessed) implementation failed instantly with a generic Azure Application
Gateway 403 HTML page -- not FFIEC's own documented JSON error shape -- which is
consistent with the request never reaching FFIEC's actual API logic at all. The
real spec differs from the earlier guess in three load-bearing ways:

  1. Every method parameter (dataSeries, reportingPeriodEndDate, fiId, etc.) is a
     HEADER, not a query-string parameter. The old client sent them all as query
     params.
  2. Dates are "MM/DD/YYYY" strings, not ISO 8601.
  3. RetrieveFacsimile/RetrieveUBPRXBRLFacsimile return a raw byte-stream file
     (base64) -- SDF/XBRL/PDF -- not pre-parsed JSON fields keyed by MDRM code.
     ratios.py's field-level mapping (verified against a real bulk download) is
     still correct; it's the *shape this client hands it* that needed fixing --
     see _parse_sdf/_unwrap_facsimile_payload below.

That gateway-level 403 turned out to be unrelated to any of the above: it was
`requests`' default User-Agent (`python-requests/x.y.z`) tripping Azure's WAF
"known bad UA" rule, independent of headers/dates/token. Fixed by sending a
real `User-Agent` (see USER_AGENT below). Once that was fixed, a live call
confirmed the doc's placeholder example for point 3 was itself slightly off:
RetrieveFacsimile returns the base64 blob as a *bare* JSON string body, not
`{"FacsimileFile": <base64>}` -- see _unwrap_facsimile_payload.

Base URL: https://ffieccdr.azure-api.us/public/<function>. Auth is a `UserID`
header plus `Authentication: Bearer <token>` header, a 90-day non-refreshing JWT,
~2,500 req/hour ceiling. An auth failure must raise loudly rather than be
swallowed into a quiet empty run (spec §1).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import requests

from .config import FFIECConfig

REQUEST_TIMEOUT_SECONDS = 30
DATE_FMT = "%m/%d/%Y"

# requests' default User-Agent ("python-requests/x.y.z") is a well-known signature
# that Azure Application Gateway WAF (OWASP Core Rule Set, e.g. rule 913101 "known
# bad UA") blocks outright at the perimeter -- before APIM or FFIEC's own API logic
# ever runs. That failure mode looks identical to a bad/expired token (403, generic
# gateway HTML instead of FFIEC's documented JSON error shape) and is IP-independent,
# which is consistent with every 403 seen so far regardless of token state or which
# runner (hosted vs. self-hosted) made the request. Sending a real, descriptive UA
# instead is the standard fix.
USER_AGENT = "call-report-tracker/1.0 (+https://github.com/jdaiken/call_report_tracker)"


class FFIECAuthError(RuntimeError):
    """A real auth problem: missing/invalid/expired token (401, or a 400 whose
    body explicitly says the security token is invalid/expired -- FFIEC's own
    error table lists "Security Token is invalid/expired" under 400, not just 401).

    This must propagate all the way out of the pipeline as a hard failure (spec §1),
    not be caught and treated as "no updates today".
    """


class FFIECRateLimitError(RuntimeError):
    """403 from the PWS -- per FFIEC's documented error table this specifically
    means "Out of call volume quota", NOT an auth failure. Distinct from
    FFIECAuthError so callers don't misdiagnose a quota hit as a bad token.
    """


class FFIECAPIError(RuntimeError):
    """Any other non-2xx response from the PWS (400 validation errors not about
    the token, 500s, 204 "not found")."""


@dataclass(frozen=True)
class FilerStatus:
    """One row from the daily lightweight ping: latest filed period + timestamp."""
    id_rssd: str
    period_end: date
    submission_ts: datetime


class FFIECClient:
    def __init__(self, config: FFIECConfig, session: Optional[requests.Session] = None):
        self._config = config
        self._session = session or requests.Session()

    def _get(self, function: str, extra_headers: Optional[dict[str, str]] = None) -> Any:
        url = f"{self._config.base_url}/{function}"
        headers = {
            "UserID": self._config.user_id,
            "Authentication": f"Bearer {self._config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(extra_headers or {}),
        }
        resp = self._session.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

        if resp.ok:
            return resp.json()

        message = _extract_error_message(resp)
        detail = f"{function} -> HTTP {resp.status_code}: {message}"

        if resp.status_code == 401 or "token" in message.lower():
            raise FFIECAuthError(
                f"FFIEC PWS auth failure ({detail}). FFIEC_TOKEN is likely missing/"
                "invalid/expired (90-day, no auto-refresh) -- renew it and update "
                "the GitHub Actions secret."
            )
        if resp.status_code == 403:
            raise FFIECRateLimitError(
                f"FFIEC PWS rate limit ({detail}). Per FFIEC's docs this means "
                "out-of-quota (~2,500 req/hour), not an auth problem -- back off "
                "and retry later, don't treat this as a bad token."
            )
        raise FFIECAPIError(f"FFIEC PWS error ({detail}). Raw body: {resp.text[:500]!r}")

    # --- tier 1: daily lightweight ping, institutions changed since last poll --

    def retrieve_filer_statuses(self, reporting_period_end: date, since: datetime) -> list[FilerStatus]:
        """ID_RSSD + submission timestamp for filers whose report changed since
        `since` -- catches both new-quarter filings and quiet restatements of an
        already-seen period (spec §2 step 1). `since` should be the last time this
        was called for this period_end (see ffiec_monitor.db poll_state), not an
        arbitrary lookback -- FFIEC's own recommended usage (SIS611 §3.1) is to
        track and advance this cursor across runs, which is also what keeps this
        cheap regardless of universe size.
        """
        payload = self._get(
            "RetrieveFilersSubmissionDateTime",
            extra_headers={
                "dataSeries": "Call",
                "reportingPeriodEndDate": reporting_period_end.strftime(DATE_FMT),
                "lastUpdateDateTime": since.strftime(DATE_FMT),
            },
        )
        return [
            FilerStatus(
                id_rssd=str(row["ID_RSSD"]),
                period_end=reporting_period_end,
                submission_ts=_parse_datetime(row["DateTime"]),
            )
            for row in payload
        ]

    def retrieve_reporting_periods(self) -> list[date]:
        """All Call Report reporting period-end dates FFIEC currently has data for."""
        payload = self._get("RetrieveReportingPeriods", extra_headers={"dataSeries": "Call"})
        return sorted(_parse_date(p) for p in payload)

    def retrieve_panel_of_reporters(self, period_end: date) -> list[dict[str, Any]]:
        """Full roster of Call Report filers for a period: RSSD, name, city, state,
        FDIC cert (real keys: ID_RSSD, Name, State, City, FDICCertNumber, ...).
        Used to resolve a bank name/state (e.g. from a third-party list with no
        RSSD) to the FFIEC join key -- not for pulling ratio data itself.
        """
        return self._get(
            "RetrievePanelOfReporters",
            extra_headers={"dataSeries": "Call", "reportingPeriodEndDate": period_end.strftime(DATE_FMT)},
        )

    # --- tier 2: full pull for institutions whose timestamp changed -----------

    def retrieve_facsimile(self, id_rssd: str, period_end: date) -> dict[str, Any]:
        """Full Call Report data for one institution/period, decoded from the raw
        SDF byte-stream FFIEC returns into a flat MDRM-code -> value dict (the
        shape ratios.extract_ratios() expects). See _parse_sdf's docstring for the
        one real caveat here: the SDF layout is inferred from the bulk download's
        convention, not confirmed against an actual single-facsimile SDF sample.

        A first live call (2026-09-14) showed the response body is a *bare* JSON
        string (the base64 blob directly), not `{"FacsimileFile": <base64>}` as
        guessed from the doc's placeholder example -- see _unwrap_facsimile_payload.
        """
        payload = self._get(
            "RetrieveFacsimile",
            extra_headers={
                "dataSeries": "Call",
                "reportingPeriodEndDate": period_end.strftime(DATE_FMT),
                "fiIdType": "ID_RSSD",
                "fiId": str(id_rssd),
                "facsimileFormat": "SDF",
            },
        )
        return _parse_sdf(_unwrap_facsimile_payload(payload, "FacsimileFile"))

    def retrieve_ubpr_facsimile(self, id_rssd: str, period_end: date) -> bytes:
        """UBPR data for one institution/period, as raw XBRL bytes (FFIEC returns
        an XBRL document here, not flat fields -- unlike retrieve_facsimile this
        is NOT parsed, since the current pipeline doesn't use UBPR at all;
        ratios.extract_ratios() computes everything from retrieve_facsimile()
        using verified Call Report MDRM codes instead. Kept for a future caller
        that wants UBPR's own peer-average figures (e.g. peer_avg_charge_off_ratio,
        currently unset).
        """
        payload = self._get(
            "RetrieveUBPRXBRLFacsimile",
            extra_headers={
                "reportingPeriodEndDate": period_end.strftime(DATE_FMT),
                "fiIdType": "ID_RSSD",
                "fiId": str(id_rssd),
            },
        )
        return _decode_byte_field(_unwrap_facsimile_payload(payload, "XBRLFile"))


def _unwrap_facsimile_payload(payload: Any, wrapper_key: str) -> Any:
    """RetrieveFacsimile/RetrieveUBPRXBRLFacsimile were guessed (from the doc's
    "[<byte stream>]" placeholder) to return `{wrapper_key: <base64>}`. A first
    live call instead returned the base64 blob as a bare JSON string body. Handle
    both rather than re-guess: if it's a dict, unwrap it as documented; if it's
    already the raw value (str/bytes), use it directly; anything else is a real
    shape surprise worth a clear error instead of an opaque TypeError two frames
    down in _parse_sdf/_decode_byte_field.
    """
    if isinstance(payload, dict) and wrapper_key in payload:
        return payload[wrapper_key]
    if isinstance(payload, (str, bytes, bytearray)):
        return payload
    raise FFIECAPIError(
        f"Unexpected facsimile payload shape: {type(payload).__name__} "
        f"(expected dict with {wrapper_key!r}, or a bare string/bytes). "
        f"Preview: {str(payload)[:200]!r}"
    )


def _extract_error_message(resp: requests.Response) -> str:
    """FFIEC's documented error bodies are JSON {StatusCode, Message}. A gateway-
    level rejection (WAF, Application Gateway) returns HTML instead -- surface
    that raw text too, since "not FFIEC's JSON shape at all" is itself the
    diagnostic signal that the request never reached FFIEC's own API logic.
    """
    try:
        body = resp.json()
        if isinstance(body, dict) and "Message" in body:
            return str(body["Message"])
    except ValueError:
        pass
    return resp.text[:300] or "(empty body)"


def _decode_byte_field(value: Any) -> bytes:
    """FFIEC's JSON serializes a byte[] as a base64 string (standard .NET/
    Newtonsoft.Json behavior, matching the sample client's libraries per SIS611 §2.3.1).
    Falls back to treating it as already-decoded text if base64 decoding fails,
    rather than raising, since this hasn't been confirmed against a live sample."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return base64.b64decode(value)
    except Exception:
        return str(value).encode("utf-8", errors="replace")


def _parse_sdf(raw_field: Any) -> dict[str, Any]:
    """Decode+parse a single-institution SDF facsimile into {MDRM code: raw value}.

    CAVEAT: FFIEC's SIS611 doc doesn't include an actual SDF content sample (just
    a "[<byte stream>]" placeholder) -- this assumes the same tab-delimited,
    quoted-header convention verified against the bulk Call Report download (a
    header row of MDRM codes, e.g. `"IDRSSD"\\tRCON2170\\t...`, then one data row).
    If a live pull comes back shaped differently, this is the one function to fix;
    ratios.py's field-level mapping is unaffected either way.
    """
    text = _decode_byte_field(raw_field).decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    codes = [c.strip().strip('"') for c in lines[0].split("\t")]
    values = lines[1].split("\t")
    return dict(zip(codes, values))


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), DATE_FMT).date()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized FFIEC datetime format: {text!r}")
