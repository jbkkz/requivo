"""Path traversal via the API's own path parameters (#425, slice 1).

Written in review, in answer to CodeQL. Introducing an HTTP surface gave CodeQL its first
user-controlled source reaching `core/persistence.py`'s path expressions, and it raised 12 high
"uncontrolled data used in path expression" alerts against lines this pull request does not touch.
Both parameters are in fact sanitized, and CodeQL models neither sanitizer:

- `{slug}` goes through `api/dependencies.py`'s `safe_slug` -> `_slug_shape`, whose `_SLUG_RE` is
  `^[a-z0-9]+(?:-[a-z0-9]+)*\\Z` -- no dot, no separator, no drive letter can match it -- plus
  `MAX_SLUG_LENGTH`, with `is_contained` still underneath at the store layer (invariant 17).
- `{artifact_type}` is never a path component at all. `ArtifactService._filename` uses it as a key
  into the fixed `ARTIFACT_FILENAMES` dict and raises `UnknownArtifactTypeError` on a miss, so the
  filename that reaches the filesystem is always one of a closed set of literals.

A regex guard and a dict-lookup-to-constant are both sanitizers CodeQL's default Python query does
not recognise, so the alerts are false positives. That conclusion was reached by reading the code,
which is exactly the kind of claim this repository does not accept on its own -- hence this file.
It fails if any future change lets a traversal attempt through either parameter, which is the event
that would turn those alerts true.
"""

from __future__ import annotations

from tests.api.conftest import seed_session

# Encoded as well as literal: Starlette normalises some of these before routing and refuses others
# with a 404 of its own, which is a fine outcome -- the assertion is that none of them *succeeds*,
# not that they all take the same road to failing.
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
    """`{artifact_type}` is a key into a closed set, never a path component -- so a traversal
    attempt is an unknown *type*, refused before any filename is resolved, and a real file that is
    not an artifact type (`session`, `model`, `.lock`) is refused on the identical grounds."""
    slug = seed_session("leave-approval")
    for attempt in _TYPE_ATTEMPTS:
        resp = client.get(f"/api/v1/sessions/{slug}/artifacts/{attempt}")
        assert 400 <= resp.status_code < 500, (
            f"type {attempt!r} returned {resp.status_code}, not a refusal")
        assert "format_version" not in resp.text and "current_revision" not in resp.text, (
            f"type {attempt!r} served a session file: {resp.text[:200]}")


def test_the_refusal_is_the_slug_guard_and_not_merely_a_missing_session(client):
    """The must-fire half, and the reason the test above is not enough on its own: a 404 from
    `../etc` proves nothing, since no session of any name exists there either. A traversal attempt
    has to be refused as an *invalid slug* -- by the guard -- and not by the accident of the target
    being absent, or widening `_SLUG_RE` later would leave every assertion above still green."""
    resp = client.get("/api/v1/sessions/..%2f..%2fetc/model")
    assert resp.status_code in (400, 404)
    if resp.status_code == 400:
        assert resp.json()["code"] == "invalid_slug", resp.json()

    # A plainly invalid slug that cannot be mistaken for a routing artefact must be the guard's own
    # 400, so the guard is demonstrably reachable through this surface at all.
    resp = client.get("/api/v1/sessions/Not_A_Slug/model")
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_slug", resp.json()


def test_a_bare_dot_segment_never_reaches_the_slug_parameter_at_all(client):
    """The one shape that is not the guard's job, recorded so nobody adds it to the list above and
    reads the 200 as a hole. `.` and `..` are resolved by the *client* before the request is sent
    (RFC 3986 section 5.2.4), so the server is never offered them as a path parameter:
    `/api/v1/sessions/.` goes out as `/api/v1/sessions` and correctly answers the collection route,
    and `/api/v1/sessions/..` goes out as `/api/v1` and 404s. Neither reads a file.

    Asserted on the request URL as sent, not only on the status, because a status alone cannot tell
    normalisation apart from a guard that let the segment through and happened to find nothing."""
    resp = client.get("/api/v1/sessions/.")
    assert resp.request.url.path == "/api/v1/sessions", "expected the client to resolve the dot"
    assert resp.status_code == 200 and "sessions" in resp.json()

    resp = client.get("/api/v1/sessions/..")
    assert resp.request.url.path == "/api/v1"
    assert resp.status_code == 404
