"""Requivo Web routing: the routes that answer, and the status each error reaches the browser as.

Split out of `test_web.py` by #142, along the five subjects that file's own docstring named. Offline
(a fake provider), isolated workspace per test; the fixtures and the seeded-session helper live in
`tests/web/conftest.py`.

Two subjects in one file because they are halves of one question. A route is only as good as the
status it answers with, so this file used to also hold the error-code to HTTP-status contract (#34)
directly. #422 moved that table, and the tests that pin it as a table (a completeness walk over every
`RequivoError` subclass, the per-code assertions, the unclassified default, the EngineError ordering),
to `tests/test_http_status_table.py` -- collectible with no `[web]` extra installed, since the table
itself no longer needs one. What stays here are the two tests that only a real request through the
real handler and templates can pin: the number a *browser* actually gets for a given fault.
"""

from __future__ import annotations

import pytest

from requivo.cli import app as cli_app
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, _make_session

# ── packaging / smoke ─────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_web_help_exits_cleanly():
    with pytest.raises(SystemExit) as ei:
        cli_app(["web", "--help"])
    assert ei.value.code == 0


def test_static_assets_are_served_locally(client):
    # HTMX is vendored — served from the package, never a CDN.
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


# ── chrome polish: favicon, human page titles (#241) ──────────────────────────

def test_favicon_is_served_and_linked(client):
    """No favicon existed anywhere in `web/` — every tab showed the browser default and every page
    load 404'd `/favicon.ico` into the operator's logs. The brand mark already sits inline in
    `base.html`; this ships it as a real icon and stops the implicit browser probe from 404ing."""
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")

    page = client.get("/").text
    assert 'rel="icon"' in page, "base.html does not link an icon at all"


