"""Requivo Web security: response headers, the disk cache, the slug guard and redirect safety.

Split by #555, once this single file crossed the 800-line ceiling -- the request-side guards
(CSRF token, body cap, Host/Origin trust) moved out to `test_web_request_guard.py`, and response
headers, the disk cache, the slug guard and redirect safety stayed here. #142's original reason for
keeping security tests apart from routing and templates still holds for both halves: among many
tests, a security assertion that stops being collected looks exactly like one that passes, and each
file here is short enough that a missing test is a shorter list.

Offline (a fake provider), isolated workspace per test; the fixtures and the seeded-session helper
live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import pytest

from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.security import CSRF_HEADER, csrf_token
from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, _make_session, engine_reply

# ── the headers every response carries ────────────────────────────────────────


@pytest.fixture
def failing_route(app):
    """A route that raises, so a test can reach the unhandled-500 path the way a bug would.

    The client fixtures pass `raise_server_exceptions=False`, so the exception goes through
    `_unexpected` and comes back as the 500 response a browser would get, instead of being re-raised
    into the test. Registered on the function-scoped `app`, so it exists for one test only.
    """
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
    # Presence only. Which value is correct is not a spelling to pin here — it is a decision, and it is
    # argued and asserted by its consequence in
    # `test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree` (#47). What belongs here is
    # that the app states the policy rather than inheriting whatever the browser defaults to.
    assert "Referrer-Policy" in h
    assert h["Cache-Control"] == "no-store"


# The 500 page was the one response class this app served with none of the above (#340): `_unexpected`
# is registered for `Exception`, which Starlette handles in `ServerErrorMiddleware`, outside the user
# middleware stack, so `security_headers` never sees it. It was closed by stating the four headers a
# second time inside the handler — which left the *next* header added to the middleware missing from
# the 500 page, the same defect one header along (#462).
#
# So this asserts the composition rather than a list of names: whatever an ordinary page carries, the
# 500 page carries too. A test enumerating headers is exactly as complete as the day it was written,
# and this one is the reason there is a single `_apply_security_headers` to enumerate them in.
#
# `content-length` and `content-type` are in the compared set on purpose. They differ in *value*
# between the two responses and not in presence, and the assertion is about presence — dropping them
# would mean maintaining an exemption list, which is the enumeration this test exists to avoid.
def test_the_500_page_carries_every_header_an_ordinary_page_carries(client, failing_route):
    ordinary = client.get("/")
    unhandled = client.get(failing_route)
    assert ordinary.status_code == 200
    assert unhandled.status_code == 500

    missing = set(ordinary.headers) - set(unhandled.headers)
    assert not missing, f"the 500 page is missing {sorted(missing)}"


# ── the disk cache (#218) ─────────────────────────────────────────────────────
#
# Where the boundary is drawn, and why it is not the content type. The obvious rule — `no-store` when
# the response is `text/html` — covers the pages and the HTMX fragments and misses the two responses
# carrying the *most* business content there is: `/sessions/{slug}/export` hands back the whole model
# as `application/json`, and an artifact download hands back the PRD as `text/markdown`. Neither is
# HTML and both are the reader's own material.
#
# So the rule is the other way round and fails closed: every response is `no-store` unless it is a
# bundled asset this package ships (`/static/…`, `/favicon.ico`), which carry nothing of the reader's.
# A route added later is covered without anybody remembering to cover it; the cost of the default
# being wrong is one re-fetch of a stylesheet, and the cost of the allowlist being wrong is a session
# page on disk.

def test_no_response_carrying_the_readers_material_may_be_written_to_the_disk_cache(
        client, with_provider):
    """Every page, fragment and download this app answers with carries the reader's own request,
    the model built from it, or the request token -- none of which belongs in a browser's disk
    cache on a shared machine. The four rows are deliberately not all HTML: the two downloads are
    why the header is keyed on the path rather than the content type, since a `text/html` rule
    would pass the first three rows and leave the whole model and PRD cacheable."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT), BRIEF_REPLY)
    _make_session()

    page = client.get("/sessions/leave-approval")
    fragment = client.post("/sessions/leave-approval/answers",
                           data={"answers": "Contractors are out of scope.", "expected_revision": "1"},
                           headers={"HX-Request": "true"})
    client.post("/sessions/leave-approval/artifacts/brief")
    rows = {
        "home": client.get("/"),
        "session page": page,
        "htmx fragment": fragment,
        "model download": client.get("/sessions/leave-approval/export"),
        "artifact page": client.get("/sessions/leave-approval/artifacts/brief"),
        # The row the comment above is *about*. Without it this test's prose named the artifact
        # download as half the reason the rule is keyed on the path, and then never drove it — so a
        # per-route `Cache-Control` added to that one route later (an ETag scheme for large PRD
        # downloads is the plausible one) would ship with nothing red, under a test claiming to cover
        # exactly that case.
        "artifact download": client.get("/sessions/leave-approval/artifacts/brief?download=1"),
    }
    for name, response in rows.items():
        # Must fire: a 404 or a 500 would satisfy an assertion about a header just as happily as the
        # real page, and the whole row would then be about an error page nobody caches anyway.
        assert response.status_code == 200, f"the {name} row never reached the response it is about"
        assert response.headers.get("Cache-Control") == "no-store", (
            f"the {name} response may be written to the browser's disk cache")


