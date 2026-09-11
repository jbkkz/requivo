"""The one piece of slice 4's cross-site posture slice 2 ships early (#425,
`docs/decisions/0004-the-http-api-facade.md` §5): every unsafe method requires
`Content-Type: application/json`, refused with 415 otherwise. The `Sec-Fetch-Site`/`Origin` checks
that complete that posture are still slice 4's and are not tested here.

Every test uses `raw_client` -- the fixture that sends nothing beyond what `httpx` sends unasked --
so the header really has to be added by the caller, not assumed by the fixture the way `client`
(used by every other write-route test in this package) assumes it."""

from __future__ import annotations

from tests.api.conftest import seed_session


def test_a_post_with_no_content_type_is_refused(raw_client):
    """Must-fire: `POST /sessions` with no `Content-Type` header at all."""
    resp = raw_client.post("/api/v1/sessions", content=b'{"request": "A leave approval system."}')
    assert resp.status_code == 415
    assert resp.json()["code"] == "unsupported_content_type"


def test_a_post_with_the_wrong_content_type_is_refused(raw_client):
    resp = raw_client.post("/api/v1/sessions", content=b'{"request": "A leave approval system."}',
                           headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert resp.json()["code"] == "unsupported_content_type"


def test_a_bodyless_post_still_needs_the_header(raw_client):
    """The rule is unconditional across every unsafe method, including one with no request model
    today (`POST .../discover`) -- a route having nothing to validate in the body is not a reason to
    skip the floor under it."""
    slug = seed_session("leave-approval")
    resp = raw_client.post(f"/api/v1/sessions/{slug}/discover")
    assert resp.status_code == 415
    assert resp.json()["code"] == "unsupported_content_type"


def test_a_post_with_the_json_content_type_is_not_refused(raw_client):
    """The must-not-fire control: the header this guard actually asks for is accepted, and the
    request reaches the route (a 201, not a 415) -- proving the guard discriminates rather than
    refusing every unsafe method outright."""
    resp = raw_client.post("/api/v1/sessions",
                           content=b'{"request": "A leave approval system."}',
                           headers={"Content-Type": "application/json"})
    assert resp.status_code == 201


def test_the_charset_parameter_is_tolerated(raw_client):
    """A real client stating an encoding (`application/json; charset=utf-8`) must not be refused --
    only the media type is matched, per `require_json_content_type`'s own docstring."""
    resp = raw_client.post("/api/v1/sessions",
                           content=b'{"request": "A leave approval system."}',
                           headers={"Content-Type": "application/json; charset=utf-8"})
    assert resp.status_code == 201


def test_a_get_needs_no_content_type(raw_client):
    """The must-not-fire control on the method axis: a safe method is never refused for missing
    `Content-Type`, because the guard only ever inspects unsafe ones."""
    resp = raw_client.get("/api/v1/sessions")
    assert resp.status_code == 200


def test_a_rebound_host_is_refused_before_the_content_type_check(raw_client):
    """Ordering, not reach: both guards refuse, but a request from a host the allowlist rejects is
    told 403 `host_not_allowed` and not 415 -- the transport-level check runs first, as
    `docs/decisions/0004-the-http-api-facade.md` §5 places it. A first draft registered the two the
    other way round (found in review). The must-not-fire control is every 415 test above, which
    all carry the allowed host."""
    resp = raw_client.post("/api/v1/sessions", content=b'{"request": "x"}',
                           headers={"Host": "evil.example.com"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "host_not_allowed"
    # ...and the refusal still carries the header policy, from the middleware outside both guards.
    assert "content-security-policy" in resp.headers
