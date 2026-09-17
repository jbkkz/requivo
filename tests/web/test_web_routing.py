"""Requivo Web routing: the routes that answer, and the status each error reaches the browser as (#142)."""

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
    """No favicon existed anywhere in `web/` — every tab showed the browser default and every page load 404'd
    `/favicon.ico` into the operator's logs."""
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")

    page = client.get("/").text
    assert 'rel="icon"' in page, "base.html does not link an icon at all"


def test_session_title_uses_the_objective_once_understood(client):
    """The `<title>` used the slug (`we-d-like-managers-to — Requivo`) rather than the human title the same
    page's `<h1>` already computes."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)

    page = client.get("/sessions/leave-approval").text

    assert "<title>Leave system — Requivo</title>" in page, (
        "the tab title still shows the slug instead of the objective " + page[:400])


def test_session_title_falls_back_to_the_slug_while_pending(client, with_provider):
    """No objective exists yet before the first analysis runs."""
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
    """#257: the create form's card selector defaults to every box unchecked, which loads every card."""
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
    """The web half of #204, and the reason it was a generic 500 rather than a bad one."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    from requivo.core import persistence as store
    (store.canonical_dir("leave-approval") / "model.json").write_text("{", encoding="utf-8")

    r = client.get("/sessions/leave-approval")
    assert r.status_code == 500
    assert "model_unreadable" in r.text, (
        "the page names the code, so a reader can tell it from session_unreadable -- which is the "
        "same status and a different situation with a different remedy")
    assert "internal_error" not in r.text, "the catch-all is what this stopped being"

    # The listing is deliberately unaffected: only the model is broken (#7).
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
# The classification table, and the tests that pin it directly as a table (a completeness walk over every `RequivoError` subclass, the per-code assertions, the unclassified default, the EngineError-ahead-of-the-table ordering), moved to `tests/test_http_status_table.py` by #422 -- once the table itself moved out of `web/app.py` to the framework-free `requivo.http`.


def test_context_unreadable_reaches_the_browser_as_a_server_error(client, monkeypatch):
    """End to end, through the real handler and the real templates."""
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
    """#609 (Codex, P2): `generatable_view()` used to return the full global `GENERATABLE` with no perimeter
    filter, so a software session's page offered a "Go-to-market plan" button."""
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
    software-only `PRIMARY_ARTIFACT` constant regardless of perimeter, so a go-to-market session."""
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
    """Why `session_exists` gets a status row but no end-to-end test."""
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
