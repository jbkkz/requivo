"""Credential resolution and diagnosis: whether a call can be made at all, and why not when it
can't -- across every surface that asks (`new_client`, `credential_present`, `credential_diagnosis`,
and the allowlist claims `test_source_form.py` makes about them).

Split out of `test_provider.py` (#555) once that file grew past the module ceiling.
"""
import anthropic
import pytest
from _credentials import _clear_credential_env, _no_credentials
from _fakes import _run_app

from requivo.providers.anthropic import new_client
from requivo.providers.errors import EngineError

# ── #201: refusing before the SDK can traceback ──────────────────────────────


# The credential tuple and the two "no credential" helpers lived here through #334/#365; they
# moved to `tests/_credentials.py` when #419 made them load-bearing for the whole suite (the
# autouse net in `tests/conftest.py` reads the same tuple). The tests below keep exercising the
# SDK's real discovery chain through the imported helpers, exactly as before.


def test_a_missing_api_key_refuses_before_the_sdk_can_traceback(monkeypatch):
    """The most likely first failure of a fresh install, turned into one line. `Anthropic()`
    constructs fine with no credential and raises a bare `TypeError` from its own internals on the
    first request, so the guard cannot be "did the client build?" (#201). `EngineError`, not
    `SystemExit`: the CLI turns it into a stderr line and the `--json` envelope; asserting the exit
    code alone would pass against a `sys.exit()` that prints nothing a machine can read."""
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
    """A guard meant to help must not refuse a setup that would have worked.

    The SDK authenticates from `ANTHROPIC_AUTH_TOKEN` as well as `ANTHROPIC_API_KEY`. Checking only
    the key would turn a working bearer-token install into a hard refusal it cannot argue with --
    the failure mode of every upfront check that knows less than the thing it is guarding.
    """
    _no_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    assert new_client() is not None


# ── #332: one definition of "is there a credential", not three ──────────────


def test_credential_present_is_the_one_definition_new_client_reads(monkeypatch):
    """web/config.py and doctor.py each kept their own `os.getenv("ANTHROPIC_API_KEY")`, so a
    bearer-token install that `new_client()` accepted rendered as "no key" on both surfaces (#332).
    `credential_present()` is meant to be the one definition every reader shares -- assert it agrees
    with `new_client()` on *every* name in `_AUTH_ENV_VARS`, not just the historical
    `ANTHROPIC_API_KEY` case, so a future widening of that tuple cannot repeat the drift silently.
    """
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
    """Whether the installed SDK has the profile/federation discovery chain at all.

    `anthropic>=0.42.0,<2` spans SDKs that resolve two environment variables and SDKs that resolve
    five sources, and the **Dependency floor** leg installs the former -- which is the point of that
    leg, and why it is the one that caught this.

    The production guard needs no branch for it: `_resolve_client` reads the three attributes through
    `getattr` defaults, so an SDK with no `credentials` attribute simply has no such source and
    resolves from the two variables it does understand. The *tests* do need one. A test that exports
    federation variables and asserts a credential resolved is asserting a capability the floor does
    not have, and it fails there for a reason that is not a defect.

    Skipped with a stated reason rather than weakened to pass everywhere: the assertion is the whole
    value of the test, and a version-agnostic rewrite of it would assert nothing on any leg. The
    modern-SDK legs keep it; this one records that it could not look.
    """
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
    """The #334 defect itself. `new_client()` pre-flighted on `_AUTH_ENV_VARS` -- two entries
    against the five sources the SDK documents -- so a federation install hit a refusal telling it
    to set `ANTHROPIC_API_KEY` while a bare `Anthropic()` in the same shell would have worked.
    Widening the tuple was rejected: the resolution order belongs to the SDK, so a copy of it here
    goes stale on the SDK's own schedule rather than on ours."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "an-identity-token")
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "rule-1")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-1")

    from requivo.providers.anthropic.client import credential_present

    assert credential_present() is True, "the SDK resolves a federation credential; the guard must see it"
    assert new_client() is not None


def test_the_resolved_credential_attributes_are_read_through_getattr_defaults():
    """Two halves of one promise, because each fails silently on its own. The names must be real --
    an SDK that renamed `credentials` would make every install read as credential-free, a refusal
    nobody can argue with. And they must be read through a default, because the supported range is
    `anthropic>=0.42.0,<2` and older majors have no `credentials` attribute at all; a bare
    `client.credentials` would turn the whole provider into an `AttributeError` on the tested floor."""
    import anthropic as sdk

    from requivo.providers.anthropic.client import _CREDENTIAL_ATTRS

    client = sdk.Anthropic(api_key="sk-ant-whatever")
    # `api_key` and `auth_token` exist on every major in `anthropic>=0.42.0,<2`; `credentials` is the
    # one that does not, so it is checked only where the chain that populates it exists. Asserting it
    # unconditionally is what went red on the Dependency floor leg -- a test contradicting the
    # `getattr` default it was written to justify.
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

    # The other half, and the one that has to hold on *every* supported major: an SDK object missing
    # an attribute must read as "no credential from that source", never as an AttributeError.
    class _OldSdkClient:  # only the two attributes every supported major has
        api_key = None
        auth_token = None

    assert all(getattr(_OldSdkClient(), a, None) is None for a in _CREDENTIAL_ATTRS), (
        "reading a missing attribute must yield None, not raise -- the floor SDK has no `credentials`"
    )


@_NEEDS_CHAIN
def test_an_unloadable_profile_is_refused_with_the_sdk_s_own_reason(monkeypatch):
    """A third state the env-var guard could not reach: configured, and unloadable.
    `ANTHROPIC_PROFILE` naming a missing file makes the SDK raise out of its own constructor; it now
    surfaces as an `EngineError` quoting the SDK, naming the missing file and the variable to change
    -- deliberately not the no-credential message, since telling someone to set `ANTHROPIC_API_KEY`
    when their profile points at the wrong path is the wrong remedy for the right symptom."""
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
    """`deterministic/doctor.py` calls `credential_present()` bare, so the verb that answers *is this
    install healthy* would traceback on exactly the unhealthy install it exists to describe. False,
    not an exception -- the detail belongs to `new_client()`, which has somewhere to put it."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    from requivo.providers.anthropic.client import credential_present

    assert credential_present() is False


