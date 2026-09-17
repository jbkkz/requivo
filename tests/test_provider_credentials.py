"""Credential resolution and diagnosis: whether a call can be made at all, and why not when it can't -- across
every surface that asks (`new_client`, `credential_present`, `credential_diagnosis`, and the allowlist
claims `test_source_form.py` makes about them, #555)."""
import anthropic
import pytest
from _credentials import _clear_credential_env, _no_credentials
from _fakes import _run_app

from requivo.providers.anthropic import new_client
from requivo.providers.errors import EngineError

# ── #201: refusing before the SDK can traceback ──────────────────────────────


# The credential tuple and the two "no credential" helpers lived here through #334/#365.


def test_a_missing_api_key_refuses_before_the_sdk_can_traceback(monkeypatch):
    """The most likely first failure of a fresh install, turned into one line (#201)."""
    _no_credentials(monkeypatch)
    with pytest.raises(EngineError) as ei:
        new_client()
    msg = str(ei.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert "ANTHROPIC_AUTH_TOKEN" in msg, (
        "the remedy must name every name the guard it is the remedy for actually accepts (#332 "
        "review) -- naming one of two would send a bearer-token-only reader to set a credential "
        "they already have a working equivalent of"
    )
    assert ".env" in msg
    assert "requivo doctor" in msg
    assert "requivo demo" in msg, "the offline escape hatches are part of the remedy, not a footnote"
    assert ei.value.to_dict()["code"] == "provider_unavailable", (
        "the published code for 'this install cannot make a call' -- a new code here would be a "
        "breaking change to the --json envelope for no gain (see "
        "test_the_missing_web_extra_keeps_its_published_error_code)"
    )


def test_a_bearer_token_alone_is_not_false_refused(monkeypatch):
    """A guard meant to help must not refuse a setup that would have worked."""
    _no_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    assert new_client() is not None


# ── #332: one definition of "is there a credential", not three ──────────────


def test_credential_present_is_the_one_definition_new_client_reads(monkeypatch):
    """web/config.py and doctor.py each kept their own `os.getenv("ANTHROPIC_API_KEY")` (#332)."""
    from requivo.providers.anthropic.client import _AUTH_ENV_VARS, credential_present

    _no_credentials(monkeypatch)
    assert credential_present() is False
    with pytest.raises(EngineError):
        new_client()

    for var in _AUTH_ENV_VARS:
        _no_credentials(monkeypatch)
        monkeypatch.setenv(var, "sk-ant-whatever")
        assert credential_present() is True, f"{var} alone must read as present"
        assert new_client() is not None, f"{var} alone must not be false-refused"


# ── #334: the guard asks the SDK, instead of keeping a list of the names it reads ─────────────


def _sdk_has_credential_chain() -> bool:
    """Whether the installed SDK has the profile/federation discovery chain at all."""
    import anthropic._client as sdk_client

    return hasattr(sdk_client, "default_credentials")


_NEEDS_CHAIN = pytest.mark.skipif(
    not _sdk_has_credential_chain(),
    reason=(
        "the installed anthropic SDK has no profile/federation discovery chain (the floor of "
        "`anthropic>=0.42.0,<2` predates it). UNTESTED ON THIS SDK: that a credential resolved from "
        "a source other than ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is not false-refused. The legs "
        "on a current SDK do test it."
    ),
)



@_NEEDS_CHAIN
def test_a_federation_install_is_not_false_refused(monkeypatch):
    """The #334 defect itself. `new_client()` pre-flighted on `_AUTH_ENV_VARS`."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "an-identity-token")
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "rule-1")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-1")

    from requivo.providers.anthropic.client import credential_present

    assert credential_present() is True, "the SDK resolves a federation credential; the guard must see it"
    assert new_client() is not None


def test_the_resolved_credential_attributes_are_read_through_getattr_defaults():
    """Two halves of one promise, because each fails silently on its own."""
    import anthropic as sdk

    from requivo.providers.anthropic.client import _CREDENTIAL_ATTRS

    client = sdk.Anthropic(api_key="sk-ant-whatever")
    # `api_key` and `auth_token` exist on every major in `anthropic>=0.42.0,<2`.
    always = tuple(a for a in _CREDENTIAL_ATTRS if a != "credentials")
    for attr in always:
        assert hasattr(client, attr), (
            f"the SDK no longer exposes `{attr}`; the guard is reading a name that is gone and would "
            f"report every install as credential-free"
        )
    if _sdk_has_credential_chain():
        assert hasattr(client, "credentials"), (
            "this SDK has the discovery chain but no `credentials` attribute to leave its result on; "
            "the guard would resolve a profile or federation credential and then not see it"
        )

    # The other half, and the one that has to hold on *every* supported major.
    class _OldSdkClient:  # only the two attributes every supported major has
        api_key = None
        auth_token = None

    assert all(getattr(_OldSdkClient(), a, None) is None for a in _CREDENTIAL_ATTRS), (
        "reading a missing attribute must yield None, not raise -- the floor SDK has no `credentials`"
    )


@_NEEDS_CHAIN
def test_an_unloadable_profile_is_refused_with_the_sdk_s_own_reason(monkeypatch):
    """A third state the env-var guard could not reach: configured, and unloadable."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    with pytest.raises(EngineError) as ei:
        new_client()
    msg = str(ei.value)
    assert "could not load the credential configuration" in msg
    assert "a-profile-that-does-not-exist" in msg, "the SDK's own reason names the profile; keep it"
    assert "No Anthropic credential found" not in msg, (
        "a configured-but-unloadable profile is not an absent credential, and must not be given the "
        "remedy for one"
    )


@_NEEDS_CHAIN
def test_credential_present_does_not_raise_on_an_unloadable_profile(monkeypatch):
    """`deterministic/doctor.py` calls `credential_present()` bare."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    from requivo.providers.anthropic.client import credential_present

    assert credential_present() is False


# ── #365: `doctor` is a second reader, and needs more than the bool ─────────


@_NEEDS_CHAIN
def test_credential_diagnosis_names_the_unloadable_profile_the_bool_hides(monkeypatch):
    """The must-fire half. `credential_present()` collapses "no credential" and "a credential that is
    configured and unloadable" onto the same False."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    from requivo.providers.anthropic.client import credential_diagnosis

    present, problem = credential_diagnosis()
    assert present is False
    assert problem is not None
    assert "could not load the credential configuration" in problem
    assert "a-profile-that-does-not-exist" in problem, "the SDK's own reason names the profile"
    assert "No Anthropic credential found" not in problem, (
        "an unloadable profile is not an absent credential and must not carry that remedy"
    )


def test_credential_diagnosis_leaves_a_genuinely_missing_credential_unnamed(monkeypatch):
    """The must-not-fire twin. A "must not say X" assertion alone passes on a harness that produces no message
    at all."""
    _no_credentials(monkeypatch)

    from requivo.providers.anthropic.client import credential_diagnosis

    present, problem = credential_diagnosis()
    assert present is False
    assert problem is None


def test_credential_diagnosis_agrees_with_credential_present_when_a_key_is_set(monkeypatch):
    _no_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    from requivo.providers.anthropic.client import credential_diagnosis, credential_present

    assert credential_present() is True
    present, problem = credential_diagnosis()
    assert present is True
    assert problem is None


# ── #374: an allowlist reason claiming "no client is built" is a claim this suite checks ─────
#
# `tests/test_source_form.py`'s `_SURFACE_PROVIDER_ALLOWLIST` justifies several entries with the words "no client is built" -- a factual claim about the function the entry names, not a style note (#334).
#
# A reason nothing re-checks is prose (#374's own point, made about #364's incomplete sweep one entry over).


def _no_client_claims() -> dict[tuple[str, str], object]:
    """Every `_SURFACE_PROVIDER_ALLOWLIST` entry whose reason claims "no client is built" (#374)."""
    from test_source_form import _SURFACE_PROVIDER_ALLOWLIST

    from requivo.providers.anthropic import client as client_module

    claims = {}
    for (label, name), reason in _SURFACE_PROVIDER_ALLOWLIST.items():
        if "no client is built" not in reason:
            continue
        fn = getattr(client_module, name, None)
        assert fn is not None, (
            f"{label}:{name}'s allowlist reason claims 'no client is built', but {name!r} is not a "
            f"requivo.providers.anthropic.client function this check knows how to call -- extend "
            f"_no_client_claims rather than leaving the claim unchecked"
        )
        claims[(label, name)] = fn
    return claims


def test_an_allowlist_reason_claiming_no_client_is_built_is_true_of_the_function_it_names(monkeypatch):
    """#374. Two of three entries making this claim were wrong."""
    calls = []
    original_init = anthropic.Anthropic.__init__

    def spy(self, *a, _orig=original_init, **kw):
        calls.append(1)
        return _orig(self, *a, **kw)

    monkeypatch.setattr(anthropic.Anthropic, "__init__", spy)
    _no_credentials(monkeypatch)

    from requivo.providers.anthropic.client import credential_present

    credential_present()
    assert calls, "the spy did not see a construction it is known to make -- the check below is inert"
    calls.clear()

    claims = _no_client_claims()
    assert claims, (
        "no _SURFACE_PROVIDER_ALLOWLIST entry claims 'no client is built' any more -- if that is "
        "not expected, the claim-detection above has drifted from the allowlist's wording"
    )
    for (label, name), fn in claims.items():
        fn()
        assert not calls, (
            f"{label}:{name}'s allowlist reason says 'no client is built', but calling {name}() "
            f"constructed {len(calls)} Anthropic client(s) -- fix the allowlist reason (or the "
            f"function, if it should build none)"
        )
        calls.clear()


def test_a_provider_verb_refuses_without_a_key_before_claiming_a_session(monkeypatch, tmp_path):
    """End to end, and the part that is not about the message: nothing is written and nothing is paid."""
    _no_credentials(monkeypatch)
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit) as ei:
        _run_app(["discover", "a leave approval system", "--once"])
    assert ei.value.code == 1
    assert not list((tmp_path / ".requivo" / "sessions").glob("*")), (
        "an upfront refusal that still claims the slug has only moved the mess"
    )


def test_the_typed_error_arms_are_inert_without_the_sdk():
    """What the auth and rate-limit arms catch when the SDK that defines them is not installed."""
    from requivo.providers.anthropic import client as mod

    for name in ("AuthenticationError", "PermissionDeniedError", "RateLimitError"):
        cls = getattr(mod, name)
        assert issubclass(cls, BaseException)
        assert cls is not Exception, (
            f"{name} bound to Exception would make the {name} arm swallow unrelated failures"
        )