def test_a_bundled_asset_stays_cacheable(client):
    """The other half of the boundary, and the reason it is an allowlist rather than a blanket.
    The CSS, HTMX and this file's own JS carry nothing of the reader's; `no-store` on them costs a
    re-fetch on every navigation and protects nothing. Asserted on a 200 (absent from a 404 too)
    and on the CSP, also absent from a response the middleware never ran on -- so requiring it
    proves the middleware looked and chose not to add `Cache-Control`."""
    for path in ("/static/css/app.css", "/static/js/app.js", "/static/vendor/htmx.min.js",
                 "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} was not served, so this row asserts nothing"
        assert "Content-Security-Policy" in response.headers, (
            f"the header middleware never ran on {path}, so its lack of Cache-Control says nothing "
            f"about the bundled-asset exclusion")
        assert "no-store" not in response.headers.get("Cache-Control", ""), (
            f"{path} is a bundled asset and must stay cacheable")


# ── the slug guard ────────────────────────────────────────────────────────────

def test_slug_traversal_is_rejected(client):
    # A dot-segment slug never resolves to a path; the guard raises invalid_slug (400) or the route
    # simply does not match (404) — either way, nothing outside the store is reached.
    assert client.get("/sessions/..%2f..%2fsecret").status_code in (400, 404)
    assert client.get("/sessions/a..b").status_code == 400   # matches {slug}, fails validation
    assert client.post("/sessions", data={"request_text": "x", "slug": "../escape",
                                          "provider": "create_only"}).status_code == 400


# ── open redirect: every RedirectResponse target stays same-origin ──────────────
#
# CodeQL's py/url-redirection flags all four RedirectResponse call sites that concatenate a slug
# onto a fixed literal prefix (#500): sessions.py's analysis_failed, discovery.py's run_discovery
# and submit_answers, and artifacts.py's generate_artifact. Each is a false positive for the same
# reason CodeQL's py/path-injection alerts are (see `decision: codeql-sanitizers-it-cannot-see` for
# the full argument): a regex-validated slug is a sanitizer CodeQL's default query does not
# recognise -- but that reasoning is a reading of the code, which this repository does not accept
# without a guard that goes red if it stops being true.

# `_FORM_FIELD_SLUG_ATTEMPTS` is for the one site whose slug is a `Form` field
# (`create_session`) rather than a `{slug}` path parameter: a form value is never subject to a
# client's own URL-path normalisation, so every one of these reaches `validate_slug` unchanged --
# verified directly against `create_app()`, each returns 400 `invalid_slug`.
_FORM_FIELD_SLUG_ATTEMPTS = [
    "../etc", "../../etc/passwd", "%2e%2e%2fetc", "%2e%2e%252fetc", "..%5cwindows",
    "/etc/passwd", "....//etc", "C:\\Windows", "evil.com", "@evil.com", "evil.com%2f%2f",
]

# `_PATH_PARAM_SLUG_ATTEMPTS` is for the three sites whose slug is a `{slug}` path parameter
# (`Depends(safe_slug)`). Several of the form-field attempts above never reach `safe_slug` at all
# on these routes: httpx's own URL-path normalisation and/or Starlette's single-segment route
# matching resolve or refuse a raw `../etc`, an encoded `%2e%2e%2fetc`, and their kin *before* the
# request is dispatched, so the app answers a bare 404 with the dependency never invoked -- one
# level past the client-side dot-segment normalisation this same file's `test_slug_traversal_is_rejected`
# already lives beside: it reaches whole traversal-shaped segments here, not only a bare `.`/`..`.
# Every entry below was verified directly against `create_app()` to return 400 `invalid_slug` --
# i.e. to actually reach and be refused by `safe_slug` -- rather than a 404 that would pass
# `_assert_never_off_site` for the wrong reason (no guard exercised at all).
_PATH_PARAM_SLUG_ATTEMPTS = [
    "a..b", "..%5cwindows", "C:\\Windows", "evil.com", "@evil.com", "leave%2Dapproval%00",
    "leave-approval..", "..leave-approval", "under_score", "with space",
]


def _assert_never_off_site(resp, attempt):
    """A hostile slug must be refused, never redirected -- the refusal is the assertion that
    always runs. The obvious shape, `if 300 <= status < 400: check Location`, is vacuous: a hostile
    slug never produces a redirect, so with the `/sessions/` prefix deleted and `_SLUG_RE` widened
    to admit `/` and `:` at once, all four callers stayed green. The always-firing claim is the
    status (4xx/5xx, never 3xx), stronger than the same-origin check and falsifiable."""
    assert resp.status_code >= 400, (
        f"slug {attempt!r} was not refused -- got {resp.status_code} "
        f"-> {resp.headers.get('location', '(no Location)')!r}; a hostile slug must never reach a "
        f"redirect")
    if 300 <= resp.status_code < 400:  # unreachable while the assertion above holds
        location = resp.headers.get("location", "")
        assert location.startswith("/") and not location.startswith("//"), (
            f"redirected off-site: {location!r}")
        assert "://" not in location, f"redirected off-site: {location!r}"


def test_the_discover_redirect_never_leaves_this_origin_under_a_hostile_slug(client):
    """discovery.py:48 -- `RedirectResponse(url=f"/sessions/{slug}")` after `run_discovery`."""
    for attempt in _PATH_PARAM_SLUG_ATTEMPTS:
        _assert_never_off_site(
            client.post(f"/sessions/{attempt}/discover", follow_redirects=False), attempt)


def test_the_answers_redirect_never_leaves_this_origin_under_a_hostile_slug(raw_client):
    """discovery.py:121 -- the no-JS (non-htmx) branch of `submit_answers`. Driven through
    `raw_client` with the CSRF field rather than `client`, which would tag `/answers` posts
    `HX-Request: true` and reach the fragment branch instead of the `RedirectResponse` under test."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    for attempt in _PATH_PARAM_SLUG_ATTEMPTS:
        resp = raw_client.post(f"/sessions/{attempt}/answers",
                               data={"answers": "x", "expected_revision": "0"},
                               follow_redirects=False)
        _assert_never_off_site(resp, attempt)


def test_the_generate_artifact_redirect_never_leaves_this_origin_under_a_hostile_slug(raw_client):
    """artifacts.py:60 -- the no-JS branch of `generate_artifact`, same reason `raw_client` is used
    above rather than `client`."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    for attempt in _PATH_PARAM_SLUG_ATTEMPTS:
        resp = raw_client.post(f"/sessions/{attempt}/artifacts/brief", follow_redirects=False)
        _assert_never_off_site(resp, attempt)


def test_the_create_session_failure_redirect_never_leaves_this_origin_under_a_hostile_slug(client):
    """`sessions.py:109` -- `analysis_failed`'s own redirect, reachable only via
    `create_session`'s `Form`-field `slug`, validated before the redirect fires. Driven through the
    route rather than calling `analysis_failed` directly, so the assertion is about the guard
    standing between the field and the redirect. Uses `_FORM_FIELD_SLUG_ATTEMPTS`: a `Form` field
    skips the URL normalisation that makes several `_PATH_PARAM_SLUG_ATTEMPTS` entries unreachable."""
    for attempt in _FORM_FIELD_SLUG_ATTEMPTS:
        resp = client.post("/sessions", data={"request_text": "x", "slug": attempt,
                                              "provider": "create_only"}, follow_redirects=False)
        _assert_never_off_site(resp, attempt)


def test_the_redirect_refusal_is_the_slug_guard_and_not_merely_a_missing_session(raw_client):
    """The must-fire half: a 404 from a nonsense slug proves nothing, since no session of that
    name exists either -- the refusal has to be the guard's own 400 naming `invalid_slug`. Built on
    `raw_client` alone, token set on the header directly: requesting `client` too would hand back
    the same mutated object under two names, silently adding `HX-Request: true` and reaching the
    fragment branch this test does not mean to exercise."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    responses = [
        raw_client.post("/sessions/Not_A_Slug/discover", follow_redirects=False),
        raw_client.post("/sessions/Not_A_Slug/answers",
                        data={"answers": "x", "expected_revision": "0"}, follow_redirects=False),
        raw_client.post("/sessions/Not_A_Slug/artifacts/brief", follow_redirects=False),
        raw_client.post("/sessions", data={"request_text": "x", "slug": "Not_A_Slug",
                                           "provider": "create_only"}, follow_redirects=False),
    ]
    for resp in responses:
        assert resp.status_code == 400, resp.text
        assert "invalid_slug" in resp.text, resp.text


def test_a_legitimate_slug_still_redirects_where_it_should(raw_client, with_provider, monkeypatch):
    """Must-not-fire control for every test above: a guard refusing a hostile slug means nothing
    next to proof an honest slug still reaches the redirect at all four sites. Built on `raw_client`
    alone rather than `client` too: `client` mutates `raw_client.post` in place, so requesting both
    hands back the same object under two names, silently injecting `HX-Request: true` and reaching
    the fragment branch, not the `RedirectResponse` this test means to exercise."""
    raw_client.headers[CSRF_HEADER] = csrf_token()

    # discover (discovery.py:48): a fresh create_only session, at the revision-0 discovery is
    # required to run from. `answer()` reasons a turn of its own further down, so the fake carries
    # a second reply for it in the same order the two calls fire.
    SessionService().create_session("A leave approval system.", slug="leave-only")
    with_provider(engine_reply(converged=True), engine_reply(converged=True))
    r = raw_client.post("/sessions/leave-only/discover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-only"

    # answers (discovery.py:121): a seeded, already-discovered session.
    _make_session("leave-approval")
    r = raw_client.post("/sessions/leave-approval/answers",
                        data={"answers": "x", "expected_revision": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"

    # generate_artifact (artifacts.py:60).
    with_provider(BRIEF_REPLY)
    r = raw_client.post("/sessions/leave-approval/artifacts/brief", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"

    # analysis_failed (sessions.py:109), reached through create_session's own failure branch.
    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")
    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "leave-failed",
                                           "provider": "anthropic"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/sessions/leave-failed?analysis_failed=")


