"""Requivo Web routing: the routes that answer, and the status each error reaches the browser as (#142)."""

from __future__ import annotations

import json

import pytest

from requivo.cli import app as cli_app
from requivo.core import persistence as store
from requivo.core.context import available_cards, average_card_byte_size
from requivo.core.contracts import schema_slot_ids
from requivo.core.errors import ContextUnreadableError
from requivo.core.perimeters import GO_TO_MARKET
from requivo.services.sessions import SessionService
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, _make_session, create_via_post


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_web_help_exits_cleanly():
    with pytest.raises(SystemExit) as ei:
        cli_app(["web", "--help"])
    assert ei.value.code == 0


def test_static_assets_are_served_locally(client):
    # HTMX is vendored — served from the package, never a CDN.
    for path in ("/static/vendor/htmx.min.js", "/static/css/app.css", "/static/js/app.js"):
        assert client.get(path).status_code == 200


def test_favicon_is_served_and_linked(client):
    """Every page load used to 404 `/favicon.ico` into the operator's logs (#241)."""
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    assert 'rel="icon"' in client.get("/").text, "base.html does not link an icon at all"


def test_session_title_uses_the_objective_once_understood_and_the_slug_while_pending(client, with_provider):
    """The `<title>` used the slug rather than the human title the page's `<h1>` already computes (#241)."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
    page = client.get("/sessions/leave-approval").text
    assert "<title>Leave system — Requivo</title>" in page, "the tab title still shows the slug " + page[:400]
    with_provider()
    create_via_post(client, slug="leave-only", provider="create_only", request_text="A leave approval system")
    assert "<title>leave-only — Requivo</title>" in client.get("/sessions/leave-only").text   # no objective yet


def test_home_lists_what_exists_and_states_the_measured_context_card_cost(client):
    """#257: the card selector defaults to every box unchecked, which loads every card, and the hint says what that costs."""
    r = client.get("/")
    assert r.status_code == 200 and "Nothing here yet" in r.text
    assert available_cards(), "no bundled context cards found -- this test is not exercising anything"
    avg = average_card_byte_size()
    assert avg, "average_card_byte_size() returned nothing for a non-empty install"
    assert "context-cost-hint" in r.text and f"{avg:,}" in r.text, f"the hint does not name the measured average ({avg:,} bytes)"
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/")
    assert r.status_code == 200 and "leave-approval" in r.text


def test_session_page_renders_understanding(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
    r = client.get("/sessions/leave-approval")
    assert r.status_code == 200 and "What Requivo understood" in r.text and "Are we ready?" in r.text


def test_missing_session_is_404(client):
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404 and "Not found" in r.text


def test_a_corrupt_model_is_the_malformed_session_page_not_a_generic_500(client):
    """The web half of #204: `model_unreadable` names the code, so a reader can tell it from `session_unreadable`."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    (store.canonical_dir("leave-approval") / "model.json").write_text("{", encoding="utf-8")
    r = client.get("/sessions/leave-approval")
    assert r.status_code == 500 and "model_unreadable" in r.text
    assert "internal_error" not in r.text, "the catch-all is what this stopped being"
    assert client.get("/").status_code == 200        # the listing is deliberately unaffected (#7)


def test_export_returns_model_json(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/sessions/leave-approval/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert "leave-approval.model.json" in r.headers["content-disposition"]
    assert "problem" in r.json()["model"]


# The error-code → HTTP status table itself is pinned in tests/test_http_status_table.py (#34, #422).


def test_context_unreadable_reaches_the_browser_as_a_server_error(client, monkeypatch):
    """End to end, through the real handler and the real templates."""
    assert client.get("/").status_code == 200       # must fire: the page is fine before the fault is injected

    def _unreadable():
        raise ContextUnreadableError("the context-card directory /x exists but cannot be read: denied",
                                     details={"directory": "/x"})
    monkeypatch.setattr("requivo.web.routes.home.available_cards", _unreadable)
    r = client.get("/")
    assert r.status_code == 500, "a permissions fault on the install's own assets is not a 400"
    assert "context_unreadable" in r.text


def test_a_go_to_market_only_type_on_a_software_session_is_a_clean_refusal_not_a_500(client, with_provider, monkeypatch):
    """#609 (Codex, P2): `generatable_view()` returned the global `GENERATABLE` with no perimeter filter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    with_provider()
    _make_session("leave-approval", problem=HIGH_EXPLICIT)  # software, the default perimeter
    page = client.get("/sessions/leave-approval").text
    assert "Generate decision brief" in page, "must-fire: the generate-buttons block did render"
    assert "gtm_plan" not in page, "the button itself must not be offered on the wrong perimeter"
    r = client.post("/sessions/leave-approval/artifacts/gtm_plan")
    assert r.status_code == 409 and "artifact_type_not_owned" in r.text, f"a real click must not 500; got {r.status_code}"
    assert "internal_error" not in r.text


def test_a_go_to_market_sessions_primary_document_is_its_own_plan_not_a_missing_brief(client, with_provider, monkeypatch):
    """#609's follow-up (Codex, P2): `session_detail()` picked the primary artifact off the software-only constant."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    with_provider(json.dumps({"plan": ["Ship one outbound sequence to the existing waitlist."]}))
    svc = SessionService()
    slug = svc.create_session("grow the funnel", slug="gtm-primary", perimeter=GO_TO_MARKET).slug
    _, required = schema_slot_ids(GO_TO_MARKET)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high", "value": "x", "evidence": "y"}
             for sid in required}
    svc.update_model(slug, json.dumps({"model": model, "questions": [], "summary": {"objective": "grow"}}), expected_revision=0)
    page = client.get(f"/sessions/{slug}").text
    assert "Generate go-to-market plan" in page and "Generate decision brief" not in page
    assert f'/sessions/{slug}/artifacts/gtm_plan"' in page and f'/sessions/{slug}/artifacts/"' not in page
    assert client.post(f"/sessions/{slug}/artifacts/gtm_plan").status_code == 200, "the route the button posts to must work"


def test_a_taken_session_name_is_suffixed_rather_than_refused(client):
    """Why `session_exists` gets a status row but no end-to-end test."""
    first = create_via_post(client, provider="create_only", request_text="A leave approval request")
    assert first.status_code == 303 and first.headers["location"] == "/sessions/leave-approval"
    second = create_via_post(client, provider="create_only", request_text="A different request entirely")
    assert second.status_code == 303
    landed = second.headers["location"]
    assert landed.startswith("/sessions/leave-approval-") and landed != "/sessions/leave-approval", landed
