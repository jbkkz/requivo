"""Requivo Web discovery and artifact flows: the workflow the product leads with, end to end (#142)."""

from __future__ import annotations

import json

from requivo.core.persistence import canonical_dir
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import Readiness, UpdateResult
from requivo.web.viewmodels.status import impact_view
from tests.web.conftest import (
    BRIEF_REPLY,
    CRITERIA_REPLY,
    HIGH_EXPLICIT,
    HIGH_INFERRED,
    PRD_REPLY,
    _make_session,
    create_via_post,
    engine_reply,
)

_ASKING = engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
_CONVERGED = engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT)
_RESOLVED = engine_reply(converged=True, problem=HIGH_EXPLICIT)
_ANSWER = {"answers": "Exceptions go to HR.", "expected_revision": "1"}
_SESSION = "/sessions/leave-approval"
_LOW = {"completeness": 10, "confidence": "empty", "impact": "low"}


def _discovered(client, with_provider, *replies, **create):
    """A session discovered through the web against the scripted replies; returns the fake."""
    fake = with_provider(*replies)
    create_via_post(client, **create)
    return fake


def _page(client, session=_SESSION) -> str:
    return client.get(session).text


def _generate(client, kind, session=_SESSION):
    return client.post(f"{session}/artifacts/{kind}")


def _answer(client, session=_SESSION, **data):
    return client.post(f"{session}/answers", data={**_ANSWER, **data})


# ── discovery (mocked provider) ───────────────────────────────────────────────


def test_create_session_runs_discovery(client, with_provider):
    """The questions lead the page in the reader's words; no raw slot id reaches it."""
    with_provider(_ASKING)
    r = create_via_post(client, request_text="A leave approval system")
    assert r.status_code == 303 and r.headers["location"] == _SESSION
    page = _page(client)
    assert "What could change the solution" in page and "How are exceptions handled" in page
    for slot_id in ("business_rules", "config_vs_custom", "success_metrics", "current_process", "edge_cases", "business_objects"):
        assert slot_id not in page, f"slot id {slot_id!r} rendered to the reader"


def test_create_only_then_run_discovery(client, with_provider):
    r = create_via_post(client, slug="leave-only", provider="create_only", request_text="A leave system")
    assert r.status_code == 303 and "Awaiting analysis" in _page(client, "/sessions/leave-only")   # no LLM yet
    with_provider(_RESOLVED)
    assert client.post("/sessions/leave-only/discover", follow_redirects=False).status_code == 303
    assert "No question left that would change the solution" in _page(client, "/sessions/leave-only")


def test_a_hand_crafted_answers_post_on_a_revision_zero_session_is_refused(client, with_provider):
    """The web's own door onto #421: the rendered page never shows the answers form at revision 0."""
    fake = with_provider()  # no reply queued: a call reaching the provider fails loudly, not silently
    create_via_post(client, slug="bare-rev0", provider="create_only")
    r = _answer(client, "/sessions/bare-rev0", answers="…", expected_revision="0")
    assert r.status_code == 409 and fake.calls == []    # a clean 409, refused before any provider construction
    assert "requivo answer" not in r.text                # the remedy no longer routes back into this path


def test_a_stale_answers_form_is_refused_before_the_provider_is_paid(client, with_provider):
    """A conflict that is already certain must not be discovered by paying for it (#205); a matching form still reaches it."""
    fake = _discovered(client, with_provider, _ASKING, _CONVERGED)
    stale = _answer(client, answers="…", expected_revision="999")
    assert stale.status_code == 409 and len(fake.calls) == 1, "a turn whose conflict was certain was billed"
    fresh = _answer(client)
    assert fresh.status_code == 200 and "What changed" in fresh.text and len(fake.calls) == 2


# ── artifacts ─────────────────────────────────────────────────────────────────


def test_generate_brief_and_prd_and_view(client, with_provider):
    _discovered(client, with_provider, _RESOLVED, BRIEF_REPLY, PRD_REPLY)
    assert _generate(client, "brief").status_code == 200 and _generate(client, "prd").status_code == 200
    page = _page(client)
    assert "Decision brief" in page and "PRD" in page and "Up to date" in page   # both listed, fresh
    view = client.get(f"{_SESSION}/artifacts/prd")
    assert view.status_code == 200 and "Leave approval — PRD" in view.text
    assert client.get(f"{_SESSION}/artifacts/prd?download=1").headers["content-disposition"].endswith('filename="prd.md"')


