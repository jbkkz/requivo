"""What "no ambient credential" means, in one place — shared by the suite-wide net and the tests
that are *about* credential discovery.

Not a test module and not a `conftest.py` — the `_fakes.py`/`_cli_harness.py` precedent, applied to
a third shared concern. This used to live inside `tests/test_provider.py` alone (now split into subject files by
#555), which made it
invisible to the rest of the suite; #419 is what that cost: with no suite-wide net, one journey
test reached the default provider path on a keyed machine and made a real paid Anthropic call.
`tests/conftest.py` now applies the environment half to every test; `test_provider_credentials.py`
and `test_cli_doctor.py` keep importing the same helpers for the tests that exercise the discovery
chain itself.
"""

# Every environment variable the SDK resolves a credential from, not just the two Requivo used to
# check. Since #334 the guard asks the SDK rather than reading a list, so a test that clears only
# `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` is not describing a credential-free install on a
# machine whose developer has a profile on disk -- it is describing whatever that machine happens to
# have, and it would go green or red for reasons no diff explains.
_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
)

# Where a call that escapes every other layer goes to die: an unroutable loopback port ("discard";
# nothing listens there), so an escaped request fails in milliseconds with a connection error
# instead of reaching Anthropic and billing. This is the net's second layer, for the one source the
# environment tuple cannot clear -- an active profile on the developer's disk resolves a real
# credential without a single variable set. The first layer (no variable resolves) already makes
# `new_client()` refuse pre-SDK on every machine without one, exactly as keyless CI does.
SINKHOLE_BASE_URL = "http://127.0.0.1:9"


def _clear_credential_env(monkeypatch):
    """Unset every credential variable, and leave the SDK's own discovery running.

    For the tests that are *about* discovery: they set one source up and assert the guard sees what
    the SDK resolved. Neutralising the chain there would assert against the stub instead of against
    the SDK, which is the one thing those tests exist to check.
    """
    for var in _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)


def _no_credentials(monkeypatch):
    """An install with no credential from *any* source the SDK reads, on any developer's machine.

    The environment half is the tuple above. The on-disk half -- the active profile under the SDK's
    config directory -- cannot be cleared by unsetting anything, so the SDK's own discovery entry
    point is neutralised instead.

    **`ANTHROPIC_CONFIG_DIR` is not the lever it looks like**, and that is worth the line it costs:
    pointing it at an empty or absent directory does not mean "find no profiles here", it makes the
    SDK *raise* `Config file not found ... (profile 'default')` out of the constructor -- and it does
    so even when federation environment variables are set, which would otherwise have resolved. A
    helper built on it turns every credential-free test into a test about a misconfigured config
    directory, which is a different thing that happens to also fail.

    `raising=False` because `default_credentials` does not exist on the older majors in
    `anthropic>=0.42.0,<2`: there is no discovery chain there to neutralise, and its absence is the
    same isolated state rather than an error.
    """
    _clear_credential_env(monkeypatch)
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)
