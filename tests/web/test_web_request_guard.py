"""Requivo Web security: the request-side guard -- CSRF token, body cap, Host/Origin trust.

Split from the single `test_web_security.py` by #555, once that file crossed the 800-line
ceiling -- response headers, the disk cache, the slug guard and redirect safety stayed there. The
final section, what must never reach the browser (an API key, unescaped user content), lives here
rather than in that file: it shares this file's `with_provider` and `full_model` imports, and
splitting two tests alone would not have cleared either file's budget on its own.

Offline (a fake provider), isolated workspace per test; the fixtures and the seeded-session helper
live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from requivo.core.errors import InputTooLargeError
from requivo.web.security import (
    CSRF_FIELD,
    CSRF_HEADER,
    MissingRequestTokenError,
    _enforce,
    _same_trust_domain,
    csrf_token,
)
from tests.web.conftest import HIGH_EXPLICIT, _make_session, full_model

# ── cross-site protection ─────────────────────────────────────────────────────
# Listening on 127.0.0.1 keeps nobody out: any page open in the same browser can post to a known local
# port without a preflight, and for this app writing *is* the damage (sessions created, provider calls
# billed). These pin each layer of web/security.py independently.

def test_a_write_without_the_request_token_is_refused(raw_client):
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "evil", "provider": "create_only"})
    assert r.status_code == 403
    assert raw_client.get("/").status_code == 200          # reads are untouched


def test_the_token_works_as_a_form_field(raw_client):
    # The browser path: a hidden input, not a header — no page in this app can set a request header.
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "ok", "provider": "create_only",
                                           CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303


def test_forms_render_the_request_token(client):
    assert csrf_token() in client.get("/").text


def test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing(raw_client):
    """A token the comparison cannot read is a wrong token, not an unhandled exception (#212).
    `secrets.compare_digest` raises `TypeError` unless both `str` args are ASCII-only, and one non-ASCII
    character escaped `_guard`'s two `except` arms and `security_headers`, landing on the outermost 500
    with no CSP, no nosniff and no Referrer-Policy. The headers assertion is the point: a fix catching
    `TypeError` at the wrong layer would satisfy the status code and still answer without them."""
    hostile = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "é"})
    assert hostile.status_code == 403, "a token that cannot be compared has to be refused, not crash"
    assert "missing_request_token" in hostile.text
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in hostile.headers, (
            f"the refusal answered without {header} — it escaped the header middleware")

    # Must fire. An ASCII wrong token already took this path before the fix, so without this control
    # the assertions above would also pass against a guard that refused *every* token, headers and
    # all, and told us nothing about the non-ASCII one.
    control = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "wrong"})
    assert control.status_code == 403
    assert "Content-Security-Policy" in control.headers

    # …and the other must-fire half: a *valid* token still passes, so the fix did not close the door
    # on the browser path it exists to serve.
    ok = raw_client.post(
        "/sessions",
        data={"request_text": "x", "slug": "still-works", "provider": "create_only",
              CSRF_FIELD: csrf_token()},
        follow_redirects=False)
    assert ok.status_code == 303


def test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison(app):
    """The header half of the same defect, driven at the ASGI seam where it actually arrives. Starlette
    decodes header bytes as latin-1, so a raw 0xe9 byte in `x-csrf-token` becomes a one-character
    non-ASCII `str` before `_enforce` sees it -- a value no HTTP client library lets a test send
    through the ordinary API, since httpx encodes headers as ASCII. Driving the scope directly is
    what makes this leg assertable, the same input a proxy or mangling client can send."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/sessions",
        "raw_path": b"/sessions",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"127.0.0.1:8765"), (b"content-length", b"0"),
                    (b"x-csrf-token", b"\xe9")],
        "client": ("127.0.0.1", 1234),
        "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(MissingRequestTokenError):
        asyncio.run(_enforce(Request(scope, receive)))


# ── the body cap (#216) ────────────────────────────────────────────────────────

