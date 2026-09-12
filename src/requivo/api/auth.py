"""The bearer-token bind discipline for the Requivo API (#425 slice 4,
`docs/decisions/0004-the-http-api-facade.md` §5).

Three explicit steps in that record, and this module is the first two; the third (cloud identity)
is never in this repo:

1. **Local, loopback: no auth.** The user on the machine already holds the key -- CLI parity.
2. **Bound beyond loopback: a static bearer token, required.** `require_token_for_bind` is the
   refusal: an API process asked to listen on anything but a loopback address without
   `REQUIVO_API_TOKEN` set does not start, the same deliberate-act shape as
   `REQUIVO_WEB_ALLOWED_HOSTS`. Once a token is set -- whatever the bind -- `check_bearer` requires
   `Authorization: Bearer <token>` on every route under `/api/v1` except the liveness probe, and
   compares it in constant time. One token, no users, no roles: "my other machine may call this",
   not identity.

Framework-free, like `requivo.host_policy` and `requivo.http`: `check_bearer` takes the raw header
**bytes** and returns or raises, `require_token_for_bind` takes two strings. Nothing here imports
fastapi, so the base install can reach it and `cli.py` can raise the start refusal before uvicorn
is ever imported.

**Bytes, not `str`, on purpose (#212's lesson, applied before it bites here).** Starlette decodes
every header as latin-1, so a token containing anything outside ASCII arrives as a `str` whose
`.encode("utf-8")` is not what the client sent -- and `hmac.compare_digest` on two `str` raises
`TypeError` unless both are ASCII, which on the web surface turned a wrong token into the one
crash path served with no security headers. Reading the header off the ASGI scope as the bytes it
arrived as, and encoding the configured token once as UTF-8, means every input reaches a verdict:
a token this check cannot read is a wrong token, never an exception.
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from requivo.core.errors import RequivoError
from requivo.host_policy import LOOPBACK_HOSTS

# The one knob. Read by `resolve_token` at app-construction time, never at import.
API_TOKEN_ENV = "REQUIVO_API_TOKEN"

# The one scheme this API speaks (RFC 6750). Matched case-insensitively, as RFC 7235 requires of an
# auth-scheme, so `bearer` and `BEARER` are not silently "missing".
_SCHEME = b"bearer"

# What the 401 says about itself, per RFC 6750 §3: the scheme and realm on every refusal, and
# `error="invalid_token"` only when a bearer credential was actually presented and did not match --
# a caller that sent nothing is told what to send, not that what it sent was wrong.
_CHALLENGE = 'Bearer realm="requivo-api"'
_CHALLENGE_INVALID = 'Bearer realm="requivo-api", error="invalid_token"'


class ApiTokenRequiredError(RequivoError):
    """The API was asked to bind beyond loopback with no `REQUIVO_API_TOKEN` set -- a start-time
    refusal, never an HTTP response (`requivo.http` still carries a nominal row for it, because
    every code in the vocabulary has one). `details`: `{host, env}`."""

    code = "api_token_required"


class UnauthorizedError(RequivoError):
    """A token is configured and this request did not carry it. `details`: `{scheme, reason}`,
    where `reason` is `missing` (no bearer credential was presented at all -- no header, or a
    different scheme) or `invalid` (one was, and it did not match). Two reasons, one fact -- the
    request is not authorized -- and one shape, so they share a code. The token itself never
    appears in `details`, for the reason `web/security.py`'s `MissingRequestTokenError` states:
    echoing a secret back is not a diagnostic."""

    code = "unauthorized"

    @property
    def challenge(self) -> str:
        """The `WWW-Authenticate` value a 401 carrying this error must send."""
        return _CHALLENGE_INVALID if self.details.get("reason") == "invalid" else _CHALLENGE


def resolve_token(explicit: Optional[str] = None) -> Optional[str]:
    """The token this process should require, or `None` for "no auth".

    An explicit value wins; otherwise `REQUIVO_API_TOKEN`. A value that is empty or whitespace-only
    is **not** a token -- `REQUIVO_API_TOKEN=` in a unit file or a `.env` is an operator who has not
    set one, and treating a blank as a credential would let the empty bearer `Authorization: Bearer `
    through. Folding it into "unset" means a non-loopback bind then refuses to start with the
    message that names the variable, which is the loud outcome for that mistake.
    """
    value = explicit if explicit is not None else os.environ.get(API_TOKEN_ENV)
    if value is None or not value.strip():
        return None
    return value


def is_loopback_bind(host: str) -> bool:
    """Does this bind address name the loopback interface, by one of the three spellings the host
    allowlist itself accepts (`host_policy.LOOPBACK_HOSTS`)?

    Deliberately the same set and nothing wider -- not `ipaddress.is_loopback`, which would also
    admit `127.0.0.2` and every other 127/8 address. Those *are* loopback at the socket layer, but a
    client addressing one is refused by `check_host` unless the operator allowlists it, so a bind
    there is already a deliberate, non-default act; this reads it as one and asks for the token.
    The direction of every mismatch is closed: an unrecognised spelling requires a token rather than
    waiving one.
    """
    return host in LOOPBACK_HOSTS


def require_token_for_bind(host: str, token: Optional[str]) -> None:
    """Refuse to start bound beyond loopback with no token -- `docs/decisions/0004-...` §5, step 2.

    Called by `create_api(bind_host=...)`, which is how `requivo api serve` reaches it, so the
    refusal is issued before a port is bound and before uvicorn is imported. A caller constructing
    the app for its own ASGI server and passing no `bind_host` is not checked here -- the factory
    cannot know what they will bind -- and the docstring on `create_api` says so.
    """
    if is_loopback_bind(host) or token is not None:
        return
    raise ApiTokenRequiredError(
        f"refusing to bind the Requivo API to {host!r} with no {API_TOKEN_ENV} set: beyond loopback "
        f"every request must carry `Authorization: Bearer <token>`, so set {API_TOKEN_ENV} to the "
        "token clients will send, or bind to 127.0.0.1.",
        details={"host": host, "env": API_TOKEN_ENV})


def check_bearer(authorization: Optional[bytes], token: str) -> None:
    """Require `Authorization: Bearer <token>` on one request, or raise `UnauthorizedError`.

    `authorization` is the header's raw bytes off the ASGI scope (`None` when absent). The
    comparison is `hmac.compare_digest` over bytes -- constant-time in the credential's length,
    never `==`, so a wrong token costs the same to refuse whether it shares a prefix with the real
    one or not. Pinned by name in `test_the_token_comparison_is_constant_time_by_construction`.

    RFC 6750 §2.1: `credentials = "Bearer" 1*SP b64token`. The scheme is matched case-insensitively
    (RFC 7235), the single space may be several, and surrounding whitespace on the header value is
    stripped -- what a real client sends is exactly `Bearer <token>`, and none of that tolerance
    widens what matches, since the credential still has to compare equal byte for byte.
    """
    if authorization is None:
        raise UnauthorizedError(
            "this API requires `Authorization: Bearer <token>`",
            details={"scheme": "Bearer", "reason": "missing"})
    scheme, _, credential = authorization.strip().partition(b" ")
    credential = credential.strip()
    # A bare `Bearer` with nothing after it presented no credential either -- `missing`, so the
    # challenge says what to send rather than claiming an empty token was wrong (found in review;
    # `test_a_bearer_scheme_with_no_credential_is_missing_not_invalid`).
    if scheme.lower() != _SCHEME or not credential:
        raise UnauthorizedError(
            "this API requires `Authorization: Bearer <token>`",
            details={"scheme": "Bearer", "reason": "missing"})
    if not hmac.compare_digest(credential, token.encode("utf-8")):
        raise UnauthorizedError(
            "the bearer token this request carried is not the one this API was started with",
            details={"scheme": "Bearer", "reason": "invalid"})
