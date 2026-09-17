"""Cross-site request protection for Requivo Web. Binding to loopback is not a boundary: any open
page can POST to a known local port and burn the server's key without reading a reply. Four checks,
in order: the host allowlist (`requivo.host_policy`, reads included, #45, #508), `Sec-Fetch-Site`,
`Origin`/`Referer` against the host (`check_request_origin`), and a per-process synchronizer token on
every unsafe method, the load-bearing one and the only one that stays here. Nothing makes this app
safe to expose on an untrusted network; it makes it safe to leave running.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import Response

from requivo.core.errors import InputTooLargeError
from requivo.host_policy import (
    ALLOWED_HOSTS_ENV,
    OPAQUE_ORIGIN,
    CrossSiteFetchError,
    CrossSiteRequestError,
    HostNotAllowedError,
    OpaqueOriginError,
    OriginMismatchError,
    UndeterminedHostError,
    allowed_hosts,
    check_host,
    check_request_origin,
)

# Re-exported under the names this module has always published (#508, #425); `tests/web/test_security_parser.py`
# and `tests/web/test_web_request_guard.py` import `_hostname`/`_same_trust_domain` by these names.
from requivo.host_policy import LOOPBACK_HOSTS as _LOOPBACK_HOSTS  # noqa: F401
from requivo.host_policy import hostname as _hostname  # noqa: F401
from requivo.host_policy import same_trust_domain as _same_trust_domain  # noqa: F401

__all__ = [
    "ALLOWED_HOSTS_ENV", "CSRF_FIELD", "CSRF_HEADER", "CrossSiteFetchError",
    "CrossSiteRequestError", "HostNotAllowedError", "MAX_BODY_BYTES", "MissingRequestTokenError",
    "OPAQUE_ORIGIN", "OpaqueOriginError", "OriginMismatchError", "SAFE_METHODS",
    "UndeterminedHostError", "allowed_hosts", "csrf_token", "install_cross_site_guard",
]

CSRF_FIELD = "csrf_token"          # the hidden form input every template renders
CSRF_HEADER = "x-csrf-token"       # the equivalent for a scripted client
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Bounded before the body is read; a request with no valid declared length is refused outright (#216).
MAX_BODY_BYTES = 1_000_000

_TOKEN = secrets.token_urlsafe(32)


class MissingRequestTokenError(CrossSiteRequestError):
    """The synchronizer token was absent or did not match. `details` is empty: echoing a token back is never a diagnostic."""

    code = "missing_request_token"


def csrf_token() -> str:
    """The token for this server process — rendered into every form, required back on every write."""
    return _TOKEN


def _submitted_token(request: Request, body: bytes) -> str:
    """The token the client sent: a header, or the hidden field of a urlencoded form; a multipart body carries none."""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return ""
    try:
        fields = parse_qs(body.decode("utf-8"))
    except UnicodeDecodeError:
        return ""
    values = fields.get(CSRF_FIELD) or [""]
    return values[0]


async def _enforce(request: Request) -> None:
    """Run the checks for one request, raising the first failure; reads run the host check only."""
    # The host axis is `requivo.host_policy.check_host` (#508, #45, #51).
    host = check_host(request.headers.get("host"))

    if request.method in SAFE_METHODS:
        return

    # The origin axis is `requivo.host_policy.check_request_origin` (#425, #43).
    check_request_origin(host, sec_fetch_site=request.headers.get("sec-fetch-site"),
                         origin=request.headers.get("origin"),
                         referer=request.headers.get("referer"))

    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        if int(declared) > MAX_BODY_BYTES:
            raise InputTooLargeError(
                f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes",
                details={"limit": MAX_BODY_BYTES, "declared": int(declared)})
    else:
        # No declared length is refused before a byte is read (#216):
        # `test_a_chunked_body_is_refused_before_being_read`.
        raise InputTooLargeError(
            "this request has no valid declared Content-Length and cannot be safely size-checked — "
            "chunked or otherwise streamed request bodies are not supported",
            details={"limit": MAX_BODY_BYTES, "reason": "missing_content_length"})

    # Starlette caches the body, so the route still parses its own form; defence in depth against a body that lies.
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise InputTooLargeError(
            f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes", details={"limit": MAX_BODY_BYTES})
    # Compared as bytes (#212): `compare_digest` on two `str` raises `TypeError` unless both are ASCII,
    # and a header is latin-1; `surrogatepass` so a lone surrogate cannot reintroduce the crash.
    # `test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing`.
    submitted = _submitted_token(request, body).encode("utf-8", errors="surrogatepass")
    if not secrets.compare_digest(submitted, _TOKEN.encode("ascii")):
        raise MissingRequestTokenError(
            "this form did not carry a valid request token — reload the page and try again",
            details={})


def install_cross_site_guard(app: FastAPI, render_error: Callable[..., Response]) -> None:
    """Register the guard as the innermost middleware. It renders its own failures: an exception in
    a `BaseHTTPMiddleware` never reaches the `RequivoError` handler. `render_error` is injected."""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        try:
            await _enforce(request)
        except InputTooLargeError as exc:
            return render_error(request, 413, exc.code, exc.message)
        except CrossSiteRequestError as exc:
            return render_error(request, 403, exc.code, exc.message)
        return await call_next(request)
