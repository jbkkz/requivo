"""The SDK handle and the model id. `anthropic` is optional; importing this module without it installed binds
`Anthropic` to None and raises cleanly at `new_client()`, so a presence probe (`web/config.py`) can import the rest."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from requivo.providers.errors import EngineError

if TYPE_CHECKING:
    # separately named so it does not widen to `type[Anthropic] | None` from the fallback below
    # (`decision: type-checking-only-anthropic-alias`)
    from anthropic import Anthropic as _AnthropicClient

try:  # optional extra: core + CLI work without it (Claude Code mode)
    from anthropic import Anthropic, APIError, AuthenticationError, PermissionDeniedError, RateLimitError
except ImportError as _e:  # pragma: no cover - exercised only in a no-SDK install
    Anthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]

    class _NeverRaised(Exception):
        """Unreachable stand-in so an absent SDK's arms catch nothing rather than swallowing every transport failure
        -- `test_the_typed_error_arms_are_inert_without_the_sdk`."""

    AuthenticationError = _NeverRaised  # type: ignore[assignment,misc]
    PermissionDeniedError = _NeverRaised  # type: ignore[assignment,misc]
    RateLimitError = _NeverRaised  # type: ignore[assignment,misc]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

MODEL_DEFAULT = "claude-sonnet-5"

# A remedy named in _NO_KEY_MESSAGE, not a decision -- _resolve_client asks the SDK's own chain (#334).
_AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# getattr defaults: `credentials` is absent on some supported SDK majors, meaning no such source.
_CREDENTIAL_ATTRS = ("api_key", "auth_token", "credentials")

# "resolved none", not "none found" -- a working profile/federation install must not be mis-refused.
_NO_KEY_MESSAGE = (
    "No Anthropic credential found: the SDK resolved none from the environment, from a profile, or "
    "from workload identity federation. The usual fix is to set ANTHROPIC_API_KEY (or "
    "ANTHROPIC_AUTH_TOKEN for a bearer-token setup) in your environment, or to put it in a `.env` "
    "file in the directory you run from (see .env.example). `requivo doctor` reports whether a "
    "credential is visible. You do NOT need one for `requivo demo`, for the offline verbs (status, "
    "impact, session, model, artifact), or for Requivo inside Claude Code."
)


def _resolve_client() -> tuple[object | None, str | None]:
    """`(client, problem)`, exactly one set. The SDK resolves credentials in `Anthropic.__init__` itself (#334);
    `problem` reports a configured profile the SDK could not load, in its own words (see test_provider.py)."""
    if Anthropic is None:
        return None, None
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
    """Can this install authenticate -- the SDK's own answer, never raising (#332, #334)."""
    client, _ = _resolve_client()
    return client is not None


def credential_diagnosis() -> tuple[bool, str | None]:
    """`credential_present()` plus *why*, for an unloadable profile (#365)."""
    client, problem = _resolve_client()
    if client is not None:
        return True, None
    if problem is None or problem == _NO_KEY_MESSAGE:
        return False, None
    return False, problem


def new_client() -> _AnthropicClient:
    """An Anthropic client, or a clean refusal -- `Anthropic()` itself does not raise on a missing credential,
    deferring to a bare `TypeError` on the first request (#201)."""
    if Anthropic is None:
        raise EngineError(
            "The Anthropic provider is not installed. Install it with `pip install 'requivo[anthropic]'` "
            "(or `uv tool install 'requivo[anthropic]'`). You do NOT need it to use Requivo inside "
            f"Claude Code — that mode uses no API key. (import error: {_IMPORT_ERROR})"
        )
    client, problem = _resolve_client()
    if client is None:
        raise EngineError(problem or _NO_KEY_MESSAGE)
    return cast("_AnthropicClient", client)


def current_model_name() -> str:
    """`REQUIVO_MODEL`, else bare `MODEL` (deprecated), else `MODEL_DEFAULT` -- a generic `MODEL` a shell exports
    for another tool must not silently steer Requivo elsewhere (#268)."""
    override = os.getenv("REQUIVO_MODEL")
    if override is not None:
        return override
    return os.getenv("MODEL", MODEL_DEFAULT)
