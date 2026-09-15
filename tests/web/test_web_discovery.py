"""Requivo Web discovery and artifact flows: the workflow the product leads with, end to end.

Split out of `test_web.py` by #142. Every test here drives the provider seam with a fake — create,
answer, generate, regenerate — including the MVP flow's own rendering assertions, which stay with the
flow they are a step of rather than moving to a file about markup.

The busy-rule (#50), honest-wait (#236) and failed-analysis-recovery (#207) tests split out to
`test_web_generation_busy.py` and `test_web_analysis_recovery.py` by #555, once this file crossed the
800-line ceiling — each is a distinct subject in its own right, not a template concern.

Offline, isolated workspace per test; the fixtures and the seeded-session helper live in
`tests/web/conftest.py`.
"""

from __future__ import annotations

import json

from requivo.core.persistence import canonical_dir
from requivo.services.artifacts import ArtifactService
from tests.web.conftest import (
    BRIEF_REPLY,
    CRITERIA_REPLY,
    HIGH_EXPLICIT,
    HIGH_INFERRED,
    PRD_REPLY,
    _make_session,
    engine_reply,
)

# ── discovery (mocked provider) ───────────────────────────────────────────────

def test_create_session_runs_discovery(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    r = client.post("/sessions", data={"request_text": "A leave approval system",
                                       "slug": "leave-approval", "provider": "anthropic"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"
    page = client.get("/sessions/leave-approval")
    assert "What could change the solution" in page.text
    assert "How are exceptions handled" in page.text


def test_create_only_then_run_discovery(client, with_provider):
    # No LLM at creation…
    r = client.post("/sessions", data={"request_text": "A leave system", "slug": "leave-only",
                                       "provider": "create_only"}, follow_redirects=False)
    assert r.status_code == 303
    pending = client.get("/sessions/leave-only")
    assert "Awaiting analysis" in pending.text
    # …then discovery on demand.
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT))
    r = client.post("/sessions/leave-only/discover", follow_redirects=False)
    assert r.status_code == 303
    assert "No question left that would change the solution" in client.get("/sessions/leave-only").text


def test_answers_apply_and_return_status_partial(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text  # the swapped body leads with the impact of the answers


def test_revision_conflict_is_clean(client, with_provider):
    # One reply, for the discovery. The answers turn never reaches the provider: since #205 the
    # certain conflict is detected against the snapshot *before* the call, so a second scripted reply
    # would go unused. `test_a_stale_answers_form_is_refused_before_the_provider_is_paid` is what pins
    # that position; this one pins that the refusal is still a clean 409 rather than a traceback.
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "…", "expected_revision": "999"})  # stale expectation
    assert r.status_code == 409  # RevisionConflictError → clean 409, not a traceback


def test_a_hand_crafted_answers_post_on_a_revision_zero_session_is_refused(client, with_provider):
    """The web's own door onto #421: the rendered page never shows the answers form at revision
    0, but invariant 14 says the service is the boundary, not the form -- a client posting straight
    to `POST /sessions/{slug}/answers` with `expected_revision=0` is as real a caller as the
    browser. Before the fix this reached the provider with the typed answers in no kwarg of the
    call; the service gate closes that regardless of which surface reaches it."""
    fake = with_provider()  # no reply queued — a call reaching the provider fails loudly, not silently
    client.post("/sessions", data={"request_text": "x", "slug": "bare-rev0", "provider": "create_only"})
    assert fake.calls == []                            # create_only never touches the provider

    r = client.post("/sessions/bare-rev0/answers",
                    data={"answers": "…", "expected_revision": "0"})

    assert r.status_code == 409                         # RevisionConflictError → clean 409
    assert fake.calls == []                             # refused before any provider construction
    assert "requivo answer" not in r.text                # the remedy no longer routes back into this path


