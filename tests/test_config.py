"""_require() strips whitespace -- a stray trailing newline/space from
copy-pasting a secret into GitHub's UI (or a local shell) can turn into a
malformed HTTP header later, which Azure's WAF can reject outright before
the app ever evaluates the real credential.
"""
import pytest

from ffiec_monitor.config import FFIECConfig, MissingSecretError


def test_trailing_newline_is_stripped(monkeypatch):
    monkeypatch.setenv("FFIEC_USER_ID", "PWSUser101\n")
    monkeypatch.setenv("FFIEC_TOKEN", "  eyJhbGciOi...  \n")
    config = FFIECConfig.from_env()
    assert config.user_id == "PWSUser101"
    assert config.token == "eyJhbGciOi..."


def test_whitespace_only_value_raises_missing_secret_error(monkeypatch):
    monkeypatch.setenv("FFIEC_USER_ID", "   \n")
    monkeypatch.setenv("FFIEC_TOKEN", "tok")
    with pytest.raises(MissingSecretError):
        FFIECConfig.from_env()