def test_session_title_uses_the_objective_once_understood(client):
    """The `<title>` used the slug (`we-d-like-managers-to — Requivo`) rather than the human title
    the same page's `<h1>` already computes."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)

    page = client.get("/sessions/leave-approval").text

    assert "<title>Leave system — Requivo</title>" in page, (
        "the tab title still shows the slug instead of the objective " + page[:400])


def test_session_title_falls_back_to_the_slug_while_pending(client, with_provider):
    """No objective exists yet before the first analysis runs — the slug is still the only human
    name for the session, so the title must not go blank or show 'None'."""
    with_provider()
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-only", "provider": "create_only"})

    page = client.get("/sessions/leave-only").text

    assert "<title>leave-only — Requivo</title>" in page


# ── app / pages ───────────────────────────────────────────────────────────────

def test_home_without_sessions(client):
    r = client.get("/")
    assert r.status_code == 200 and "Nothing here yet" in r.text


def test_home_states_the_measured_context_card_cost(client):
    """#257: the create form's card selector defaults to every box unchecked, which loads every
    card — the most expensive and most diluted path (CLAUDE.md's own "Known limit" note). The hint
    must be additive disclosure only, so this does not touch which cards a submission actually loads
    -- see the discovery service's own tests for that half."""
    from requivo.core.context import available_cards, average_card_byte_size

    page = client.get("/").text
    assert available_cards(), "no bundled context cards found -- this test is not exercising anything"
    avg = average_card_byte_size()
    assert avg, "average_card_byte_size() returned nothing for a non-empty install"
    assert "context-cost-hint" in page
    assert f"{avg:,}" in page, f"the rendered hint does not name the measured average ({avg:,} bytes)"


def test_home_lists_an_existing_session(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/")
    assert r.status_code == 200 and "leave-approval" in r.text


def test_session_page_renders_understanding(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
    r = client.get("/sessions/leave-approval")
    assert r.status_code == 200
    assert "What Requivo understood" in r.text and "Are we ready?" in r.text


def test_missing_session_is_404(client):
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404 and "Not found" in r.text


def test_a_corrupt_model_is_the_malformed_session_page_not_a_generic_500(client):
    """The web half of #204, and the reason it was a generic 500 rather than a bad one. `GET
    /sessions/<slug>` reads the model; a pydantic `ValidationError` is not a `RequivoError`, so it
    missed the handler's whole vocabulary for a malformed session and landed in the catch-all as
    "Something went wrong on the server" with nothing to act on. 500 is still right: now the page
    can say which fact about the store made it so."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    from requivo.core import persistence as store
    (store.canonical_dir("leave-approval") / "model.json").write_text("{", encoding="utf-8")

    r = client.get("/sessions/leave-approval")
    assert r.status_code == 500
    assert "model_unreadable" in r.text, (
        "the page names the code, so a reader can tell it from session_unreadable -- which is the "
        "same status and a different situation with a different remedy")
    assert "internal_error" not in r.text, "the catch-all is what this stopped being"

    # The listing is deliberately unaffected: only the model is broken, and #7/#80's rule is that one
    # unreadable member must not take the page down with it.
    assert client.get("/").status_code == 200


def test_export_returns_model_json(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/sessions/leave-approval/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "leave-approval.model.json" in r.headers["content-disposition"]
    assert "problem" in r.json()["model"]


# ── the error-code → HTTP status contract (#34) ───────────────────────────────
#
# The classification table, and the tests that pin it directly as a table (a completeness walk
# over every `RequivoError` subclass, the per-code assertions, the unclassified default, the
# EngineError-ahead-of-the-table ordering), moved to `tests/test_http_status_table.py` by #422 --
# once the table itself moved out of `web/app.py` to the framework-free `requivo.http`. What
# stays here is what only a real request through the real handler and templates can pin: the
# number a *browser* actually gets.


def test_context_unreadable_reaches_the_browser_as_a_server_error(client, monkeypatch):
    """End to end, through the real handler and the real templates — the mapping test above pins the
    number, this pins that the number is what a reader actually gets."""
    from requivo.core.errors import ContextUnreadableError

    # must fire: the page is fine before the fault is injected
    assert client.get("/").status_code == 200

    def _unreadable():
        raise ContextUnreadableError(
            "the context-card directory /x exists but cannot be read: denied",
            details={"directory": "/x"})

    monkeypatch.setattr("requivo.web.routes.home.available_cards", _unreadable)
    r = client.get("/")
    assert r.status_code == 500, "a permissions fault on the install's own assets is not a 400"
    assert "context_unreadable" in r.text


def test_a_go_to_market_only_type_on_a_software_session_is_a_clean_refusal_not_a_500(
        client, with_provider, monkeypatch):
    """#609 (Codex, P2): `generatable_view()` used to return the full global `GENERATABLE` with no
    perimeter filter, so a software session's page offered a "Go-to-market plan" button -- clicking
    it reached `_require_owned_artifact_type`, which raised a bare `ValueError`. `ValueError` is not
    a `RequivoError`, so it missed the handler above entirely and landed in the catch-all as an
    ordinary click's 500. Both halves of the fix are exercised here: `generatable_view()` no longer
    offers the type in the first place (the session page's own generate-buttons list, gated on
    `provider.available` -- a real key is faked so that block actually renders), and, defence in
    depth, the refusal itself is now a structured `ArtifactTypeNotOwnedError`
    (409, `artifact_type_not_owned`) rather than the 500 a stray request would otherwise still hit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    with_provider()
    _make_session("leave-approval", problem=HIGH_EXPLICIT)  # software, the default perimeter

    page = client.get("/sessions/leave-approval").text
    assert "Generate decision brief" in page, "must-fire: the generate-buttons block did render"
    assert "gtm_plan" not in page, "the button itself must not be offered on the wrong perimeter"

    r = client.post("/sessions/leave-approval/artifacts/gtm_plan")
    assert r.status_code == 409, f"a real click must not 500; got {r.status_code}"
    assert "artifact_type_not_owned" in r.text
    assert "internal_error" not in r.text, "the catch-all is what this stopped being"


def test_a_go_to_market_sessions_primary_document_is_its_own_plan_not_a_missing_brief(
        client, with_provider, monkeypatch):
    """#609's follow-up review (Codex, P2): `session_detail()` picked the primary artifact off the
    software-only `PRIMARY_ARTIFACT` constant regardless of perimeter, so a go-to-market session --
    whose only artifact is `gtm_plan`, never `brief` -- had no primary at all: buried under "More
    documents" while the primary card's own generate form still rendered, bound to `None`, posting
    to `/sessions/<slug>/artifacts/` (a trailing empty type) with no matching route. Drives a real
    go-to-market session through the real handler and templates, with a real credential so the
    generate-buttons block actually renders (the same trap
    `test_a_go_to_market_only_type_on_a_software_session_is_a_clean_refusal_not_a_500` above caught
    by hand): the button names the right document, posts to the right, real route, and that route
    actually works end to end."""
    import json

    from requivo.core.contracts import schema_slot_ids
    from requivo.core.perimeters import GO_TO_MARKET
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    with_provider(json.dumps({"plan": ["Ship one outbound sequence to the existing waitlist."]}))
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm-primary", perimeter=GO_TO_MARKET)
    _, required = schema_slot_ids(GO_TO_MARKET)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                   "value": "x", "evidence": "y"} for sid in required}
    svc.update_model(meta.slug, json.dumps({"model": model, "questions": [],
                                            "summary": {"objective": "grow"}}), expected_revision=0)

    page = client.get(f"/sessions/{meta.slug}").text
    assert "Generate go-to-market plan" in page, "must-fire: the primary card's own button did render"
    assert "Generate decision brief" not in page, "the wrong perimeter's document must not lead the page"
    assert f'/sessions/{meta.slug}/artifacts/gtm_plan"' in page, "the form must post to a real route"
    assert f'/sessions/{meta.slug}/artifacts/"' not in page, "must not post to the route that does not exist"

    r = client.post(f"/sessions/{meta.slug}/artifacts/gtm_plan")
    assert r.status_code == 200, f"the route the button posts to must actually work; got {r.status_code}"


def test_a_taken_session_name_is_suffixed_rather_than_refused(client):
    """Why `session_exists` gets a status row but no end-to-end test, recorded where the next
    reader will look. Posting a name already taken by a different request does not raise
    `session_exists`: `create_session` falls through to a free `<base>-<identity hash>` candidate,
    silently redirecting the reader to a session with a name they did not choose. This pins the
    behaviour as-is; the silent rename is an adjacent finding, not fixed here."""
    first = client.post("/sessions", data={"request_text": "A leave approval request",
                                           "slug": "leave-approval", "provider": "create_only"},
                        follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/sessions/leave-approval"

    second = client.post("/sessions", data={"request_text": "A different request entirely",
                                            "slug": "leave-approval", "provider": "create_only"},
                         follow_redirects=False)
    assert second.status_code == 303
    landed = second.headers["location"]
    assert landed.startswith("/sessions/leave-approval-"), landed
    assert landed != "/sessions/leave-approval", (
        "must fire: the second request really did get its own session, so the rename is real")