def test_a_stale_answers_form_is_refused_before_the_provider_is_paid(client, with_provider):
    """A conflict that is already certain must not be discovered by paying for it (#205).
    `answer()` read a fresh snapshot, ran the paid call, and only then applied against
    `expected_revision` -- so a second tab moving the session past that revision was billed for a
    full turn thrown away (invariant 13's "the check is cheap and the call is not"). Asserted on
    the call count, not the 409: the refusal already happened at the apply."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    spent_on_discovery = len(fake.calls)

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "…", "expected_revision": "999"})

    assert r.status_code == 409
    assert len(fake.calls) == spent_on_discovery, (
        f"{len(fake.calls) - spent_on_discovery} provider call(s) were billed for a turn whose "
        f"conflict was certain before the call — the gate is downstream of the reasoning")


def test_a_matching_answers_form_still_reaches_the_provider(client, with_provider):
    """The must-fire half of the test above (#205).

    `len(fake.calls) == spent_on_discovery` is also true of a gate that refuses *every* answers turn,
    of a route that stopped working and of a harness that never posted — so without this control the
    pre-call check could be wrong in the widening-refusal direction and still look green.
    """
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                         engine_reply(converged=True, problem=HIGH_EXPLICIT,
                                      business_rules=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    spent_on_discovery = len(fake.calls)

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert len(fake.calls) == spent_on_discovery + 1, (
        "a form rendered at the current revision has to reach the provider — the pre-call check "
        "refused a turn it should have let through")


# ── artifacts ─────────────────────────────────────────────────────────────────

def test_generate_brief_and_prd_and_view(client, with_provider):
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY, PRD_REPLY)
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    assert client.post("/sessions/leave-approval/artifacts/brief").status_code == 200
    assert client.post("/sessions/leave-approval/artifacts/prd").status_code == 200
    # both are listed, fresh
    page = client.get("/sessions/leave-approval")
    assert "Decision brief" in page.text and "PRD" in page.text and "Up to date" in page.text
    # view + download the PRD
    view = client.get("/sessions/leave-approval/artifacts/prd")
    assert view.status_code == 200 and "Leave approval — PRD" in view.text
    dl = client.get("/sessions/leave-approval/artifacts/prd?download=1")
    assert dl.headers["content-disposition"].endswith('filename="prd.md"')


def test_downloading_an_unknown_artifact_type_refuses_rather_than_inventing_a_filename(client, with_provider):
    """#270. The route used to fall back to `f"{artifact_type}.md"` for a type
    `ARTIFACT_FILENAMES` does not know, against invariant 3 -- a fallback that could never fire,
    since `artifacts.show()` already raises `UnknownArtifactTypeError` for this input. Must-fire
    half: an unknown type must produce the structured refusal (400), not a guessed filename.
    `test_generate_brief_and_prd_and_view` is the must-not-fire control."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})

    resp = client.get("/sessions/leave-approval/artifacts/bogus?download=1")

    assert resp.status_code == 400
    assert "bogus" in resp.text


def test_a_saved_artifact_reads_as_a_document_not_as_source(client, with_provider):
    """The money screen, rendered (#235). The decision brief is the product's stated primary
    deliverable -- "the one to hand someone who has a request and half an hour" -- and it was
    served as literal `# Decision Brief` and `**Objective:**` inside a monospace code block. The
    reader who must not have to learn the engine's model was handed Markdown source at the exact
    moment the product delivers its value."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/brief")

    page = client.get("/sessions/leave-approval/artifacts/brief").text
    document = page.split('class="artifact"')[1]

    assert "<h1>" in document and "<strong>" in document, "the document has no structure"
    assert "# Decision Brief" not in document, "a heading marker reached the reader"
    assert "**" not in document, "a bold marker reached the reader"


def test_downloading_an_artifact_still_serves_the_bytes_that_were_saved(client, with_provider):
    """Rendering is a *view*. The file is the artifact, and it is what the reader hands on — to a
    tracker, to a colleague, to the CLI — so the download has to be byte-identical to what
    `ArtifactService` saved. A renderer that quietly reformatted the download would break every
    consumer that is not a browser."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/brief")

    saved = ArtifactService().show("leave-approval", "brief")
    downloaded = client.get("/sessions/leave-approval/artifacts/brief?download=1")

    assert downloaded.text == saved
    assert "# Decision Brief" in saved, (
        "must fire: the saved file really is Markdown, so the assertion above is comparing something")