def test_a_chunked_body_is_refused_before_being_read(app):
    """#216: `MAX_BODY_BYTES` used to be checked only against a declared `Content-Length` -- a chunked
    request (no such header) sailed past that check and was read in full before the size was measured.
    An instrumented `receive` reproduces a chunked POST at the ASGI layer: no `content-length`, one
    `http.request` event after another. Asserting the status code alone is not enough (the old code
    also answers 413, after buffering everything) -- what matters is that `receive` is never called."""
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import MAX_BODY_BYTES, _enforce, csrf_token

    receive_calls = []

    async def instrumented_receive():
        # However many bytes are behind it, this body must never be asked for one of them. Bounded
        # at MAX_BODY_BYTES worth of chunks (plus one) rather than genuinely unbounded, so a version
        # of the guard that does not refuse in time fails this test in finite time instead of hanging
        # the suite -- MAX_BODY_BYTES is comfortably exceeded well before the generator runs out.
        receive_calls.append(len(receive_calls))
        more = len(receive_calls) <= MAX_BODY_BYTES // 1_000 + 1
        return {"type": "http.request", "body": b"x" * 1_000, "more_body": more}

    scope = {
        "type": "http", "method": "POST", "path": "/sessions", "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8765"), (b"sec-fetch-site", b"same-origin"),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"x-csrf-token", csrf_token().encode())],
        "app": app,
    }
    with pytest.raises(InputTooLargeError):
        asyncio.run(_enforce(Request(scope, instrumented_receive)))
    assert receive_calls == [], "the body was read before the missing Content-Length was refused"


def test_a_declared_length_post_is_unaffected(app):
    """Must-fire control for the refusal above: a real client always declares a length (the
    app's own forms, curl, httpx, requests all do), and that path must still work exactly as
    before -- this is not a tightening of what a legitimate caller can do."""
    control = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    control.headers[CSRF_HEADER] = csrf_token()
    accepted = control.post(
        "/sessions", data={"request_text": "x", "slug": "declared-length-still-fine",
                           "provider": "create_only"}, follow_redirects=False)
    assert accepted.status_code == 303


def test_a_request_with_no_body_and_no_content_length_is_still_refused(app):
    """The refusal is keyed on the *header*, not on whether bytes actually follow -- an unsafe
    method carrying no declared length at all is refused the same way regardless of what a
    real chunked stream would eventually contain, which is the point: the check must never
    need to look."""
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import _enforce, csrf_token

    async def empty_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/sessions", "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8765"), (b"sec-fetch-site", b"same-origin"),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"x-csrf-token", csrf_token().encode())],
        "app": app,
    }
    with pytest.raises(InputTooLargeError):
        asyncio.run(_enforce(Request(scope, empty_receive)))


def test_a_write_from_another_origin_is_refused(client):
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_a_browser_declared_cross_site_write_is_refused(client):
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_a_request_addressed_to_another_host_is_refused(app):
    # DNS rebinding: `evil.example` resolving to 127.0.0.1 is same-origin to the browser, so it would
    # pass every other check *and* be able to read the token off the page. The host allowlist is the
    # only guard that catches it, which is why it also runs on reads.
    rebound = TestClient(app, base_url="http://evil.example", raise_server_exceptions=False)
    assert rebound.get("/").status_code == 403


