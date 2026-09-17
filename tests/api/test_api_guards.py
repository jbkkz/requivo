"""The API's request guards (#425 slice 4, `docs/decisions/0004-the-http-api-facade.md` §5), in the order they
run: host allowlist, bearer token, cross-site origin, content type; then the path parameters and the
self-hosted docs. Ordering is pinned by the three tests `api/app.py` names."""

from __future__ import annotations

import hmac
import re

import pytest
from fastapi.testclient import TestClient

from requivo.api import auth
from requivo.api.app import create_api, requires_token
from requivo.api.auth import (
    API_TOKEN_ENV,
    ApiTokenRequiredError,
    UnauthorizedError,
    check_bearer,
    require_token_for_bind,
    resolve_token,
)
from requivo.host_policy import CrossSiteFetchError, OpaqueOriginError, OriginMismatchError, check_request_origin
from tests.api.conftest import BODY, JSON, seed_session

TOKEN = "s3cret-token-for-tests"
CREATE = "/api/v1/sessions"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _refused(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code
    assert "content-security-policy" in resp.headers   # a refusal still leaves through the header middleware


@pytest.fixture
def locked():
    """A client against an app built with a token: the state every gated route is tested in."""
    return TestClient(create_api(token=TOKEN), base_url="http://127.0.0.1:8767", raise_server_exceptions=False)


# ── the refuse-to-start rule ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "::", "app.internal", "127.0.0.2"])
def test_a_bind_beyond_loopback_with_no_token_refuses_to_build_the_app(monkeypatch, host):
    """Must-fire: no token anywhere, a non-loopback bind -> `api_token_required`, the remedy named."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    with pytest.raises(ApiTokenRequiredError) as exc:
        create_api(bind_host=host)
    assert exc.value.code == "api_token_required"
    assert exc.value.details == {"host": host, "env": API_TOKEN_ENV} and API_TOKEN_ENV in str(exc.value)


@pytest.mark.parametrize(("host", "token"), [
    ("127.0.0.1", None), ("localhost", None), ("::1", None),   # loopback needs no token: §5 step 1, CLI parity
    ("192.168.1.50", TOKEN),                                    # beyond loopback, the env var the operator sets
    (None, None),                                               # a caller that names no bind is not checked
])
def test_the_app_builds_when_the_bind_rule_is_not_both_conditions_at_once(monkeypatch, host, token):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    if token:
        monkeypatch.setenv(API_TOKEN_ENV, token)
    assert create_api(bind_host=host) is not None


def test_a_blank_token_is_not_a_token(monkeypatch):
    """`REQUIVO_API_TOKEN=` (empty) or whitespace is an operator who has not set one."""
    for blank in ("", "   "):
        monkeypatch.setenv(API_TOKEN_ENV, blank)
        assert resolve_token() is None
        with pytest.raises(ApiTokenRequiredError):
            create_api(bind_host="192.168.1.50")
    assert resolve_token("  ") is None and resolve_token("real") == "real"


def test_the_bind_rule_is_one_function_and_it_holds_for_every_input():
    """`require_token_for_bind` refuses exactly when both conditions hold, never on one alone."""
    with pytest.raises(ApiTokenRequiredError):
        require_token_for_bind("0.0.0.0", None)
    for host, token in (("0.0.0.0", TOKEN), ("127.0.0.1", None), ("127.0.0.1", TOKEN)):
        require_token_for_bind(host, token)


# ── the bearer check on the wire ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("headers", "reason"), [
    ({}, "missing"),                                            # must-fire: no `Authorization` at all
    ({"Authorization": "Basic dXNlcjpwYXNz"}, "missing"),        # another scheme presented no bearer credential
    (_bearer("not-the-token"), "invalid"),
    ({b"authorization": "Bearer café-token".encode()}, "invalid"),   # #212's shape: bytes outside ASCII, never a 500
], ids=["none", "basic", "wrong", "undecodable"])
def test_a_gated_route_is_refused_with_401_and_a_bearer_challenge(locked, headers, reason):
    """401 `unauthorized`, the reason named, a `WWW-Authenticate: Bearer` challenge (RFC 6750), no secret echoed."""
    resp = locked.get(CREATE, headers=headers)
    _refused(resp, 401, "unauthorized")
    assert resp.json()["details"] == {"scheme": "Bearer", "reason": reason}
    assert resp.headers["www-authenticate"].startswith("Bearer ")
    assert ('error="invalid_token"' in resp.headers["www-authenticate"]) == (reason == "invalid")
    assert TOKEN not in resp.text


@pytest.mark.parametrize("spelling", ["Bearer", "bearer", "BEARER"])
def test_the_right_token_is_accepted_whatever_the_schemes_case(locked, spelling):
    """The must-not-fire control: the guard discriminates, and RFC 7235's auth-scheme is case-insensitive."""
    assert locked.get(CREATE, headers={"Authorization": f"{spelling} {TOKEN}"}).status_code == 200
    resp = locked.post(CREATE, json={"request": "A leave approval system."}, headers=_bearer(TOKEN))
    assert resp.status_code == 201


def test_every_route_under_the_prefix_is_gated_and_the_probe_and_docs_are_not(locked):
    """The gate is by prefix, so a later route is gated without anyone saying so; `/docs`, `/redoc`, the spec
    and this package's own assets stay reachable, since a browser navigation cannot carry a bearer header."""
    slug = seed_session("leave-approval")
    for path in (f"{CREATE}/{slug}", f"{CREATE}/{slug}/model", f"{CREATE}/{slug}/status", f"{CREATE}/{slug}/artifacts"):
        assert locked.get(path).status_code == 401, path
        assert locked.get(path, headers=_bearer(TOKEN)).status_code == 200, path
    for path in ("/api/v1/health", "/docs", "/redoc", "/openapi.json", "/api-static/favicon.svg"):
        assert locked.get(path).status_code == 200, path
    assert requires_token(CREATE) is True and requires_token("/docs") is False
    assert requires_token("/api/v1/health") is False
    assert requires_token("/api/v1/healthz") is True    # by name, not by prefix of the name


def test_with_no_token_configured_nothing_is_gated(client):
    """§5 step 1: the loopback, CLI-parity mode."""
    assert client.get(CREATE).status_code == 200
    assert client.post(CREATE, json={"request": "A leave approval system."}).status_code == 201


def test_a_non_ascii_token_can_still_be_matched():
    """The configured token is compared as its UTF-8 bytes; the ASCII near-miss is the must-fire control."""
    token = "café-☃"
    client = TestClient(create_api(token=token), base_url="http://127.0.0.1:8767", raise_server_exceptions=False)
    assert client.get(CREATE, headers={b"authorization": b"Bearer " + token.encode("utf-8")}).status_code == 200
    assert client.get(CREATE, headers=_bearer("cafe-snowman")).status_code == 401


def test_a_missing_token_is_refused_before_the_cross_site_checks(locked):
    """Ordering: whether a caller may speak at all is settled before the shape of what it said."""
    hostile = {"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}
    assert locked.post(CREATE, content=b"{}", headers=hostile).status_code == 401
    _refused(locked.post(CREATE, content=b"{}", headers={**hostile, **_bearer(TOKEN)}), 403, "cross_site_fetch")


# ── the comparison itself ────────────────────────────────────────────────────────────────────────


def test_the_token_comparison_is_constant_time_by_construction(monkeypatch):
    """Asserts *which function decides*, on bytes, not a timing measurement."""
    seen = []
    real = hmac.compare_digest       # captured first: `auth.hmac` *is* the stdlib module
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: (seen.append((a, b)), real(a, b))[1])
    check_bearer(b"Bearer " + TOKEN.encode(), TOKEN)
    assert seen == [(TOKEN.encode(), TOKEN.encode())]
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: False)
    with pytest.raises(UnauthorizedError) as exc:
        check_bearer(b"Bearer " + TOKEN.encode(), TOKEN)
    assert exc.value.details["reason"] == "invalid"