def test_hostile_markup_in_a_saved_artifact_is_shown_not_executed(client, with_provider):
    """The rendered page turns Jinja's autoescape off for this one value, so escaping has to be
    complete before it gets there (#235). Written to disk directly -- the honest reproduction,
    since an artifact file is a file the user owns and can edit, written by a language model.
    `test_render_html.py` owns the per-construct proof; this is the end-to-end check that the page
    actually goes through it."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/brief")

    saved = canonical_dir("leave-approval") / "artifacts" / "solution-assessment.md"
    saved.write_text("# Owned\n\n<script>alert(1)</script>\n", encoding="utf-8")

    page = client.get("/sessions/leave-approval/artifacts/brief").text

    assert "<script>alert(1)</script>" not in page, "the page would run markup out of a saved file"
    assert "&lt;script&gt;" in page, "the tag was dropped rather than shown, which hides the tampering"
    assert "<h1>Owned</h1>" in page, (
        "must fire: the document really was re-rendered, so the assertions above are about this file")


def test_related_change_marks_artifact_stale(client, with_provider):
    # generate a PRD (consumes business_rules), then a material change to business_rules → PRD stale.
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),   # discover
        PRD_REPLY,                                                           # generate prd @ rev1
        engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),  # answer
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    client.post("/sessions/leave-approval/answers", data={"answers": "explicit now", "expected_revision": "1"})
    assert "Needs updating" in client.get("/sessions/leave-approval").text


def test_the_web_offers_every_artifact_the_service_can_generate(client, with_provider, monkeypatch):
    # The Web used to keep its own two-entry list while the service could produce five. The buttons
    # still come from the service's vocabulary — what changed is their weight: the decision brief is
    # the primary action, the rest live under "More documents". Available, not equal.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    with_provider(CRITERIA_REPLY)
    _make_session("leave-approval", problem=HIGH_EXPLICIT)

    page = client.get("/sessions/leave-approval").text
    assert "Generate decision brief" in page          # the one primary call to action
    assert "More documents" in page
    for label in ("PRD", "Acceptance criteria", "Delivery epic", "Release notes"):
        assert label in page, f"no generate button for {label}"

    assert client.post("/sessions/leave-approval/artifacts/criteria").status_code == 200
    saved = client.get("/sessions/leave-approval/artifacts/criteria")
    assert saved.status_code == 200 and "acceptance criteria" in saved.text


def test_the_two_former_analyses_are_generatable_and_an_unknown_type_still_is_not(client, with_provider):
    # Until #519 this test pinned the opposite for `stories`: it reasoned but produced no document, so
    # the route refused it. Both analyses save a document now (`decision: the-estimate-graduates`),
    # and `estimate` saves the stories it was reasoned against beside itself, from one snapshot. The
    # unknown-type refusal is the must-fire half: the fake would raise if reached, since it holds no
    # reply for that call.
    with_provider(
        json.dumps({"stories": [{"id": "S1", "title": "Request leave"}]}),
        json.dumps({"stories": [{"id": "S1", "title": "Request leave"}]}),
        json.dumps({"items": [{"story_id": "S1", "title": "Request leave", "complexity": "S",
                               "days_low": 1, "days_high": 2}]}),
    )
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    assert client.post("/sessions/leave-approval/artifacts/risk-register").status_code == 400

    assert client.post("/sessions/leave-approval/artifacts/stories").status_code == 200
    assert client.post("/sessions/leave-approval/artifacts/estimate").status_code == 200
    saved = client.get("/sessions/leave-approval/artifacts/estimate")
    assert saved.status_code == 200 and "Request leave" in saved.text
    page = client.get("/sessions/leave-approval").text
    assert "Estimate" in page and "User stories" in page


# ── the MVP flow ──────────────────────────────────────────────────────────────
# One workflow leads the product: paste a request → read what was understood → answer the few
# questions that could change the solution → see what moved → generate one decision brief. These
# tests pin that flow's shape, not just that its routes respond.

def test_home_leads_with_the_request_form(client):
    page = client.get("/").text
    assert 'name="request_text"' in page
    assert "Save request" in page or "Analyse request" in page
    # The provider is a setting, not a question the reader has to answer: it exists, but only inside
    # the advanced disclosure. Position is the honest assertion here — server-rendered <details>
    # keeps its contents in the markup, so "not present" would be a lie.
    assert page.index("Advanced settings") < page.index('id="provider"')


def test_home_survives_a_session_with_no_model(client):
    """A captured-but-unanalysed session is a normal row. It used to take the whole list down: the row
    builder asked for a status, `status()` needs a model, and one 404 became the home page's."""
    from requivo.services.discovery import DiscoveryService
    DiscoveryService().create_only("A leave approval system", slug="not-analysed-yet")
    r = client.get("/")
    assert r.status_code == 200
    assert "Awaiting analysis" in r.text and "not-analysed-yet" not in r.text.split("Recent")[0]


