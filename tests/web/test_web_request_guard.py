"""Requivo Web security: the request-side guard -- CSRF token, body cap, Host/Origin trust (#555)."""

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
# Listening on 127.0.0.1 keeps nobody out: any page open in the same browser can post to a known local port without a preflight, and for this app writing *is* the damage (sessions created, provider calls billed).

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
    """A token the comparison cannot read is a wrong token, not an unhandled exception (#212)."""
    hostile = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "é"})
    assert hostile.status_code == 403, "a token that cannot be compared has to be refused, not crash"
    assert "missing_request_token" in hostile.text
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in hostile.headers, (
            f"the refusal answered without {header} — it escaped the header middleware")

    # Must fire.
    control = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "wrong"})
    assert control.status_code == 403
    assert "Content-Security-Policy" in control.headers

    # …and the other must-fire half: a *valid* token still passes.
    ok = raw_client.post(
        "/sessions",
        data={"request_text": "x", "slug": "still-works", "provider": "create_only",
              CSRF_FIELD: csrf_token()},
        follow_redirects=False)
    assert ok.status_code == 303


def test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison(app):
    """The header half of the same defect, driven at the ASGI seam where it actually arrives."""
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
    """#216: `MAX_BODY_BYTES` used to be checked only against a declared `Content-Length`."""
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import MAX_BODY_BYTES, _enforce, csrf_token

    receive_calls = []

    async def instrumented_receive():
        # However many bytes are behind it, this body must never be asked for one of them.
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
    """Must-fire control for the refusal above: a real client always declares a length (the app's own forms,
    curl, httpx, requests all do), and that path must still work exactly as before."""
    control = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    control.headers[CSRF_HEADER] = csrf_token()
    accepted = control.post(
        "/sessions", data={"request_text": "x", "slug": "declared-length-still-fine",
                           "provider": "create_only"}, follow_redirects=False)
    assert accepted.status_code == 303


def test_a_request_with_no_body_and_no_content_length_is_still_refused(app):
    """The refusal is keyed on the *header*, not on whether bytes actually follow."""
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
    # DNS rebinding: `evil.example` resolving to 127.0.0.1 is same-origin to the browser.
    rebound = TestClient(app, base_url="http://evil.example", raise_server_exceptions=False)
    assert rebound.get("/").status_code == 403


def test_a_host_the_server_cannot_determine_is_refused_rather_than_skipped(app):
    """#45: the allowlist used to read `if host and host not in allowed_hosts()`, so `""`."""
    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)

    empty = c.get("/", headers={"Host": ""})
    assert empty.status_code == 403
    # Names its own arm rather than borrowing the generic another-origin wording (#43).
    assert "undetermined_host" in empty.text

    # whitespace-only is the same undetermined state by a different spelling — `_hostname` strips first
    assert c.get("/", headers={"Host": "   "}).status_code == 403

    # must still pass, same fixture: a determined loopback host on a read
    assert c.get("/").status_code == 200
    assert c.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_a_request_that_states_no_host_at_all_is_refused(app):
    """The other observed row: `GET / HTTP/1.0` with no `Host` header."""
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import CrossSiteRequestError, _enforce

    def verdict(headers: list[tuple[bytes, bytes]]) -> str:
        """`"accepted"`, or the refusal's error **code** (#52)."""
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
    """One write, addressed to `host`, carrying a valid request token."""
    c = TestClient(app, base_url=f"http://{host}", raise_server_exceptions=False)
    c.headers[CSRF_HEADER] = csrf_token()
    return c.post("/sessions",
                  data={"request_text": "A leave approval system.", "slug": slug,
                        "provider": "create_only"},
                  headers=headers or {}, follow_redirects=False)


def test_the_loopback_spellings_are_one_origin_and_evil_example_still_is_not(app):
    """#43: `localhost`, `127.0.0.1` and `::1` are three spellings of one machine."""
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
    """`Referer` is the fallback the same line reads, so it has to move with `Origin`."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-loopback",
                       headers={"Referer": "http://localhost:8765/sessions"}).status_code == 303
    # must still fire
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-hostile",
                       headers={"Referer": "http://evil.example/x"}).status_code == 403


def test_the_opaque_origin_is_refused_deliberately_and_says_which_arm_fired(app):
    """`Origin: null` is a browser declining to attribute where it is posting from (#43)."""
    r = _guard_post(app, host="127.0.0.1:8765", slug="opaque-origin", headers={"Origin": "null"})
    assert r.status_code == 403
    assert "opaque origin" in r.text          # the opaque arm, not the generic another-origin refusal
    # must be accepted, same fixture
    assert _guard_post(app, host="127.0.0.1:8765", slug="attributed-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303


# Fetch's *append a request `Origin` header* consults the referrer policy for any request that is not CORS-mode — an ordinary HTML form submit is a navigation, not CORS — and whose method is not GET/HEAD.
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
    """The header this app emits must not produce an `Origin` this app's own guard refuses (#47)."""
    policy = client.get("/").headers["Referrer-Policy"]
    assert policy in _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST, (
        f"Referrer-Policy {policy!r} is not in the table this test reasons over. Add what Fetch says "
        "it does to a same-origin form post; do not drop the assertion.")
    origin = _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST[policy]
    if origin == "SELF":
        origin = "http://127.0.0.1:8765"

    # must fire: the guard really is refusing opaque origins in this fixture.
    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-opaque",
                       headers={"Origin": "null"}).status_code == 403

    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-real",
                       headers={"Origin": origin}).status_code == 303, (
        f"Referrer-Policy: {policy} makes a browser attach Origin: {origin} to a same-origin form "
        "post, and this app's own cross-site guard refuses it — the form cannot be submitted (#47)")


def test_no_origin_headers_at_all_keeps_its_current_behaviour(app):
    """A scripted client sends neither header and the request token is what gates it (#43)."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="no-origin-stated").status_code == 303
    bare = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    assert bare.post("/sessions", data={"request_text": "x", "slug": "no-token-at-all",
                                        "provider": "create_only"}).status_code == 403


def test_two_hostnames_nobody_could_determine_are_not_a_match():
    """`_hostname` returns `""` when it could not find a hostname."""
    assert _same_trust_domain("", "") is False              # neither side determined — not a match
    assert _same_trust_domain("", "127.0.0.1") is False
    assert _same_trust_domain("127.0.0.1", "") is False
    # must fire
    assert _same_trust_domain("localhost", "127.0.0.1") is True
    assert _same_trust_domain("evil.example", "evil.example") is True
    assert _same_trust_domain("evil.example", "127.0.0.1") is False


def test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision(app):
    """#46: `_hostname` discards the port on both sides, so this check accepts any page on any loopback port."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-loopback",
                       headers={"Origin": "http://localhost:3000"}).status_code == 303
    # must still fire — a port is not what makes a foreign host acceptable either
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-hostile",
                       headers={"Origin": "http://evil.example:8765"}).status_code == 403


def test_operator_listed_hosts_are_not_interchangeable_with_one_another(app, monkeypatch):
    """The equivalence is the fixed loopback set this module defines."""
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
