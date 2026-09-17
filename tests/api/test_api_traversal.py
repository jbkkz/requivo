"""Path traversal via the API's own path parameters (#425, slice 1)."""

from __future__ import annotations

from tests.api.conftest import seed_session

# Encoded as well as literal: Starlette normalises some of these before routing and refuses others with a 404 of its own, which is a fine outcome -- the assertion is that none of them *succeeds*, not that they all take the same road to failing.
_SLUG_ATTEMPTS = [
    "../etc",
    "../../etc/passwd",
    "%2e%2e%2fetc",
    "%2e%2e%252fetc",
    "..%5cwindows",
    "/etc/passwd",
    "leave-approval/../../../etc",
    "....//etc",
    "C:\\Windows",
    "leave%2Dapproval%00",
]

_TYPE_ATTEMPTS = [
    "../../../etc/passwd",
    "..%2f..%2fsession.json",
    "prd/../../../session.json",
    "session",          # a real file in the session dir, but not an artifact type
    "model",            # likewise
    ".lock",
]


def test_no_slug_shaped_traversal_reaches_the_filesystem(client):
    """Every route taking `{slug}` refuses a traversal attempt rather than resolving it."""
    seed_session("leave-approval")
    for attempt in _SLUG_ATTEMPTS:
        for route in ("", "/model", "/status", "/revisions", "/artifacts"):
            resp = client.get(f"/api/v1/sessions/{attempt}{route}")
            assert 400 <= resp.status_code < 500, (
                f"{attempt!r}{route} returned {resp.status_code}, not a refusal")
            assert "root:" not in resp.text and "/etc/passwd" not in resp.text, (
                f"{attempt!r}{route} leaked content: {resp.text[:200]}")


def test_no_artifact_type_traversal_reaches_the_filesystem(client):
    """`{artifact_type}` is a key into a closed set, never a path component."""
    slug = seed_session("leave-approval")
    for attempt in _TYPE_ATTEMPTS:
        resp = client.get(f"/api/v1/sessions/{slug}/artifacts/{attempt}")
        assert 400 <= resp.status_code < 500, (
            f"type {attempt!r} returned {resp.status_code}, not a refusal")
        assert "format_version" not in resp.text and "current_revision" not in resp.text, (
            f"type {attempt!r} served a session file: {resp.text[:200]}")


def test_the_refusal_is_the_slug_guard_and_not_merely_a_missing_session(client):
    """The must-fire half, and the reason the test above is not enough on its own."""
    resp = client.get("/api/v1/sessions/..%2f..%2fetc/model")
    assert resp.status_code in (400, 404)
    if resp.status_code == 400:
        assert resp.json()["code"] == "invalid_slug", resp.json()

    # A plainly invalid slug that cannot be mistaken for a routing artefact must be the guard's own 400.
    resp = client.get("/api/v1/sessions/Not_A_Slug/model")
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_slug", resp.json()


def test_a_bare_dot_segment_never_reaches_the_slug_parameter_at_all(client):
    """The one shape that is not the guard's job, recorded so nobody adds it to the list above and reads the
    200 as a hole."""
    resp = client.get("/api/v1/sessions/.")
    assert resp.request.url.path == "/api/v1/sessions", "expected the client to resolve the dot"
    assert resp.status_code == 200 and "sessions" in resp.json()

    resp = client.get("/api/v1/sessions/..")
    assert resp.request.url.path == "/api/v1"
    assert resp.status_code == 404