def test_a_host_the_server_cannot_determine_is_refused_rather_than_skipped(app):
    """#45: the allowlist used to read `if host and host not in allowed_hosts()`, so `""` -- what `_hostname`
    returns when it could not determine a host -- skipped the check entirely instead of failing it: a check
    that cannot read its input treats that as no check needed, reporting nothing while off. Observed at the
    socket against 0.10.1: an empty `Host:` answered 200, reproduced here via `TestClient`. The accept
    beside it is the control: a determined loopback `GET` must still answer 200 in this same fixture."""
    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)

    empty = c.get("/", headers={"Host": ""})
    assert empty.status_code == 403
    # Names its own arm rather than borrowing the generic another-origin wording, as #43 did for the
    # opaque origin: a guard that could not look must not print what a guard that looked and refused
    # prints. Since #52 that arm has its own **code**, which is the stable identifier
    # `docs/compatibility.md` says to assert on — this used to match the message text because
    # `cross_site_request` was raised for all six arms and the wording was the only handle there was.
    assert "undetermined_host" in empty.text

    # whitespace-only is the same undetermined state by a different spelling — `_hostname` strips first
    assert c.get("/", headers={"Host": "   "}).status_code == 403

    # must still pass, same fixture: a determined loopback host on a read
    assert c.get("/").status_code == 200
    assert c.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_a_request_that_states_no_host_at_all_is_refused(app):
    """The other observed row: `GET / HTTP/1.0` with no `Host` header, which h11 admits (only required on
    HTTP/1.1), and which answered 200. Driven against `_enforce` over a hand-built ASGI scope, since no client
    this suite can build will omit the header -- httpx raises on a `None` value, and `TestClient` always derives
    one from `base_url`. The determined-host scope is asserted first as the must-fire control -- without it a
    malformed scope, or an `_enforce` that raised on everything, would pass this test while checking nothing."""
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import CrossSiteRequestError, _enforce

    def verdict(headers: list[tuple[bytes, bytes]]) -> str:
        """`"accepted"`, or the refusal's error **code** — never a bare boolean, so the arm is
        visible here. The code rather than the message since #52: each arm now carries its own,
        and a code is the identifier `docs/compatibility.md` says to assert on."""
        async def run() -> str:
            scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
                     "headers": headers}
            try:
                await _enforce(Request(scope))
            except CrossSiteRequestError as exc:
                return exc.code
            return "accepted"
        return asyncio.run(run())

    assert verdict([(b"host", b"127.0.0.1:8765")]) == "accepted"          # must fire
    assert verdict([]) == "undetermined_host"                            # no Host header at all
    assert verdict([(b"host", b"evil.example")]) == "host_not_allowed"   # and the mismatch arm survives


# ── the origin check: which hostnames are one trust domain (#43) ──────────────

def _guard_post(app, *, host: str, slug: str, headers: dict | None = None):
    """One write, addressed to `host`, carrying a valid request token — so the only thing under
    test is the origin check. Redirects are not followed, so an accepted write reads as 303
    and a refused one as 403 rather than both landing on a rendered page."""
    c = TestClient(app, base_url=f"http://{host}", raise_server_exceptions=False)
    c.headers[CSRF_HEADER] = csrf_token()
    return c.post("/sessions",
                  data={"request_text": "A leave approval system.", "slug": slug,
                        "provider": "create_only"},
                  headers=headers or {}, follow_redirects=False)


def test_the_loopback_spellings_are_one_origin_and_evil_example_still_is_not(app):
    """#43: `localhost`, `127.0.0.1` and `::1` are three spellings of one machine -- the host allowlist
    already treats them as interchangeable, but the origin check compared them as strings, so a page
    served on one spelling could not post to the other. Reported from a real browser on 0.10.0: the
    form could not be submitted, and the natural recovery resubmits the stale `Origin` and reproduces
    the same 403. The refusal half lives here on purpose: acceptance alone would pass a deleted guard."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="lb-localhost-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303
    assert _guard_post(app, host="localhost:8765", slug="lb-ipv4-origin",
                       headers={"Origin": "http://127.0.0.1:8765"}).status_code == 303
    assert _guard_post(app, host="127.0.0.1:8765", slug="lb-ipv6-origin",
                       headers={"Origin": "http://[::1]:8765"}).status_code == 303
    # must still fire — same fixture, same valid token, only the origin differs
    assert _guard_post(app, host="127.0.0.1:8765", slug="hostile-vs-ipv4",
                       headers={"Origin": "http://evil.example"}).status_code == 403
    assert _guard_post(app, host="localhost:8765", slug="hostile-vs-localhost",
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_a_referer_gets_the_same_equivalence_and_the_same_refusal(app):
    """`Referer` is the fallback the same line reads, so it has to move with `Origin` — the
    reporter's probe measured both, and a fix that widened only one would leave half the
    dead end in place."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-loopback",
                       headers={"Referer": "http://localhost:8765/sessions"}).status_code == 303
    # must still fire
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-hostile",
                       headers={"Referer": "http://evil.example/x"}).status_code == 403


