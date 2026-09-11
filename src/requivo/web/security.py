"""Cross-site request protection for Requivo Web.

Binding to `127.0.0.1` is not a security boundary. Any page the user has open in the same browser can
POST to a known local port, and a plain HTML form post is not subject to a CORS preflight — the browser
sends it and simply refuses to let the attacker *read* the reply. For this app, writing is the damage:
a hostile page could create sessions, submit answers, and burn the server's Anthropic key, and the
attacker never needs to see a single response to do it.

Four checks close that, in the order they run:

  * **Host allowlist** — the `Host` header must name a loopback address (or one the operator opted into
    via `REQUIVO_WEB_ALLOWED_HOSTS`), and a request that names no host *at all* is refused rather than
    waved through — a check that cannot read its input has to say so, not treat it as nothing to check
    (#45). This is the DNS-rebinding guard, and it is the only check that applies to reads as well: a
    rebound `evil.com` that resolves to 127.0.0.1 is same-origin from the browser's point of view, so
    it would sail through every other check *and* be able to read the token. **It lives in
    `requivo.host_policy` now, not here** (#508): being the only read-applicable check is exactly
    what made it the one a formless, cookieless JSON surface also needs, and `api/app.py` shipped
    without it. The three checks below stay, because they are about an unsafe method arriving with
    ambient form trust — which is this surface's shape, not every surface's.
  * **`Sec-Fetch-Site`** — the browser's own account of where the request came from. Free, unspoofable
    from script, and rejects `cross-site` / `same-site` outright.
  * **`Origin` / `Referer`** — when present, it must name the same trust domain as the host being
    addressed. The three loopback spellings are one machine and are interchangeable here; an
    operator-listed host is not, and must match exactly (`_same_trust_domain`). The opaque origin
    `null` is refused outright, and `_enforce` says why that differs from sending no origin at all.
  * **A synchronizer token** — minted once per process, rendered into every form, required on every
    unsafe method. This is the load-bearing check; the three above are cheap filters in front of it.

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
    CrossSiteRequestError,
    HostNotAllowedError,
    UndeterminedHostError,
    allowed_hosts,
    check_host,
)
from requivo.host_policy import LOOPBACK_HOSTS as _LOOPBACK_HOSTS
from requivo.host_policy import hostname as _hostname

# Re-exported under the names this module has always published them under, so every existing caller
# and test reads unchanged after #508 moved the host axis out. `_hostname` and `_LOOPBACK_HOSTS` keep
# their underscore here because that is how the rest of this file spells them; in `host_policy` they
# are public, because a module two surfaces import from has no private half.
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


class CrossSiteFetchError(CrossSiteRequestError):
    """The browser's own `Sec-Fetch-Site` says this came from elsewhere. `details`:
    `{sec_fetch_site}`."""

    code = "cross_site_fetch"


class OpaqueOriginError(CrossSiteRequestError):
    """`Origin: null` — a browser speaking and declining to attribute itself (#43). `details`:
    `{origin, host}`."""

    code = "opaque_origin"


class OriginMismatchError(CrossSiteRequestError):
    """The stated origin is not the same trust domain as the host addressed. `details`:
    `{origin, host}` — the same keys as `opaque_origin` and a different fact, which is why they are
    two codes rather than one: a shared shape is not a shared meaning."""

    code = "origin_mismatch"


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


# The Origin header's opaque value, sent verbatim and never as part of a URL: a browser saying "a
# context I decline to attribute". Matched on the raw header rather than on `_hostname()`, which parses
# it into the plausible-looking hostname `"null"`.
OPAQUE_ORIGIN = "null"


def _same_trust_domain(origin_host: str, host: str) -> bool:
    """Is a page served from `origin_host` the same trust domain as the server answering to `host`?

    The same string always is. Beyond that, only the loopback set: `localhost`, `127.0.0.1` and `::1`
    are three spellings of one interface on one machine, and the host check above already accepts any
    of them interchangeably. Comparing the two spellings as strings refused a post that used both at
    once, which is a false positive rather than a boundary (#43).

    What that accepts is the loopback **interface**, not this process. `_hostname` discards the port on
    both sides, so `http://localhost:3000` and `http://localhost:8765` arrive here as the same string:
    the accepted set is every page served by every process on any loopback port, which on a developer
    machine is a populated one. This docstring used to claim the opposite — that such a page *"can only
    have been served by this process, nothing else is listening there"* — and that was simply false. A
    rationale is what the next change gets reasoned from, so a wrong one is worse than none (#46).

    The port-blindness is deliberate, and it predates #43 rather than following from it: before that
    fix, `Origin: http://localhost:3000` against `Host: localhost:8765` already compared equal. It
    stays, because the **request token** is what gates the write and a page on another loopback port
    cannot get hold of one. The browser's own same-origin policy is (scheme, host, port), so reading a
    page this server rendered is a cross-origin read; this app sends no CORS headers, so the body never
    reaches the script. `Sec-Fetch-Site` refuses that same post one check earlier, as `same-site`, on
    every browser that sends it. Comparing ports here would add nothing those two do not already do,
    and it would reintroduce #43's exact failure shape — a default port elided in an `Origin` but
    spelled out in a `Host`, refusing a form with no way forward. Tightening it is a separate decision
    needing its own tests; `test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision`
    pins the behaviour so that change has to argue with this paragraph rather than slip past it.

    The hosts an operator listed in `REQUIVO_WEB_ALLOWED_HOSTS` deliberately do **not** join that
    equivalence class, so this is not a membership test over `allowed_hosts()`. Those are real
    hostnames pointing at a deliberate non-loopback bind, and two of them may well be meant as two
    distinct origins — that is the operator's call to make, and inferring it from co-membership in one
    comma-separated list would make it for them, silently, in the widening direction.

    An empty string on either side is not a match, and that arm is the point rather than a special
    case. `""` is what `_hostname` returns when it could not find a hostname *at all* — an absent or
    unparseable `Host`, or an origin such as `http:///` that is a well-formed URL naming nobody. Two of
    those facing each other used to compare equal, so the one input where **neither** side was
    determined produced the same verdict as a verified match: a check that could not look, answering
    anyway. Refusing costs nothing real — no browser omits `Host`, and a request that reaches here at
    all has already stated an origin — and it keeps this function's name true for every input.

    Since #45 the `host` half of that arm is unreachable from the only caller: `_enforce` refuses an
    undetermined `Host` outright, several checks earlier, so `host` is always determined by the time it
    gets here. It is kept rather than pruned as now-dead, and deliberately. A helper that makes a claim
    by name should hold for every input it is handed, this one is called directly by its own tests, and
    narrowing a security helper on the grounds that today's single caller happens to pre-filter its
    input is how the next caller inherits a guarantee nobody re-checked.
    """
    if not origin_host or not host:
        return False
    if origin_host == host:
        return True
    return origin_host in _LOOPBACK_HOSTS and host in _LOOPBACK_HOSTS


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

    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise CrossSiteFetchError(
            "this request came from another site", details={"sec_fetch_site": fetch_site})

    # `Origin: null` is refused on purpose, and the asymmetry with an *absent* origin below is the
    # reason rather than an oversight. A browser attaches `Origin` to every POST, so no origin at all
    # means no browser is speaking — a scripted client, which the token already gates and which is a
    # supported caller. `null` is the opposite: a browser that is speaking and declining to attribute
    # itself, and it is the one origin a browser-borne attacker can *choose* to emit, from a sandboxed
    # cross-site frame. No page this server serves ever produces it. Before #43 this arm fired only by
    # accident, because `_hostname("null")` happens to return `"null"` and fail an equality test; the
    # outcome is unchanged and the reason is now stated. It is a cheap filter either way — the token
    # remains the load-bearing check for both cases.
    # Read unstripped, so a whitespace-only `Origin` stays truthy here exactly as it did before and
    # still reaches the hostname comparison (where it resolves to `""` and is refused) rather than
    # newly falling through to `Referer`.
    origin_header = request.headers.get("origin", "")
    if origin_header.strip().lower() == OPAQUE_ORIGIN:
        raise OpaqueOriginError(
            "this request came from an opaque origin, which this server does not accept",
            details={"origin": OPAQUE_ORIGIN, "host": host})

    origin = origin_header or request.headers.get("referer") or ""
    if origin and not _same_trust_domain(_hostname(origin), host):
        raise OriginMismatchError(
            "this request came from another origin",
            details={"origin": _hostname(origin), "host": host})

    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        if int(declared) > MAX_BODY_BYTES:
            raise InputTooLargeError(
                f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes",
                details={"limit": MAX_BODY_BYTES, "declared": int(declared)})
    else:
        # No declared length at all — what a chunked or otherwise streamed body looks like at this
        # layer — cannot be measured before it is read, and reading it to find out is exactly the bug
        # this refuses (#216): `await request.body()` used to run unconditionally below, so the cap
        # only ever fired *after* the whole thing — 6.5MB in the field, unbounded in principle — was
        # already sitting in memory. No supported caller produces this: every form this app renders,
        # curl, httpx and requests all declare a length, the same argument the undetermined-`Host`
        # refusal above already makes for HTTP/1.0. Refusing outright, rather than reading with a
        # running byte count, was chosen over the streaming alternative because it needs nothing from
        # Starlette's body cache — the alternative would have to poke `request._body` to keep
        # downstream form parsing working, which is a private attribute of a library this module does
        # not otherwise reach into.
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
