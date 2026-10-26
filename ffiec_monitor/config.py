"""Environment/secret wiring. Nothing here is ever hardcoded -- see spec §1, §8, §9."""
from __future__ import annotations

import os
from dataclasses import dataclass


class MissingSecretError(RuntimeError):
    """Raised when a required secret/env var is absent -- fail loudly, never skip silently."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is not None:
        # A trailing newline/space from copy-pasting into GitHub's secret field
        # (or a local shell) turns into a malformed HTTP header value later --
        # Azure's WAF can reject that outright with a generic gateway error
        # page, before the app ever sees (or complains about) the real token.
        value = value.strip()
    if not value:
        raise MissingSecretError(
            f"Required environment variable {name!r} is not set. "
            "Set it as a GitHub Actions secret (see README) or in your local shell."
        )
    return value


@dataclass(frozen=True)
class FFIECConfig:
    user_id: str
    token: str
    base_url: str = "https://ffieccdr.azure-api.us/public"
    # FFIEC's documented ceiling is ~2,500 requests/hour. Default target stays
    # comfortably under that (not right at it) because the ceiling is FFIEC's
    # side of a shared quota (this UserID may see other traffic) and because
    # request pacing measured live (~1/sec unthrottled) would blow through
    # 2,500/hour on anything beyond a few hundred institutions -- see
    # ffiec_client.py's rate limiter. Override via FFIEC_MAX_REQUESTS_PER_HOUR
    # for a token known to have a different/dedicated quota.
    max_requests_per_hour: int = 2000

    @classmethod
    def from_env(cls) -> "FFIECConfig":
        return cls(
            user_id=_require("FFIEC_USER_ID"),
            token=_require("FFIEC_TOKEN"),
            base_url=os.environ.get("FFIEC_BASE_URL", cls.base_url),
            max_requests_per_hour=int(
                os.environ.get("FFIEC_MAX_REQUESTS_PER_HOUR", cls.max_requests_per_hour)
            ),
        )


@dataclass(frozen=True)
class EmailConfig:
    address: str
    app_password: str
    recipient: str

    @classmethod
    def from_env(cls) -> "EmailConfig":
        address = _require("EMAIL_ADDRESS")
        return cls(
            address=address,
            app_password=_require("EMAIL_APP_PASSWORD"),
            recipient=os.environ.get("ALERT_RECIPIENT") or address,   # an unset GitHub secret arrives as ""
        )


def get_duckdb_path() -> str:
    return os.environ.get("DUCKDB_PATH", "call_report_monitor.duckdb")
