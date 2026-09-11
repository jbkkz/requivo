"""Web configuration + provider capability probe — no secrets ever reach the browser.

The Anthropic API key is read from the *server* environment (never a form field, never rendered into
HTML). The UI only needs to know *whether* a provider action is possible, so it can offer discovery /
generation or fall back to 'create session only' — that verdict, and a sentence saying why not, are
all that crosses to the template. Never the key, and never a client.

It is a *verdict* rather than a boolean because the probe has three answers, not two: installed,
absent, and could-not-look (#339). See `ProviderStatus`, and
`test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed`.
"""

from __future__ import annotations

from dataclasses import dataclass

from requivo.core.contracts import MAX_INPUT_CHARS

# Field length ceilings — a local app still bounds input so a pasted megabyte can't wedge a turn. Past
# the ceiling the field is *refused*, never trimmed to fit: half a request folded into the model reads
# exactly like a whole one, so the user would never learn which half the engine saw. (The body itself
# is capped earlier still, in `security.py`, before anything is parsed.)
#
# `MAX_REQUEST_CHARS`/`MAX_ANSWERS_CHARS` are aliases for the one cap Core defines and the service
# layer enforces (invariant 14), not two hand-kept ones that could drift apart (#255). They stay as
# two names because the routes import them for two different fields. Pinned by
# `test_an_oversized_request_is_refused_not_truncated`.
MAX_REQUEST_CHARS = MAX_INPUT_CHARS
MAX_ANSWERS_CHARS = MAX_INPUT_CHARS
MAX_SLUG_CHARS = 80

# Said once rather than at each of the sites that need it, because they have to agree: a probe that
# failed with no message still has to read as *we could not look*, and an empty string spliced into
# the sentence would render as a failure for no reason. Deliberately a second, local copy of the
# constant `deterministic/_shared.py` states for the CLI's degraded rows: that module is the *CLI*
# surface's shared primitives and importing it here would make the Web depend on the terminal
# surface for a five-word string.
_NO_DETAIL = "no further detail"


@dataclass(frozen=True)
class ProviderStatus:
    """What the probe established, in three states per fact rather than two: `True`/`False` are
    answers, `None` is *the probe could not look* and is load-bearing -- an absence the tool produced
    must not render as an absence in the world (#339). Pinned by
    `test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed`.
    """

    sdk_installed: bool | None   # True importable / False absent / None the probe could not look
    key_present: bool | None     # a credential `new_client()` would authenticate from (#332)
    probe_error: str | None = None   # what stopped the probe, when it could not look

    @property
    def available(self) -> bool:
        """A provider action (discovery, generation) can actually run.

        `is True` on both, never truthiness: `None` means unestablished, and an unestablished fact
        must fall on the same side as a negative one here -- offering a paid action on the strength
        of a question nobody answered is the expensive direction to be wrong in.
        """
        return self.sdk_installed is True and self.key_present is True

    @property
    def reason(self) -> str:
        """Why the provider is unavailable, for a clear UI hint (empty when available).

        Ordered so that a *could not look* is never answered with a remedy: a remedy the reader has
        already applied reads as the tool not listening, and it buries the real cause.
        """
        if self.sdk_installed is None:
            return (
                "Could not determine whether the provider is installed; importing it failed: "
                f"{self.probe_error or _NO_DETAIL}. This is not the same as it being missing, so "
                "reinstalling may not be the fix; read the error above first."
            )
        if not self.sdk_installed:
            return "Install the provider: pip install 'requivo[anthropic]'."
        if self.key_present is None:
            return (
                "The provider is installed, but the credential probe itself failed: "
                f"{self.probe_error or _NO_DETAIL}. Requivo cannot tell whether a credential is "
                "visible, so it is not offering provider actions."
            )
        if not self.key_present:
            # Names both names `credential_present()` reads (#332 review) -- a bearer-token-only
            # reader must not be told to set a credential they already have a working equivalent of.
            return (
                "Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the server environment to "
                "enable provider actions."
            )
        return ""


def provider_status() -> ProviderStatus:
    """Probe the provider without touching the key value itself, or making a call.

    `key_present` reads `credential_present()` -- the one definition `new_client()` itself
    authenticates from -- rather than a second, independent `os.getenv("ANTHROPIC_API_KEY")`. Before
    #332 this probe checked only that one name while `new_client()` (widened by #201) also accepted
    `ANTHROPIC_AUTH_TOKEN`, so a working bearer-token install reported `key_present=False` here,
    `available` fell to False, and `routes/sessions.py` (which branches on `available`) silently
    dropped every provider action to `create_only` for an install that would actually have worked.
    Pinned by `test_a_bearer_token_alone_is_read_as_a_credential`.

    **Not "without importing a client"**, corrected here for the same reason
    `tests/test_boundaries.py`'s allowlist entry for this call was corrected (#374): since #334,
    `credential_present()` asks the SDK's own resolution chain, which does construct a transient
    `Anthropic()` to answer -- it never makes a call, which is what this docstring actually needs to
    promise.
    """
    try:
        # the SDK handle (or None if not installed) and the shared credential probe
        from requivo.providers.anthropic import Anthropic, credential_present
    except Exception as e:  # noqa: BLE001 - the probe must survive anything an import can raise
        # Not `sdk = False`. The import failing is not evidence that the package is absent: absence
        # is the case where this import *succeeds* and binds `Anthropic` to None, which is exactly
        # what `providers/anthropic/client.py` arranges so that a surface can probe the extra
        # without it. Everything else -- a broken transitive dependency, a half-written install, an
        # SDK major this Requivo cannot import -- is the probe failing, and neither fact below was
        # established by it. Pinned by
        # `test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed`.
        return ProviderStatus(sdk_installed=None, key_present=None, probe_error=_describe(e))
    sdk = Anthropic is not None
    try:
        key = credential_present()
    except Exception as e:  # noqa: BLE001 - same reasoning, one fact along
        # Split from the arm above rather than sharing one `try`, because by here the SDK question
        # has a real answer and collapsing it would throw away a fact the probe did establish.
        # `credential_present()` is documented not to raise on the case that prompted #365 (an
        # unloadable profile); this arm is for the ones nobody has met yet.
        return ProviderStatus(sdk_installed=sdk, key_present=None, probe_error=_describe(e))
    return ProviderStatus(sdk_installed=sdk, key_present=key)


def _describe(e: BaseException) -> str:
    """The exception as one line a reader can act on -- type *and* message.

    The type alone names nothing actionable; the message alone can be empty (a bare `raise
    ImportError`), and an empty cause reads as a failure for no reason. Both, always."""
    return f"{type(e).__name__}: {e}".strip()
