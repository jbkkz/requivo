"""The bearer-token bind discipline for the Requivo API (`decision: the-http-api-facade`): local
loopback needs no auth; a bind beyond loopback refuses to start without `REQUIVO_API_TOKEN`; once a
token is set, `check_bearer` requires it on every `/api/v1` route but the liveness probe, compared
in constant time. Framework-free, and over header *bytes*: Starlette decodes headers as latin-1 and
`compare_digest` on two `str` raises unless both are ASCII (#212).
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from requivo.core.errors import RequivoError
from requivo.host_policy import LOOPBACK_HOSTS

# The one knob, read at app-construction time, never at import.
API_TOKEN_ENV = "REQUIVO_API_TOKEN"

# The one scheme (RFC 6750), matched case-insensitively (RFC 7235).
_SCHEME = b"bearer"

# The 401's challenge (RFC 6750 §3); `error="invalid_token"` only when a credential was presented.
_CHALLENGE = 'Bearer realm="requivo-api"'
_CHALLENGE_INVALID = 'Bearer realm="requivo-api", error="invalid_token"'


class ApiTokenRequiredError(RequivoError):
    """A bind beyond loopback with no `REQUIVO_API_TOKEN`: a start-time refusal, never an HTTP
    response. `details`: `{host, env}`."""

    code = "api_token_required"


class UnauthorizedError(RequivoError):
    """A token is configured and this request did not carry it. `details`: `{scheme, reason}`, with
    `reason` `missing` or `invalid`; the token itself never appears."""

    code = "unauthorized"

    @property
    def challenge(self) -> str:
        """The `WWW-Authenticate` value a 401 carrying this error must send."""
        return _CHALLENGE_INVALID if self.details.get("reason") == "invalid" else _CHALLENGE


def resolve_token(explicit: Optional[str] = None) -> Optional[str]:
    """The token this process requires, or `None` for no auth: an explicit value, else
    `REQUIVO_API_TOKEN`; blank folds into unset, so the empty bearer never passes."""
    value = explicit if explicit is not None else os.environ.get(API_TOKEN_ENV)
    if value is None or not value.strip():
        return None
    return value


def is_loopback_bind(host: str) -> bool:
    """Does this bind address name loopback by one of `host_policy.LOOPBACK_HOSTS`' three spellings?
    Deliberately no wider than the host allowlist (`127.0.0.2` is a deliberate act and requires a token)."""
    return host in LOOPBACK_HOSTS


def require_token_for_bind(host: str, token: Optional[str]) -> None:
    """Refuse to start bound beyond loopback with no token, before a port is bound. A caller passing
    no `bind_host` to `create_api` is not checked here."""
    if is_loopback_bind(host) or token is not None:
        return
    raise ApiTokenRequiredError(
        f"refusing to bind the Requivo API to {host!r} with no {API_TOKEN_ENV} set: beyond loopback "
        f"every request must carry `Authorization: Bearer <token>`, so set {API_TOKEN_ENV} to the "
        "token clients will send, or bind to 127.0.0.1.",
        details={"host": host, "env": API_TOKEN_ENV})


def check_bearer(authorization: Optional[bytes], token: str) -> None:
    """Require `Authorization: Bearer <token>` on one request, or raise `UnauthorizedError`.
    `authorization` is the header's raw bytes; the comparison is `hmac.compare_digest`, never `==`
    (`test_the_token_comparison_is_constant_time_by_construction`). RFC 6750 §2.1: scheme
    case-insensitive, whitespace tolerated, the credential byte-for-byte."""
    if authorization is None:
        raise UnauthorizedError(
            "this API requires `Authorization: Bearer <token>`",
            details={"scheme": "Bearer", "reason": "missing"})
    scheme, _, credential = authorization.strip().partition(b" ")
    credential = credential.strip()
    # A bare `Bearer` presented no credential: `missing`, not `invalid`.
    # `test_a_bearer_scheme_with_no_credential_is_missing_not_invalid`.
    if scheme.lower() != _SCHEME or not credential:
        raise UnauthorizedError(
            "this API requires `Authorization: Bearer <token>`",
            details={"scheme": "Bearer", "reason": "missing"})
    if not hmac.compare_digest(credential, token.encode("utf-8")):
        raise UnauthorizedError(
            "the bearer token this request carried is not the one this API was started with",
            details={"scheme": "Bearer", "reason": "invalid"})
