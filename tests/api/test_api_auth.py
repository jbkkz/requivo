"""The bearer-token bind discipline (#425 slice 4, `docs/decisions/0004-the-http-api-facade.md` §5 steps 1 and
2; `api/auth.py`, wired by `api/app.py`'s `bearer_guard`)."""

from __future__ import annotations

import hmac

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
from tests.api.conftest import seed_session

TOKEN = "s3cret-token-for-tests"


@pytest.fixture
def locked():
    """A client against an app built with a token -- the state every gated route is tested in."""
    return TestClient(create_api(token=TOKEN), base_url="http://127.0.0.1:8767",
                      raise_server_exceptions=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── the refuse-to-start rule ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "::", "app.internal", "127.0.0.2"])
def test_a_bind_beyond_loopback_with_no_token_refuses_to_build_the_app(monkeypatch, host):
    """Must-fire: no token anywhere (explicit or env), a non-loopback bind -> `api_token_required`, and no app
    comes back."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    with pytest.raises(ApiTokenRequiredError) as exc:
        create_api(bind_host=host)
    assert exc.value.code == "api_token_required"
    assert exc.value.details == {"host": host, "env": API_TOKEN_ENV}
    assert API_TOKEN_ENV in str(exc.value)          # the remedy is named, not hinted at


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_needs_no_token(monkeypatch, host):
    """The must-not-fire control on the bind axis: §5 step 1, CLI parity."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    app = create_api(bind_host=host)
    assert app is not None


def test_a_bind_beyond_loopback_builds_once_the_token_is_set_in_the_environment(monkeypatch):
    """The must-not-fire control on the token axis, through the env var the operator actually sets."""
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    assert create_api(bind_host="192.168.1.50") is not None


def test_a_blank_token_is_not_a_token(monkeypatch):
    """`REQUIVO_API_TOKEN=` (empty) or whitespace is an operator who has not set one."""
    for blank in ("", "   "):
        monkeypatch.setenv(API_TOKEN_ENV, blank)
        assert resolve_token() is None
        with pytest.raises(ApiTokenRequiredError):
            create_api(bind_host="192.168.1.50")
    assert resolve_token("  ") is None
    assert resolve_token("real") == "real"           # must fire: a real value survives untouched


def test_the_bind_rule_is_one_function_and_it_holds_for_every_input():
    """`require_token_for_bind` directly, since it is the definition the factory and any other launcher share:
    refuses exactly when both conditions hold, never on one alone."""
    with pytest.raises(ApiTokenRequiredError):
        require_token_for_bind("0.0.0.0", None)
    require_token_for_bind("0.0.0.0", TOKEN)         # token set: fine
    require_token_for_bind("127.0.0.1", None)        # loopback: fine
    require_token_for_bind("127.0.0.1", TOKEN)       # both: fine


