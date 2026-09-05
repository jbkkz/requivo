"""The provider call: the `providers/anthropic/` package, driven offline against a canned client.

Split out of `test_engine.py` (#72). Everything here reaches the one place an LLM is called — the JSON
extraction and retry loop, the discovery turn and its completeness self-heal, the generators, the
context-card threading, and the prompt-cache breakpoints of #9. No real network: `FakeClient` returns
canned replies in order and records the request that came out, so each test asserts on what would have
been sent.
"""
import io
import json
from contextlib import redirect_stdout

import anthropic
import httpx
import pytest
from _credentials import _clear_credential_env, _no_credentials
from _fakes import _ENGINE_REPLY, FakeClient, _FakeBlock, _run_app, full_slots, out, slot

from requivo.core.contracts import PRD, Brief, EngineOutput, Stories, Story
from requivo.core.persistence import load_model
from requivo.providers.anthropic import (
    advise,
    answer_turn,
    current_model_name,
    derive_stories,
    generate_prd,
    new_client,
    run,
)
from requivo.providers.anthropic.completion import _complete, _extract_json, _response_text
from requivo.providers.anthropic.pricing import price_call
from requivo.providers.errors import EngineError
from requivo.render.markdown import prd_markdown
from requivo.render.terminal import render_stories
from requivo.usage import CallRecord, UsageLedger

# ── JSON extraction ──────────────────────────────────────────────────────────


def test_extract_json_strips_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_slices_surrounding_text():
    assert _extract_json('here it is: {"b": 2} — done') == {"b": 2}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        _extract_json("no json object anywhere")


# ── Characterization: discovery, generators, errors, context ─────────────────
# These pin CURRENT behavior (shapes, formats, error surfaces). They are not quality tests.


def test_run_returns_engine_output_and_wires_schema_and_context():
    # The --once discovery pass is a single run() call. Characterize its result
    # AND that the engine turn is driven by prompts/engine.md with schema + context injected.
    fake = FakeClient(_ENGINE_REPLY)
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert isinstance(result, EngineOutput)
    assert result.model["problem"].completeness == 80
    # system is a cache-controlled text block so its stable prefix is cached across calls.
    block = fake.calls[0]["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    system = block["text"]
    assert "slots" in system              # framework/model_schema.json injected ({{SCHEMA}})
    assert "## b2b-platform" in system    # context card injected ({{CONTEXT}})


def test_run_rejects_a_model_missing_required_slots(tmp_path, monkeypatch):
    # A discovery reply missing a required slot is refused: the completeness invariant is enforced at
    # the boundary. The FakeClient returns the same incomplete reply every retry, so run() gives up.
    from requivo.core.errors import ProviderOutputError

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},  # 1 of 15 required
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, incomplete, incomplete)  # every retry attempt
    # A RequivoError with a stable code, not a bare RuntimeError: the CLI's handler catches the former
    # and prints a clean message, and lets the latter through as a traceback.
    with pytest.raises(ProviderOutputError, match="missing required slots") as exc:
        run(fake, [{"role": "user", "content": "leave approval"}])
    assert exc.value.to_dict()["code"] == "provider_output_invalid"
    assert exc.value.details["attempts"] == 3


def test_run_self_heals_when_a_retry_completes_the_model():
    # The completeness check rides the existing retry loop: a first incomplete reply nudges the model,
    # and a complete reply on the next attempt is accepted. This is why the invariant is safe to
    # enforce on a non-deterministic model — an omission is corrected, not fatal.
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, _ENGINE_REPLY)  # 1st attempt short, 2nd complete
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert result.model["problem"].completeness == 80
    assert len(fake.calls) == 2  # it took a retry
    # the nudge names the missing slots so the model knows what to add
    nudge = fake.calls[1]["messages"][-1]["content"]
    assert "missing required slots" in nudge


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    # The --from path: reload a saved model and regenerate an artifact, no discovery.
    model = out({"problem": slot(80, "explicit", "high")})
    path = tmp_path / "model.json"
    path.write_text(model.model_dump_json())

    loaded = load_model(path)
    assert loaded.model["problem"].completeness == 80

    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval", "problem": "Approvals are lost in email."})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "generated by Requivo" in md


