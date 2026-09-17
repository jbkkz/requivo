"""Requivo Web security: response headers, the disk cache, the slug guard, redirect safety, the request-side
guard (CSRF token, body cap, Host/Origin trust), the authority parser and the provider probe (#555, #627)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from requivo.core import persistence as store
from requivo.core.errors import InputTooLargeError
from requivo.http import STATUS_BY_CODE
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web import security
from requivo.web.config import provider_status
from requivo.web.security import (
    ALLOWED_HOSTS_ENV,
    CSRF_FIELD,
    CSRF_HEADER,
    MAX_BODY_BYTES,
    CrossSiteRequestError,
    MissingRequestTokenError,
    _enforce,
    _hostname,
    _same_trust_domain,
    allowed_hosts,
    csrf_token,
)
from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, _make_session, create_via_post, engine_reply, full_slots

_CREATE = {"request_text": "x", "provider": "create_only"}

# ── the headers every response carries ────────────────────────────────────────


@pytest.fixture
def failing_route(app):
    """A route that raises, so a test can reach the unhandled-500 path the way a bug would."""
    @app.get("/_boom")
    def _boom():
        raise ValueError("a bug, not a refusal")
    return "/_boom"


@pytest.mark.parametrize("path,expected_status", [("/", 200), ("/not-found", 404)])
def test_security_headers_present(client, path, expected_status):
    r = client.get(path)
    assert r.status_code == expected_status
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in h and "default-src 'self'" in h["Content-Security-Policy"]
    assert "Referrer-Policy" in h  # presence only (#47)
    assert h["Cache-Control"] == "no-store"


def test_the_500_page_carries_every_header_an_ordinary_page_carries(client, failing_route):
    """The 500 page was the one response class served with none of the above (#340); the composition is compared."""
    ordinary, unhandled = client.get("/"), client.get(failing_route)
    assert ordinary.status_code == 200 and unhandled.status_code == 500
    missing = set(ordinary.headers) - set(unhandled.headers)
    assert not missing, f"the 500 page is missing {sorted(missing)}"


# ── the disk cache (#218): an allowlist of bundled assets, so it fails closed ──


def test_no_response_carrying_the_readers_material_may_be_written_to_the_disk_cache(client, with_provider):
    """Every page, fragment and download carries the reader's request, the model built from it, or the token."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT), BRIEF_REPLY)
    _make_session()
    page = client.get("/sessions/leave-approval")
    fragment = client.post("/sessions/leave-approval/answers",
                           data={"answers": "Contractors are out of scope.", "expected_revision": "1"},
                           headers={"HX-Request": "true"})
    client.post("/sessions/leave-approval/artifacts/brief")
    rows = {
        "home": client.get("/"), "session page": page, "htmx fragment": fragment,
        "model download": client.get("/sessions/leave-approval/export"),
        "artifact page": client.get("/sessions/leave-approval/artifacts/brief"),
        "artifact download": client.get("/sessions/leave-approval/artifacts/brief?download=1"),
    }
    for name, response in rows.items():
        # Must fire: an error page would satisfy a header assertion just as happily as the real page.
        assert response.status_code == 200, f"the {name} row never reached the response it is about"
        assert response.headers.get("Cache-Control") == "no-store", (
            f"the {name} response may be written to the browser's disk cache")


@pytest.mark.parametrize("path", ["/static/css/app.css", "/static/js/app.js", "/static/vendor/htmx.min.js",
                                  "/favicon.ico"])
def test_a_bundled_asset_stays_cacheable(client, path):
    """The other half of the boundary, and the reason it is an allowlist rather than a blanket."""
    response = client.get(path)
    assert response.status_code == 200, f"{path} was not served, so this row asserts nothing"
    assert "Content-Security-Policy" in response.headers, f"the header middleware never ran on {path}"
    assert "no-store" not in response.headers.get("Cache-Control", ""), f"{path} must stay cacheable"


# ── the slug guard ────────────────────────────────────────────────────────────


def test_slug_traversal_is_rejected(client):
    # A dot-segment slug never resolves to a path: invalid_slug (400) or no route match (404), never the store.
    assert client.get("/sessions/..%2f..%2fsecret").status_code in (400, 404)
    assert client.get("/sessions/a..b").status_code == 400
    assert client.post("/sessions", data={**_CREATE, "slug": "../escape"}).status_code == 400


# ── open redirect: every RedirectResponse target stays same-origin (#500) ──────
# A `Form`-field slug (create_session) is never URL-normalised by a client, so every attempt reaches
# `validate_slug` unchanged; a `{slug}` path parameter goes through `Depends(safe_slug)`.
_FORM_FIELD_SLUG_ATTEMPTS = [
    "../etc", "../../etc/passwd", "%2e%2e%2fetc", "%2e%2e%252fetc", "..%5cwindows",
    "/etc/passwd", "....//etc", "C:\\Windows", "evil.com", "@evil.com", "evil.com%2f%2f",
]
_PATH_PARAM_SLUG_ATTEMPTS = [
    "a..b", "..%5cwindows", "C:\\Windows", "evil.com", "@evil.com", "leave%2Dapproval%00",
    "leave-approval..", "..leave-approval", "under_score", "with space",
]
_ANSWERS = {"answers": "x", "expected_revision": "0"}
# The four call sites CodeQL's py/url-redirection flags: discovery.py's run_discovery and submit_answers
# (no-JS branch), artifacts.py's generate_artifact (no-JS branch), sessions.py's analysis_failed.
_REDIRECT_SITES = {
    "discover": lambda c, s: c.post(f"/sessions/{s}/discover", follow_redirects=False),
    "answers": lambda c, s: c.post(f"/sessions/{s}/answers", data=_ANSWERS, follow_redirects=False),
    "generate": lambda c, s: c.post(f"/sessions/{s}/artifacts/brief", follow_redirects=False),
    "create": lambda c, s: c.post("/sessions", data={**_CREATE, "slug": s}, follow_redirects=False),
}


def _assert_never_off_site(resp, attempt):
    """A hostile slug must be refused, never redirected -- the refusal is the assertion that always runs."""
    assert resp.status_code >= 400, (
        f"slug {attempt!r} was not refused -- got {resp.status_code} "
        f"-> {resp.headers.get('location', '(no Location)')!r}; a hostile slug must never reach a redirect")
    if 300 <= resp.status_code < 400:  # unreachable while the assertion above holds
        location = resp.headers.get("location", "")
        assert location.startswith("/") and not location.startswith("//"), f"redirected off-site: {location!r}"
        assert "://" not in location, f"redirected off-site: {location!r}"


@pytest.mark.parametrize("site", sorted(_REDIRECT_SITES))
def test_a_redirect_never_leaves_this_origin_under_a_hostile_slug(raw_client, site):
    """Each of the four flagged `RedirectResponse` sites, under every attempt its slug's route lets through (#500)."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    attempts = _FORM_FIELD_SLUG_ATTEMPTS if site == "create" else _PATH_PARAM_SLUG_ATTEMPTS
    for attempt in attempts:
        _assert_never_off_site(_REDIRECT_SITES[site](raw_client, attempt), attempt)


def test_the_redirect_refusal_is_the_slug_guard_and_not_merely_a_missing_session(raw_client):
    """The must-fire half: a 404 from a nonsense slug proves nothing."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    for site in _REDIRECT_SITES.values():
        resp = site(raw_client, "Not_A_Slug")
        assert resp.status_code == 400 and "invalid_slug" in resp.text, resp.text


def test_a_legitimate_slug_still_redirects_where_it_should(raw_client, with_provider, monkeypatch):
    """Must-not-fire control: an honest slug still reaches the redirect at all four sites."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    SessionService().create_session("A leave approval system.", slug="leave-only")
    with_provider(engine_reply(converged=True), engine_reply(converged=True))
    r = raw_client.post("/sessions/leave-only/discover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-only"

    _make_session("leave-approval")
    r = raw_client.post("/sessions/leave-approval/answers", data={**_ANSWERS, "expected_revision": "1"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"

    with_provider(BRIEF_REPLY)
    r = raw_client.post("/sessions/leave-approval/artifacts/brief", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"

    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")
    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "leave-failed", "provider": "anthropic"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/sessions/leave-failed?analysis_failed=")


# ── #396: a session already on disk under a Windows reserved device name is reachable ──


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named 'con' on disk, "
                    "which Windows refuses at the OS level. REASONED, NOT OBSERVED, as the sibling #372 fixtures.")
def test_a_reserved_slug_already_on_disk_is_reachable_through_the_web_read_routes(client, with_provider):
    """The read/create asymmetry, both halves in one fixture (#396)."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None, "context_cards": None,
        "current_revision": 0, "format_version": 1, "revisions": [], "artifact_status": {}}), encoding="utf-8")

    assert client.get("/sessions/con").status_code == 200
    with_provider(engine_reply(converged=True))
    r = client.post("/sessions/con/discover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/con", r.text
    assert store.read_meta("con").current_revision == 1
    assert client.get("/sessions/con/export").status_code == 200
    # Must-not-fire control: `POST /sessions` takes its slug from a form field and stays strict (#372).
    created = client.post("/sessions", data={"request_text": "A leave approval system.", "slug": "nul",
                                             "provider": "create_only"})
    assert created.status_code == 400 and "reserved Windows device name" in created.text
    assert not (store.session_root() / "nul").exists()


# ── cross-site protection: listening on 127.0.0.1 keeps nobody out, and writing *is* the damage ──


def test_a_write_without_the_request_token_is_refused(raw_client):
    assert raw_client.post("/sessions", data={**_CREATE, "slug": "evil"}).status_code == 403
    assert raw_client.get("/").status_code == 200          # reads are untouched


def test_the_token_works_as_a_form_field(raw_client, client):
    # The browser path: a hidden input, not a header — no page in this app can set a request header.
    r = raw_client.post("/sessions", data={**_CREATE, "slug": "ok", CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    assert csrf_token() in client.get("/").text            # …and every rendered form carries it


def test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing(raw_client):
    """A token the comparison cannot read is a wrong token, not an unhandled exception (#212)."""
    hostile = raw_client.post("/sessions", data={**_CREATE, CSRF_FIELD: "é"})
    assert hostile.status_code == 403 and "missing_request_token" in hostile.text
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in hostile.headers, f"the refusal answered without {header} — it escaped the middleware"
    control = raw_client.post("/sessions", data={**_CREATE, CSRF_FIELD: "wrong"})   # must fire
    assert control.status_code == 403 and "Content-Security-Policy" in control.headers
    ok = raw_client.post("/sessions", data={**_CREATE, "slug": "still-works", CSRF_FIELD: csrf_token()},
                         follow_redirects=False)
    assert ok.status_code == 303                            # …and a valid token still passes


def _scope(app, headers, *, method="POST", path="/sessions"):
    return {"type": "http", "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "scheme": "http", "headers": headers,
            "client": ("127.0.0.1", 1234), "app": app}


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


_POST_HEADERS = [(b"host", b"127.0.0.1:8765"), (b"sec-fetch-site", b"same-origin"),
                 (b"content-type", b"application/x-www-form-urlencoded")]


def test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison(app):
    """The header half of the same defect, driven at the ASGI seam where it actually arrives (#212)."""
    headers = [(b"host", b"127.0.0.1:8765"), (b"content-length", b"0"), (b"x-csrf-token", b"\xe9")]
    with pytest.raises(MissingRequestTokenError):
        asyncio.run(_enforce(Request(_scope(app, headers), _empty_receive)))


# ── the body cap (#216) ────────────────────────────────────────────────────────


def test_a_chunked_body_is_refused_before_being_read(app):
    """#216: `MAX_BODY_BYTES` used to be checked only against a declared `Content-Length`."""
    receive_calls = []

    async def instrumented_receive():
        receive_calls.append(len(receive_calls))
        more = len(receive_calls) <= MAX_BODY_BYTES // 1_000 + 1
        return {"type": "http.request", "body": b"x" * 1_000, "more_body": more}

    headers = [*_POST_HEADERS, (b"x-csrf-token", csrf_token().encode())]
    with pytest.raises(InputTooLargeError):
        asyncio.run(_enforce(Request(_scope(app, headers), instrumented_receive)))
    assert receive_calls == [], "the body was read before the missing Content-Length was refused"
    # The refusal is keyed on the header, not on whether bytes actually follow.
    with pytest.raises(InputTooLargeError):
        asyncio.run(_enforce(Request(_scope(app, headers), _empty_receive)))


def test_a_declared_length_post_is_unaffected(app):
    """Must-fire control: a real client always declares a length, and that path still works."""
    control = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    control.headers[CSRF_HEADER] = csrf_token()
    accepted = control.post("/sessions", data={**_CREATE, "slug": "declared-length-still-fine"}, follow_redirects=False)
    assert accepted.status_code == 303


# ── Host and Origin trust ─────────────────────────────────────────────────────


def test_a_write_from_another_origin_is_refused(client):
    r = client.post("/sessions", data=_CREATE, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_a_browser_declared_cross_site_write_is_refused(client):
    r = client.post("/sessions", data=_CREATE, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_a_request_addressed_to_another_host_is_refused(app):
    # DNS rebinding: `evil.example` resolving to 127.0.0.1 is same-origin to the browser.
    rebound = TestClient(app, base_url="http://evil.example", raise_server_exceptions=False)
    assert rebound.get("/").status_code == 403


def test_a_host_the_server_cannot_determine_is_refused_rather_than_skipped(app):
    """#45: the allowlist used to read `if host and host not in allowed_hosts()`, so `""` passed (#43 names the arm)."""
    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    empty = c.get("/", headers={"Host": ""})
    assert empty.status_code == 403 and "undetermined_host" in empty.text
    assert c.get("/", headers={"Host": "   "}).status_code == 403   # whitespace-only, `_hostname` strips first
    assert c.get("/").status_code == 200                            # must still pass: a determined loopback host
    assert c.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def _verdict(headers: list[tuple[bytes, bytes]]) -> str:
    """`"accepted"`, or the refusal's error **code** (#52)."""
    async def run() -> str:
        try:
            await _enforce(Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                                    "headers": headers}))
        except CrossSiteRequestError as exc:
            return exc.code
        return "accepted"
    return asyncio.run(run())


def test_a_request_that_states_no_host_at_all_is_refused():
    """The other observed row: `GET / HTTP/1.0` with no `Host` header (#45)."""
    assert _verdict([(b"host", b"127.0.0.1:8765")]) == "accepted"          # must fire
    assert _verdict([]) == "undetermined_host"
    assert _verdict([(b"host", b"evil.example")]) == "host_not_allowed"    # and the mismatch arm survives


def _guard_post(app, *, host: str, slug: str, headers: dict | None = None):
    """One write, addressed to `host`, carrying a valid request token."""
    c = TestClient(app, base_url=f"http://{host}", raise_server_exceptions=False)
    c.headers[CSRF_HEADER] = csrf_token()
    return c.post("/sessions", data={"request_text": "A leave approval system.", "slug": slug,
                                     "provider": "create_only"}, headers=headers or {}, follow_redirects=False)


_LOOPBACK = "127.0.0.1:8765"
_NAMED = "app.internal,admin.internal"
# (host addressed, headers, REQUIVO_WEB_ALLOWED_HOSTS, expected status): `localhost`, `127.0.0.1` and `::1`
# are one origin (#43), `Referer` is the fallback the same line reads, and no origin at all is the token's job.
_ORIGIN_VERDICTS = [
    ("loopback-alias", _LOOPBACK, {"Origin": "http://localhost:8765"}, None, 303),
    ("ipv4-alias", "localhost:8765", {"Origin": "http://127.0.0.1:8765"}, None, 303),
    ("ipv6-alias", _LOOPBACK, {"Origin": "http://[::1]:8765"}, None, 303),
    ("hostile-vs-ipv4", _LOOPBACK, {"Origin": "http://evil.example"}, None, 403),
    ("hostile-vs-localhost", "localhost:8765", {"Origin": "http://evil.example"}, None, 403),
    ("referer-loopback", _LOOPBACK, {"Referer": "http://localhost:8765/sessions"}, None, 303),
    ("referer-hostile", _LOOPBACK, {"Referer": "http://evil.example/x"}, None, 403),
    ("no-origin-stated", _LOOPBACK, {}, None, 303),
    ("named-self", "app.internal", {"Origin": "http://app.internal"}, _NAMED, 303),
    ("named-sibling", "app.internal", {"Origin": "http://admin.internal"}, _NAMED, 403),
    ("loopback-into-named", "app.internal", {"Origin": "http://localhost:8765"}, _NAMED, 403),
    ("named-into-loopback", _LOOPBACK, {"Origin": "http://app.internal"}, _NAMED, 403),
]


@pytest.mark.parametrize("slug, host, headers, env, expected", _ORIGIN_VERDICTS, ids=[v[0] for v in _ORIGIN_VERDICTS])
def test_the_origin_guard_reads_the_loopback_spellings_as_one_origin_and_nothing_else(app, monkeypatch, slug, host,
                                                                                  headers, env, expected):
    """#43: the equivalence is the fixed loopback set; operator-listed hosts are not interchangeable."""
    if env:
        monkeypatch.setenv("REQUIVO_WEB_ALLOWED_HOSTS", env)
    assert _guard_post(app, host=host, slug=slug, headers=headers).status_code == expected
    if slug == "no-origin-stated":
        bare = TestClient(app, base_url=f"http://{_LOOPBACK}", raise_server_exceptions=False)
        assert bare.post("/sessions", data={**_CREATE, "slug": "no-token-at-all"}).status_code == 403


def test_the_opaque_origin_is_refused_deliberately_and_says_which_arm_fired(app):
    """`Origin: null` is a browser declining to attribute where it is posting from (#43)."""
    r = _guard_post(app, host=_LOOPBACK, slug="opaque-origin", headers={"Origin": "null"})
    assert r.status_code == 403 and "opaque origin" in r.text
    assert _guard_post(app, host=_LOOPBACK, slug="attributed-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303   # must be accepted


# What Fetch appends as `Origin` to a same-origin form POST (a navigation, not CORS) under each referrer policy.
_ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST = {
    "no-referrer": "null", "no-referrer-when-downgrade": "SELF", "origin": "SELF",
    "origin-when-cross-origin": "SELF", "same-origin": "SELF", "strict-origin": "SELF",
    "strict-origin-when-cross-origin": "SELF", "unsafe-url": "SELF",
}


def test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree(app, client):
    """The header this app emits must not produce an `Origin` this app's own guard refuses (#47)."""
    policy = client.get("/").headers["Referrer-Policy"]
    assert policy in _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST, (
        f"Referrer-Policy {policy!r} is not in the table this test reasons over; add what Fetch says it does")
    origin = _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST[policy]
    if origin == "SELF":
        origin = f"http://{_LOOPBACK}"
    assert _guard_post(app, host=_LOOPBACK, slug="composed-opaque", headers={"Origin": "null"}).status_code == 403
    assert _guard_post(app, host=_LOOPBACK, slug="composed-real", headers={"Origin": origin}).status_code == 303, (
        f"Referrer-Policy: {policy} makes a browser attach Origin: {origin} and this app's guard refuses it (#47)")


def test_two_hostnames_nobody_could_determine_are_not_a_match():
    """`_hostname` returns `""` when it could not find a hostname, and `""` matches nothing, not even itself."""
    assert _same_trust_domain("", "") is False
    assert _same_trust_domain("", "127.0.0.1") is False and _same_trust_domain("127.0.0.1", "") is False
    assert _same_trust_domain("localhost", "127.0.0.1") is True                  # must fire
    assert _same_trust_domain("evil.example", "evil.example") is True
    assert _same_trust_domain("evil.example", "127.0.0.1") is False


def test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision(app):
    """#46: `_hostname` discards the port on both sides, so this check accepts any page on any loopback port."""
    assert _guard_post(app, host=_LOOPBACK, slug="cross-port-loopback",
                       headers={"Origin": "http://localhost:3000"}).status_code == 303
    assert _guard_post(app, host=_LOOPBACK, slug="cross-port-hostile",
                       headers={"Origin": "http://evil.example:8765"}).status_code == 403   # must still fire


# ── what must not reach the browser ───────────────────────────────────────────


def test_api_key_never_reaches_the_browser(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-sentinel-123")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    for path in ("/", "/sessions/new", "/sessions/leave-approval"):
        assert "sk-secret-sentinel-123" not in client.get(path).text


def test_user_content_is_escaped(client, with_provider):
    with_provider(json.dumps({"model": full_slots(problem=HIGH_EXPLICIT), "questions": [],
                              "summary": {"objective": "<script>alert(1)</script>"}}))
    create_via_post(client)
    page = client.get("/sessions/leave-approval").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


# ── #51: an authority the parser cannot determine a host from ─────────────────

UNDETERMINABLE = [
    ("evil.com@127.0.0.1", "userinfo: the host is the part after the @, and no browser sends one"),
    ("http://evil.com@127.0.0.1", "the same, spelled as an origin URL"),
    ("user:pass@127.0.0.1", "userinfo with a password"),
    ("@127.0.0.1", "empty userinfo — still an authority a browser never produces"),
    ("127.0.0.1 evil.com", "whitespace inside the authority: two things, not one host"),
    ("127.0.0.1@", "userinfo with nothing after it"),
    ("[::1", "an unbalanced bracket"),
    ("http:///", "a well-formed URL naming nobody"),
    ("", "nothing at all"),
    ("   ", "whitespace only"),
]
DETERMINABLE = [
    ("127.0.0.1:8765", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"), ("localhost", "localhost"),
    ("LOCALHOST:8765", "localhost"), ("[::1]:8765", "::1"), ("http://localhost:3000", "localhost"),
    ("https://requivo.example.test", "requivo.example.test"),
    ("null", "null"),           # parsed, and refused later by name — the opaque arm
]


@pytest.mark.parametrize("value, why", UNDETERMINABLE)
def test_an_authority_that_is_not_a_host_is_refused_by_the_parser(value, why):
    assert _hostname(value) == "", why


@pytest.mark.parametrize("value, expected", DETERMINABLE)
def test_an_ordinary_authority_still_parses(value, expected):
    """Must fire. Returning `""` for everything satisfies every assertion above."""
    assert _hostname(value) == expected


def test_userinfo_no_longer_smuggles_a_loopback_host_past_the_allowlist():
    """The measured instance (#51), stated as the two properties it violated."""
    assert _hostname("evil.com@127.0.0.1") not in allowed_hosts()
    assert _hostname("127.0.0.1") in allowed_hosts()          # must fire
    assert not _same_trust_domain(_hostname("http://evil.com@127.0.0.1"), "127.0.0.1")
    assert _same_trust_domain(_hostname("http://127.0.0.1:3000"), "127.0.0.1")   # must fire


@pytest.mark.parametrize("listed, host", [(None, "127.0.0.1"), ("requivo.example.test", "requivo.example.test")],
                         ids=["loopback", "operator-listed"])
def test_the_guard_refuses_a_host_header_carrying_userinfo(app, monkeypatch, listed, host):
    """End to end, at the header rather than at the helper; a deliberate non-loopback bind does not widen it."""
    if listed:
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, listed)
    port = "" if listed else ":8765"
    c = TestClient(app, base_url=f"http://{host}{port}", raise_server_exceptions=False)
    assert c.get("/", headers={"Host": f"evil.com@{host}{port}"}).status_code == 403
    assert c.get("/", headers={"Host": f"{host}{port}"}).status_code == 200      # must fire


def test_the_guard_refuses_an_origin_carrying_userinfo(client):
    r = client.post("/sessions", data={**_CREATE, "request_text": "A leave approval system."},
                    headers={"Origin": "http://evil.com@127.0.0.1:8765"})
    assert r.status_code == 403


def test_a_host_we_could_not_read_is_a_different_arm_from_one_we_read_and_refused(app):
    """A guard that could not read its input must not print what one that read it and refused prints (#43, #45, #52)."""
    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    unreadable = c.get("/", headers={"Host": "evil.com@127.0.0.1:8765"})
    refused = c.get("/", headers={"Host": "example.test"})
    assert unreadable.status_code == refused.status_code == 403
    assert "undetermined_host" in unreadable.text and "host_not_allowed" in refused.text
    assert "evil.com@127.0.0.1:8765" not in unreadable.text     # the input is not reflected back


# ── #52: one code, one fact, one details shape ────────────────────────────────

# The six arms of `_enforce`, each with its code and the exact `details` keys that code guarantees.
ARMS = [
    ("undetermined_host", {"host_header_present", "host_header", "hint"}),
    ("host_not_allowed", {"host", "hint"}),
    ("cross_site_fetch", {"sec_fetch_site"}),
    ("opaque_origin", {"origin", "host"}),
    ("origin_mismatch", {"origin", "host"}),
    ("missing_request_token", set()),
]
_ARM_CLASSES = {c.code: c for c in CrossSiteRequestError.__subclasses__()}


def test_every_arm_has_its_own_code_and_the_family_base_is_raised_by_none():
    """Before #52 all six raised `cross_site_request`; the family survives for a caller catching any refusal."""
    codes = [code for code, _ in ARMS]
    assert set(_ARM_CLASSES) == set(codes) and len(set(codes)) == len(codes)
    assert CrossSiteRequestError.code == "cross_site_request" and "cross_site_request" not in _ARM_CLASSES
    for code in codes + ["cross_site_request"]:
        assert STATUS_BY_CODE[code] == 403, code


@pytest.mark.parametrize("code, keys", ARMS)
def test_each_arm_carries_exactly_the_details_shape_its_code_promises(code, keys, raw_client, monkeypatch):
    """The point of the split, driven through the real middleware rather than by constructing the exception."""
    raised = {}
    real = security._enforce

    async def capture(request):
        try:
            await real(request)
        except security.CrossSiteRequestError as exc:
            raised["exc"] = exc
            raise

    monkeypatch.setattr(security, "_enforce", capture)
    headers, method = {
        "undetermined_host": ({"Host": "evil.com@127.0.0.1"}, "get"),
        "host_not_allowed": ({"Host": "example.test"}, "get"),
        "cross_site_fetch": ({CSRF_HEADER: csrf_token(), "Sec-Fetch-Site": "cross-site"}, "post"),
        "opaque_origin": ({CSRF_HEADER: csrf_token(), "Origin": "null"}, "post"),
        "origin_mismatch": ({CSRF_HEADER: csrf_token(), "Origin": "http://example.test"}, "post"),
        "missing_request_token": ({}, "post"),
    }[code]
    if method == "get":
        raw_client.get("/", headers=headers)
    else:
        raw_client.post("/sessions", data=_CREATE, headers=headers)
    exc = raised.get("exc")
    assert exc is not None, f"no cross-site refusal was raised for {code}"
    assert exc.code == code
    assert set(exc.details or {}) == keys, f"{code} carried {sorted(exc.details or {})}"


def test_a_legitimate_post_raises_no_arm_at_all(client):
    """Must fire. Six arms that all trigger unconditionally would satisfy every assertion above."""
    r = client.post("/sessions", data={**_CREATE, "request_text": "A leave approval system."},
                    headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303


# ── `web/config.py`'s provider probe (#332) ───────────────────────────────────


@pytest.mark.parametrize("variable, present", [(None, False), ("ANTHROPIC_AUTH_TOKEN", True), ("ANTHROPIC_API_KEY", True)],
                         ids=["nothing", "bearer-token-alone", "api-key-alone"])
def test_the_provider_probe_reads_either_credential_name(monkeypatch, variable, present):
    """#332 measured `ANTHROPIC_AUTH_TOKEN` alone read as absent; the remedy names every name the probe reads."""
    if variable:
        monkeypatch.setenv(variable, "sk-ant-whatever")
    status = provider_status()
    assert status.key_present is present
    if not present:
        assert status.available is False
        assert "ANTHROPIC_API_KEY" in status.reason and "ANTHROPIC_AUTH_TOKEN" in status.reason