@pytest.mark.parametrize("raw", [b"Bearer", b"Bearer ", b"  bearer   "])
def test_a_bearer_scheme_with_no_credential_is_missing_not_invalid(raw):
    """`missing` is "no bearer credential was presented at all", and a bare `Bearer` presented none."""
    with pytest.raises(UnauthorizedError) as exc:
        check_bearer(raw, TOKEN)
    assert exc.value.details["reason"] == "missing" and "invalid_token" not in exc.value.challenge


def test_the_comparison_never_sees_a_str():
    """The #212 guarantee as a property: whatever the header bytes, a verdict and never a `TypeError`."""
    for raw in (bytes(range(256)), b"Bearer " + bytes(range(128, 256)), b"Bearer \xe9",
                b"", b"Bearer", b"Bearer ", b"   Bearer   " + TOKEN.encode() + b"   "):
        try:
            check_bearer(raw, TOKEN)
            assert raw.strip().lower().endswith(TOKEN.encode()), raw   # only the padded real token passes
        except UnauthorizedError as exc:
            assert exc.details["reason"] in ("missing", "invalid")


# ── the cross-site checks on unsafe methods ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("headers", "code", "details"), [
    ({"Sec-Fetch-Site": "cross-site"}, "cross_site_fetch", {"sec_fetch_site": "cross-site"}),   # must-fire
    ({"Sec-Fetch-Site": "same-site"}, "cross_site_fetch", {"sec_fetch_site": "same-site"}),
    ({"Origin": "null"}, "opaque_origin", {"origin": "null", "host": "127.0.0.1"}),             # #43
    ({"Origin": "http://evil.example.com"}, "origin_mismatch", {"origin": "evil.example.com", "host": "127.0.0.1"}),
    ({"Referer": "http://evil.example.com/page"}, "origin_mismatch", None),                     # no Origin stated
], ids=["cross-site", "same-site", "opaque", "foreign-origin", "foreign-referer"])
def test_a_request_attributed_elsewhere_is_refused(raw_client, headers, code, details):
    resp = raw_client.post(CREATE, content=BODY, headers={**JSON, **headers})
    _refused(resp, 403, code)
    if details is not None:
        assert resp.json()["details"] == details


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
    {"Origin": "http://127.0.0.1:8767"}, {"Origin": "http://localhost:3000"}, {"Origin": "http://[::1]"},   # #43's own case
    {},   # no origin at all is a scripted client: a browser attaches `Origin` to every POST
], ids=["same-origin", "user-initiated", "loopback", "localhost-port", "ipv6", "scripted"])
def test_a_request_attributed_here_or_not_at_all_is_accepted(raw_client, headers):
    """The must-not-fire control on both headers."""
    assert raw_client.post(CREATE, content=BODY, headers={**JSON, **headers}).status_code == 201