def test_derive_stories_returns_structured_stories():
    reply = json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})
    stories = derive_stories(FakeClient(reply), out({"problem": slot(80, "explicit", "high")}))
    assert isinstance(stories, Stories)
    assert [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    text = buf.getvalue()
    assert "=== USER STORIES ===" in text and "[S1] Submit a leave request" in text


def test_run_restricts_context_cards_when_only_given():
    # The --context selection threads run() → build_prompt() → load_context(): the assembled system
    # carries only the chosen card, so it can't dilute impact estimation with the others.
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}], only=["b2b-platform"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## b2b-platform" in system
    assert "## financial-reporting" not in system


def test_answer_turn_threads_the_discovery_context_cards():
    # A refinement turn must reason over the same cards the original discovery used, not silently all.
    fake = FakeClient(_ENGINE_REPLY)
    answer_turn(fake, out({"problem": slot(80, "explicit", "high")}), "req", "answers",
                only=["event-ops"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## event-ops" in system
    assert "## financial-reporting" not in system


def test_generators_thread_the_context_selection():
    # A generator grounds its artifact in the discovery's card subset, not the full set.
    fake = FakeClient(json.dumps({"complexity": "low", "solution": "S"}))
    advise(fake, out({"problem": slot(80, "explicit", "high")}), only=["financial-reporting"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## financial-reporting" in system
    assert "## b2b-platform" not in system


def test_response_text_concatenates_text_blocks_and_skips_others():
    class _Block:
        def __init__(self, type_, text=""):
            self.type = type_
            self.text = text

    class _Resp:
        content = [_Block("thinking", "IGNORE"), _Block("text", "abc"), _Block("text", "def")]

    assert _response_text(_Resp()) == "abcdef"


class _RaisingClient:
    """A client whose create() raises — to exercise the API-error boundary in _complete()."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


def test_complete_wraps_api_errors_as_a_clean_engine_error():
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "not modified" in str(ei.value)  # the reassurance that nothing was written


# ── #201: refusing before the SDK can traceback ──────────────────────────────


# The credential tuple and the two "no credential" helpers lived here through #334/#365; they
# moved to `tests/_credentials.py` when #419 made them load-bearing for the whole suite (the
# autouse net in `tests/conftest.py` reads the same tuple). The tests below keep exercising the
# SDK's real discovery chain through the imported helpers, exactly as before.


def test_a_missing_api_key_refuses_before_the_sdk_can_traceback(monkeypatch):
    """The most likely first failure of a fresh install, turned into one line.

    `Anthropic()` constructs fine with no credential -- it defers auth resolution to the first
    request and raises a bare `TypeError` from its own internals there. So the guard cannot be "did
    the client build?", and there was nothing else on the CLI paid path asking: `requivo discover`
    on a fresh `pip install requivo[anthropic]` produced a stack ending in the SDK, naming neither
    the environment variable, nor `.env`, nor `requivo doctor` (#201).

    `EngineError` and not `SystemExit`, deliberately: the CLI turns the former into a one-line stderr
    message *and* the `--json` envelope, and asserting the exit code instead would pass just as well
    against a `sys.exit()` that prints nothing a machine can read.
    """
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
    """The #334 defect itself, and the reason this file no longer keeps a list of variable names.

    `new_client()` pre-flighted on `_AUTH_ENV_VARS` -- two entries against the five sources the SDK
    documents. An install authenticating by workload identity federation therefore hit a refusal
    telling it to set `ANTHROPIC_API_KEY`, while a bare `Anthropic()` in the same shell resolved a
    credentials provider and would have made the call. Widening the tuple was rejected as the fix:
    the resolution order belongs to the SDK, so a copy of it here is a copy that goes stale on the
    SDK's schedule rather than on ours. Goes red on the tuple-based guard.
    """
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "an-identity-token")
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "rule-1")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-1")

    from requivo.providers.anthropic.client import credential_present

    assert credential_present() is True, "the SDK resolves a federation credential; the guard must see it"
    assert new_client() is not None


def test_the_resolved_credential_attributes_are_read_through_getattr_defaults():
    """Two halves of one promise, because each fails silently on its own.

    The names must be *real* -- an SDK that renamed `credentials` would make every install read as
    credential-free, which is a refusal nobody can argue with. And they must be read through a
    default, because the supported range is `anthropic>=0.42.0,<2` and the older majors have no
    `credentials` attribute at all; a bare `client.credentials` would turn the whole provider into an
    `AttributeError` on the floor this repository tests against.
    """
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

    `ANTHROPIC_PROFILE` naming a file that is not there makes the SDK raise out of its own
    constructor. Nothing here expected construction to fail -- the guard's whole premise was that
    `Anthropic()` never raises -- so it escaped `new_client()` as a traceback. It is an `EngineError`
    now, quoting the SDK, which names the missing file and the variable to change; and it is
    deliberately *not* the no-credential message, because telling someone to set `ANTHROPIC_API_KEY`
    when they have a profile pointed at the wrong path is the wrong remedy for the right symptom.
    """
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
# `tests/test_boundaries.py`'s `_SURFACE_PROVIDER_ALLOWLIST` justifies several entries with the
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
    from test_boundaries import _SURFACE_PROVIDER_ALLOWLIST

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
    `credential_diagnosis()` both build a client via `_resolve_client()` (since #334), and were
    corrected as part of this fix. `current_model_name` genuinely builds none and stays a claim.

    Positive control in the same fixture, and the must-fire half: `credential_present()` is known
    to construct a client and is deliberately *not* a registered claim after this fix, so calling it
    through the same spy proves the spy detects a real construction -- without this, a spy that
    silently caught nothing would make every assertion below pass for the wrong reason.
    """
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
    """What the auth and rate-limit arms catch when the SDK that defines them is not installed.

    The obvious binding for an unimportable error class is `Exception`, which is what `APIError`
    already does -- and it is wrong for these two. `except Exception` in the auth arm would catch
    every transport failure and answer a network drop with a credential remedy. A class nothing ever
    raises catches nothing, which is the correct behaviour: with no SDK there is no call to fail.
    """
    from requivo.providers.anthropic import client as mod

    for name in ("AuthenticationError", "PermissionDeniedError", "RateLimitError"):
        cls = getattr(mod, name)
        assert issubclass(cls, BaseException)
        assert cls is not Exception, (
            f"{name} bound to Exception would make the {name} arm swallow unrelated failures"
        )



# ── #201: which transport failures are worth retrying, and which are not ─────


def _api_status(cls, status: int):
    """An SDK status error of `cls`, built the way the SDK builds one."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls(message="boom", response=response, body=None)


def _complete_failing_with(exc):
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    return str(ei.value)


def test_an_auth_failure_names_the_key_and_does_not_advise_retry():
    """The message that was actively wrong, and the assertion that keeps it from coming back.

    `AuthenticationError`, `PermissionDeniedError` and `RateLimitError` are all `APIError`
    subclasses, so one `except APIError` arm answered a rejected key with "Retry the command in a
    moment" -- advice that never works on a 401, given to the single most likely failure of a fresh
    install, with the real cause reduced to a parenthetical class name (#201).

    The negative half is the load-bearing half: it is easy to add the key remedy and leave the retry
    sentence sitting underneath it, which reads as "your key is wrong, try again".
    """
    for cls in (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        msg = _complete_failing_with(_api_status(cls, 401 if cls is anthropic.AuthenticationError else 403))
        assert "ANTHROPIC_API_KEY" in msg, "the remedy is the message's whole job"
        assert "requivo doctor" in msg
        assert cls.__name__ in msg, "the SDK class is what a bug report is diagnosed from"
        assert "Retry the command in a moment" not in msg, (
            "retrying a rejected credential never helps, and saying so wastes the one line the "
            "operator reads"
        )
        assert "not modified" in msg


def test_a_rate_limit_says_so_and_does_not_send_the_operator_straight_back():
    msg = _complete_failing_with(_api_status(anthropic.RateLimitError, 429))
    assert "rate-limited" in msg
    assert "ANTHROPIC_API_KEY" not in msg, "a rate limit is not a credential problem"
    assert "Wait for the limit to reset" in msg


def test_a_connection_failure_keeps_the_wording_that_was_right_for_it():
    """The third branch exists to leave something alone.

    Splitting an over-general message is only an improvement if the case it was actually correct
    for still gets it: a connection drop, a timeout or a 5xx *is* transient, and "retry in a moment"
    is the right thing to say.
    """
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    msg = _complete_failing_with(exc)
    assert "Anthropic API unavailable" in msg
    assert "Retry the command in a moment" in msg
    assert "ANTHROPIC_API_KEY" not in msg


def test_a_typeerror_out_of_the_sdk_is_not_a_traceback():
    """The belt, and the shape of the defect it is a belt against.

    #201 was an SDK raising a bare `TypeError` out of its own auth resolution: not an `APIError`, so
    `_complete`'s transport arm did not see it, and not a `RequivoError`, so `cli.app()` did not
    either. It threaded through every handler in the product and reached the operator as twenty-five
    lines of stack. `new_client()` refuses upfront now, so this arm should be unreachable -- which is
    the point of testing it, because an unreachable arm nobody exercises is one that rots.
    """
    msg = _complete_failing_with(TypeError("Could not resolve authentication method."))
    assert "TypeError" in msg
    assert "ANTHROPIC_API_KEY" in msg
    assert "not modified" in msg


class _MaxTokensClient:
    """Returns a reply flagged as cut off at the token ceiling (stop_reason == 'max_tokens'),
    carrying whatever text it is given — so we can exercise both the broken- and complete-JSON cases."""

    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        text = self._text

        class _Resp:
            stop_reason = "max_tokens"
            content = [_FakeBlock(text)]
        return _Resp()


def test_complete_rejects_a_truncated_reply_that_fails_to_parse():
    # Genuine truncation: the JSON is cut off mid-object, so parsing fails and the ceiling is the
    # named cause — retrying at the same ceiling wouldn't help, so it fails fast and cleanly.
    client = _MaxTokensClient('{"model": {"problem":')
    with pytest.raises(EngineError) as ei:
        _complete(client, "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "max_tokens" in str(ei.value)


def test_complete_accepts_a_max_tokens_reply_whose_json_is_complete():
    # Parse-first: rich discovery outputs run right against the ceiling and can be flagged max_tokens
    # while still carrying complete, valid JSON. That must succeed — not be rejected as truncated.
    complete = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")),
                           "questions": [], "summary": {}})
    result = _complete(_MaxTokensClient(complete), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert result.model["problem"].completeness == 80


# ── Prompt-cache breakpoints: paid for only where the prefix is re-read (#9) ──
#
# `cache_control` costs 1.25x input to write and pays back at 0.1x on a read, so it is a saving only
# when the *same* system prompt is sent again inside the cache TTL. It is, across the calls of one
# operation — a JSON retry, converse()'s turns, a golden capture's K runs. It is not, across
# operations: `build_prompt()` substitutes the shared schema + context cards into a *per-operation*
# template, and every template places {{SCHEMA}}/{{CONTEXT}} near its end with an "Output format"
# section after them. The shared bulk is therefore a suffix, and a cache is a prefix match — no
# breakpoint placement can let a second operation hit a warm one. A one-shot generator was writing a
# cache it could never read, a flat ~25% premium on the largest part of its input.
#
# Every "must not fire" assertion below is paired with a "must fire" control in the same fixture: a
# fix that strips the directive everywhere breaks the operations where caching genuinely pays, and
# these tests fail on that too.


def _system_block(fake, i: int) -> dict:
    return fake.calls[i]["system"][0]


_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})


def test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call():
    # Both halves in ONE fixture. Discovery keeps the breakpoint (converse() loops it, the golden
    # harness loops it, a retry re-sends it); a one-shot generator does not.
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}])
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    assert _system_block(fake, 0)["cache_control"] == {"type": "ephemeral"}  # must fire
    assert "cache_control" not in _system_block(fake, 1)                     # must not fire


def test_the_provider_seam_is_single_call_on_both_analyze_branches():
    """#58: `AnthropicProvider.analyze` is one call per service operation, so it must not write a
    cache entry nothing reads — on *either* branch.

    Driven through the real object rather than by reading a signature, because the defect this
    replaces was a function that took `reuse_system` and dropped it on the floor: `analyze` could
    declare False, pass nothing down, and inherit `run()`'s True.

    **What this test isolates, stated precisely because the first draft of it overclaimed.** Only the
    `run()` arm is pinned here: dropping `analyze`'s explicit `reuse_system=False` on that arm makes
    this red, because `run()`'s own default is True. Dropping it on the `answer_turn` arm does *not*,
    because `answer_turn` already defaults to False — the explicit keyword there is a call-site
    declaration (the design asks for one) sitting on top of a default that agrees with it, not a
    second guard. What pins the `answer_turn` arm is the neighbouring
    `test_a_looping_caller_can_still_ask_for_the_breakpoint_back`, which fails if that default is
    flipped. Both branches are still driven here so the assertion covers the observable behaviour of
    each; the claim about which mutation each one catches is the part that has to be exact.

    The control is in the same fixture and it is the point: `run()` called directly — which is what
    `converse()` and the golden harness do — must still carry the directive. A change that strips
    the breakpoint from `run()` fails here rather than looking like this fix."""
    from requivo.providers.anthropic import AnthropicProvider

    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY, _ENGINE_REPLY)
    provider = AnthropicProvider(fake)

    provider.analyze("leave approval")                                     # first discovery
    provider.analyze("leave approval", current_model=model, answers="A")   # a refinement turn
    run(fake, [{"role": "user", "content": "leave approval"}])             # the multi-call caller

    assert "cache_control" not in _system_block(fake, 0), "a first discovery pays for a cache nothing reads"
    assert "cache_control" not in _system_block(fake, 1), "a refinement turn pays for a cache nothing reads"
    assert _system_block(fake, 2)["cache_control"] == {"type": "ephemeral"}, "converse() lost its breakpoint"
    # MUST FIRE: all three sent the same engine prompt, so the assertions above are about the
    # directive and not about three different system blocks.
    assert _system_block(fake, 0)["text"] == _system_block(fake, 1)["text"] == _system_block(fake, 2)["text"]


def test_a_looping_caller_can_still_ask_for_the_breakpoint_back():
    """The escape hatch, kept honest. `reuse_system` is a per-call-site decision, not a per-function
    one — the same `run()` is single-call under the provider seam and multi-call under `converse()`.
    A future surface that genuinely loops `answer_turn` passes True and gets the directive."""
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    answer_turn(fake, model, "leave approval", "A")
    answer_turn(fake, model, "leave approval", "A", reuse_system=True)
    assert "cache_control" not in _system_block(fake, 0)                     # must not fire
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


# A minimal contract-valid reply per generator, so the assertions below can drive the *real* call
# rather than reading a signature. An earlier version of this test checked
# `inspect.signature(fn).parameters["reuse_system"].default is False` and nothing else, which both
# reviewers independently called vacuous and they were right: a generator that declared the parameter
# and then ignored it — passing nothing to `_complete`, falling back to its `True` default — satisfied
# every assertion while writing exactly the cache entry #9 is about. A signature is not a behaviour.
_GENERATOR_REPLIES = {
    "brief": _BRIEF_REPLY,
    "stories": json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
    "prd": json.dumps({"title": "T", "problem": "P"}),
    "criteria": json.dumps({"title": "T", "features": [
        {"name": "F", "scenarios": [{"id": "SC1", "title": "T", "when": "w", "then": ["t"]}]}]}),
    "epic": json.dumps({"title": "T", "issues": [{"id": "E1", "title": "T"}]}),
    "release": json.dumps({"title": "T"}),
    "estimate": json.dumps({"items": [
        {"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
}

# What a generator takes beyond `(client, model)`. Keyed by generator so the fixture above stays one
# reply per registry entry and the equality below can be exact.
#
# `estimate` is the only one, because it is the only registry entry that is not the plain
# model → contract shape: it is a pipeline stage reading the *prior* stories, and it returns
# `(draft, soft_slots, confidence)` rather than a document. It is nonetheless in `_GENERATORS` and
# reached through the seam like every other operation — `cli.py:_cmd_estimate` calls
# `disco.reason_from(snap, "estimate", stories=stories)`, and `AnthropicProvider.generate` dispatches
# `fn(self.client, model, only=only, **kwargs)`. So `stories` is passed here as a keyword, the way
# the real dispatch passes it. (Until #146 this file said the opposite in both clauses — that
# `estimate` was outside `_GENERATORS` and that the CLI called it directly past the provider seam.
# Both had been false since #77 and #135.)
_GENERATOR_KWARGS = {
    "estimate": {"stories": Stories(stories=[Story(id="S1", title="T")])},
}


@pytest.mark.parametrize("artifact_type", sorted(_GENERATOR_REPLIES))
def test_every_generator_drives_a_real_call_without_a_cache_write(artifact_type):
    # Drives each registered generator for real and reads the request that came out, so a generator
    # that takes `reuse_system` and drops it on the floor fails here. Both arms in one test: the
    # default must not carry the directive, and `reuse_system=True` must — so "deleted it everywhere"
    # fails too, per generator rather than only for `brief`.
    from requivo.providers.anthropic.generators import _GENERATORS

    reply = _GENERATOR_REPLIES[artifact_type]
    extra = _GENERATOR_KWARGS.get(artifact_type, {})
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(reply, reply)
    _GENERATORS[artifact_type](fake, model, **extra)
    _GENERATORS[artifact_type](fake, model, **extra, reuse_system=True)
    assert "cache_control" not in _system_block(fake, 0), f"{artifact_type} pays for a cache nothing reads"
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}, f"{artifact_type} lost its opt-in"


def test_the_cache_fixture_covers_every_registered_generator():
    # The parametrization above reads its cases off `_GENERATOR_REPLIES`, so an eighth generator
    # registered tomorrow would ship with no cache assertion and nothing would go red — under a test
    # whose name reads *every generator*. That is a guard that did not run and a guard that found
    # nothing rendering identically, which is this repository's own recurring shape, and it had
    # already happened once: `estimate` joined `_GENERATORS` in v1.1.0 and the fixture stayed at six
    # (#146). Asserted in both directions, like the boundaries allowlist: a registry entry with no
    # reply is an uncovered generator, and a reply for a name the registry does not hold is a case
    # that stopped exercising anything.
    from requivo.providers.anthropic.generators import _GENERATORS

    assert set(_GENERATOR_REPLIES) == set(_GENERATORS)
    assert set(_GENERATOR_KWARGS) <= set(_GENERATORS)


def test_complete_still_defaults_to_caching_for_an_undeclared_caller():
    # The control for every assertion above. "Will this be sent again?" is the caller's question, and
    # a caller that has not considered it should pay the safe answer — 25% once — rather than silently
    # lose a real cache worth up to 90% per repeat. If this default ever flips, the generators' saving
    # stops being a decision and becomes the accident of a global.
    import inspect

    assert inspect.signature(_complete).parameters["reuse_system"].default is True
    fake = FakeClient(_BRIEF_REPLY)
    _complete(fake, "SYSTEM", [{"role": "user", "content": "u"}], Brief)
    assert _system_block(fake, 0)["cache_control"] == {"type": "ephemeral"}


def test_a_generator_can_opt_back_in_when_its_caller_loops_it():
    # scripts/golden_run.py --brief calls advise() K times with one system prompt, so the harness is a
    # genuine re-reader. The escape hatch has to actually reach the request, not just exist.
    fake = FakeClient(_BRIEF_REPLY, _BRIEF_REPLY)
    model = out({"problem": slot(80, "explicit", "high")})
    advise(fake, model)                       # production: one call
    advise(fake, model, reuse_system=True)    # harness: K calls, same prompt
    assert "cache_control" not in _system_block(fake, 0)                     # must not fire
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


def test_skipping_the_breakpoint_does_not_change_the_system_prompt_bytes():
    # The cheap fix must stay a cheap fix. Moving the shared bulk to the front of every template is
    # the other way to make this pay, and it changes what the model reads — a behaviour change that
    # owes the golden harness a cycle. This pins that no such reordering rode along: the text sent is
    # still exactly what build_prompt() assembles.
    from requivo.core.context import build_prompt

    fake = FakeClient(_BRIEF_REPLY)
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    assert _system_block(fake, 0)["text"] == build_prompt("brief.md", None)


def test_retry_resends_a_byte_identical_system_whether_or_not_it_is_cached():
    # The intra-operation invariant, on both arms: a retry must re-send the same bytes, or the cache
    # is lost exactly where it does pay. Asserted for the cached arm too, so a future edit that makes
    # the directive conditional on the attempt number fails here.
    for reuse, expect_directive in ((True, True), (False, False)):
        fake = FakeClient("not json at all", _BRIEF_REPLY)
        _complete(fake, "SYSTEM PROMPT", [{"role": "user", "content": "u"}], Brief,
                  reuse_system=reuse)
        assert len(fake.calls) == 2, "expected one retry"
        assert _system_block(fake, 0)["text"] == _system_block(fake, 1)["text"] == "SYSTEM PROMPT"
        for i in (0, 1):
            assert ("cache_control" in _system_block(fake, i)) is expect_directive


def test_cost_estimate_bills_a_write_premium_and_plain_input_differently():
    # Not a new-behaviour test — a guard that the fix's whole point survives in the rendered number.
    # The ledger prices what the API *reported*, so dropping the directive moves those tokens from
    # cache_write (1.25x) to input (1.0x) with no arithmetic change here. If these two ever bill the
    # same, the saving becomes invisible and the issue's "the number rendered is correct" stops
    # holding.
    # The ratio, not the two dollar figures, is what this asserts. Stated absolutely it read as a
    # guard on the write premium and behaved as a guard on Sonnet 5's rate, so a rate correction went
    # red here with nothing to say about caching at all (#254).
    from datetime import date

    on = date(2026, 9, 1)
    cached = UsageLedger()
    cached.record(price_call(CallRecord(model="claude-sonnet-5", cache_write_tokens=1_000_000), on))
    plain = UsageLedger()
    plain.record(price_call(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000), on))
    assert cached.cost_usd() == pytest.approx(plain.cost_usd() * 1.25)
    assert cached.cost_usd() > plain.cost_usd()


def test_current_model_name_reads_env_override(monkeypatch):
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    assert current_model_name() == "claude-sonnet-5"


def test_current_model_name_prefers_requivo_model_over_the_default(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_falls_back_to_bare_model(monkeypatch):
    # #268: the bare MODEL name predates REQUIVO_MODEL and existing setups must keep working --
    # it is read only when REQUIVO_MODEL is unset.
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_prefers_requivo_model_when_both_are_set(monkeypatch):
    # The precedence case #268 exists to pin: a shell that already exports a generic MODEL for some
    # other tool must not silently steer Requivo once REQUIVO_MODEL is set alongside it.
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


# ── #283: the malformed reply survives retry give-up ─────────────────────────
# `_complete`'s give-up exit used to discard the raw reply along with the retry loop's local state.
# The final reply that never validated is now written to `.requivo/debug/` and named in the raised
# error, so a bug report has something to attach and the maintainer can tell a prompt regression from
# a model-side change. Successful calls and transport-level failures write nothing -- there is no
# malformed reply to save on either path, which the two negative tests below assert with a positive
# control each (the give-up test itself), so a broken harness that writes nothing on *every* path
# cannot pass all three silently.


class _RaisingTransportClient:
    """Raises the SDK's own transport error before any reply exists — the `except APIError` exit,
    which has no raw text to save."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


def test_a_retry_give_up_saves_the_final_raw_reply_and_names_it_in_the_error(tmp_path, monkeypatch):
    """The positive control: drive `_complete` all the way to give-up and check the file it leaves
    behind, not just that an error was raised."""
    from requivo.core.errors import ProviderOutputError

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    bad_reply = '{"not": "an engine output"}'
    fake = FakeClient(bad_reply, bad_reply, bad_reply)  # every retry attempt, never conforms
    with pytest.raises(ProviderOutputError) as exc:
        run(fake, [{"role": "user", "content": "leave approval"}])

    saved = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved) == 1, "the give-up exit must write exactly one debug file"
    assert saved[0].read_text(encoding="utf-8") == bad_reply, (
        "the saved file must hold the exact final raw reply, byte for byte -- not a summary of it"
    )
    assert str(saved[0]) in str(exc.value), (
        "the error message must name the path a bug report should attach"
    )
    assert exc.value.details.get("raw_reply_path") == str(saved[0])


def test_a_successful_call_writes_no_debug_file(tmp_path, monkeypatch):
    """The negative half. Passes trivially if nothing ever writes the debug file at all -- which is
    exactly why the give-up test above exists as the paired positive control."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}])
    assert not (tmp_path / ".requivo" / "debug").exists(), (
        "a successful call has nothing to debug and must write nothing"
    )


def test_a_transport_failure_writes_no_debug_file(tmp_path, monkeypatch):
    """The other negative half: an `EngineError` alone is not the trigger -- only give-up is. A
    transport failure never reaches the JSON/contract stage, so there is no raw reply to save."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(EngineError):
        run(_RaisingTransportClient(), [{"role": "user", "content": "leave approval"}])
    assert not (tmp_path / ".requivo" / "debug").exists(), (
        "a transport-level failure has no raw reply to save and must write nothing"
    )


def test_a_prune_failure_does_not_discard_an_already_saved_reply(tmp_path, monkeypatch):
    """Found in review: `_prune_debug_dir`'s `unlink` calls carried no exception handling of their
    own, so a failure there -- this codebase already knows a `PermissionError` on an open handle is
    real and platform-specific, invariant 18's own `_replace_with_retry` exists because of it --
    propagated up into `_save_failed_reply`'s broad `except Exception: return None` and discarded the
    path of a reply that had, moments earlier, been written successfully. The write and the prune are
    two separate failure domains now: a prune failure must never un-report a completed write.
    """
    from requivo.providers.anthropic import completion as mod

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))

    def _raise(root):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(mod, "_prune_debug_dir", _raise)
    path = mod._save_failed_reply("some raw reply text", "EngineOutput")
    assert path is not None, (
        "a write that succeeded must still be reported even when the prune right after it fails"
    )
    assert path.read_text(encoding="utf-8") == "some raw reply text"


# ── #434: a constructor-level model id ────────────────────────────────────────
# `AnthropicProvider` used to resolve the model ambiently, per call, through
# `current_model_name()` -- an env-chain read -- so one process could not run two models
# concurrently, and per-tenant/per-session model choice meant mutating process env, which races
# across concurrent calls. An optional `model=` on the constructor threads a fixed id into every
# completion call, `model_name()` and provenance; `model=None` (the default) stays byte-identical
# to the env-chain resolution.


def test_two_constructed_providers_record_and_price_independently(monkeypatch):
    """Acceptance criterion 1, first half: two `AnthropicProvider`s in one process, each built with
    its own model id, make independent calls and are billed at their own model's rate -- no state
    shared between them."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.usage import track_usage

    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")  # ambient value neither provider should use
    sonnet = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-sonnet-5")
    haiku = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")

    with track_usage() as ledger:
        sonnet.analyze("leave approval")
        haiku.analyze("leave approval")

    assert [c.model for c in ledger.calls] == ["claude-sonnet-5", "claude-haiku-4-5"]
    sonnet_rate, haiku_rate = ledger.calls[0].rate_per_mtok, ledger.calls[1].rate_per_mtok
    assert sonnet_rate is not None and haiku_rate is not None and sonnet_rate != haiku_rate, (
        "each call must be priced at its own model's rate, independently of the other"
    )


def test_a_constructed_model_makes_no_env_read(monkeypatch):
    """Acceptance criterion 1, second half, with its required positive control in the same fixture:
    a provider constructed with a model id must not consult `REQUIVO_MODEL`/`MODEL` at all -- proven
    by setting the environment to a *third*, distinct value the constructed provider must never
    produce, so a harness where the environment merely happened to be empty cannot make this pass by
    accident. The default-constructed provider alongside it DOES read the environment (the must-fire
    control), so a broken plumbing that ignores the constructor argument entirely cannot pass either."""
    from requivo.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")

    constructed = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")
    constructed.analyze("leave approval")
    assert constructed.client.calls[0]["model"] == "claude-haiku-4-5", (
        "a constructed model id must win over the ambient REQUIVO_MODEL, not merely agree with it"
    )
    assert constructed.model_name() == "claude-haiku-4-5"

    # MUST FIRE: the positive control -- default construction still reads the environment.
    ambient = AnthropicProvider(FakeClient(_ENGINE_REPLY))
    ambient.analyze("leave approval")
    assert ambient.client.calls[0]["model"] == "claude-opus-4-8"
    assert ambient.model_name() == "claude-opus-4-8"


def test_default_construction_is_byte_identical_to_before_434(monkeypatch):
    """Acceptance criterion 2: `AnthropicProvider()`/`AnthropicProvider(client)` with no `model=`
    keyword is pinned to the pre-#434 behaviour -- every call and `model_name()` still resolve
    through `current_model_name()`'s env chain, unchanged."""
    from requivo.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("REQUIVO_MODEL", "claude-haiku-4-5")
    fake = FakeClient(_ENGINE_REPLY)
    provider = AnthropicProvider(fake)
    provider.analyze("leave approval")
    assert fake.calls[0]["model"] == current_model_name() == "claude-haiku-4-5"
    assert provider.model_name() == current_model_name()


def test_generate_threads_the_constructed_model_too():
    """The issue's own direction says 'threaded into the completion calls' (plural): `generate()`
    reaches `_complete` exactly like `analyze()` does, through a different registry entry, and must
    carry the same constructed model id -- not only the discovery turn."""
    from requivo.providers.anthropic import AnthropicProvider

    fake = FakeClient(_BRIEF_REPLY)
    provider = AnthropicProvider(fake, model="claude-haiku-4-5")
    model = out({"problem": slot(80, "explicit", "high")})
    provider.generate("brief", model)
    assert fake.calls[0]["model"] == "claude-haiku-4-5"