def test_the_opaque_origin_is_refused_deliberately_and_says_which_arm_fired(app):
    """`Origin: null` is a browser declining to attribute where it is posting from. Before #43 it was
    refused only by accident -- `_hostname("null")` returns the literal string `"null"`, which
    failed an equality test -- and an accident is not a decision. Refused on purpose now: browsers
    attach `Origin` to every POST, so silence means no browser is speaking, while `null` is the
    one origin a browser-borne attacker can emit. The accept beside it is the control."""
    r = _guard_post(app, host="127.0.0.1:8765", slug="opaque-origin", headers={"Origin": "null"})
    assert r.status_code == 403
    assert "opaque origin" in r.text          # the opaque arm, not the generic another-origin refusal
    # must be accepted, same fixture
    assert _guard_post(app, host="127.0.0.1:8765", slug="attributed-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303


# Fetch's *append a request `Origin` header* consults the referrer policy for any request that is not
# CORS-mode — an ordinary HTML form submit is a navigation, not CORS — and whose method is not
# GET/HEAD. This table is that algorithm restricted to the only case this app's plain forms produce: a
# **same-origin** post over plain HTTP. Only `no-referrer` replaces the origin with the opaque value.
# The downgrade-sensitive policies null it solely on an HTTPS→HTTP downgrade, which a same-origin
# request cannot be, and `same-origin` nulls it solely when the request is genuinely cross-origin.
_ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST = {
    "no-referrer": "null",
    "no-referrer-when-downgrade": "SELF",
    "origin": "SELF",
    "origin-when-cross-origin": "SELF",
    "same-origin": "SELF",
    "strict-origin": "SELF",
    "strict-origin-when-cross-origin": "SELF",
    "unsafe-url": "SELF",
}


def test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree(app, client):
    """The header this app emits must not produce an `Origin` this app's own guard refuses (#47). Neither half was wrong alone:
    `Referrer-Policy: no-referrer` is defensible, and refusing the opaque origin is a deliberate decision (#43) -- the
    defect lived only in the composition, a same-origin post arriving as `Origin: null` because of a header this server had
    just sent, then refused by the server that sent it. `TestClient` implements no referrer policy, so the browser's half
    comes from the table above; what is under test is the header this app emits and the guard's verdict on it."""
    policy = client.get("/").headers["Referrer-Policy"]
    assert policy in _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST, (
        f"Referrer-Policy {policy!r} is not in the table this test reasons over. Add what Fetch says "
        "it does to a same-origin form post; do not drop the assertion.")
    origin = _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST[policy]
    if origin == "SELF":
        origin = "http://127.0.0.1:8765"

    # must fire: the guard really is refusing opaque origins in this fixture. Without it the acceptance
    # below would read exactly the same against a guard that had been deleted — and `no-referrer`
    # shipping a second time would then land green.
    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-opaque",
                       headers={"Origin": "null"}).status_code == 403

    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-real",
                       headers={"Origin": origin}).status_code == 303, (
        f"Referrer-Policy: {policy} makes a browser attach Origin: {origin} to a same-origin form "
        "post, and this app's own cross-site guard refuses it — the form cannot be submitted (#47)")