def test_a_read_is_never_attributed(raw_client):
    """The method axis: the checks run on unsafe methods only; the host allowlist guards reads."""
    resp = raw_client.get(CREATE, headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example.com"})
    assert resp.status_code == 200


def test_the_checks_cover_every_unsafe_method(raw_client):
    """`PUT` as well as `POST`, on a real route: the guard reads `_UNSAFE_METHODS`, not one verb."""
    slug = seed_session("leave-approval")
    resp = raw_client.put(f"{CREATE}/{slug}/context-cards", content=b'{"context_cards": []}',
                          headers={**JSON, "Sec-Fetch-Site": "cross-site"})
    _refused(resp, 403, "cross_site_fetch")


def test_a_cross_site_request_is_refused_before_the_content_type_check(raw_client):
    """Ordering: a cross-site page that also sent the wrong content type is told 403, not 415."""
    resp = raw_client.post(CREATE, content=BODY, headers={"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"})
    _refused(resp, 403, "cross_site_fetch")


def test_check_request_origin_reaches_a_verdict_for_every_input():
    """The framework-free function both surfaces call."""
    check_request_origin("127.0.0.1", sec_fetch_site=None, origin=None, referer=None)
    check_request_origin("127.0.0.1", sec_fetch_site="same-origin", origin="http://localhost", referer=None)
    for error, kwargs in ((CrossSiteFetchError, dict(sec_fetch_site="cross-site", origin=None, referer=None)),
                          (OpaqueOriginError, dict(sec_fetch_site=None, origin=" NULL ", referer=None)),
                          (OriginMismatchError, dict(sec_fetch_site=None, origin="http:///", referer=None)),
                          (OriginMismatchError, dict(sec_fetch_site=None, origin=None, referer="http://evil.example.com")),
                          # a whitespace-only Origin does not fall through to a clean Referer
                          (OriginMismatchError, dict(sec_fetch_site=None, origin="   ", referer="http://127.0.0.1"))):
        with pytest.raises(error):
            check_request_origin("127.0.0.1", **kwargs)


def test_the_web_surface_calls_the_same_definition():
    """`web/security.py` re-exports these names from `host_policy`: identity, not equality."""
    from requivo.web import security
    assert (security.CrossSiteFetchError, security.OpaqueOriginError, security.OriginMismatchError,
            security.check_request_origin) == (CrossSiteFetchError, OpaqueOriginError, OriginMismatchError,
                                               check_request_origin)


# ── the content type on unsafe methods ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "headers"), [
    (CREATE, {}),                                   # must-fire: no `Content-Type` at all
    (CREATE, {"Content-Type": "text/plain"}),
    ("/discover", {}),                              # a bodyless unsafe method still needs the header
], ids=["none", "wrong", "bodyless"])
def test_an_unsafe_method_without_the_json_content_type_is_refused(raw_client, path, headers):
    if path == "/discover":
        path = f"{CREATE}/{seed_session('leave-approval')}/discover"
    _refused(raw_client.post(path, content=BODY, headers=headers), 415, "unsupported_content_type")


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8"])
def test_the_json_content_type_is_accepted_with_or_without_a_charset(raw_client, content_type):
    """The must-not-fire control: the header the guard asks for, and a real client's charset parameter."""
    assert raw_client.post(CREATE, content=BODY, headers={"Content-Type": content_type}).status_code == 201


