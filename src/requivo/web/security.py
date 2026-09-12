"""Cross-site request protection for Requivo Web.

Binding to `127.0.0.1` is not a security boundary. Any page the user has open in the same browser can
POST to a known local port, and a plain HTML form post is not subject to a CORS preflight — the browser
sends it and simply refuses to let the attacker *read* the reply. For this app, writing is the damage:
a hostile page could create sessions, submit answers, and burn the server's Anthropic key, and the
attacker never needs to see a single response to do it.

Four checks close that, in the order they run:

  * **Host allowlist** — the `Host` header must name a loopback address (or one the operator opted into
    via `REQUIVO_WEB_ALLOWED_HOSTS`), and a request that names no host *at all* is refused rather than
    waved through -- a check that cannot read its input has to say so, not treat it as nothing to check
    (#45; pinned by `test_a_host_nobody_could_read_is_refused_by_every_surface`). This is the
    DNS-rebinding guard, and it is the only check that applies to reads as well, which is why it lives
    in `requivo.host_policy` now, shared by every FastAPI surface this project ships rather than only
    this one (#508; pinned by `test_a_loopback_host_is_accepted_by_every_surface` and
    `test_an_unrecognised_host_is_refused_by_every_surface`).
  * **`Sec-Fetch-Site`** — the browser's own account of where the request came from. Free, unspoofable
    from script, and rejects `cross-site` / `same-site` outright.
  * **`Origin` / `Referer`** — when present, it must name the same trust domain as the host being
    addressed. The three loopback spellings are one machine and are interchangeable here; an
    operator-listed host is not, and must match exactly (`same_trust_domain`). The opaque origin
    `null` is refused outright, and `check_request_origin` says why that differs from sending no
    origin at all. These two live in `requivo.host_policy` as well since #425 slice 4, for the same
    reason as the host axis: the API runs the identical checks on the identical headers before its
    own writes, and `_enforce` below calls the one definition rather than keeping a copy.
  * **A synchronizer token** — minted once per process, rendered into every form, required on every
    unsafe method. This is the load-bearing check; the three above are cheap filters in front of it.
    This one stays here: it is shaped by the fact that this surface serves HTML forms, and the API's
    equivalent (`docs/decisions/0004-the-http-api-facade.md` §5) is a JSON content-type floor, not a
    token.

There is no login and no session cookie here, so a process-lifetime token is the whole ceremony: it
only ever appears in HTML that the checks above keep cross-origin readers away from, and a page left
open across a server restart just needs a reload. Nothing here makes Requivo Web safe to expose on an
untrusted network — it is still single-user and unauthenticated. It makes it safe to leave *running*
while the user browses the rest of the web, which is the actual threat model of a local tool.
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

# Re-exported under the names this module has always published them under, so every existing caller
# and test reads unchanged after #508 moved the host axis out and #425 (slice 4) moved the origin
# axis after it. `_hostname`, `_LOOPBACK_HOSTS` and `_same_trust_domain` keep their underscore here
# because that is how this module's tests (`tests/web/test_security_parser.py`,
# `tests/web/test_web_security.py`) spell them; in `host_policy` they are public, because a module two
# surfaces import from has no private half. Nothing in this file calls them any more -- the `noqa` is
# the re-export, not an oversight.
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

# Bounded before the body is read, let alone parsed: an unauthenticated local endpoint should not be
# willing to buffer an arbitrary upload just to discover it has no valid token. That claim held only
# for a request that *declares* its length — a chunked body (no `Content-Length`) used to be read in
# full by `await request.body()` regardless, with the cap checked only once the whole thing was
# already in memory (#216). A request with no valid declared length is now refused outright, before a
# byte is read, rather than measured after the fact.
MAX_BODY_BYTES = 1_000_000

_TOKEN = secrets.token_urlsafe(32)


class MissingRequestTokenError(CrossSiteRequestError):
    """The synchronizer token was absent or did not match — the load-bearing check.

    `details` is deliberately empty. The only fact beyond the code is the token itself, and echoing
    a token back is never a diagnostic. An empty payload is a shape; a payload carrying a secret is
    a defect.
    """

    code = "missing_request_token"


def csrf_token() -> str:
    """The token for this server process — rendered into every form, required back on every write."""
    return _TOKEN


def _submitted_token(request: Request, body: bytes) -> str:
    """The token the client sent: a header (scripted clients, tests) or the hidden field of a
    urlencoded form (every page in this app). A multipart body carries no token we will read — nothing
    here uploads files, and refusing to parse one keeps the pre-token surface as small as possible."""
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
    """Run the checks for one request, raising the first failure. Reads run the host check only;
    anything that can change state runs all four."""
    # The host axis is `requivo.host_policy.check_host` since #508 -- one definition, every surface.
    # Its own docstring carries the two narratives that used to sit here (#45's undetermined-host
    # refusal and #51's non-authority arm), because that is where the code they describe now lives.
    host = check_host(request.headers.get("host"))

    if request.method in SAFE_METHODS:
        return

    # The origin axis -- `Sec-Fetch-Site`, then `Origin`/`Referer` against the host just determined --
    # is `requivo.host_policy.check_request_origin` since #425 slice 4, one definition for both
    # surfaces. Its docstring carries the #43 narrative (why `Origin: null` is refused while an absent
    # origin is not) that used to sit here; the token below remains this surface's load-bearing check
    # for both of those cases.
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
        # No declared length at all -- a chunked or otherwise streamed body -- is refused outright
        # rather than read to find its size, which is what buffered the whole thing into memory before
        # the cap could fire (#216). No supported caller produces this: every form this app renders,
        # curl, httpx and requests all declare a length. Pinned by
        # `test_a_chunked_body_is_refused_before_being_read` and
        # `test_a_declared_length_post_is_unaffected`.
        raise InputTooLargeError(
            "this request has no valid declared Content-Length and cannot be safely size-checked — "
            "chunked or otherwise streamed request bodies are not supported",
            details={"limit": MAX_BODY_BYTES, "reason": "missing_content_length"})

    # Starlette caches the body for the downstream app, so reading it here to find the token does not
    # consume it — the route still parses its own form as usual. Reachable only once a valid declared
    # length at or under the cap has already been confirmed above, so this is defence in depth against
    # a body that lies about its own length, not the size check itself.
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise InputTooLargeError(
            f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes", details={"limit": MAX_BODY_BYTES})
    # **Compared as bytes, because a token this check cannot read is a wrong token — not a crash**
    # (#212). `secrets.compare_digest` on two `str` arguments raises `TypeError` unless both are
    # ASCII-only, and neither side of `_submitted_token` is guaranteed to be: a form field is decoded
    # from the body as UTF-8, and a header is decoded by Starlette as latin-1. So one accented
    # character in a mangled token took the module's own stated rule — a check that cannot read its
    # input has to refuse, not treat it as nothing to check — and broke it in the loudest available
    # way. The `TypeError` escaped `_guard`'s two `except` arms, so it never became the 403 this line
    # is written to raise; it propagated *past* `security_headers` and landed on the outermost 500
    # handler, making the one crash path in the security module also the one response class served
    # with no CSP, no nosniff and no Referrer-Policy.
    #
    # `surrogatepass` rather than a bare `.encode()` for the same reason the line is here at all: a
    # lone surrogate would raise `UnicodeEncodeError` and reintroduce the identical shape one codec
    # along. Neither path above can produce one today — that is precisely the kind of "today's
    # callers happen to pre-filter it" argument this module declines to narrow a check on. Every
    # `str` now has an encoding, so every input reaches a verdict.
    #
    # Pinned by `test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing` and
    # `test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison`.
    submitted = _submitted_token(request, body).encode("utf-8", errors="surrogatepass")
    if not secrets.compare_digest(submitted, _TOKEN.encode("ascii")):
        raise MissingRequestTokenError(
            "this form did not carry a valid request token — reload the page and try again",
            details={})


def install_cross_site_guard(app: FastAPI, render_error: Callable[..., Response]) -> None:
    """Register the guard as the innermost middleware.

    It renders its own failures rather than raising: an exception raised in a `BaseHTTPMiddleware` is
    outside the app's `ExceptionMiddleware`, so the registered `RequivoError` handler never sees it and
    the caller would get a bare 500 instead of the intended 403. `render_error` is passed in (rather
    than imported) to keep this module free of any dependency on the app factory.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        try:
            await _enforce(request)
        except InputTooLargeError as exc:
            return render_error(request, 413, exc.code, exc.message)
        except CrossSiteRequestError as exc:
            return render_error(request, 403, exc.code, exc.message)
        return await call_next(request)
