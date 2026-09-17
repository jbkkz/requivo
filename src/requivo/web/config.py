"""Web configuration and the provider capability probe: the key is read from the server environment
and never reaches the browser; only a three-state verdict (installed, absent, could-not-look, #339)
crosses to the template.
"""

from __future__ import annotations

from dataclasses import dataclass

from requivo.core.contracts import MAX_INPUT_CHARS

# Field ceilings, refused rather than trimmed (invariant 3): aliases for the one cap Core enforces
# (#255, invariant 14). `test_an_oversized_request_is_refused_not_truncated`.
MAX_REQUEST_CHARS = MAX_INPUT_CHARS
MAX_ANSWERS_CHARS = MAX_INPUT_CHARS
MAX_SLUG_CHARS = 80

# Said once so the sites that need it agree; a local copy, since the Web must not import the CLI's `_shared`.
_NO_DETAIL = "no further detail"


@dataclass(frozen=True)
class ProviderStatus:
    """What the probe established, three states per fact: `True`/`False` are answers, `None` is *could
    not look* (#339). `test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed`."""

    sdk_installed: bool | None   # True importable / False absent / None the probe could not look
    key_present: bool | None     # a credential `new_client()` would authenticate from (#332)
    probe_error: str | None = None   # what stopped the probe, when it could not look

    @property
    def available(self) -> bool:
        """A provider action can run: `is True` on both, never truthiness, since an unestablished fact
        must not offer a paid action."""
        return self.sdk_installed is True and self.key_present is True

    @property
    def reason(self) -> str:
        """Why the provider is unavailable (empty when available); a *could not look* is never answered with a remedy."""
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
            # Names both names `credential_present()` reads (#332).
            return (
                "Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the server environment to "
                "enable provider actions."
            )
        return ""


def provider_status() -> ProviderStatus:
    """Probe the provider without touching the key value or making a call. `key_present` reads
    `credential_present()`, the definition `new_client()` authenticates from, so a bearer token counts
    (#332, `test_the_provider_probe_reads_either_credential_name`); since #334 that constructs a transient
    client but never calls."""
    try:
        # the SDK handle (or None if not installed) and the shared credential probe
        from requivo.providers.anthropic import Anthropic, credential_present
    except Exception as e:  # noqa: BLE001 - the probe must survive anything an import can raise
        # Not `sdk = False`: absence is the import *succeeding* with `Anthropic` bound to None; a
        # failing import established nothing. `test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed`.
        return ProviderStatus(sdk_installed=None, key_present=None, probe_error=_describe(e))
    sdk = Anthropic is not None
    try:
        key = credential_present()
    except Exception as e:  # noqa: BLE001 - same reasoning, one fact along
        # Split from the arm above: by here the SDK question has a real answer worth keeping.
        return ProviderStatus(sdk_installed=sdk, key_present=None, probe_error=_describe(e))
    return ProviderStatus(sdk_installed=sdk, key_present=key)


def _describe(e: BaseException) -> str:
    """The exception as one line: type and message, since either alone can be empty or unactionable."""
    return f"{type(e).__name__}: {e}".strip()
