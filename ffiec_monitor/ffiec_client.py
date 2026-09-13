"""Client for the FFIEC CDR Public Web Service (PWS), REST/OAuth2 front end.

Spec §1: base URL https://ffieccdr.azure-api.us/public/<function>, auth via a
`UserID` header plus `Authentication: Bearer <token>` header, 90-day non-refreshing
JWT, 2,500 req/hour ceiling, and -- critically -- an auth failure must raise loudly
rather than be swallowed into a quiet empty run.

Endpoint names/params below follow the FFIEC CDR PWS's long-standing SOAP-era method
names (RetrieveFilersSubmissionDateTime, RetrieveFacsimile, RetrieveUBPRXBRLFacsimile,
RetrieveReportingPeriods) as re-exposed through the Azure API Management REST front
end. The exact query-string shape has not been verified against a live token in this
environment -- if FFIEC has since renamed a parameter, only `_get()`'s callers below
need to change, not the rest of the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import requests

from .config import FFIECConfig

REQUEST_TIMEOUT_SECONDS = 30


class FFIECAuthError(RuntimeError):
    """Raised on 401/403 from the PWS -- almost always an expired/missing 90-day token.

    This must propagate all the way out of the pipeline as a hard failure (spec §1),
    not be caught and treated as "no updates today".
    """


class FFIECAPIError(RuntimeError):
    """Any other non-2xx response from the PWS."""


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

    def _headers(self) -> dict[str, str]:
        return {
            "UserID": self._config.user_id,
            "Authentication": f"Bearer {self._config.token}",
            "Accept": "application/json",
        }

    def _get(self, function: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self._config.base_url}/{function}"
        resp = self._session.get(url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code in (401, 403):
            raise FFIECAuthError(
                f"FFIEC PWS auth failure ({resp.status_code}) calling {function}. "
                "FFIEC_TOKEN is very likely expired (90-day, no auto-refresh) -- "
                "renew it and update the GitHub Actions secret."
            )
        if not resp.ok:
            raise FFIECAPIError(f"FFIEC PWS error {resp.status_code} calling {function}: {resp.text[:500]}")
        return resp.json()

    # --- tier 1: daily lightweight ping, all institutions -----------------

    def retrieve_filer_statuses(self, reporting_period_end: date) -> list[FilerStatus]:
        """Latest filed period + submission/acceptance timestamp, all filers.

        Catches both new-quarter filings (period_end advances) and quiet restatements
        (submission_ts advances on an already-seen period) -- spec §2 step 1.
        """
        payload = self._get(
            "RetrieveFilersSubmissionDateTime",
            params={"reportingPeriodEndDate": reporting_period_end.isoformat(), "dataSeries": "Call"},
        )
        statuses = []
        for row in payload.get("filers", payload if isinstance(payload, list) else []):
            statuses.append(
                FilerStatus(
                    id_rssd=str(row["idRssd"]),
                    period_end=_parse_date(row.get("reportingPeriodEndDate", reporting_period_end)),
                    submission_ts=_parse_datetime(row["submissionDateTime"]),
                )
            )
        return statuses

    def retrieve_reporting_periods(self) -> list[date]:
        """All Call Report reporting period-end dates FFIEC currently has data for."""
        payload = self._get("RetrieveReportingPeriods", params={"dataSeries": "Call"})
        return sorted(_parse_date(p) for p in payload)

    # --- tier 2: full pull for institutions whose timestamp changed -------

    def retrieve_facsimile(self, id_rssd: str, period_end: date) -> dict[str, Any]:
        """Full Call Report data for one institution/period (facsimile, JSON/SDF)."""
        return self._get(
            "RetrieveFacsimile",
            params={
                "idRssd": id_rssd,
                "reportingPeriodEndDate": period_end.isoformat(),
                "facsimileFormat": "SDF",
            },
        )

    def retrieve_ubpr_facsimile(self, id_rssd: str, period_end: date) -> dict[str, Any]:
        """UBPR ratios (peer-relative) for one institution/period -- source of the
        12 ratios of concern, spec §5."""
        return self._get(
            "RetrieveUBPRXBRLFacsimile",
            params={"idRssd": id_rssd, "reportingPeriodEndDate": period_end.isoformat()},
        )


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
