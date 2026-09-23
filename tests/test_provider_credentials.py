"""Credential resolution and diagnosis: whether a call can be made at all, and why not when it can't, across
`new_client`, `credential_present`, `credential_diagnosis` and the allowlist claims about them (#201, #332, #334, #365, #374)."""
import anthropic
import pytest
from _credentials import _clear_credential_env, _no_credentials
from _fakes import run_cli

from requivo.providers.anthropic import client as client_module
from requivo.providers.anthropic import new_client
from requivo.providers.anthropic.client import (
    _AUTH_ENV_VARS,
    _CREDENTIAL_ATTRS,
    credential_diagnosis,
    credential_present,
)
from requivo.providers.errors import EngineError

_NEEDS_CHAIN = pytest.mark.skipif(
    not hasattr(anthropic._client, "default_credentials"),
    reason="the installed anthropic SDK has no profile/federation discovery chain (the floor predates it). UNTESTED ON "
           "THIS SDK: that a credential resolved from a source other than the two env vars is not false-refused.")


@pytest.fixture
def unloadable_profile(monkeypatch):
    """Configured, and unloadable: the third state an env-var guard could not reach (#334)."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")


def test_a_missing_api_key_refuses_before_the_sdk_can_traceback(monkeypatch):
    """The most likely first failure of a fresh install, turned into one line naming every remedy (#201, #332)."""
    _no_credentials(monkeypatch)
    with pytest.raises(EngineError) as ei:
        new_client()
    msg = str(ei.value)
    assert all(s in msg for s in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", ".env", "requivo doctor", "requivo demo")), msg
    assert ei.value.to_dict()["code"] == "provider_unavailable", "the published code for 'this install cannot make a call'"


def test_credential_present_is_the_one_definition_new_client_reads(monkeypatch):
    """Each auth variable alone reads as present and is not false-refused (#332)."""
    _no_credentials(monkeypatch)
    assert credential_present() is False
    with pytest.raises(EngineError):
        new_client()
    for var in _AUTH_ENV_VARS:
        _no_credentials(monkeypatch)
        monkeypatch.setenv(var, "sk-ant-whatever")
        assert credential_present() is True and new_client() is not None, var


@_NEEDS_CHAIN
def test_a_federation_install_is_not_false_refused(monkeypatch):
    """The #334 defect itself: `new_client()` pre-flighted on `_AUTH_ENV_VARS`."""
    _clear_credential_env(monkeypatch)
    for var, value in (("ANTHROPIC_IDENTITY_TOKEN", "an-identity-token"), ("ANTHROPIC_FEDERATION_RULE_ID", "rule-1"),
                       ("ANTHROPIC_ORGANIZATION_ID", "org-1")):
        monkeypatch.setenv(var, value)
    assert credential_present() is True and new_client() is not None


def test_the_resolved_credential_attributes_are_read_through_getattr_defaults():
    """The SDK still exposes what the guard reads, and a missing attribute yields None rather than raising (#334)."""
    client = anthropic.Anthropic(api_key="sk-ant-whatever")
    for attr in _CREDENTIAL_ATTRS:
        if attr != "credentials" or hasattr(anthropic._client, "default_credentials"):
            assert hasattr(client, attr), f"the SDK no longer exposes `{attr}`; every install would read as credential-free"

    class _OldSdkClient:  # only the two attributes every supported major has
        api_key = None
        auth_token = None

    assert all(getattr(_OldSdkClient(), a, None) is None for a in _CREDENTIAL_ATTRS)


@_NEEDS_CHAIN
def test_an_unloadable_profile_is_refused_with_the_sdk_s_own_reason(unloadable_profile):
    with pytest.raises(EngineError) as ei:
        new_client()
    msg = str(ei.value)
    assert "could not load the credential configuration" in msg and "a-profile-that-does-not-exist" in msg
    assert "No Anthropic credential found" not in msg, "not an absent credential, so not that remedy"


@_NEEDS_CHAIN
def test_credential_present_does_not_raise_on_an_unloadable_profile(unloadable_profile):
    """`deterministic/doctor.py` calls `credential_present()` bare."""
    assert credential_present() is False


@_NEEDS_CHAIN
def test_credential_diagnosis_names_the_unloadable_profile_the_bool_hides(unloadable_profile):
    """The must-fire half: the bool collapses "none" and "configured and unloadable" onto one False (#365)."""
    present, problem = credential_diagnosis()
    assert present is False and problem is not None
    assert "could not load the credential configuration" in problem and "a-profile-that-does-not-exist" in problem
    assert "No Anthropic credential found" not in problem


@pytest.mark.parametrize("key, expected", [(None, (False, None)), ("sk-ant-whatever", (True, None))], ids=["missing", "set"])
def test_credential_diagnosis_agrees_with_credential_present_and_names_no_problem(monkeypatch, key, expected):
    """The must-not-fire twin: a genuinely missing or present credential carries no problem text (#365)."""
    _no_credentials(monkeypatch)
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    assert credential_diagnosis() == expected and credential_present() is expected[0]


def test_an_allowlist_reason_claiming_no_client_is_built_is_true_of_the_function_it_names(monkeypatch):
    """`_SURFACE_PROVIDER_ALLOWLIST`'s "no client is built" is a factual claim; two of three were wrong (#374)."""
    from test_source_form import _SURFACE_PROVIDER_ALLOWLIST

    calls = []
    original_init = anthropic.Anthropic.__init__

    def spy(self, *a, _orig=original_init, **kw):
        calls.append(1)
        return _orig(self, *a, **kw)

    monkeypatch.setattr(anthropic.Anthropic, "__init__", spy)
    _no_credentials(monkeypatch)
    credential_present()
    assert calls, "the spy did not see a construction it is known to make; the check below is inert"
    calls.clear()
    claims = {(label, name): getattr(client_module, name) for (label, name), reason in _SURFACE_PROVIDER_ALLOWLIST.items()
              if "no client is built" in reason}
    assert claims, "no allowlist entry claims 'no client is built' any more; the claim detection has drifted"
    for (label, name), fn in claims.items():
        fn()
        assert not calls, f"{label}:{name}'s reason says 'no client is built', but calling it constructed {len(calls)}"
        calls.clear()


def test_a_provider_verb_refuses_without_a_key_before_claiming_a_session(monkeypatch, workspace):
    """End to end: nothing is written and nothing is paid."""
    _no_credentials(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        run_cli(["discover", "a leave approval system", "--once"])
    assert ei.value.code == 1
    assert not list((workspace / ".requivo" / "sessions").glob("*")), "an upfront refusal that claims the slug moved the mess"


def test_the_typed_error_arms_are_inert_without_the_sdk():
    """What the auth and rate-limit arms catch when the SDK that defines them is not installed."""
    for name in ("AuthenticationError", "PermissionDeniedError", "RateLimitError"):
        cls = getattr(client_module, name)
        assert issubclass(cls, BaseException) and cls is not Exception, f"{name} bound to Exception swallows unrelated failures"