def test_a_get_needs_no_content_type(raw_client):
    assert raw_client.get(CREATE).status_code == 200


# ── the host guard runs first of all ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "headers"), [
    ("post", {}),                                                   # before the content-type check
    ("post", {**JSON, "Origin": "http://evil.example.com"}),         # before the origin checks
    ("get", {}),                                                    # before the bearer guard (a locked app)
], ids=["content-type", "origin", "bearer"])
def test_a_rebound_host_is_refused_before_the_content_type_check(raw_client, locked, method, headers):
    """§5's ordering constraint: a host the allowlist rejects is told 403 `host_not_allowed` and nothing else."""
    headers = {**headers, "Host": "evil.example.com"}
    resp = locked.get(CREATE, headers=headers) if method == "get" else raw_client.post(CREATE, content=BODY, headers=headers)
    _refused(resp, 403, "host_not_allowed")


# ── path traversal via the API's own path parameters ─────────────────────────────────────────────

# Encoded as well as literal: Starlette normalises some and refuses others with its own 404, which is fine --
# the assertion is that none of them *succeeds*, not that they all fail the same way.
_SLUG_ATTEMPTS = ["../etc", "../../etc/passwd", "%2e%2e%2fetc", "%2e%2e%252fetc", "..%5cwindows", "/etc/passwd",
                  "leave-approval/../../../etc", "....//etc", "C:\\Windows", "leave%2Dapproval%00"]
_TYPE_ATTEMPTS = ["../../../etc/passwd", "..%2f..%2fsession.json", "prd/../../../session.json",
                  "session", "model", ".lock"]   # the last three: real files in the session dir, not artifact types