# ── #365: `doctor` is a second reader, and needs more than the bool ─────────


@_NEEDS_CHAIN
def test_credential_diagnosis_names_the_unloadable_profile_the_bool_hides(monkeypatch):
    """The must-fire half. `credential_present()` collapses "no credential" and "a credential that
    is configured and unloadable" onto the same False -- correct for a caller that only ever wanted
    a yes/no, and the wrong answer for `doctor`, whose whole job is naming the remedy. This is the
    second reader's version: `present` is still False, but `problem` names the SDK's own reason,
    the same text `new_client()` raises with (`test_an_unloadable_profile_is_refused_with_the_sdk_s_own_reason`).
    """
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
    """The must-not-fire twin. A "must not say X" assertion alone passes on a harness that produces
    no message at all -- this is the "must say nothing extra" case genuinely reached: no credential
    anywhere, so there is no SDK exception to report, and `problem` must stay `None` rather than
    manufacture one. Without this, a `credential_diagnosis()` that always returned some placeholder
    string would satisfy the positive test above and still be wrong here.
    """
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
# `tests/test_source_form.py`'s `_SURFACE_PROVIDER_ALLOWLIST` justifies several entries with the
# words "no client is built" -- a factual claim about the function the entry names, not a style
# note. Measured once by spying on `Anthropic.__init__`: since #334, `credential_present()` and
# `credential_diagnosis()` both route through `_resolve_client()`, which does construct a client
# (transient, discarded, no network call) to ask the SDK's own resolution chain. The allowlist
# reasons for those two no longer make the claim (see the entries themselves); `current_model_name`
# still does, correctly, and stays registered below so the check has something to hold true.
#
# A reason nothing re-checks is prose (#374's own point, made about #364's incomplete sweep one
# entry over). This ties the claim to the actual call so the next stale one is a failing test
# instead of a paragraph nobody re-reads.


def _no_client_claims() -> dict[tuple[str, str], object]:
    """Every `_SURFACE_PROVIDER_ALLOWLIST` entry whose reason claims "no client is built",
    resolved to the `requivo.providers.anthropic.client` function it names.

    Derived from the allowlist's own text rather than hand-copied, so a future entry reusing this
    exact phrase is picked up automatically -- and a name this function cannot resolve fails loudly
    rather than being silently left unchecked, which is the failure mode #374 is itself an instance
    of (`credential_diagnosis`'s reason went stale and nothing re-read it).
    """
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
    """#374. Two of three entries making this claim were wrong: `credential_present()` and
    `credential_diagnosis()` both build a client via `_resolve_client()` (since #334); corrected
    here. Positive control in the same fixture, and the must-fire half: `credential_present()` is
    known to construct a client and is deliberately not a registered claim, so calling it through
    the spy proves the spy detects a real construction."""
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
    """End to end, and the part that is not about the message: nothing is written and nothing is paid.

    The alternative to an upfront refusal is not merely an uglier error -- it is a session claimed at
    revision 0 and a billed call that 401s, which the operator then has to clean up.
    """
    _no_credentials(monkeypatch)
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit) as ei:
        _run_app(["discover", "a leave approval system", "--once"])
    assert ei.value.code == 1
    assert not list((tmp_path / ".requivo" / "sessions").glob("*")), (
        "an upfront refusal that still claims the slug has only moved the mess"
    )


def test_the_typed_error_arms_are_inert_without_the_sdk():
    """What the auth and rate-limit arms catch when the SDK that defines them is not installed. The
    obvious binding for an unimportable error class is `Exception`, which is what `APIError` already
    does -- and it is wrong for these two: `except Exception` in the auth arm would catch every
    transport failure and answer a network drop with a credential remedy. A class nothing ever
    raises catches nothing, which is correct: with no SDK there is no call to fail."""
    from requivo.providers.anthropic import client as mod

    for name in ("AuthenticationError", "PermissionDeniedError", "RateLimitError"):
        cls = getattr(mod, name)
        assert issubclass(cls, BaseException)
        assert cls is not Exception, (
            f"{name} bound to Exception would make the {name} arm swallow unrelated failures"
        )
