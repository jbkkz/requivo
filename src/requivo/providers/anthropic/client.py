"""The SDK handle and the model id — the two facts about the vendor that everything else needs.

`anthropic` is an **optional** dependency (`requivo[anthropic]`); importing this module without the
SDK installed binds `Anthropic` to None and raises a clean, actionable error at `new_client()`
rather than an ImportError deep in a call stack. That is why the whole package can be imported by a
surface that only wants to probe whether the extra is present (`web/config.py`).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from requivo.providers.errors import EngineError

if TYPE_CHECKING:
    # A separately-named, type-checking-only alias of the real SDK class -- pyright merges every
    # binding of one name into one declared type, so importing the real `Anthropic` here too would
    # still widen to `type[Anthropic] | None` from the runtime fallback below.
    # `decision: type-checking-only-anthropic-alias`
    from anthropic import Anthropic as _AnthropicClient

try:  # The SDK is an optional extra: the deterministic core + CLI work without it (Claude Code mode).
    from anthropic import Anthropic, APIError, AuthenticationError, PermissionDeniedError, RateLimitError
except ImportError as _e:  # pragma: no cover - exercised only in a no-SDK install
    Anthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    # Bound to a private sentinel rather than to `Exception`, and the difference is the whole point:
    # `except Exception` would make `completion.py`'s auth arm swallow every transport failure and
    # advise a key remedy for a network drop. A class nothing ever raises catches nothing, which is
    # the correct behaviour when the SDK that defines these is absent -- and the SDK being absent
    # means no call can be made anyway. Pinned by `test_the_typed_error_arms_are_inert_without_the_sdk`.
    class _NeverRaised(Exception):
        """Unreachable stand-in for an SDK error class that is not importable."""

    AuthenticationError = _NeverRaised  # type: ignore[assignment,misc]
    PermissionDeniedError = _NeverRaised  # type: ignore[assignment,misc]
    RateLimitError = _NeverRaised  # type: ignore[assignment,misc]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

MODEL_DEFAULT = "claude-sonnet-5"

# The two environment variables a user is *told* about, and the only two this file still names.
# They are the remedy in `_NO_KEY_MESSAGE`, not the decision: nothing here branches on them, because
# a hand-kept list of the names the SDK reads is what #334 was.
#
# It was the decision for two releases, and it was two entries short of five. The installed SDK
# resolves credentials from explicit arguments, then these two variables, then `ANTHROPIC_PROFILE`,
# then workload identity federation, then the active profile on disk -- so an install authenticating
# by profile or federation was refused by Requivo *before* the SDK was given a chance, and told to
# set a variable, while a bare `Anthropic()` in the same shell would have built a working client.
# Widening the tuple would have restored the same defect one release later, against a resolution
# order this repository does not own. `_resolve_client` asks the SDK instead.
_AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# The three attributes the SDK exposes its resolved credential on, read through `getattr` defaults
# because the supported range is `anthropic>=0.42.0,<2` and `credentials` -- the profile/federation
# provider -- does not exist on the older majors. An SDK without the attribute has no such source,
# so `None` is the right answer there rather than a crash. Pinned by
# `test_the_resolved_credential_attributes_are_read_through_getattr_defaults`.
_CREDENTIAL_ATTRS = ("api_key", "auth_token", "credentials")


# Names every `_AUTH_ENV_VARS` entry and says "resolved none" rather than "none found" -- a
# narrower remedy or a stronger claim silently mis-refuses a working bearer-token or federation
# install (#332, #334). Pinned by `test_credential_present_is_the_one_definition_new_client_reads`
# and `test_a_federation_install_is_not_false_refused`.
_NO_KEY_MESSAGE = (
    "No Anthropic credential found: the SDK resolved none from the environment, from a profile, or "
    "from workload identity federation. The usual fix is to set ANTHROPIC_API_KEY (or "
    "ANTHROPIC_AUTH_TOKEN for a bearer-token setup) in your environment, or to put it in a `.env` "
    "file in the directory you run from (see .env.example). `requivo doctor` reports whether a "
    "credential is visible. You do NOT need one for `requivo demo`, for the offline verbs (status, "
    "impact, session, model, artifact), or for Requivo inside Claude Code."
)


def _resolve_client() -> tuple[object | None, str | None]:
    """Ask the SDK to resolve credentials, and return `(client, problem)` -- exactly one of them set.

    **The SDK runs its whole resolution chain in `Anthropic.__init__`, offline**, and leaves the
    result on the instance. Measured against 0.122.0: nothing set leaves all three attributes None;
    `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` each land on their own attribute; federation
    environment variables land a provider on `credentials`; and construction takes single-digit
    milliseconds with no network call. So the question "does this install have a credential" has an
    authoritative answer that costs nothing, and reimplementing the resolution order in Python --
    which is what #334 was -- is never necessary.

    The `problem` arm covers a case the env-var guard could not reach at all: a profile that *is*
    configured and cannot be loaded (`ANTHROPIC_PROFILE` naming a missing file, a malformed
    `active_config`) makes the SDK raise out of the constructor. That used to escape `new_client()`
    as a traceback, because nothing here expected construction to fail. It is caught broadly rather
    than by the SDK's own error class on purpose: the class is not importable on every supported
    major, and this function's contract is that an install which cannot make a call is refused
    cleanly whatever the reason. The original message is quoted rather than replaced -- the SDK
    names the file and the variable, which is more than this module could say.
    Pinned by `test_an_unloadable_profile_is_refused_with_the_sdk_s_own_reason`.
    """
    if Anthropic is None:
        return None, None  # the caller reports the not-installed case; there is nothing to resolve
    try:
        client = Anthropic()
    except Exception as e:  # noqa: BLE001 - deliberate; see the docstring
        return None, (
            f"The Anthropic SDK could not load the credential configuration it was pointed at: {e}"
        )
    if all(getattr(client, attr, None) is None for attr in _CREDENTIAL_ATTRS):
        return None, _NO_KEY_MESSAGE
    return client, None


def credential_present() -> bool:
    """Whether this install can authenticate -- the **one** definition every "is there a key" reader
    shares, and since #334 an answer the SDK gives rather than one this file guesses at.

    Before #332, `web/config.py` and `deterministic/doctor.py` each kept their own
    `os.getenv("ANTHROPIC_API_KEY")`, current at the time `new_client()` read only that name too.
    #201 widened `new_client()` for a bearer-token setup and left the other two behind, so a working
    bearer-token install built a client from the CLI while both the web surface and
    `requivo doctor --json` reported no key. Both read this instead of a second copy, so they cannot
    drift from `new_client()` again -- which now matters more, not less, since what they agree on is
    the SDK's own resolution rather than a list.

    **It never raises**, and that is a constraint from its callers rather than a preference:
    `deterministic/doctor.py` calls it bare, and the verb that answers *is this install healthy*
    must not traceback on the unhealthy install it exists to describe. A configured-but-unloadable
    profile therefore reads as False here, the same as a genuinely absent credential -- collapsing
    the two is deliberate for a caller that only ever needed a boolean, and `new_client()` is where
    the unloadable case gets its own message. **`doctor` is a second reader that does not only want
    a boolean** (#365): it names a remedy, and "set an API key" is the wrong remedy for a file the
    SDK could not read. `credential_diagnosis()` below is that second reader's answer; this
    function's own contract is unchanged for its other callers (`web/config.py`, the CLI).
    Pinned by `test_credential_present_is_the_one_definition_new_client_reads` and
    `test_credential_present_does_not_raise_on_an_unloadable_profile`.
    """
    client, _ = _resolve_client()
    return client is not None


def credential_diagnosis() -> tuple[bool, str | None]:
    """`credential_present()` plus the one thing it deliberately discards for its own callers: *why*,
    for the one case where "why" is more specific than "nothing is configured".

    `_resolve_client()` already tells the two `False` causes apart -- see its own docstring. This
    wraps that into a shape a *reporting* surface can render safely: it never raises, either, and it
    hands back a `problem` string only for the arm that needs one. "SDK not installed" and
    "genuinely no credential visible" both already say what they are without a wrapped SDK
    exception, and `doctor` prints its own remedy for the first of those -- so `problem` is `None`
    for both, and non-`None` only for a profile that is configured and could not be loaded, which is
    the one arm `credential_present()`'s own docstring names as the reason a second reader exists.

    Returns `(present, problem)`; `present=True` implies `problem is None`. Pinned by
    `test_credential_diagnosis_names_the_unloadable_profile_the_bool_hides` and
    `test_credential_diagnosis_leaves_a_genuinely_missing_credential_unnamed`.
    """
    client, problem = _resolve_client()
    if client is not None:
        return True, None
    if problem is None or problem == _NO_KEY_MESSAGE:
        return False, None
    return False, problem



def new_client() -> _AnthropicClient:
    """Construct an Anthropic client, or refuse cleanly when the install cannot make a call.

    Every provider-backed verb funnels through here, so the two install/config remedies are stated
    once rather than scattered: the SDK is missing, or no credential is visible.

    **The key check is here and not at the call site, because `Anthropic()` does not raise.** The SDK
    constructs fine with no credential and defers auth resolution to the first request, where it
    raises a bare `TypeError` ("Could not resolve authentication method") out of its own internals.
    That threaded through this function, through `_complete`'s `except APIError` and through
    `cli.app()`'s `except RequivoError` untouched, so the single most likely failure of a fresh
    `pip install requivo[anthropic]` -- run the command the demo suggests before setting a key --
    was twenty-five lines of traceback naming neither the environment variable, nor `.env`, nor
    `requivo doctor` (#201). Refusing here also makes it free: nothing is claimed and nothing is
    billed, where the alternative is a session claimed at revision 0 and a 401.
    Pinned by `test_a_missing_api_key_refuses_before_the_sdk_can_traceback`.
    """
    if Anthropic is None:
        raise EngineError(
            "The Anthropic provider is not installed. Install it with `pip install 'requivo[anthropic]'` "
            "(or `uv tool install 'requivo[anthropic]'`). You do NOT need it to use Requivo inside "
            f"Claude Code — that mode uses no API key. (import error: {_IMPORT_ERROR})"
        )
    client, problem = _resolve_client()
    if client is None:
        raise EngineError(problem or _NO_KEY_MESSAGE)
    # `_resolve_client()` deliberately returns `object` (see its own docstring) rather than
    # `Anthropic | None`, which is what keeps *that* function's annotation valid without the
    # `TYPE_CHECKING` alias this function needed above. The cast is the one place that looseness has
    # to be undone: past the `is None` check, the only way `_resolve_client()` produces a non-None
    # value is `Anthropic()` succeeding, so the value really is an `Anthropic` instance.
    #
    # `"_AnthropicClient"` is quoted -- `_AnthropicClient` only exists under `TYPE_CHECKING`, which is
    # `False` at runtime, so the bare name would raise `NameError` the moment this line actually ran
    # (caught once, by `tests/test_provider.py`'s federation-credential case, which is the one
    # `new_client()` path that reaches a real return rather than a raise). `cast()`'s first argument
    # is never evaluated as a type at runtime -- it only has to exist as an *expression* -- and a
    # string is a valid one pyright still resolves for the static check.
    return cast("_AnthropicClient", client)


def current_model_name() -> str:
    """The model id this process will call — the env override or the default. Exposed so provenance
    (session.json) records the exact model a discovery ran against.

    `REQUIVO_MODEL` is read first, bare `MODEL` as a deprecated fallback (docs/compatibility.md) --
    a shell exporting a generic `MODEL` some other tool also sets must not silently steer Requivo to
    the wrong model (#268). Pinned by `test_current_model_name_falls_back_to_bare_model` and
    `test_current_model_name_prefers_requivo_model_when_both_are_set`.
    """
    # No stderr notice on the fallback path, decided rather than merely omitted. `core/` is barred
    # from the standard streams by invariant 7, and this module sits just outside `core/` — but the
    # actual caller here is `_complete()`, deep inside a retry loop that already owns its own error
    # channel (a clean `EngineError`/`ProviderOutputError`, never a bare print), and a provider
    # printing on a path that *works* would be the one line in this call graph that bypasses it. A
    # user who only ever set MODEL sees nothing wrong — the call succeeds with the model they asked
    # for — so a notice here is a warning about vocabulary, not about a failure, and `requivo doctor`
    # (which already reports environment-derived facts back to the user) is the honest place to
    # eventually surface "your REQUIVO_MODEL setup falls back to bare MODEL" once one exists.
    #
    # `os.getenv("REQUIVO_MODEL") is not None`, not a bare `or` -- presence, not truthiness.
    # `test_a_model_override_that_is_set_but_empty_is_reported_as_one` already pins the reason for
    # bare `MODEL`: an exported-but-empty variable is an override in effect (of nothing), and a
    # truthy check would fall through to `MODEL`/the default and report the comfortable lie that
    # nothing was overridden. `REQUIVO_MODEL=""` deserves the identical answer, not a quieter one.
    override = os.getenv("REQUIVO_MODEL")
    if override is not None:
        return override
    return os.getenv("MODEL", MODEL_DEFAULT)