def test_downloading_an_unknown_artifact_type_refuses_rather_than_inventing_a_filename(client, with_provider):
    """#270: the route fell back to `f"{artifact_type}.md"` for a type `ARTIFACT_FILENAMES` does not know (invariant 3)."""
    _discovered(client, with_provider, _RESOLVED)
    resp = client.get(f"{_SESSION}/artifacts/bogus?download=1")
    assert resp.status_code == 400 and "bogus" in resp.text


def test_a_saved_artifact_reads_as_a_document_not_as_source(client, with_provider):
    """The money screen is rendered; the download is the saved bytes, which really are Markdown (#235)."""
    _discovered(client, with_provider, _RESOLVED, BRIEF_REPLY)
    _generate(client, "brief")
    document = client.get(f"{_SESSION}/artifacts/brief").text.split('class="artifact"')[1]
    assert "<h1>" in document and "<strong>" in document, "the document has no structure"
    assert "# Decision Brief" not in document and "**" not in document, "a Markdown marker reached the reader"
    saved = ArtifactService().show("leave-approval", "brief")
    assert client.get(f"{_SESSION}/artifacts/brief?download=1").text == saved and "# Decision Brief" in saved


def test_hostile_markup_in_a_saved_artifact_is_shown_not_executed(client, with_provider):
    """The rendered page turns Jinja's autoescape off for this one value (#235)."""
    _discovered(client, with_provider, _RESOLVED, BRIEF_REPLY)
    _generate(client, "brief")
    saved = canonical_dir("leave-approval") / "artifacts" / "solution-assessment.md"
    saved.write_text("# Owned\n\n<script>alert(1)</script>\n", encoding="utf-8")
    page = client.get(f"{_SESSION}/artifacts/brief").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page, "the tag ran, or was dropped rather than shown"
    assert "<h1>Owned</h1>" in page, "must fire: the document really was re-rendered"


def test_answers_report_what_changed_and_what_needs_review(client, with_provider):
    """A PRD consumes business_rules; a material change there names the area moved, flags the PRD and marks it stale."""
    _discovered(client, with_provider, _ASKING, PRD_REPLY, _CONVERGED)
    _generate(client, "prd")
    r = _answer(client, answers="explicit now")
    assert r.status_code == 200 and "What changed" in r.text and "Business rules" in r.text
    assert "Needs review" in r.text and "PRD" in r.text  # the document the change reaches
    assert "Needs updating" in _page(client)


def test_the_web_offers_every_artifact_the_service_can_generate(client, with_provider, monkeypatch):
    # The Web used to keep its own two-entry list while the service could produce five.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    with_provider(CRITERIA_REPLY)
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = _page(client)
    assert "Generate decision brief" in page and "More documents" in page   # the one primary call to action
    for label in ("PRD", "Acceptance criteria", "Delivery epic", "Release notes"):
        assert label in page, f"no generate button for {label}"
    assert _generate(client, "criteria").status_code == 200
    saved = client.get(f"{_SESSION}/artifacts/criteria")
    assert saved.status_code == 200 and "acceptance criteria" in saved.text


def test_the_two_former_analyses_are_generatable_and_an_unknown_type_still_is_not(client, with_provider):
    # Until #519 this test pinned the opposite for `stories`.
    stories = json.dumps({"stories": [{"id": "S1", "title": "Request leave"}]})
    estimate = json.dumps({"items": [{"story_id": "S1", "title": "Request leave", "complexity": "S", "days_low": 1, "days_high": 2}]})
    with_provider(stories, stories, estimate)
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    assert _generate(client, "risk-register").status_code == 400
    assert _generate(client, "stories").status_code == 200 and _generate(client, "estimate").status_code == 200
    saved = client.get(f"{_SESSION}/artifacts/estimate")
    assert saved.status_code == 200 and "Request leave" in saved.text
    assert "Estimate" in _page(client) and "User stories" in _page(client)


# ── the MVP flow: paste → read → answer the few questions → see what moved → one decision brief ──