def test_no_origin_headers_at_all_keeps_its_current_behaviour(app):
    """A scripted client sends neither header and the request token is what gates it — `curl`
    with a valid token is a supported caller, and this suite's own posts are that caller.
    #43 asked whether the absent case should be tightened to match `null`; it is
    deliberately left alone, so it is pinned here rather than left implicit, with the token-
    less post beside it as the control that the write path is still guarded at all."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="no-origin-stated").status_code == 303
    bare = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    assert bare.post("/sessions", data={"request_text": "x", "slug": "no-token-at-all",
                                        "provider": "create_only"}).status_code == 403


def test_two_hostnames_nobody_could_determine_are_not_a_match():
    """`_hostname` returns `""` when it could not find a hostname -- an absent/unparseable `Host`, or an origin
    like `http:///` naming nobody. Two of those compared equal and read as same trust domain, so the one
    input where neither side was determined produced the same verdict as a verified match -- a check that
    could not look has to say so rather than answer. Asserted directly rather than over HTTP, since no
    client this suite can build will omit a `Host` header; the rows below are the must-fire control."""
    assert _same_trust_domain("", "") is False              # neither side determined — not a match
    assert _same_trust_domain("", "127.0.0.1") is False
    assert _same_trust_domain("127.0.0.1", "") is False
    # must fire
    assert _same_trust_domain("localhost", "127.0.0.1") is True
    assert _same_trust_domain("evil.example", "evil.example") is True
    assert _same_trust_domain("evil.example", "127.0.0.1") is False


def test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision(app):
    """#46: `_hostname` discards the port on both sides, so this check accepts any page on any loopback port -- not, as the
    docstring used to claim, only a page from this process; the behaviour is deliberate and pinned here. Deliberate because the
    request token gates the write, a foreign-port page cannot obtain it, the browser's own same-origin policy already blocks the
    cross-origin read, and `Sec-Fetch-Site` refuses the cross-port post before this line is reached -- comparing the port would
    add nothing and repeats #43's exact false-positive shape. The hostile row beside it is the must-fire control."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-loopback",
                       headers={"Origin": "http://localhost:3000"}).status_code == 303
    # must still fire — a port is not what makes a foreign host acceptable either
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-hostile",
                       headers={"Origin": "http://evil.example:8765"}).status_code == 403


def test_operator_listed_hosts_are_not_interchangeable_with_one_another(app, monkeypatch):
    """The equivalence is the fixed loopback set this module defines, never whatever an
    operator put in `REQUIVO_WEB_ALLOWED_HOSTS`. Those are real hostnames and two of them
    may well be meant as two distinct origins; a blanket `allowed_hosts()` membership test
    would have made that call on the operator's behalf. Each is still same-origin with
    itself, which is the accept half."""
    monkeypatch.setenv("REQUIVO_WEB_ALLOWED_HOSTS", "app.internal,admin.internal")
    assert _guard_post(app, host="app.internal", slug="named-self",
                       headers={"Origin": "http://app.internal"}).status_code == 303
    # must still fire, in both directions — loopback does not launder into the opt-in set either
    assert _guard_post(app, host="app.internal", slug="named-sibling",
                       headers={"Origin": "http://admin.internal"}).status_code == 403
    assert _guard_post(app, host="app.internal", slug="loopback-into-named",
                       headers={"Origin": "http://localhost:8765"}).status_code == 403
    assert _guard_post(app, host="127.0.0.1:8765", slug="named-into-loopback",
                       headers={"Origin": "http://app.internal"}).status_code == 403


# ── what must not reach the browser ───────────────────────────────────────────

def test_api_key_never_reaches_the_browser(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-sentinel-123")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    for path in ("/", "/sessions/new", "/sessions/leave-approval"):
        assert "sk-secret-sentinel-123" not in client.get(path).text


def test_user_content_is_escaped(client, with_provider):
    with_provider(json.dumps({
        "model": full_model(problem=HIGH_EXPLICIT), "questions": [],
        "summary": {"objective": "<script>alert(1)</script>"}}))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    assert "<script>alert(1)</script>" not in page       # escaped
    assert "&lt;script&gt;" in page
