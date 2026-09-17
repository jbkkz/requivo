"""What "no ambient credential" means, in one place — shared by the suite-wide net and the tests that are
*about* credential discovery (#555)."""

# Every environment variable the SDK resolves a credential from (#334).
_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
)

# Where a call that escapes every other layer goes to die.
SINKHOLE_BASE_URL = "http://127.0.0.1:9"


def _clear_credential_env(monkeypatch):
    """Unset every credential variable, and leave the SDK's own discovery running."""
    for var in _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)


def _no_credentials(monkeypatch):
    """An install with no credential from *any* source the SDK reads, on any developer's machine."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)
