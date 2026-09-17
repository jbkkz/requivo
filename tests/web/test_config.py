"""`web/config.py`'s provider probe -- #332."""
from __future__ import annotations

from requivo.web.config import provider_status


def _no_credentials(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_no_credential_reports_absent(monkeypatch):
    """The negative half of the pair below -- a probe that always said "present" would not be caught by the
    bearer-token test alone."""
    _no_credentials(monkeypatch)
    status = provider_status()
    assert status.key_present is False
    assert status.available is False
    assert "ANTHROPIC_API_KEY" in status.reason
    assert "ANTHROPIC_AUTH_TOKEN" in status.reason, (
        "the UI remedy must name every name `credential_present()` reads (#332 review), or a "
        "bearer-token-only reader is told to set a credential they already have a working "
        "equivalent of"
    )


def test_a_bearer_token_alone_is_read_as_a_credential(monkeypatch):
    """The specific defect #332 measured: `ANTHROPIC_AUTH_TOKEN` alone, no `ANTHROPIC_API_KEY`."""
    _no_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    status = provider_status()
    assert status.key_present is True


def test_the_api_key_alone_still_works(monkeypatch):
    """The case that already worked before #332's fix -- must not regress."""
    _no_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    status = provider_status()
    assert status.key_present is True