def test_no_slug_shaped_traversal_reaches_the_filesystem(client):
    """Every route taking `{slug}` refuses a traversal attempt rather than resolving it."""
    seed_session("leave-approval")
    for attempt in _SLUG_ATTEMPTS:
        for route in ("", "/model", "/status", "/revisions", "/artifacts"):
            resp = client.get(f"{CREATE}/{attempt}{route}")
            assert 400 <= resp.status_code < 500, f"{attempt!r}{route} returned {resp.status_code}, not a refusal"
            assert "root:" not in resp.text and "/etc/passwd" not in resp.text, f"{attempt!r}{route} leaked: {resp.text[:200]}"


def test_no_artifact_type_traversal_reaches_the_filesystem(client):
    """`{artifact_type}` is a key into a closed set, never a path component."""
    slug = seed_session("leave-approval")
    for attempt in _TYPE_ATTEMPTS:
        resp = client.get(f"{CREATE}/{slug}/artifacts/{attempt}")
        assert 400 <= resp.status_code < 500, f"type {attempt!r} returned {resp.status_code}, not a refusal"
        assert "format_version" not in resp.text and "current_revision" not in resp.text, f"{attempt!r} served a session file"


def test_the_refusal_is_the_slug_guard_and_not_merely_a_missing_session(client):
    """The must-fire half, and the reason the test above is not enough on its own."""
    resp = client.get(f"{CREATE}/..%2f..%2fetc/model")
    assert resp.status_code in (400, 404)
    if resp.status_code == 400:
        assert resp.json()["code"] == "invalid_slug", resp.json()
    resp = client.get(f"{CREATE}/Not_A_Slug/model")   # plainly invalid, never a routing artefact: the guard's own 400
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_slug", resp.text


def test_a_bare_dot_segment_never_reaches_the_slug_parameter_at_all(client):
    """The one shape that is not the guard's job, recorded so nobody reads the client's own resolution as a hole."""
    resp = client.get(f"{CREATE}/.")
    assert resp.request.url.path == CREATE and resp.status_code == 200 and "sessions" in resp.json()
    resp = client.get(f"{CREATE}/..")
    assert resp.request.url.path == "/api/v1" and resp.status_code == 404


# ── `/docs` and `/redoc` self-host every asset they need (#504) ─────────────────────────────────


def test_no_external_origin_appears_in_the_served_docs_html(client):
    """No third-party origin loads into the origin `GET /api/v1/sessions` answers with no credential, and
    every referenced asset resolves from this app (the favicon and fonts are #504's own named risk)."""
    referenced = set()
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} did not render"
        assert not re.search(r"https?://", r.text), f"{path} references an external origin:\n{r.text}"
        referenced.update(re.findall(r'(?:src|href)="([^"]+)"', r.text))
    assert referenced, "no asset references found -- this test would pass on an empty page too"
    for ref in referenced:
        assert ref.startswith("/"), f"{ref} is not a same-origin reference"
        assert client.get(ref).status_code == 200, f"{ref} is referenced but does not resolve"


def test_the_swagger_ui_page_does_not_phone_home_to_the_spec_validator(client):
    """Unset, Swagger UI POSTs the loaded spec to `https://validator.swagger.io/validator` for a badge."""
    r = client.get("/api-static/swagger-initializer.js")
    assert r.status_code == 200 and "validatorUrl: null" in r.text
    assert re.search(r'url:\s*"https?://', r.text) is None and 'url: "/openapi.json"' in r.text


def test_the_docs_pages_run_under_the_strict_csp_with_no_exception_carved_for_them(client):
    """#503's `script-src 'self'` and #504's self-hosted assets land together; `redoc.standalone.js` tries
    `https://cdn.redoc.ly/redoc/logo-mini.svg` on every mount and `img-src` still carves nothing for it."""
    for path in ("/docs", "/redoc"):
        csp = client.get(path).headers["Content-Security-Policy"]
        script_src = csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "script-src 'self'" in csp and "'unsafe-inline'" not in script_src
        img_src = csp.split("img-src", 1)[1].split(";", 1)[0]
        assert img_src.strip() == "'self' data:", f"{path}: img-src widened to {img_src!r}"