def test_no_bind_host_means_no_bind_check(monkeypatch):
    """The stated gap: a caller that does not say what it will bind is not checked."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    assert create_api() is not None


# ── the bearer check on the wire ─────────────────────────────────────────────────────────────────

def test_a_gated_route_without_a_token_is_refused_with_401(locked):
    """Must-fire: no `Authorization` at all -> 401 `unauthorized`, reason `missing`, and a `WWW-Authenticate:
    Bearer` challenge naming what to send (RFC 6750)."""
    resp = locked.get("/api/v1/sessions")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "unauthorized"
    assert body["details"] == {"scheme": "Bearer", "reason": "missing"}
    assert resp.headers["www-authenticate"].startswith("Bearer ")
    assert "invalid_token" not in resp.headers["www-authenticate"]
    assert TOKEN not in resp.text                      # a secret is never a diagnostic
    # The refusal still leaves through the header middleware.
    assert "content-security-policy" in resp.headers


def test_a_gated_route_with_the_wrong_token_is_refused_with_401(locked):
    resp = locked.get("/api/v1/sessions", headers=_bearer("not-the-token"))
    assert resp.status_code == 401
    assert resp.json()["details"] == {"scheme": "Bearer", "reason": "invalid"}
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
    assert TOKEN not in resp.text


def test_a_different_scheme_reads_as_missing_not_invalid(locked):
    """`Basic ...` presented no bearer credential, so the challenge says what to send rather than claiming a
    bearer token was wrong."""
    resp = locked.get("/api/v1/sessions", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401
    assert resp.json()["details"]["reason"] == "missing"


def test_the_right_token_is_accepted(locked):
    """The must-not-fire control: the guard discriminates."""
    assert locked.get("/api/v1/sessions", headers=_bearer(TOKEN)).status_code == 200
    resp = locked.post("/api/v1/sessions", json={"request": "A leave approval system."},
                       headers=_bearer(TOKEN))
    assert resp.status_code == 201


@pytest.mark.parametrize("spelling", ["bearer", "BEARER", "Bearer"])
def test_the_scheme_is_matched_case_insensitively(locked, spelling):
    """RFC 7235: an auth-scheme is case-insensitive."""
    resp = locked.get("/api/v1/sessions", headers={"Authorization": f"{spelling} {TOKEN}"})
    assert resp.status_code == 200


def test_every_route_under_the_prefix_is_gated_and_the_probe_is_not(locked):
    """The gate is by prefix, so a route added later is gated without anyone remembering to say so."""
    slug = seed_session("leave-approval")
    for path in (f"/api/v1/sessions/{slug}", f"/api/v1/sessions/{slug}/model",
                 f"/api/v1/sessions/{slug}/status", f"/api/v1/sessions/{slug}/artifacts"):
        assert locked.get(path).status_code == 401, path
        assert locked.get(path, headers=_bearer(TOKEN)).status_code == 200, path
    assert locked.get("/api/v1/health").status_code == 200
    assert requires_token("/api/v1/sessions") is True
    assert requires_token("/api/v1/health") is False
    assert requires_token("/api/v1/healthz") is True    # by name, not by prefix of the name


def test_the_docs_and_the_spec_stay_reachable_without_the_token(locked):
    """A browser navigation cannot carry a bearer header, and these serve the route skeleton and this
    package's own vendored assets, never session data -- `api/app.py`'s exemption comment."""
    for path in ("/docs", "/redoc", "/openapi.json", "/api-static/favicon.svg"):
        assert locked.get(path).status_code == 200, path
    assert requires_token("/docs") is False


def test_with_no_token_configured_nothing_is_gated(client):
    """§5 step 1: the loopback, CLI-parity mode."""
    assert client.get("/api/v1/sessions").status_code == 200
    resp = client.post("/api/v1/sessions", json={"request": "A leave approval system."})
    assert resp.status_code == 201


def test_a_token_this_server_cannot_decode_is_refused_rather_than_crashing(locked):
    """#212's shape, closed before it opens here: a header carrying bytes outside ASCII reaches a 401, never a
    `TypeError` and never the 500 handler."""
    resp = locked.get("/api/v1/sessions",
                      headers={b"authorization": "Bearer café-token".encode()})
    assert resp.status_code == 401
    assert resp.json()["details"]["reason"] == "invalid"


def test_a_non_ascii_token_can_still_be_matched():
    """The other half of the bytes decision: the configured token is compared as its UTF-8 bytes."""
    token = "café-☃"
    app = create_api(token=token)
    client = TestClient(app, base_url="http://127.0.0.1:8767", raise_server_exceptions=False)
    raw = b"Bearer " + token.encode("utf-8")
    resp = client.get("/api/v1/sessions", headers={b"authorization": raw})
    assert resp.status_code == 200
    # and the must-fire control on the same app: the ASCII near-miss is refused
    assert client.get("/api/v1/sessions", headers=_bearer("cafe-snowman")).status_code == 401


def test_the_host_guard_runs_before_the_bearer_guard(locked):
    """Ordering: a rebound host is told 403 `host_not_allowed`, not 401."""
    resp = locked.get("/api/v1/sessions", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "host_not_allowed"


def test_a_missing_token_is_refused_before_the_cross_site_checks(locked):
    """Ordering, the other side: a cross-site, wrongly-typed, unauthenticated write is told 401 and nothing
    else -- whether it may speak at all is settled before the shape of what it said."""
    hostile = {"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}
    resp = locked.post("/api/v1/sessions", content=b"{}", headers=hostile)
    assert resp.status_code == 401
    resp = locked.post("/api/v1/sessions", content=b"{}", headers={**hostile, **_bearer(TOKEN)})
    assert resp.status_code == 403
    assert resp.json()["code"] == "cross_site_fetch"


# ── the comparison itself ────────────────────────────────────────────────────────────────────────

def test_the_token_comparison_is_constant_time_by_construction(monkeypatch):
    """Asserts *which function decides*, not a timing measurement."""
    seen = []
    real = hmac.compare_digest       # captured first: `auth.hmac` *is* the stdlib module

    def probe(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", probe)
    check_bearer(b"Bearer " + TOKEN.encode(), TOKEN)
    assert seen == [(TOKEN.encode(), TOKEN.encode())]
    assert all(isinstance(x, bytes) for pair in seen for x in pair)

    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: False)
    with pytest.raises(UnauthorizedError) as exc:
        check_bearer(b"Bearer " + TOKEN.encode(), TOKEN)
    assert exc.value.details["reason"] == "invalid"


@pytest.mark.parametrize("raw", [b"Bearer", b"Bearer ", b"  bearer   "])
def test_a_bearer_scheme_with_no_credential_is_missing_not_invalid(raw):
    """The docstring's contract, pinned: `missing` is "no bearer credential was presented at all", and a bare
    `Bearer` with nothing after it presented none."""
    with pytest.raises(UnauthorizedError) as exc:
        check_bearer(raw, TOKEN)
    assert exc.value.details["reason"] == "missing"
    assert "invalid_token" not in exc.value.challenge


def test_the_comparison_never_sees_a_str():
    """The #212 guarantee stated as a property: whatever the header bytes."""
    for raw in (bytes(range(256)), b"Bearer " + bytes(range(128, 256)), b"Bearer \xe9",
                b"", b"Bearer", b"Bearer ", b"   Bearer   " + TOKEN.encode() + b"   "):
        try:
            check_bearer(raw, TOKEN)
            assert raw.strip().lower().endswith(TOKEN.encode()), raw   # only the padded real token passes
        except UnauthorizedError as exc:
            assert exc.details["reason"] in ("missing", "invalid")