def test_home_leads_with_the_request_form(client):
    page = client.get("/").text
    assert 'name="request_text"' in page and ("Save request" in page or "Analyse request" in page)
    assert page.index("Advanced settings") < page.index('id="provider"')   # the provider is a setting, not a question
    r = client.get("/sessions/new", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"


def test_home_survives_a_session_with_no_model(client):
    """A captured-but-unanalysed session is a normal row."""
    DiscoveryService().create_only("A leave approval system", slug="not-analysed-yet")
    r = client.get("/")
    assert r.status_code == 200 and "Awaiting analysis" in r.text and "not-analysed-yet" not in r.text.split("Recent")[0]


def test_only_the_top_questions_lead_the_page(client, with_provider):
    six = [{"q": f"Question {i}?", "slot": "business_rules", "why": "it moves the build"} for i in range(6)]
    _discovered(client, with_provider, engine_reply(questions=six, problem=HIGH_EXPLICIT))
    page = _page(client)
    lead = page.split("Traceability details")[0]
    assert lead.count("Why it matters") == 5 and "1 further question" in lead   # five lead; the sixth is accounted for
    assert "Question 5?" in page                       # still there, under traceability


def test_readiness_is_one_action_state_with_reasons(client, with_provider):
    _discovered(client, with_provider, engine_reply(converged=True, problem=HIGH_INFERRED))   # a high-impact topic unresolved
    page = _page(client)
    assert "Not ready to produce a reliable scope" in page and "Real problem" in page   # the reason, by its label
    assert "Ready for a first decision brief" not in page


def test_a_threshold_only_invalidation_is_not_a_false_all_clear_on_the_web():
    """Codex review on #604: `impact_view` ignored `invalidated_thresholds`."""
    result = UpdateResult(status="applied", revision=2, changed_slots=["constraints"],
                          invalidated_thresholds=["Budget exceeded"], readiness=Readiness(True, []))
    view = impact_view(result)
    assert view["needs_review"] is True and view["thresholds_to_review"] == ["Budget exceeded"]


def test_an_unrelated_change_leaves_a_document_alone(client, with_provider):
    """The differentiator cuts both ways: a change that misses a document's dependencies must not flag it."""
    _discovered(client, with_provider, engine_reply(problem=HIGH_EXPLICIT, reporting=_LOW), PRD_REPLY,
                engine_reply(converged=True, problem=HIGH_EXPLICIT, reporting={**_LOW, "completeness": 80, "confidence": "explicit"}))
    _generate(client, "prd")
    r = _answer(client, answers="Reporting is a weekly export.")
    assert "Needs review" not in r.text and "Needs updating" not in _page(client)


def test_a_changed_answer_moves_the_scope_and_the_brief(client, with_provider):
    """The canonical scope-change story, end to end."""
    before = {"completeness": 40, "confidence": "inferred", "impact": "high", "value": "Both systems stay in sync during the pilot."}
    after = {**before, "completeness": 90, "confidence": "explicit", "value": "One-time migration; the legacy system becomes read-only."}
    session = "/sessions/leave-migration"
    _discovered(client, with_provider, engine_reply(problem=HIGH_EXPLICIT, integrations=before), BRIEF_REPLY,
                engine_reply(converged=True, problem=HIGH_EXPLICIT, integrations=after),
                slug="leave-migration", request_text="Migrate leave to the new HR system")
    assert _generate(client, "brief", session).status_code == 200 and "Up to date" in _page(client, session)
    r = _answer(client, session, answers="The migration is one-time — the legacy system becomes read-only.", expected_revision="2")
    assert "What changed" in r.text and "Integrations &amp; notifications" in r.text   # the topic, in the reader's words
    assert "Decision brief" in r.text and "Needs updating" in _page(client, session)   # …and the document it reaches


def test_a_resolved_session_can_still_be_refined(client, with_provider):
    """Questions run out; the conversation does not."""
    _discovered(client, with_provider, _RESOLVED, _CONVERGED)
    page = _page(client)
    assert "No question left that would change the solution" in page and 'name="answers"' in page
    r = _answer(client, answers="One more thing — contractors are out of scope.")
    assert r.status_code == 200 and "What changed" in r.text
