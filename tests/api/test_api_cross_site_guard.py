"""The `Sec-Fetch-Site`/`Origin` checks on the API's unsafe methods (#425 slice 4,
`docs/decisions/0004-the-http-api-facade.md` §5) -- `requivo.host_policy.check_request_origin`, the
one definition `web/security.py`'s `_enforce` also calls, wired by `api/app.py`'s `cross_site_guard`.

Every rule in both directions, on `raw_client` (nothing added on the caller's behalf) so each
header really is the test's own. The web surface keeps its own tests of the same checks
(`tests/web/test_web_security.py`); these prove the API answers identically, and the unit tests at
the foot prove the shared function's third state directly.
"""

from __future__ import annotations

import pytest

from requivo.host_policy import CrossSiteFetchError, OpaqueOriginError, OriginMismatchError, check_request_origin
from tests.api.conftest import seed_session

JSON = {"Content-Type": "application/json"}
BODY = b'{"request": "A leave approval system."}'


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_cross_site_fetch_is_refused(raw_client, site):
    """Must-fire: the browser's own account of the request's origin says elsewhere -> 403
    `cross_site_fetch`, whatever else the request carries."""
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={**JSON, "Sec-Fetch-Site": site})
    assert resp.status_code == 403
    assert resp.json()["code"] == "cross_site_fetch"
    assert resp.json()["details"] == {"sec_fetch_site": site}
    assert "content-security-policy" in resp.headers     # the refusal keeps the header policy


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_a_same_origin_or_user_initiated_fetch_is_accepted(raw_client, site):
    """The must-not-fire control on the same header: the two values a legitimate caller sends."""
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={**JSON, "Sec-Fetch-Site": site})
    assert resp.status_code == 201


def test_an_opaque_origin_is_refused(raw_client):
    """`Origin: null` -- a browser speaking and declining to attribute itself (#43)."""
    resp = raw_client.post("/api/v1/sessions", content=BODY, headers={**JSON, "Origin": "null"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "opaque_origin"
    assert resp.json()["details"] == {"origin": "null", "host": "127.0.0.1"}


def test_a_foreign_origin_is_refused(raw_client):
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={**JSON, "Origin": "http://evil.example.com"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "origin_mismatch"
    assert resp.json()["details"] == {"origin": "evil.example.com", "host": "127.0.0.1"}


def test_a_foreign_referer_is_refused_when_no_origin_is_stated(raw_client):
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={**JSON, "Referer": "http://evil.example.com/page"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "origin_mismatch"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8767", "http://localhost:3000", "http://[::1]"])
def test_a_loopback_origin_is_accepted_whatever_its_spelling_or_port(raw_client, origin):
    """The must-not-fire control on the origin axis, and #43's own case: the three loopback
    spellings are one trust domain, and the port is deliberately not compared
    (`same_trust_domain`'s docstring carries the decision)."""
    resp = raw_client.post("/api/v1/sessions", content=BODY, headers={**JSON, "Origin": origin})
    assert resp.status_code == 201


def test_no_origin_at_all_is_a_scripted_client_and_is_accepted(raw_client):
    """The asymmetry `check_request_origin` states: a browser attaches `Origin` to every POST, so
    none at all means no browser is speaking -- a supported caller, gated by the content-type floor
    rather than by attribution."""
    resp = raw_client.post("/api/v1/sessions", content=BODY, headers=JSON)
    assert resp.status_code == 201


def test_a_read_is_never_attributed(raw_client):
    """The method axis: the checks run on unsafe methods only, so a cross-site *read* is not refused
    here (the host allowlist is what guards reads, and it ran already)."""
    resp = raw_client.get("/api/v1/sessions",
                          headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example.com"})
    assert resp.status_code == 200


def test_the_checks_cover_every_unsafe_method(raw_client):
    """`PUT` as well as `POST`, on a real route: the guard reads `_UNSAFE_METHODS`, not one verb."""
    slug = seed_session("leave-approval")
    resp = raw_client.put(f"/api/v1/sessions/{slug}/context-cards", content=b'{"context_cards": []}',
                          headers={**JSON, "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "cross_site_fetch"


def test_a_cross_site_request_is_refused_before_the_content_type_check(raw_client):
    """Ordering: a cross-site page that also sent the wrong content type is told 403, not 415 --
    who is asking is settled before what they sent. The must-not-fire control is every 415 in
    `test_api_content_type_guard.py`, none of which states an origin."""
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "cross_site_fetch"


def test_a_rebound_host_is_refused_before_the_origin_checks(raw_client):
    """And the host guard still runs first of all (§5's ordering constraint): 403 `host_not_allowed`,
    never `origin_mismatch`, for a request that would fail both."""
    resp = raw_client.post("/api/v1/sessions", content=BODY,
                           headers={**JSON, "Host": "evil.example.com",
                                    "Origin": "http://evil.example.com"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "host_not_allowed"


# ── the shared definition, directly ─────────────────────────────────────────────────────────────

def test_check_request_origin_reaches_a_verdict_for_every_input():
    """The framework-free function both surfaces call: each arm fires on its own input, none fires
    on the clean one, and an undetermined origin (`http:///`) is refused rather than matched."""
    check_request_origin("127.0.0.1", sec_fetch_site=None, origin=None, referer=None)
    check_request_origin("127.0.0.1", sec_fetch_site="same-origin", origin="http://localhost",
                         referer=None)
    with pytest.raises(CrossSiteFetchError):
        check_request_origin("127.0.0.1", sec_fetch_site="cross-site", origin=None, referer=None)
    with pytest.raises(OpaqueOriginError):
        check_request_origin("127.0.0.1", sec_fetch_site=None, origin=" NULL ", referer=None)
    with pytest.raises(OriginMismatchError):
        check_request_origin("127.0.0.1", sec_fetch_site=None, origin="http:///", referer=None)
    with pytest.raises(OriginMismatchError):
        check_request_origin("127.0.0.1", sec_fetch_site=None, origin=None,
                             referer="http://evil.example.com")
    # a whitespace-only Origin does not fall through to a clean Referer (the unstripped read)
    with pytest.raises(OriginMismatchError):
        check_request_origin("127.0.0.1", sec_fetch_site=None, origin="   ",
                             referer="http://127.0.0.1")


def test_the_web_surface_calls_the_same_definition():
    """The sharing claim, mechanically: `web/security.py` re-exports these names from
    `host_policy` rather than defining its own -- identity, not equality."""
    from requivo.web import security
    assert security.CrossSiteFetchError is CrossSiteFetchError
    assert security.OpaqueOriginError is OpaqueOriginError
    assert security.OriginMismatchError is OriginMismatchError
    assert security.check_request_origin is check_request_origin