def test_sessions_new_redirects_to_the_request_form(client):
    r = client.get("/sessions/new", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"


def test_only_the_top_questions_lead_the_page(client, with_provider):
    six = [{"q": f"Question {i}?", "slot": "business_rules", "why": "it moves the build"}
           for i in range(6)]
    with_provider(engine_reply(questions=six, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    lead = page.split("Traceability details")[0]
    assert lead.count("Why it matters") == 5           # five lead the page…
    assert "1 further question" in lead                # …and the sixth is accounted for, not dropped
    assert "Question 5?" in page                       # still there, under traceability


def test_no_raw_slot_ids_reach_the_page(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    # Every slot id with an underscore — the ones no human label contains, so a hit is the engine's
    # vocabulary leaking into the reader's.
    for slot_id in ("business_rules", "config_vs_custom", "success_metrics", "current_process",
                    "edge_cases", "business_objects"):
        assert slot_id not in page, f"slot id {slot_id!r} rendered to the reader"


def test_readiness_is_one_action_state_with_reasons(client, with_provider):
    with_provider(engine_reply(converged=True, problem=HIGH_INFERRED))   # a high-impact topic unresolved
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    assert "Not ready to produce a reliable scope" in page
    assert "Real problem" in page                       # the reason, by its human label
    assert "Ready for a first decision brief" not in page


def test_answers_report_what_changed_and_what_needs_review(client, with_provider):
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
        PRD_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text
    assert "Business rules" in r.text                   # the area that moved, by its human label
    assert "Needs review" in r.text and "PRD" in r.text  # the document the change reaches


def test_a_threshold_only_invalidation_is_not_a_false_all_clear_on_the_web():
    """Codex review on #604: `impact_view` ignored `invalidated_thresholds`, so a change that
    unseats only a threshold rendered as "nothing to review" -- the primary screen stating a fact
    it cannot support (CLAUDE.md: "the counts are always stated"). A direct unit test, not an
    end-to-end flow: `impact_view` is a pure function over `UpdateResult`."""
    from requivo.services.sessions import Readiness, UpdateResult
    from requivo.web.viewmodels.status import impact_view

    result = UpdateResult(status="applied", revision=2, changed_slots=["constraints"],
                          invalidated_thresholds=["Budget exceeded"],
                          readiness=Readiness(True, []))
    view = impact_view(result)
    assert view["needs_review"] is True, "a threshold-only invalidation must not read as all-clear"
    assert view["thresholds_to_review"] == ["Budget exceeded"]


def test_an_unrelated_change_leaves_a_document_alone(client, with_provider):
    """The differentiator cuts both ways: a change that misses a document's dependencies must not
    flag it. Reporting is not one of the PRD's inputs, so moving it changes nothing the PRD rests on."""
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, reporting={"completeness": 10, "confidence": "empty",
                                                       "impact": "low"}),
        PRD_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT,
                     reporting={"completeness": 80, "confidence": "explicit", "impact": "low"}),
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Reporting is a weekly export.", "expected_revision": "1"})
    assert "Needs review" not in r.text
    assert "Needs updating" not in client.get("/sessions/leave-approval").text


def test_a_changed_answer_moves_the_scope_and_the_brief(client, with_provider):
    """The canonical scope-change story, end to end: a two-way sync during migration is decided, a
    brief is written from it, then the answer changes to a one-time cutover — and the integration
    topic moves, the brief is marked as needing an update, and the page says so."""
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT,
                     integrations={"completeness": 40, "confidence": "inferred", "impact": "high",
                                   "value": "Both systems stay in sync during the pilot."}),
        BRIEF_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT,
                     integrations={"completeness": 90, "confidence": "explicit", "impact": "high",
                                   "value": "One-time migration; the legacy system becomes read-only."}),
    )
    client.post("/sessions", data={"request_text": "Migrate leave to the new HR system",
                                   "slug": "leave-migration", "provider": "anthropic"})
    assert client.post("/sessions/leave-migration/artifacts/brief").status_code == 200
    assert "Up to date" in client.get("/sessions/leave-migration").text

    r = client.post("/sessions/leave-migration/answers",
                    data={"answers": "The migration is one-time — the legacy system becomes read-only.",
                          "expected_revision": "2"})
    assert "What changed" in r.text
    assert "Integrations &amp; notifications" in r.text  # the topic that moved, in the reader's words
    assert "Decision brief" in r.text                   # …and the document it reaches
    assert "Needs updating" in client.get("/sessions/leave-migration").text


def test_a_resolved_session_can_still_be_refined(client, with_provider):
    """Questions run out; the conversation does not. Once the engine returns no question, the
    page says so -- and used to remove the answer box with the question list it lived in, so a
    session ready for a first decision brief could no longer be told anything: not a correction,
    not a constraint that arrived late, not scope the client
    added after the fact. `answer()` never needed a question to fold text into the model."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})

    page = client.get("/sessions/leave-approval").text
    assert "No question left that would change the solution" in page
    assert 'name="answers"' in page, "a resolved session still has to be answerable"

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "One more thing — contractors are out of scope.",
                          "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text


# ── one generation at a time (#50) ────────────────────────────────────────────
