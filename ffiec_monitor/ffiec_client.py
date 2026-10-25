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
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import requests

from .config import FFIECConfig

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
DATE_FMT = "%m/%d/%Y"

# Safety net for when proactive throttling (FFIECConfig.max_requests_per_hour)
# still isn't enough -- e.g. this UserID's quota is shared with other traffic
# this process doesn't know about. FFIEC's 403 body is documented to include a
# replenishment estimate ("Quota will be replenished in 12 minutes."); prefer
# that over a blind guess when present.
RATE_LIMIT_DEFAULT_BACKOFF_SECONDS = 300.0
RATE_LIMIT_MAX_RETRIES = 3
_REPLENISH_PATTERN = re.compile(r"replenished in (\d+)\s*minute", re.IGNORECASE)

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
        # Proactive pacing so a large run (e.g. the full ~4,500-institution
        # universe, mostly RetrieveFacsimile calls) never reaches FFIEC's own
        # ~2,500/hour ceiling in the first place. A live unthrottled run
        # measured ~1 request/sec (~3,600/hour) -- comfortably over quota on
        # anything beyond a few hundred institutions. 0 or negative disables
        # throttling entirely (e.g. for tests).
        self._min_interval = (
            3600.0 / config.max_requests_per_hour if config.max_requests_per_hour > 0 else 0.0
        )
        self._last_request_at: Optional[float] = None

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

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

        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            self._throttle()
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
                # Proactive throttling should make this rare; when it still
                # happens (e.g. quota shared with other traffic on this
                # UserID), back off and retry rather than crashing the whole
                # run over one institution -- FFIEC's own docs frame 403 as a
                # temporary, self-clearing condition, not a hard failure.
                if attempt < RATE_LIMIT_MAX_RETRIES:
                    backoff = _parse_replenish_seconds(message) or RATE_LIMIT_DEFAULT_BACKOFF_SECONDS
                    logger.warning(
                        "FFIEC PWS rate limit on %s (attempt %d/%d) -- backing off %.0fs: %s",
                        function, attempt + 1, RATE_LIMIT_MAX_RETRIES, backoff, message,
                    )
                    time.sleep(backoff)
                    continue
                raise FFIECRateLimitError(
                    f"FFIEC PWS rate limit ({detail}), still failing after "
                    f"{RATE_LIMIT_MAX_RETRIES} backoff retries. Per FFIEC's docs this "
                    "means out-of-quota (~2,500 req/hour), not an auth problem."
                )
            raise FFIECAPIError(f"FFIEC PWS error ({detail}). Raw body: {resp.text[:500]!r}")

        raise AssertionError("unreachable: retry loop must return or raise")

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


def _parse_replenish_seconds(message: str) -> Optional[float]:
    """Extracts a wait time from FFIEC's documented 403 body, e.g. "Out of call
    volume quota. Quota will be replenished in 12 minutes." Returns None (caller
    falls back to a fixed default) if the message doesn't match that shape --
    this is a best-effort parse of prose text, not a stable API contract.
    """
    match = _REPLENISH_PATTERN.search(message)
    if not match:
        return None
    minutes = int(match.group(1))
    return float(minutes * 60)


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

    The live layout is semicolon-delimited, one row per MDRM field (verified
    2026-10-25 against a real pull). Until then this assumed the bulk download's
    tab-delimited one-row layout, which parsed real facsimiles to nothing -- every
    stored ratio was None, so no Tier 1/2 alert could fire. That layout is still
    accepted as a fallback.
    """
    text = _decode_byte_field(raw_field).decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    # Live format (confirmed 2026-10-25 with scripts/probe_facsimile.py): semicolon-
    # delimited, ONE ROW PER FIELD --
    #   Call Date;Bank RSSD Identifier;MDRM #;Value;Last Update;Short Definition;...
    #   20260930;142179;RCOA7204;22.3548%;20261024;Leverage ratio (...);RCRI;31
    # The definition text can itself contain semicolons, so only the first four
    # columns are read positionally.
    header = [c.strip().strip('"') for c in lines[0].split(";")]
    if "MDRM #" in header and "Value" in header:
        code_i, value_i = header.index("MDRM #"), header.index("Value")
        out: dict[str, Any] = {}
        for line in lines[1:]:
            cols = line.split(";")
            if len(cols) > max(code_i, value_i):
                out[cols[code_i].strip().strip('"')] = cols[value_i].strip().strip('"')
        return out
    # Older assumption (bulk-download convention): tab-delimited header of codes + one data row.
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
