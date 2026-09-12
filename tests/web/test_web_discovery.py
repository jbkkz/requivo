"""Requivo Web discovery and artifact flows: the workflow the product leads with, end to end.

Split out of `test_web.py` by #142. Every test here drives the provider seam with a fake — create,
answer, generate, regenerate — including the MVP flow's own rendering assertions, which stay with the
flow they are a step of rather than moving to a file about markup.

The busy-rule tests (#50) are here for the same reason: what they protect is a second paid
generation, not a template.

Offline, isolated workspace per test; the fixtures and the seeded-session helper live in
`tests/web/conftest.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from requivo.core.persistence import canonical_dir
from requivo.providers.errors import EngineError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS
from requivo.web.templating import TEMPLATES_DIR
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
    """The web's own door onto #421, named in its acceptance criteria: the rendered page never shows
    the answers form at revision 0 (`routes/sessions.py`'s "offer to run discovery" branch), but
    invariant 14 says the service is the boundary, not the form — a client that posts straight to
    `POST /sessions/{slug}/answers` with `expected_revision=0` is exactly as real a caller as the
    browser. Before the fix this reached the provider (`fake.calls == 1`) with the typed answers
    appearing in no kwarg of the call, per the issue's own fake-client probe; the service gate closes
    that regardless of which surface reaches it, so no route-level check is needed here."""
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

    `answer()` took the caller's `expected_revision`, read a fresh snapshot, ran the paid analyze
    call, and only then applied with that precondition. When a second tab, the CLI or a back-button
    form had moved the session past the revision the form was rendered at, the conflict was decided
    the moment the snapshot was read — and the user was still billed for a full turn whose result was
    guaranteed to be thrown away. This is invariant 13's own sentence, "the check is cheap and the
    call is not", applied to the gate it was written for and not to this one.

    **The assertion is the call count, not the 409.** The refusal already happened before the fix, at
    the apply, so a test asserting only the status code was green on the defect — the same trap
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying` documents for the
    revision-zero gate.
    """
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
    """#270. The route used to fall back to `f"{artifact_type}.md"` for a type `ARTIFACT_FILENAMES`
    does not know, against the repo's own refuse-don't-guess rule (invariant 3) -- and the fallback
    could never actually fire, because `artifacts.show()` (called one line above it) already raises
    `UnknownArtifactTypeError` for exactly this input. This is the must-fire half: an unknown type
    must produce the existing structured refusal (400) rather than a 200 with a guessed filename.
    `test_generate_brief_and_prd_and_view` above is the must-not-fire control -- a known type still
    gets the real filename."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})

    resp = client.get("/sessions/leave-approval/artifacts/bogus?download=1")

    assert resp.status_code == 400
    assert "bogus" in resp.text


def test_a_saved_artifact_reads_as_a_document_not_as_source(client, with_provider):
    """The money screen, rendered (#235).

    The decision brief is the product's stated primary deliverable — "the one to hand someone who has
    a request and half an hour" — and it was served as literal `# Decision Brief` and `**Objective:**`
    inside a monospace code block. The audience the web vocabulary exists for, the reader who must not
    have to learn the engine's model, was handed Markdown source at the exact moment the product
    delivers its value.
    """
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
    """The rendered page turns Jinja's autoescape off for this one value, so the escaping has to be
    complete before it gets there (#235).

    Written to disk directly, which is the honest reproduction: an artifact file is a file the user
    owns and can edit, and its content was written by a language model. `test_render_html.py` owns the
    per-construct proof; this is the end-to-end one that says the page actually goes through it.
    """
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
    """Questions run out; the conversation does not.

    Once the engine returns no question, the page says so — and used to remove the answer box with the
    question list it lived in, so a session that reached "ready for a first decision brief" could no
    longer be told anything: not a correction, not a constraint that arrived late, not scope the client
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

_BUSY_HARNESS = Path(__file__).parent / "busy_harness.js"
_ERROR_SWAP_HARNESS = Path(__file__).parent / "error_swap_harness.js"
_ELAPSED_HARNESS = Path(__file__).parent / "elapsed_harness.js"


def _busy_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a minimal DOM and return what it did, step by step.

    A `TestClient` runs no JavaScript, so without this the only assertable thing about #50 is that some
    literal string appears in the asset — which pins the implementation's spelling instead of its
    effect, and passes just as well against code that disables nothing.

    Node is the one thing here that is not guaranteed. When it is missing this **skips loudly** and
    names what went unasserted, rather than leaving a green run implying coverage it does not have.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the page-wide busy rule in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#50)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_BUSY_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_one_generation_at_a_time_is_the_pages_rule_not_the_forms():
    """Mutual exclusion is a property of the page, not of a form (#50).

    Every generator under *More documents* is its own form posting to the same `#artifacts-region`, so
    a second click while the first call is in flight buys a second paid provider call whose result the
    first swap then discards. `markLoading` disabled only the submitting form's own button, which left
    every sibling live — the state the reader reported, and reproduced by this harness against the
    shipped asset as `disabled=[True, False, False]`.

    Every assertion below drives the real file. The two that matter most are the ones a weaker shape
    misses: that the *first* response does not unmute the page while a second call is still running (a
    boolean flag passes the simple case and fails this one), and that the state is re-asserted over
    markup a swap just brought in, which carries no `disabled` attribute of its own.
    """
    t = _busy_timeline()

    assert t["initial"]["disabled"] == [False, False, False]

    # must fire. A harness that dispatched nothing, or an app.js that muted nothing, would leave these
    # False — and every later assertion would then be about silence rather than about the rule.
    assert all(t["one in flight"]["disabled"]), (
        "one request in flight has to mute every submit button on the page, not only the one that was "
        "clicked — the sibling generator buttons are exactly what buys the duplicate call")
    assert t["one in flight"]["busy"] is True, "the page has to say it is working, not only look it"
    assert all(t["two in flight"]["disabled"])

    assert all(t["first finished, second still running"]["disabled"]), (
        "the first response must not hand the reader live buttons while a second call is still in "
        "flight — that needs a count, not a flag")
    assert not any(t["both finished"]["disabled"]), "the page has to come back when the work is done"
    assert t["both finished"]["busy"] is False

    # A swap replaces the region mid-flight; the incoming markup carries no disabled attribute.
    assert not any(t["swapped in, before afterSwap"]["disabled"]), (
        "precondition: swapped-in buttons really do arrive enabled, so the next assertion is repairing "
        "something rather than observing a no-op")
    assert all(t["swapped in, after afterSwap"]["disabled"]), (
        "htmx:afterSwap has to re-assert the busy state over markup the swap just brought in")
    assert not any(t["after the swap, request finished"]["disabled"])

    # bfcache: the shipped asset left a button disabled forever here, because the reset it does have
    # only ever touched the progress bar.
    assert all(t["in flight before pageshow"]["disabled"])
    assert not any(t["after pageshow"]["disabled"]), (
        "returning to a cached page must not restore it with its buttons still muted")


def test_the_generator_forms_all_target_one_region_which_is_why_the_rule_is_page_wide(client,
                                                                                     monkeypatch):
    """The server-side half of #50, and the reason the rule cannot live in a form.

    The JS test above needs Node; this one needs nothing, and pins the precondition that makes the
    defect possible — several sibling forms whose responses all land on the same target. If a later
    change gave each generator its own region, that would be a different (and also valid) fix, and this
    test is where the argument would surface instead of the page-wide rule quietly becoming pointless.

    **It is green on both sides of this change, deliberately.** It characterises the shape the fix
    reasons about; it does not detect the fix's absence, and it is not evidence that the fix works.
    That evidence is the Node test above, which fails on the shipped asset as `[True, False, False]`.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = client.get("/sessions/leave-approval").text
    targets = re.findall(r'hx-post="/sessions/leave-approval/artifacts/[^"]+"[^>]*?'
                         r'hx-target="([^"]+)"', page, re.S)
    assert len(targets) > 1, "expected several generator forms on the page, found: " + repr(targets)
    assert set(targets) == {"#artifacts-region"}, (
        "every generator form posts to one region, so their responses collide — mutual exclusion has "
        "to be page-wide (#50)")


def test_every_rendered_button_is_reachable_by_the_page_wide_busy_rule(client, monkeypatch):
    """The busy rule selects `button[type="submit"]`, so a button without that attribute escapes it.

    HTML makes `type="submit"` the default for a bare `<button>` inside a form, which is exactly what
    makes this worth pinning: such a button submits, buys a provider call, and is invisible to the one
    selector that is supposed to mute it. It would reproduce #50 for that button alone while every test
    here stayed green — the same shape as the original defect, one attribute lower.

    A CSS-side rule cannot be asserted from Python, and the selector lives in `app.js`; what is
    assertable here is the other half of the contract, which is that the markup holds up its end.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    pages = {path: client.get(path).text for path in ("/", "/sessions/leave-approval")}

    for path, html in pages.items():
        opening_tags = re.findall(r"<button\b[^>]*>", html)
        # must fire: these pages really do render buttons, so the loop below is checking something
        # rather than iterating over nothing.
        assert opening_tags, f"expected buttons on {path}, found none — this assertion saw nothing"
        for tag in opening_tags:
            assert 'type="submit"' in tag, (
                f'{path} renders a button with no explicit type="submit", so the page-wide busy rule '
                f"in static/js/app.js cannot reach it and it can still buy a provider call: {tag}")


# ── an honest wait (#236) ────────────────────────────────────────────────────


def _elapsed_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a fake clock and report what the status text said,
    second by second.

    Skips loudly without node, for the reason `_busy_timeline` gives: a green run must not imply
    coverage of browser behaviour nothing else in this suite can see.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the elapsed-time signal in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour over time and nothing else in "
                    "this suite can see it (#236)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_ELAPSED_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, nothing observed: " + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_a_long_call_says_so_after_ten_seconds_rather_than_looking_stuck():
    """A wait that outlives its own copy has to keep speaking (#236).

    The provider call is synchronous and the page blocks on it; this repo's own invariants 2 and 12
    put that at "seconds to minutes". The status text was a single static label, so past the point
    where a first-time reader expected an answer the page looked identical to one that had hung — and
    the natural next move on a blocked create is to reload or re-paste, which buys a second session
    and a second paid call.

    **The first assertion is the control, and it is the one that matters.** "The text changed" is
    trivially satisfiable by a page that rewrites its label on every tick from the start, which would
    say nothing about a long call and would be noise on a short one. The signal is that it holds still
    through the first nine seconds and only then starts reporting.
    """
    t = _elapsed_timeline()
    started = t["request started"]["text"]
    eleven = t["eleven seconds in"]["text"]

    # must fire: nothing changes while the wait is still within what the copy promised.
    assert t["nine seconds in"]["text"] == started, (
        "the status text moved before the wait was long enough to need explaining — a label that "
        "always churns tells a reader nothing about a call that is genuinely slow")

    assert eleven != started, (
        "past ten seconds the page still said exactly what it said at second one, which is what a "
        "hung page also says")
    assert any("11s" in line for line in eleven), (
        "the elapsed seconds have to be visible, got: " + repr(eleven))
    assert any("20s" in line for line in t["twenty seconds in"]["text"]), (
        "the signal has to keep moving — one update and then stillness is a page that hung later")

    # The original label comes back, so a finished turn does not leave "still working" on screen.
    assert t["request finished"]["text"] == started
    assert t["request finished"]["liveTimers"] == 0, "the clock kept ticking after the call finished"

    # A second turn counts from zero. Resuming the first one's total would open by claiming this
    # call has already been running for half a minute.
    assert t["second request, three seconds in"]["text"] == started

    assert t["after pageshow"]["liveTimers"] == 0, (
        "returning to a cached page must not leave a timer running against a request that is over")


def test_no_provider_backed_button_still_promises_a_few_seconds():
    """The copy and the measurement have to agree (#236).

    "Takes a few seconds." sat beside both provider-backed buttons while CLAUDE.md's invariant 2 said
    "Provider calls take seconds to minutes" and invariant 12 said "the call (which takes minutes)".
    A promise the product's own documentation contradicts is worse than no promise: it sets an
    expectation that expires, and what a reader does when it expires is resubmit.

    Scanned as raw text over the templates rather than over a rendered page, because the claim is
    about the copy that ships — a page rendered with no provider configured does not show it at all.
    """
    offenders = [p.name for p in sorted(TEMPLATES_DIR.rglob("*.html"))
                 if "a few seconds" in p.read_text(encoding="utf-8")]
    assert offenders == [], (
        "these templates still promise 'a few seconds' for a call this repo documents as taking "
        "seconds to minutes: " + repr(offenders))


def test_the_no_js_path_still_states_how_long_it_will_take(client, monkeypatch):
    """The elapsed counter is an enhancement; the honest static copy is the floor.

    With JavaScript off there is no counter, so the page has to have said something true before the
    call started. This is the must-fire half of the test above: deleting the copy outright would
    satisfy "no page promises a few seconds" and leave the reader with nothing at all.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    assert "Usually under a minute" in client.get("/").text, (
        "the create form states no duration at all, so a reader with JS off learns nothing")

    client.post("/sessions", data={"request_text": "x", "slug": "later",
                                   "provider": "create_only"})
    assert "Usually under a minute" in client.get("/sessions/later").text, (
        "the deferred-analysis page runs the same paid call and has to make the same promise")


def _swap_decisions() -> dict[int, dict]:
    """Execute the real `static/js/app.js` against htmx's own swap gate and report, per status,
    whether the page would render the response.

    Skips loudly without node, for the reason `_busy_timeline` gives: a green run must not imply
    coverage of browser behaviour nothing in this suite can otherwise see.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the error-swap opt-in in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#203)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_ERROR_SWAP_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    out = json.loads(proc.stdout)
    result = {row["status"]: row for row in out["decisions"]}
    result["flash_cleared"] = out["flashClearedOnNewRequest"]
    return result


def test_error_responses_are_swapped_into_the_page_rather_than_dropped():
    """Every 4xx/5xx fragment this app builds was invisible in a real browser (#203).

    The vendored htmx swaps only 200-399, and nothing opted in. So a revision conflict from a second
    tab, the 413 that #30 built to keep the reader's typed answers, and a 502 after a minutes-long
    *paid* generation all produced the same thing: the progress bar completed, the buttons came back,
    the page did not change, and nothing was said. On the paid one the natural next move is to click
    again and pay again. The entire server-side error architecture — the status table, the fragments,
    #30's keep-your-work region — was dead code past the network boundary, and `TestClient` runs no
    JavaScript, so the Python suite was asserting bodies the browser never rendered.

    **The control is the `htmxWouldSwap` column**, and it is what makes this test mean anything: it
    records htmx's own decision before our listener runs. Without it, a harness that swapped
    everything by construction would pass while proving nothing about the fix.
    """
    d = _swap_decisions()

    # must fire: the defect, still present in the vendored library, is that htmx drops all of these.
    # If this ever goes green on its own the harness has stopped modelling htmx and every row below
    # is measuring itself.
    assert not any(d[s]["htmxWouldSwap"] for s in (400, 403, 409, 413, 500, 502)), (
        "the harness no longer reproduces htmx's swap gate, so the rows below assert nothing"
    )

    for status in (409, 413, 502):
        assert d[status]["swapped"] is True, (
            f"a {status} response is still dropped, so the reader sees a completed progress bar and "
            f"an unchanged page — for 502 that is an invitation to buy the same call twice"
        )
    for status in (400, 403, 500):
        assert d[status]["swapped"] is True, f"a {status} response is still invisible"

    assert all(d[s]["isError"] is False for s in (409, 413, 502)), (
        "a handled, rendered response should stop logging as an uncaught one"
    )

    # The other direction, which a blanket `shouldSwap = true` would quietly break: a success must
    # still swap, and htmx's own 204 "no content, change nothing" must still be honoured.
    assert d[200]["swapped"] is True, "the opt-in broke ordinary successful swaps"
    assert d[204]["swapped"] is False, (
        "204 means 'nothing to render' and htmx is right to skip it; the opt-in must not reach below "
        "400 and turn it into a swap of an empty body"
    )

    # #320: making errors visible is only half of it. `#flash` is written by every retargeted error
    # and by nothing else, so without this a 409 from an artifact generation stays on screen through
    # a *successful* answers turn — "still broken" and "already fixed" rendering identically, which
    # is the class this whole page is careful about. Only a full page navigation cleared it, and the
    # htmx paths never navigate.
    assert d["flash_cleared"] is True, (
        "a new request did not clear the previous error notice, so a resolved failure goes on being "
        "displayed as a current one"
    )


# ── a failed first analysis is not a dead end (#207) ─────────────────────────


@pytest.fixture
def failing_analysis(monkeypatch):
    """A provider that claims the session fine and then fails the paid call — the transient-529 shape.

    `claim_session` stays real on purpose, because the whole point of #207 is that the request *is*
    already safely on disk when the call fails. A fake that failed earlier would test a different bug.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")

    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)


def test_a_failed_first_analysis_lands_on_the_session_that_was_saved(client, with_provider,
                                                                     failing_analysis):
    """`start()` claims the session *before* the provider call, deliberately, so a refusal costs
    nothing — which means that when the call fails the pasted email is already safe at revision 0 and
    the session's own page already renders the 'Analyse request' retry button.

    The route let `EngineError` propagate anyway, so it was mapped to 502 and `errors/500.html`:
    "Something went wrong… check the server logs. Back to sessions." Nothing said the request had been
    saved or where, so a first-time user on a transient API error reasonably concludes the product ate
    what they pasted. The good outcome was one redirect away the whole time.
    """
    with_provider()
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200, "a failed first analysis still dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "Anthropic API unavailable" in r.text, "the cause was dropped, so the reader cannot act"
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text, "the retry button the whole fix rests on is not on the page"

    metas = SessionService().list_sessions()
    assert [m.current_revision for m in metas] == [0]


def test_a_retry_exhausted_first_analysis_also_lands_on_the_saved_session(client, with_provider,
                                                                           monkeypatch):
    """`run_discovery`'s provider call can fail two ways: a transport failure (`EngineError`, the
    fixture above), or the JSON retry loop giving up on a reply that never matches the contract
    (`ProviderOutputError`, `core/errors.py`). `app.py`'s own `_status_for` already treats both as
    the same family -- 502 either way -- so the recovery route must too.

    Malformed replies drive this through the *real* retry loop rather than an injected `EngineError`,
    which is what let the narrower `except EngineError` in `create_session` go uncaught for this class
    and reach the app's generic 500/502 handler instead of the recovery page -- the same request the
    reader just pasted, silently harder to get back to.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider("not json", "not json", "not json")

    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200, "a retry-exhausted first analysis dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text

    metas = SessionService().list_sessions()
    assert [m.current_revision for m in metas] == [0]


def _long_contract_violation_reply() -> str:
    """Valid JSON, so this drives a real pydantic `ValidationError` rather than the shorter
    `no JSON object found in the reply` a non-JSON reply produces above -- a realistic contract
    violation, not the shortest possible cause. Long enough (measured: well past 1000 chars once
    wrapped in `ProviderOutputError.message`) that the saved-reply path #283 appends sits nowhere
    near the first `_MAX_NOTICE_CHARS` of it (#362)."""
    payload = {"model": {}, "summary": {}}
    for i in range(7):
        payload[f"extra_field_{i}"] = "x" * 10
    return json.dumps(payload)


def test_a_retry_exhausted_analysis_carries_the_full_saved_reply_path_on_the_web_surface(
        client, with_provider, monkeypatch, tmp_path):
    """#362: #283 appends the saved-reply path to the *tail* of `ProviderOutputError.message`, and
    #253 routes that message into `analysis_failed`, which truncates at `_MAX_NOTICE_CHARS = 300` --
    so on a realistic contract violation the path is gone entirely, and on the shortest possible
    cause the notice ends mid-filename at a path that does not resolve.

    Both shapes in one fixture, driven through the *real* retry loop (never an injected error -- a
    hand-written short message would pass against the unfixed code, since the defect only shows up
    once the message is genuinely long): the long cause is the control that fails on the unfixed
    code with the path silently dropped; the short cause is the one the issue measured ending
    mid-path.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    long_reply = _long_contract_violation_reply()
    with_provider(long_reply, long_reply, long_reply)
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)
    assert r.status_code == 200
    saved = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved) == 1, "the give-up exit must have written exactly one debug file"
    debug_path = str(saved[0])
    assert debug_path in r.text, (
        "the saved-reply path is the one artifact a bug report needs, and it must be reachable on "
        "the web surface -- not silently dropped by the notice's 300-char cap"
    )
    assert r.text.count(debug_path) == 1, (
        "the full path must appear exactly once -- a second occurrence would mean a truncated "
        "fragment is still leaking into the page alongside the complete one"
    )
    # Found in review: stripping only the path (and not the connector clause around it) left the
    # notice ending "...was saved to" with nothing after it, immediately followed by the template's
    # own "The reply that failed validation was saved to <path>." -- the same four words twice in a
    # row across two lines. The connector clause is stripped whole now, so the phrase appears once.
    assert r.text.count("was saved to") == 1, (
        "the connector phrase must appear once, from the separately-rendered path sentence -- twice "
        "means the truncated notice still carries the dangling clause the path sentence repeats"
    )
    saved[0].unlink()  # isolate the second POST below to its own single debug file

    short_reply = "not json"
    with_provider(short_reply, short_reply, short_reply)
    r = client.post("/sessions", data={"request_text": "A different request entirely.",
                                       "provider": "anthropic"}, follow_redirects=True)
    assert r.status_code == 200
    saved2 = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved2) == 1
    debug_path2 = str(saved2[0])
    assert debug_path2 in r.text, "the shortest-cause message must not end mid-path either"
    # A truncated notice ending mid-path would leave an orphaned *prefix* of the filename sitting in
    # the page next to nothing that resolves -- assert no such partial fragment survives once the
    # complete path is rendered.
    fragment = Path(debug_path2).name[:20]
    assert r.text.count(fragment) == 1, (
        "the debug filename's own characters must appear only where the complete path does -- a "
        "second, partial occurrence is exactly the mid-path truncation this fix closes"
    )
    # This is the short-message case where the connector clause -- not just the path -- sits inside
    # the first _MAX_NOTICE_CHARS, so it is the one the dangling-clause regression (found in review)
    # actually reaches: the long-message case above never gets far enough into the message to include
    # the clause at all, truncated or otherwise, so it cannot exercise this.
    assert r.text.count("was saved to") == 1, (
        "the connector phrase must appear once, from the separately-rendered path sentence -- twice "
        "means the truncated notice still carries the dangling clause the path sentence repeats"
    )


def test_a_retry_exhausted_deferred_analysis_also_lands_on_the_saved_session(client, with_provider,
                                                                              monkeypatch):
    """The second door onto the same first analysis, same failure family."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider()
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug
    with_provider("not json", "not json", "not json")

    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text
    assert "Analyse request" in r.text


def test_a_failed_retry_from_the_pending_page_re_renders_it_rather_than_a_500(client, with_provider,
                                                                             failing_analysis):
    """The second door onto the same first analysis. Both must fail into the same place, or the
    reader's experience depends on which button they happened to press."""
    with_provider()
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug

    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text and "Anthropic API unavailable" in r.text
    assert "Analyse request" in r.text


def test_the_revision_zero_gate_still_holds_after_a_failed_analysis(client, with_provider,
                                                                   monkeypatch):
    """The must-fire half: recovering from the failure must not have cost the gate. A session left at
    revision 0 by a failed call is still a session a *successful* analysis may land on — and exactly
    once."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    real = DiscoveryService.run_discovery
    monkeypatch.setattr(DiscoveryService, "run_discovery",
                        lambda self, slug, *, surface="discover": (_ for _ in ()).throw(
                            EngineError("Anthropic API unavailable (529).")))

    with_provider(engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "provider": "anthropic"}, follow_redirects=True)
    slug = SessionService().list_sessions()[0].slug

    monkeypatch.setattr(DiscoveryService, "run_discovery", real)
    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert SessionService().meta(slug).current_revision == 1, (
        "the retry after a failed analysis did not land, so the recovery path is a cul-de-sac"
    )


def test_an_error_fragment_retargets_but_a_full_region_keeps_its_own_target(client, with_provider):
    """The server half of #203, and the distinction is the whole design.

    Once `app.js` swaps 4xx/5xx, *where* they land decides whether the fix helps or repeats #30. The
    one-line `errors/_error.html` fragment carries `HX-Retarget: #flash`, because the form's own
    target is `#session-body` — the region holding the textarea the reader just typed into, which a
    one-line notice would replace wholesale. The 413 answers refusal returns the **full region** with
    the submission still in it, so it must keep the form's target and carry no retarget at all.

    Asserted together, in one test, because the bug is the pair being wrong relative to each other: a
    retarget on both loses #30's preserved answers, and a retarget on neither destroys the form on
    every conflict.
    """
    # One reply, because the conflict below is only reached *after* the provider call — the answers
    # turn reasons first and checks `expected_revision` at the apply. That ordering is #205's subject,
    # not this test's; noted so the reply count reads as a fact about the route rather than padding.
    with_provider(engine_reply())
    slug = _make_session()

    oversized = client.post(f"/sessions/{slug}/answers",
                            data={"answers": "x" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"},
                            headers={"HX-Request": "true"})
    assert oversized.status_code == 413
    assert "HX-Retarget" not in oversized.headers, (
        "the full-region refusal was retargeted, so #30's preserved answers land in the flash strip "
        "and the form they were typed into is left holding the stale text"
    )
    assert "<textarea" in oversized.text and "x" * 300 in oversized.text

    conflict = client.post(f"/sessions/{slug}/answers",
                           data={"answers": "The HR lead approves.", "expected_revision": "0"},
                           headers={"HX-Request": "true"})
    assert conflict.status_code == 409
    assert conflict.headers["HX-Retarget"] == "#flash", (
        "the conflict notice would swap over #session-body and delete the answers form — #30 again, "
        "reintroduced by the very change that made errors visible"
    )
    assert conflict.headers["HX-Reswap"] == "innerHTML"
    assert "notice danger" in conflict.text


def test_every_page_carries_the_flash_region_the_retarget_aims_at(client):
    """`HX-Retarget: #flash` is a promise about the document, not about the response. If the element
    is missing htmx has nowhere to put the notice and the error is silently invisible again — the
    exact failure #203 exists to end, restored by a template edit that looked cosmetic."""
    slug = _make_session()
    for path in ("/", f"/sessions/{slug}"):
        assert 'id="flash"' in client.get(path).text, f"{path} has no flash region to retarget into"
