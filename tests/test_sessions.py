"""Core + services tests for the versioned session store, validation, and the apply pipeline.

All offline — no API, no provider. A temp workspace is pointed at with REQUIVO_WORKSPACE so the
canonical `.requivo/sessions/` layout is exercised in isolation.
"""
from __future__ import annotations

import contextlib
import json
import threading
import typing

import pytest
from _fakes import out, slot
from pydantic import BaseModel, ValidationError

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, ModelProposal, PersistedEngineOutput, _schema_order, schema_slot_ids
from requivo.core.errors import MissingRequiredSlotError, RequivoError, SessionNotFoundError, UnknownSlotError
from requivo.core.validation import validate_proposal
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    # A complete model owes an objective as much as it owes its slots (see `completeness_gap`),
    # so the shared fixture carries one.
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate legacy root too
    return tmp_path


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_accepts_a_complete_model():
    out = validate_proposal(_full_model())
    assert isinstance(out, EngineOutput)


def test_validate_rejects_unknown_slot():
    bad = _full_model()
    bad["model"]["not_a_real_slot"] = _slot()
    with pytest.raises(UnknownSlotError) as e:
        validate_proposal(bad)
    assert e.value.code == "unknown_slot"
    assert "not_a_real_slot" in e.value.details["slots"]


def test_validate_rejects_missing_required_slot():
    partial = _full_model()
    a_required = next(iter(partial["model"]))
    del partial["model"][a_required]
    with pytest.raises(MissingRequiredSlotError) as e:
        validate_proposal(partial)
    assert e.value.code == "missing_required_slot"
    assert a_required in e.value.details["slots"]


def test_validate_rejects_a_complete_model_with_no_objective():
    """Completeness is the full slot set *and* an objective. The provider's retry hook required both;
    the deterministic path required only the slots, so the same model was complete when Anthropic
    produced it and complete-enough when Claude Code applied it — and a session of fifteen filled
    slots with nothing naming what they are for renders as a blank heading in every view. Both
    boundaries now read the one definition (`completeness_gap`)."""
    from requivo.core.errors import InvalidModelError

    with pytest.raises(InvalidModelError) as e:
        validate_proposal({**_full_model(), "summary": {"objective": "   "}})
    assert e.value.path == "summary.objective"
    # A projection is a different claim — it never promised completeness in the first place.
    validate_proposal({**_full_model(), "summary": {}}, require_complete=False)


def test_validate_allows_partial_when_not_required():
    partial = _full_model()
    del partial["model"][next(iter(partial["model"]))]
    out = validate_proposal(partial, require_complete=False)  # no raise
    assert isinstance(out, EngineOutput)


def test_validate_rejects_non_json_string():
    with pytest.raises(RequivoError) as e:
        validate_proposal("{not json")
    assert e.value.code == "invalid_model"


def test_error_to_dict_is_serializable():
    err = UnknownSlotError("bad", path="model.x", details={"slots": ["x"]})
    d = err.to_dict()
    assert d == {"code": "unknown_slot", "message": "bad", "path": "model.x", "details": {"slots": ["x"]}}
    json.dumps(d)  # must round-trip


# ── store: revisions + artifacts ────────────────────────────────────────────────


def test_store_creates_session_and_revisions(workspace):
    store.create_session("s1", "Build a leave system.", provider="claude-code")
    assert store.read_meta("s1").current_revision == 0
    out = EngineOutput.model_validate(_full_model())
    rev1, _ = store.save_revision("s1", out)
    rev2, meta = store.save_revision("s1", out)
    assert (rev1, rev2, meta.current_revision) == (1, 2, 2)
    d = store.canonical_dir("s1")
    assert (d / "model.json").exists()
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "revisions" / "0002-model.json").exists()
    assert store.list_session_slugs() == ["s1"]


def test_store_migrate_session_rejects_a_future_format(workspace):
    from requivo.core.errors import InvalidSessionError
    with pytest.raises(InvalidSessionError):
        store.migrate_session({"format_version": 999, "session_id": "x", "slug": "s",
                               "created_at": "t", "updated_at": "t"})


# ── services: the apply pipeline ─────────────────────────────────────────────────


def test_session_service_create_and_apply(workspace):
    svc = SessionService()
    meta = svc.create_session("Build a leave approval system.", slug="leave", provider="claude-code")
    assert meta.slug == "leave" and meta.current_revision == 0

    # A high-impact slot left unconfirmed must block readiness.
    result = svc.update_model("leave", _full_model(**{"problem": _slot(0, "empty", "high")}))
    assert result.status == "applied"
    assert result.revision == 1
    assert set(result.changed_slots)  # every slot present counts as changed on the first apply
    assert result.readiness.ready is False
    assert "problem" in result.readiness.blocking_slots


def test_apply_diff_reports_changed_slots_and_readiness(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    # First model: everything empty.
    svc.update_model("s", _full_model())
    # Second: fill one slot explicitly → it should be the changed slot.
    changed_model = _full_model(**{"problem": _slot(90, "explicit", "high", "A real problem")})
    result = svc.update_model("s", changed_model)
    assert result.revision == 2
    assert "problem" in result.changed_slots


def test_diff_does_not_write(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    before = store.read_meta("s").current_revision
    plan = svc.diff("s", _full_model(**{"problem": _slot(90, "explicit", "high", "X")}))
    assert plan.status == "planned"
    assert store.read_meta("s").current_revision == before  # unchanged — no write


def test_apply_flags_generated_artifact_stale(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "prd", "# PRD\n", source_revision=1)  # generated at revision 1
    assert art.list("s")["prd"]["stale"] is False
    # Change a slot the PRD consumes (workflow) → PRD goes stale.
    result = svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new flow")}))
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def _with_reasoning(model: dict) -> dict:
    """A full model that also carries baked-in reasoning: a decision on `permissions`, a challenge
    contesting `workflow`."""
    model["decisions"] = [{"decision": "Draft-first", "derived_from": ["permissions"]}]
    model["challenges"] = [{
        "headline": "Archive vs delete", "premise": "p", "alternative": "a",
        "consequence": "c", "recommendation": "r", "contests": ["workflow"],
    }]
    return model


def test_propagate_reports_challenges_via_contests():
    from requivo.core.dependencies import propagate
    out = EngineOutput.model_validate(_with_reasoning(_full_model()))
    hit = propagate(out, ["workflow"])
    assert [c.headline for c in hit.challenges] == ["Archive vs delete"]
    assert hit.reasoning_hit is True
    # A change that touches neither derived_from nor contests unseats no reasoning.
    miss = propagate(out, ["success_metrics"])
    assert not miss.challenges and not miss.decisions and not miss.reasoning_hit


def test_apply_flags_assessment_stale_when_reasoning_is_unseated(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_reasoning(_full_model()))
    art.save("s", "brief", "# Assessment\n", source_revision=1)  # the saved assessment renders that reasoning
    assert art.list("s")["brief"]["stale"] is False

    # Change `workflow` — a challenge contests it → the assessment on disk no longer holds.
    changed = _with_reasoning(_full_model())
    changed["model"]["workflow"] = _slot(80, "explicit", "high", "new flow")
    result = svc.update_model("s", changed)
    assert "Archive vs delete" in result.invalidated_challenges
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True
    # The decision (on `permissions`) was untouched, so it is not reported.
    assert result.invalidated_decisions == []
    assert "invalidated_challenges" in result.to_dict()


def test_changing_the_problem_marks_a_saved_assessment_stale(workspace):
    # The assessment used to sit outside the artifact→slot map entirely, on the grounds that it was the
    # live analysis layer rather than a deliverable. Once it is saved to disk that stops holding: an
    # assessment whose problem statement has since been rewritten is not "fresh", it is out of date.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # no decisions, no challenges — nothing to unseat
    art.save("s", "brief", "# Assessment\n", source_revision=1)
    assert art.list("s")["brief"]["stale"] is False

    result = svc.update_model("s", _full_model(**{"problem": _slot(80, "explicit", "high", "reframed")}))
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True


def test_artifact_cannot_be_recorded_against_an_impossible_revision(workspace):
    # Provenance that cannot be true is worse than none: every freshness answer downstream is read off
    # this number, so a revision from the future is refused rather than stored.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # session is at revision 1
    with pytest.raises(RequivoError) as ei:
        art.save("s", "prd", "# PRD\n", source_revision=999)
    assert ei.value.code == "artifact_revision_out_of_range"
    with pytest.raises(RequivoError):
        art.save("s", "prd", "# PRD\n", source_revision=0)
    assert "prd" not in art.list("s")            # nothing was recorded


# ── generation vs. concurrent writes ──────────────────────────────────────────
# A provider call runs for seconds to minutes, and the session can move underneath it (a second browser
# tab, a CLI apply, a Claude Code turn). These two tests pin the behaviour at that seam: the model the
# generator read is the revision it writes against, and a change that lands mid-flight is never lost
# and never silently inherited.

class _RacingClient:
    """A provider whose reply arrives only after someone else has already moved the session."""

    def __init__(self, reply: str, on_call):
        self._reply, self._on_call = reply, on_call
        self.messages = self

    def create(self, **kwargs):
        self._on_call()          # the concurrent write lands while "reasoning" is in flight
        return _Reply(self._reply)


class _Reply:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = None


def test_generation_that_races_a_concurrent_apply_does_not_lose_it(workspace):
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — what the generator will read

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"business_rules": _slot(90, "explicit", "high", "HR signs off")}))

    brief_reply = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                              "risks": [], "next_steps": []})
    disco = DiscoveryService(client=_RacingClient(brief_reply, concurrent_answer))
    with pytest.raises(RevisionConflictError):
        disco.generate("s", "brief")

    # The rule that landed mid-flight is still there — the assessment's apply did not write over it.
    assert svc.load_model("s").model["business_rules"].value == "HR signs off"


def test_an_answers_turn_holds_the_revision_it_read(workspace):
    # A turn has the same seam as a generation, so a caller that passes no expectation still gets one:
    # the revision the turn actually read. Without it, the CLI's `answer` would quietly overwrite a
    # change made in a browser tab between the read and the apply.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = _full_model()
    reply["summary"] = {"objective": "A leave approval system"}   # a discovery reply owes an objective
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.answer("s", "here are my answers")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it(workspace):
    """The full user journey the tri-state exists for: discovery → assessment → an ordinary answer.

    `engine.md` asks a turn for model/questions/summary only, so a refinement reply carries no
    decisions — and this whole path (provider parse → apply → diff → freshness) used to read that as a
    deletion, wiping the reasoning the assessment had just established while reporting no change and
    leaving the PRD marked fresh. The reply below is exactly what the engine returns; nothing about
    the reasoning is mentioned in it."""
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", {**_full_model(), "decisions": [
        {"decision": "Managers approve in-app", "derived_from": ["permissions"]}]})
    art.save("s", "prd", "# PRD\n", source_revision=1)

    reply = {**_full_model(**{"workflow": _slot(90, "explicit", "high", "request → approve")}),
             "summary": {"objective": "A leave approval system"}}
    DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).answer("s", "in-app")

    after = svc.load_model("s")
    assert [d.decision for d in after.decisions] == ["Managers approve in-app"]
    assert after.model["workflow"].value == "request → approve"   # the facts did move
    assert art.list("s")["prd"]["stale"] is True                  # …and that alone marks the PRD stale


def test_the_same_request_under_different_cards_is_a_different_session(workspace):
    """Context cards are provenance, not decoration: the same request read against `b2b-platform` and
    against `event-ops` gets different impact estimates, so different questions. Creation keyed on the
    request alone, so the second call silently handed back the first session — with a card selection
    the caller had not asked for and no way to notice."""
    svc = SessionService()
    first = svc.create_session("Same request.", context_cards=["b2b-platform"])
    again = svc.create_session("Same request.", context_cards=["b2b-platform"])
    other = svc.create_session("Same request.", context_cards=["event-ops"])

    assert again.slug == first.slug                       # same discovery: still idempotent
    assert other.slug != first.slug
    assert svc.cards(other.slug) == ["event-ops"]         # and it got the cards it asked for


# ── rescoping context cards (#168) ──────────────────────────────────────────────


def test_rescope_before_any_model_only_mutates_metadata(workspace):
    """Before any turn has reasoned against the old selection, there is no provenance to keep honest —
    nothing describes a model produced under it. Revision 0 (no model yet) so a re-scope here is a
    plain metadata write: no revision, no revisions-log entry."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])

    result = svc.rescope("s", context_cards=["event-ops"])

    assert result.changed is True
    assert result.revision == 0
    assert svc.cards("s") == ["event-ops"]
    meta = store.read_meta("s")
    assert meta.current_revision == 0
    assert meta.revisions == []


def test_rescope_after_a_model_records_a_new_revision_with_unchanged_content(workspace):
    """Once a model exists, every revision already on disk was reasoned under the *old* selection.
    A re-scope is recorded as its own revision — an unchanged model, a provenance entry naming the
    surface as a context switch rather than a reasoning turn — so the history shows exactly where
    the selection changed, instead of silently rewriting what revision 1 was reasoned against."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())          # revision 1, reasoned under b2b-platform

    result = svc.rescope("s", context_cards=["event-ops"])

    assert result.changed is True
    assert result.revision == 2
    assert result.previous_context_cards == ["b2b-platform"]
    assert result.context_cards == ["event-ops"]
    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert meta.context_cards == ["event-ops"]
    assert len(meta.revisions) == 2
    new_rec = meta.revisions[-1]
    assert new_rec.revision == 2
    assert new_rec.surface == "session-rescope"
    # the model itself did not move — same content, same hash as the revision it succeeds
    assert new_rec.model_hash == meta.revisions[0].model_hash
    assert store.load_revision_model("s", 2).model_dump() == store.load_revision_model("s", 1).model_dump()


def test_rescope_resolves_and_normalizes_cards_like_creation(workspace):
    """Invariant 14's second door: `create_session` resolves the caller's selection rather than
    trusting it, and a re-scope is a second entrance onto the same persisted value — an unknown name
    must be refused here too, not recorded and discovered on the next turn."""
    from requivo.core.errors import UnknownContextCardError

    svc = SessionService()
    svc.create_session("Something.", slug="s")

    with pytest.raises(UnknownContextCardError):
        svc.rescope("s", context_cards=["made-up"])
    assert svc.cards("s") is None  # refused before anything was written


def test_rescope_to_the_current_selection_is_a_no_op(workspace):
    """Re-scoping to the selection a session already has changes nothing — order aside, since the
    selection is a set. No new revision, no rewritten metadata: repeating the command is safe."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform", "event-ops"])
    svc.update_model("s", _full_model())           # revision 1

    result = svc.rescope("s", context_cards=["event-ops", "b2b-platform"])  # same set, other order

    assert result.changed is False
    assert result.revision == 1
    meta = store.read_meta("s")
    assert meta.current_revision == 1
    assert len(meta.revisions) == 1


def test_rescope_to_every_card_resets_the_selection_to_none(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])

    result = svc.rescope("s", context_cards=None)

    assert result.changed is True
    assert result.context_cards is None
    assert svc.cards("s") is None


def test_rescope_does_not_mark_existing_artifacts_stale(workspace):
    """Question 2, decided: context is not a fifth kind of dependency edge. An artifact already on
    disk still faithfully describes the model it was generated from — nothing in `ARTIFACT_SLOTS` or
    `REASONING_CONSUMERS` names context as an input, and the model itself has not moved."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())                       # revision 1
    art.save("s", "prd", "# PRD\n", source_revision=1)

    svc.rescope("s", context_cards=["event-ops"])

    assert art.list("s")["prd"]["stale"] is False


def test_a_model_change_still_marks_the_same_artifact_stale(workspace):
    """The positive control for the assertion above: proves the harness can observe staleness at
    all, on the very same artifact, so "rescope leaves it fresh" is not passing on a fixture that
    can never turn STALE regardless of what runs."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                       # revision 1
    art.save("s", "prd", "# PRD\n", source_revision=1)

    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    assert art.list("s")["prd"]["stale"] is True


def test_rescope_does_not_re_run_anything_the_next_snapshot_reads_the_new_cards(workspace):
    """Question 3, decided: a re-scope re-runs nothing. It only changes what the *next* provider call
    reasons against — proven here without a provider at all, by reading the same snapshot every
    discovery call reads from."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())

    svc.rescope("s", context_cards=["event-ops"])

    assert svc.snapshot("s").context_cards == ["event-ops"]


def test_a_rescoped_session_with_a_model_still_passes_its_own_integrity_check(workspace):
    """The duplicated revision this produces is a real revision, not a shortcut: `check_session_dir`
    is the same anti-tampering pass `session verify` runs, and it must find nothing wrong with one."""
    from requivo.core.integrity import check_session_dir

    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())
    svc.rescope("s", context_cards=["event-ops"])

    problems = check_session_dir(store.canonical_dir("s"), expected_slug="s")
    assert problems == []


def test_rescope_refuses_a_session_that_does_not_exist(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().rescope("ghost", context_cards=["event-ops"])


def test_a_fresh_discovery_refuses_to_replace_a_model_that_already_exists(workspace):
    """Session creation is idempotent, so re-running `discover` on the same request lands on the same
    session — and used to overwrite whatever it held, replacing a model refined over several turns
    with a naive first-turn one. A conflict is recoverable; a silent replacement is not."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    disco = DiscoveryService(_FakeProvider())
    slug = disco.start("A leave approval system.", slug="dup")
    SessionService().update_model(slug, _full_model(**{"workflow": _slot(90, "explicit", "high", "kept")}))

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert SessionService().load_model(slug).model["workflow"].value == "kept"


def test_run_discovery_refuses_a_session_that_already_has_a_model(workspace):
    """`run_discovery` reasons from the request alone — it never sees the current model — so on a
    refined session it does not improve the understanding, it discards it. The optimistic lock does
    not catch this: the call reads revision N and writes against revision N, so the precondition is
    satisfied while the content is a regression. `POST /sessions/{slug}/discover` reaches this
    directly; the Web only shows the button at revision 0, but a rule enforced by a hidden button is
    not enforced. The refusal is also *before* the call — reasoning that can only be thrown away
    should not be paid for."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "refined")}))

    provider = _CountingProvider()
    with pytest.raises(RevisionConflictError) as e:
        DiscoveryService(provider).run_discovery("s")

    assert e.value.details["actual"] == 1 and e.value.details["expected"] == 0
    assert provider.calls == 0                                    # refused before the paid call
    assert svc.load_model("s").model["workflow"].value == "refined"


@pytest.mark.parametrize("call", [
    lambda d: d.generate("s", "brief"),
    lambda d: d.generate("s", "prd"),
    lambda d: d.reason("s", "stories"),
], ids=["generate-brief", "generate-prd", "reason-stories"])
def test_generation_refuses_a_session_that_has_no_model_yet(workspace, call):
    """The mirror of the rule above, and it was missing (#152). `SessionSnapshot.model` is `None`
    before the first model — the field says so — and `generate`/`reason` unpacked it and handed it
    to the provider unchecked.

    Nothing was lost and nothing was spent: every generator builds its user message as
    `out.model_dump_json(...)`, so it died assembling the prompt, before the client was touched. What
    a user got was a raw `AttributeError` where every other refusal here is a structured error naming
    the remedy — and the only place the rule existed was `web/routes/sessions.py`, which renders an
    "offer to run discovery" page at revision 0. One surface out of three, which is the same defect
    the sibling test above describes in its own docstring.

    Parametrized over all three doors rather than asserted once: `generate` and `reason` take
    separate paths to the provider, and a guard added to one of them is exactly how this comes back.
    """
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    SessionService().create_session("Something.", slug="s")     # created, never analysed
    provider = _CountingProvider()

    with pytest.raises(RevisionConflictError) as e:
        call(DiscoveryService(provider))

    assert e.value.details["actual"] == 0 and e.value.details["expected"] == 1
    assert provider.calls == 0                                    # refused before reaching the provider
    assert "discover" in str(e.value)                             # the refusal names the remedy


def test_answer_refuses_a_session_that_has_no_model_yet(workspace):
    """`answer()` is the one write verb `_require_a_model` did not cover (#421) — the mirror of
    #152, one write verb over. At revision 0 `snap.model` is `None`, so the provider's `analyze()`
    falls through to its own first-discovery branch: the answers the caller typed appear in no kwarg
    of the call, the reply is applied as revision 1 with `cli-answer`/`web-answer` provenance, and the
    write bypasses `run_discovery`'s own double-submission guard — a paid turn that both ignored what
    it was given and recorded an answer turn that never happened.

    Must-fire: without the gate this reaches the provider (`provider.calls == 1`) and raises nothing,
    so this test is red on the pre-fix code rather than merely descriptive of it. The sibling control
    — `answer` still working at revision >= 1 — is already covered by
    `test_an_answers_turn_holds_the_revision_it_read` and
    `test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it`, both of which apply a model
    before calling `answer`; duplicating that here would test nothing this file does not already
    pin."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    SessionService().create_session("Something.", slug="s")     # created, never analysed
    provider = _CountingProvider()

    with pytest.raises(RevisionConflictError) as e:
        DiscoveryService(provider).answer("s", "here are my answers")

    assert e.value.details["actual"] == 0 and e.value.details["expected"] == 1
    assert provider.calls == 0                                    # refused before reaching the provider
    assert "discover" in str(e.value)                             # the refusal names the remedy
    # The co-requisite half of #421: before this fix the remedy text itself suggested `requivo answer`
    # "if a discovery is in progress" — i.e. it routed a reader straight back into the ungated path.
    # Since #202 an interrupted discovery lands at revision 1, so `answer` is never the right verb at
    # revision 0; naming it here would be self-contradictory the moment this very gate exists.
    assert "requivo answer" not in str(e.value)


def test_a_repeat_discovery_is_refused_before_the_provider_is_paid(workspace):
    """Same rule, the other entry point. `start()` used to reason first and discover the conflict
    afterwards, so an accidental re-run bought a discovery turn — and, when finalizing, an assessment
    too — purely to throw both away."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    provider = _CountingProvider()
    disco = DiscoveryService(provider)
    disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1                                    # the second run never reasoned


def test_the_artifact_service_defaults_to_the_session_service_s_storage(workspace):
    """Two services, one backing. On files the default and the injected repository resolve to the same
    workspace, so a split was invisible — but `DiscoveryService(sessions=SessionService(postgres))`
    sent sessions to Postgres and artifacts to the local filesystem, and every call succeeded. This is
    the shape an external deployment constructs, so the default has to follow the session service."""
    from requivo.services.discovery import DiscoveryService
    from requivo.services.repository import FileSessionRepository

    repo = FileSessionRepository()
    disco = DiscoveryService(_FakeProvider(), sessions=SessionService(repo))
    assert disco.artifacts.repo is repo
    assert DiscoveryService(_FakeProvider(), repo=repo).sessions.repo is repo


def test_the_service_refuses_a_context_card_that_does_not_exist(workspace):
    """The CLI and the Web both resolve cards before they get here, which made the service look safe.
    It is not a boundary until it holds the rule itself: an unknown card recorded on a session is read
    back by every later turn, and an empty resolved selection means *every* card — so a bad name
    silently widens the context instead of narrowing it. An external consumer calls exactly this layer."""
    from requivo.core.errors import UnknownContextCardError

    with pytest.raises(UnknownContextCardError):
        SessionService().create_session("Something.", context_cards=["made-up"])
    assert SessionService().create_session(
        "Something.", context_cards=["b2b-platform"]).context_cards == ["b2b-platform"]


def test_impact_reports_what_a_named_slot_reaches(workspace):
    """`SessionService.impact` -- the XS addition #425's HTTP API `/impact` route is built on -- is
    exactly `propagate(load_model(slug), resolve_slots(...))` behind the service seam: a decision
    derived from the named slot, and a challenge contesting it, both come back."""
    from requivo.core.contracts import Challenge, DesignDecision

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    model = EngineOutput.model_validate({
        **_full_model(**{"workflow": _slot(80, "explicit", "high")}),
        "decisions": [DesignDecision(decision="Draft-first invoices",
                                     derived_from=["workflow"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a",
                                 consequence="c", recommendation="r",
                                 contests=["workflow"]).model_dump()],
    })
    svc.update_model("s", model.model_dump())

    report = svc.impact("s", ["workflow"])
    assert any(d.decision == "Draft-first invoices" for d in report.decisions)
    assert any(c.headline == "Invoice at signature" for c in report.challenges)
    assert report.to_dict()["decisions"][0]["decision"] == "Draft-first invoices"


def test_impact_refuses_an_unknown_slot_naming_it_in_details(workspace):
    """Unlike the CLI's own `_cmd_impact`, which prints a warning for an unmatched token and keeps
    rendering whatever did match, the service raises -- a caller over HTTP gets one structured
    refusal rather than a partial report with no signal that something was left out."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    with pytest.raises(UnknownSlotError) as e:
        svc.impact("s", ["not-a-real-slot"])
    assert e.value.details["unmatched"] == ["not-a-real-slot"]


def test_impact_with_no_slots_named_is_an_empty_report_not_a_refusal(workspace):
    """An empty list is "no slots named", the same reading `resolve_slots([])` already gives it
    (`normalize_tokens`'s own docstring) -- not the same thing as a token that matched nothing."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    report = svc.impact("s", [])
    assert report.empty


def test_show_with_status_reads_content_and_freshness_together(workspace):
    """`ArtifactService.show_with_status` -- the coherent read the HTTP API's artifact envelope needs
    (invariant 12, one layer over from the provider-snapshot case it was written for): both facts
    come from one locked read rather than two separate calls that could disagree."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "brief", "# Brief\n", source_revision=1)

    content, row = art.show_with_status("s", "brief")
    assert content == "# Brief\n"
    assert row == {"revision": 1, "filename": "solution-assessment.md",
                   "updated_at": row["updated_at"], "stale": False}


def test_show_with_status_404s_when_nothing_was_ever_saved(workspace):
    from requivo.core.errors import SessionNotFoundError

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    with pytest.raises(SessionNotFoundError):
        art.show_with_status("s", "brief")


def test_show_with_status_is_not_interleaved_by_a_concurrent_save(workspace):
    """The must-fire proof behind the claim in `show_with_status`'s own docstring (found in review,
    #425): a single-threaded test that only checks content and status agree when nothing else is
    writing would pass identically whether the lock were there or not. This drives a real second
    thread through `save()` while the read is paused *inside* the held lock, and shows it is
    genuinely blocked -- not merely usually-fast-enough -- until the read completes."""
    import threading

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "brief", "V1", source_revision=1)

    reader_inside_lock = threading.Event()
    reader_may_finish = threading.Event()
    real_load_artifact = art.repo.load_artifact

    def paused_load_artifact(slug, filename):
        content = real_load_artifact(slug, filename)
        reader_inside_lock.set()
        assert reader_may_finish.wait(timeout=5), "the writer thread below never released the reader"
        return content

    art.repo.load_artifact = paused_load_artifact

    result: dict = {}

    def do_show():
        result["content"], result["row"] = art.show_with_status("s", "brief")

    reader = threading.Thread(target=do_show, daemon=True)
    reader.start()
    assert reader_inside_lock.wait(timeout=5), "the reader never reached its locked read"

    writer_finished = threading.Event()

    def do_save():
        art.save("s", "brief", "V2", source_revision=1)
        writer_finished.set()

    writer = threading.Thread(target=do_save, daemon=True)
    writer.start()

    # Must genuinely be waiting on the reader's held lock, not racing ahead of it -- the same
    # "not merely usually fast enough" assertion `test_persistence_guards.py`'s own lock tests make.
    assert not writer_finished.wait(timeout=0.2), (
        "a concurrent save() proceeded while show_with_status still held the lock -- reverting to "
        "two separate unlocked calls (show() then list()) would let this assertion fail")

    reader_may_finish.set()
    reader.join(timeout=5)
    assert writer_finished.wait(timeout=5), "the writer never finished once the reader released"

    # Because the read was atomic, content and status describe the SAME save -- the first one,
    # since the reader's locked read ran to completion before the writer's save() could start.
    assert result["content"] == "V1"
    assert result["row"]["revision"] == 1


def test_an_artifact_is_refused_when_its_freshness_cannot_be_established(workspace):
    """`False` is not "I don't know" — it is the claim that the artifact is up to date. It was being
    returned for a session whose history could not be read at all, which is the one case where the
    answer is genuinely unavailable. Refusing the save is the honest outcome: the provenance it would
    record cannot be verified."""
    from requivo.core.errors import RequivoError

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                   # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "moved")}))  # 2
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()  # the history is now a lie

    with pytest.raises(RequivoError) as e:
        art.save("s", "prd", "# PRD\n", source_revision=1)
    assert e.value.code == "unreadable_source_revision"
    assert "prd" not in art.list("s")                                      # nothing was recorded


def test_a_first_discovery_that_races_a_concurrent_write_conflicts(workspace):
    """`run_discovery` reasons from revision N and applies; the call takes minutes, so it captures the
    revision it read and holds the write to it — the same precondition every other provider-backed
    operation carries. Without it the concurrent model was replaced by one reasoned from the older
    state, which is exactly the case optimistic locking exists for."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.run_discovery("s")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_artifact_generated_from_a_superseded_revision_is_born_stale(workspace):
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — the PRD's actual source

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    prd_reply = json.dumps({"title": "PRD", "problem": "Approvals are lost in email."})
    DiscoveryService(client=_RacingClient(prd_reply, concurrent_answer)).generate("s", "prd")

    saved = art.list("s")["prd"]
    assert saved["revision"] == 1        # recorded against the revision it was written from…
    assert saved["stale"] is True        # …and the workflow change it never saw makes it stale


# ── the provider seam ─────────────────────────────────────────────────────────
# The point of the protocol is that the orchestration is not Anthropic-shaped. These two tests are the
# proof: one drives a whole discovery through a provider that has never heard of Anthropic, the other
# checks that what lands in the revision log is enough to reproduce the run.

class _FakeProvider:
    """A `ReasoningProvider` with no vendor behind it — the stand-in for a second implementation."""

    name = "fake"

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        return EngineOutput.model_validate({**_full_model(), "summary": {"objective": "A leave system"}})

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not needed for this test")

    def model_name(self):
        return "fake-model-1"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:fake"}


class _CountingProvider(_FakeProvider):
    """A provider that records whether it was asked to reason — the point of a pre-flight check."""

    def __init__(self):
        self.calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        self.calls += 1
        return super().analyze(request, current_model=current_model, answers=answers, only=only)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        # Overrides `_FakeProvider.generate`, which raises "not needed for this test". A guard test
        # has to be able to tell *reached the provider* from *raised somewhere else on the way*, and
        # an AssertionError from the stand-in reads like a failed assertion in the test itself.
        self.calls += 1
        raise AssertionError(f"the provider was reached with model={model!r}")


def test_discovery_runs_on_a_provider_that_is_not_anthropic(workspace):
    from requivo.services.discovery import DiscoveryService

    slug = DiscoveryService(_FakeProvider()).start("A leave approval system.", slug="fake-prov")
    meta = SessionService().meta(slug)
    # Nothing hard-codes "anthropic": the session and its revision are stamped by the provider itself.
    assert meta.provider == "fake" and meta.model_name == "fake-model-1"
    assert [(r.provider, r.model_name) for r in meta.revisions] == [("fake", "fake-model-1")]


class _NamelessProvider:
    """Implements every member `ReasoningProvider` *declares* — and nothing more.

    The stand-in for the second implementation the seam exists for. `name` is read by the very first
    thing a discovery does, so an object without one is not a provider; whether anything can *tell*
    is what this pins."""

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        raise AssertionError("a provider missing `name` must fail before it is asked to reason")

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not reached")

    def model_name(self):
        return "nameless-1"

    def provenance(self, op, *, only=None):
        return {"provider": "nameless", "model_name": self.model_name(), "prompt_version": "sha256:x"}


def test_the_provider_protocol_declares_every_member_the_orchestration_reads(workspace):
    """`provider.name` is read on the first discovery, so it is part of the contract or the contract
    is not the contract. `@runtime_checkable` does check a bare data annotation — only `issubclass`
    is refused for a protocol with non-method members — so declaring it is enforcement, not comment."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.providers.base import ReasoningProvider
    from requivo.services.discovery import DiscoveryService

    # Must-fire half: real conformers satisfy the protocol. Without it, a protocol that rejected
    # everything — or one that stopped being runtime-checkable — would pass the assertion below.
    assert isinstance(_FakeProvider(), ReasoningProvider)
    assert isinstance(AnthropicProvider.__new__(AnthropicProvider), ReasoningProvider)  # no API key needed

    # Must-not-fire half: everything declared, `name` absent.
    assert not isinstance(_NamelessProvider(), ReasoningProvider)

    # …and the positive control that keeps the line above from being about a decorative member:
    # `name` is what the orchestration actually reaches for, before it reasons.
    with pytest.raises(AttributeError, match="name"):
        DiscoveryService(_NamelessProvider()).start("A leave approval system.", slug="nameless")


def test_a_revision_records_the_prompt_it_was_reasoned_against(workspace):
    # A revision log that is only "anthropic, at 14:02" cannot reproduce anything: behaviour here is
    # tuned by editing prompts and context cards, so the prompt identity is half the provenance.
    from requivo.providers.anthropic import prompt_version
    from requivo.services.discovery import DiscoveryService

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    slug = DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).start(
        "A leave approval system.", slug="prov")

    rec = SessionService().meta(slug).revisions[-1]
    assert rec.provider == "anthropic" and rec.model_name
    assert rec.prompt_version and rec.prompt_version.startswith("sha256:")
    # It follows the context-card selection, because a different card set is different reasoning.
    # A *real* card rather than `only=[]`: an empty selection is now refused (#13), because a
    # selection that selects nothing renders exactly like a clean load of everything.
    assert prompt_version("analyze") != prompt_version("analyze", only=["b2b-platform"])


# ── the session format is public ──────────────────────────────────────────────
# `.requivo/sessions/` is the interface between the CLI, the Claude Code plugin, the Web and anything
# built on top. These tests are the contract: a session written by an older Requivo keeps loading, and
# a session written by a newer one is refused clearly instead of being half-understood.

# Verbatim shape of a session.json as 0.8.2 wrote it — including `prompt_versions`, a key that has
# since been removed. Frozen on purpose: editing it to match today's model would defeat the test.
SESSION_JSON_0_8_2 = """{
  "format_version": 1,
  "requivo_version": "0.8.2",
  "session_id": "d4f1a0c2e5b74d0e9a3c8b1f2e6d7a45",
  "slug": "leave-approval",
  "created_at": "2026-07-30T09:12:00Z",
  "updated_at": "2026-07-30T09:41:00Z",
  "provider": "anthropic",
  "model_name": "claude-sonnet-5",
  "context_cards": null,
  "request_hash": "sha256:6b2f1c",
  "schema_version": 1,
  "prompt_versions": {},
  "current_revision": 2,
  "revisions": [
    {"revision": 1, "created_at": "2026-07-30T09:12:00Z", "previous_revision": null,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-discover",
     "prompt_version": null, "model_hash": "sha256:aaa"},
    {"revision": 2, "created_at": "2026-07-30T09:41:00Z", "previous_revision": 1,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-answer",
     "prompt_version": null, "model_hash": "sha256:bbb"}
  ],
  "artifact_status": {
    "prd": {"revision": 2, "filename": "prd.md", "updated_at": "2026-07-30T09:42:00Z", "stale": false}
  }
}"""


def test_a_session_written_by_an_older_requivo_still_loads(workspace):
    d = store.canonical_dir("leave-approval")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2)

    meta = store.read_meta("leave-approval")
    assert meta.current_revision == 2 and meta.provider == "anthropic"
    assert [r.surface for r in meta.revisions] == ["cli-discover", "cli-answer"]
    assert meta.artifact_status["prd"].filename == "prd.md"
    assert meta.artifact_status["prd"].stale is False
    # A field this version dropped is ignored, not fatal — that is what lets a key be retired without
    # a format bump, and what makes the next reader's job survivable.
    assert not hasattr(meta, "prompt_versions")
    # Fields added since simply take their defaults.
    assert meta.revisions[0].prompt_version is None


# ── the malformed-session family (#82) ───────────────────────────────────────
#
# `invalid_session` carried seven facts across eight raise sites with four `details` shapes, and
# unlike `cross_site_request` it is serialized: `cli.py` prints `to_dict()` on every `--json` verb, so
# a consumer could observe the inconsistency, and `details["slug"]` raised `KeyError` on three of the
# eight. The split gives each fact a code. The two tests below are what keep it split.


def test_nothing_raises_the_malformed_session_family_base():
    """The base is a family, and a family with a raise site is not one.

    The split is only worth anything while every arm names its fact. One `raise InvalidSessionError(`
    added later re-creates the exact defect #82 removed — a code that means "one of eight things" —
    and it would pass every other test in this suite, including the HTTP-status completeness check,
    because the base legitimately keeps its row.

    Scanned as source rather than by walking the vocabulary: an unraised class is invisible to a
    subclass walk, which is the whole difficulty. `raise X(` is the only shape that constructs one to
    throw; a bare `raise` re-raises something already built, and an `except InvalidSessionError` is
    the catch this family exists to keep working.
    """
    import re
    from pathlib import Path

    import requivo

    root = Path(requivo.__file__).parent
    files = sorted(root.rglob("*.py"))
    # must fire: a glob over a moved package returns [], and `assert not []` is an all-clear nobody
    # earned — the same point `tests/test_boundaries.py` makes about its own scan set.
    assert len(files) >= 10, f"scan set looks blind: {files}"

    pattern = re.compile(r"\braise\s+InvalidSessionError\s*\(")
    offenders = {}
    for p in files:
        hits = [i for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if pattern.search(line)]
        if hits:
            offenders[str(p.relative_to(root))] = hits
    assert not offenders, (
        "these sites raise the family base instead of an arm that names the fact; give the condition "
        f"its own subclass and a row in requivo/http.py::STATUS_BY_CODE: {offenders}")


def test_every_arm_of_the_family_names_a_distinct_fact():
    """Ten arms, ten codes, and none of them the base.

    Named individually rather than counted: a test asserting `len(subclasses) == 10` would pass just
    as well if two arms were merged and a third invented, which is a different vocabulary answering
    the same number.

    `invalid_archive` is the tenth, added by #101. It joined here rather than standing alone because
    it sits between `unreadable_archive` and `inconsistent_archive` on one code path in
    `_cmd_session_import`, and a consumer writing `except InvalidSessionError` for *any*
    malformed-session refusal would otherwise catch the arm on either side of it and miss the seven
    conditions in between. **This list going red on a new arm is the guard doing its job** — it is
    what made #101 state the family question rather than answer it by accident.
    """
    from requivo.core.errors import InvalidSessionError
    from requivo.services import artifacts  # noqa: F401  - registers the two service-layer arms

    def arms(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from arms(sub)

    codes = {a.code for a in arms(InvalidSessionError)}
    assert codes == {
        "unsupported_format_version", "unsupported_schema_version", "session_unreadable",
        "model_unreadable", "artifact_revision_out_of_range", "unstated_source_revision",
        "unreadable_source_revision", "inconsistent_archive", "unreadable_archive",
        "invalid_archive", "import_move_failed",
    }
    assert "invalid_session" not in codes, "the base is the family, not an arm"


def test_a_session_from_a_newer_requivo_is_refused_not_guessed(workspace):
    d = store.canonical_dir("from-the-future")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2.replace('"format_version": 1', '"format_version": 2'))
    with pytest.raises(RequivoError) as ei:
        store.read_meta("from-the-future")
    assert ei.value.code == "unsupported_format_version"
    assert "upgrade requivo" in str(ei.value).lower()
    # `newer than what` is half the fact, and #82 made the payload carry it: a reader who learns only
    # that their session is from the future still cannot tell which build they are holding.
    assert ei.value.details == {"format_version": 2, "supported_format_version": 1}


# ── the other half of the format promise: model.json (#14) ────────────────────
# `session.json` has carried forward compatibility since 0.9.4; `model.json` never did, although
# `docs/compatibility.md` promised it for the layout as a whole. The tests below are the model-side
# mirror of the two above: a model a *newer* Requivo wrote loads and survives a round-trip, and the
# provider boundary that shares its shape still refuses the same payload.


def _model_from_the_future() -> dict:
    """A model as a Requivo one minor version ahead might write it — a field added at the top level,
    inside a slot, inside the summary, inside a question and inside the reasoning layer. "Anywhere"
    is the word `docs/compatibility.md` uses, so the fixture puts one in each kind of place rather
    than only at the root, where a top-level-only relaxation would pass and still be broken."""
    m = _full_model()
    m["risk_register"] = [{"id": "R1", "text": "adoption"}]        # a new top-level collection
    m["model"]["workflow"]["provenance"] = "interview-3"           # a new field inside a slot
    m["summary"]["horizon"] = "two quarters"                       # a new field inside the summary
    m["questions"] = [{"q": "Who approves?", "slot": "workflow", "why": "unclear", "asked_at": "r2"}]
    m["decisions"] = [{"decision": "Approve-first", "derived_from": ["workflow"], "settled_at": "r1"}]
    return m


def test_a_model_written_by_a_newer_requivo_loads_and_survives_a_round_trip(workspace):
    """The forward half of invariant 8, for `model.json`. Before #14 this raised a bare pydantic
    `ValidationError` — `EngineOutput` inherits `extra="forbid"` from `StrictModel` — so a 0.9.x
    install in a mixed-version workspace could not open the session at all, and the failure arrived
    as a traceback rather than as anything a user could act on."""
    store.create_session("mixed", "A leave approval system.")
    d = store.canonical_dir("mixed")
    (d / "model.json").write_text(json.dumps(_model_from_the_future(), indent=2), encoding="utf-8")

    loaded = store.load_session_model("mixed")           # used to raise ValidationError
    assert loaded.summary.objective == "A leave approval system"
    assert loaded.model["workflow"].confidence.value == "empty"

    # Loading is only half of it. Under `extra="ignore"` the load succeeds and the unknown keys are
    # dropped the first time this version writes the file back, which turns "an old reader tolerates
    # a new field" into "an old reader destroys it" — the exact regression 0.9.4 fixed for
    # session.json. So the assertion is on what is on disk after a write.
    store.save_revision("mixed", loaded)
    for path in (d / "model.json", d / "revisions" / "0001-model.json"):
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["risk_register"] == [{"id": "R1", "text": "adoption"}], path.name
        assert written["model"]["workflow"]["provenance"] == "interview-3", path.name
        assert written["summary"]["horizon"] == "two quarters", path.name
        assert written["questions"][0]["asked_at"] == "r2", path.name
        assert written["decisions"][0]["settled_at"] == "r1", path.name


def test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save(workspace):
    """The half the first version of this fix got wrong, and the reason it is worth a second test.

    Making the *read* permissive is not enough: `resolve()` carries an unstated reasoning collection
    forward from the model being refined (invariant 10), so the carried items are permissive
    instances sitting under the strict tree's annotation — and pydantic serializes by the annotated
    type, so the unknown key stayed alive in memory and vanished on the next write. The same held
    for a key at the top level, which the proposal cannot speak to at all because it is
    `extra="forbid"`. Both turned the refusal #14 removed into a silent loss on the first ordinary
    turn, which is strictly worse: nothing failed and nothing said so."""
    store.create_session("refined", "A leave approval system.")
    d = store.canonical_dir("refined")
    (d / "model.json").write_text(json.dumps(_model_from_the_future(), indent=2), encoding="utf-8")
    store.save_revision("refined", store.load_session_model("refined"))   # now at revision 1

    # An ordinary refinement turn: the full slot set and a new objective, saying nothing about the
    # reasoning layer — the shape `engine.md` actually asks for.
    SessionService().update_model("refined", {**_full_model(), "summary": {"objective": "Refined"}})

    written = json.loads((d / "model.json").read_text(encoding="utf-8"))
    assert written["summary"]["objective"] == "Refined", "the turn did not land"
    assert written["decisions"][0]["settled_at"] == "r1"
    assert written["risk_register"] == [{"id": "R1", "text": "adoption"}]
    # And the narrowing that is real, pinned so it is a stated limit rather than a belief: the
    # slots, the summary and the questions come *from* the proposal, which replaces them wholesale,
    # so an unknown key inside one of those does not survive an apply. `docs/compatibility.md` says
    # exactly this; the assertion is here so the sentence cannot quietly stop being true.
    assert "provenance" not in written["model"]["workflow"]
    assert "horizon" not in written["summary"]


def test_the_provider_boundary_still_refuses_the_same_payload():
    """The positive control for the test above, and the reason the fix is a sibling contract rather
    than a relaxed flag on `StrictModel`. Invariant 4 is not collateral damage: the *same* keys that
    are carried when they come off disk must still be rejected when they come from a provider, where
    they mean a drifted prompt and where `_complete()` has a retry that can fix it."""
    for contract in (EngineOutput, ModelProposal):
        with pytest.raises(ValidationError) as ei:
            contract.model_validate(_model_from_the_future())
        assert "risk_register" in str(ei.value)
    # And permissive is not the same as credulous: the persisted contract still enforces the slot
    # vocabulary, because a slot id from the future is `schema_version`'s business and is refused
    # with its own message rather than absorbed as an unknown key.
    with pytest.raises(ValidationError):
        PersistedEngineOutput.model_validate(
            {**_full_model(), "model": {**_full_model()["model"], "real_problem": _slot()}})


def _contracts_reachable_from(cls: type[BaseModel]) -> set[type[BaseModel]]:
    """Every pydantic contract reachable from `cls` through its own fields, including `cls`."""
    found: set[type[BaseModel]] = set()

    def walk_type(ann) -> None:
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            walk_model(ann)
        for arg in typing.get_args(ann):
            walk_type(arg)

    def walk_model(model: type[BaseModel]) -> None:
        if model in found:
            return
        found.add(model)
        for field in model.model_fields.values():
            walk_type(field.annotation)

    walk_model(cls)
    return found


def test_the_persisted_contract_is_permissive_all_the_way_down():
    """The guard on the shape of the fix. `PersistedEngineOutput` mirrors the model tree class by
    class, and the failure mode of a hand-written mirror is a nested contract nobody remembered to
    twin — which does not fail loudly, it just re-forbids extras one level in. So the tree is walked
    rather than listed: add a nested contract to the model and this fails until it has a sibling.

    Both directions are asserted, because the point is the asymmetry and not either half of it."""
    persisted = _contracts_reachable_from(PersistedEngineOutput)
    strict = _contracts_reachable_from(EngineOutput)
    # A walk that finds nothing is an all-clear nobody earned; the model tree has seven contracts.
    assert len(persisted) == len(strict) >= 7
    assert [c.__name__ for c in persisted if c.model_config.get("extra") != "allow"] == []
    assert [c.__name__ for c in strict if c.model_config.get("extra") != "forbid"] == []


def test_the_persisted_mirror_copies_every_constraint_it_restates():
    """The sibling of the walk above, for the other half of a field's contract.

    That test compares `extra` policy; this one compares *constraints*, and the gap between them was
    a live bug: both trees carried a hand-written `max_length=6` on `questions` and nothing made them
    agree. The failure is asymmetric and does not need any version skew to bite — raise the strict cap
    to 8, miss the mirror, and a session **this build just wrote** with seven questions fails to load.
    That is the loud-failure-on-read #14 exists to remove, reintroduced one field along.

    The mirror cannot simply inherit its way out of this. Pydantic drops the parent's `FieldInfo`
    when a subclass re-annotates a field, so `questions: list[PersistedQuestion]` with no `Field(...)`
    loses the cap *and* the default factory — which would quietly make `questions` required, a worse
    failure than the drift. Restating is forced; what is not forced is restating it *correctly*, so
    that is what gets pinned."""
    pairs = [(p, p.__mro__[1]) for p in _contracts_reachable_from(PersistedEngineOutput)]
    for permissive, strict in pairs:
        assert issubclass(strict, BaseModel) and strict is not BaseModel, permissive.__name__
    # Not vacuous, on both counts: seven twins exist, and the mirror really does re-point six fields
    # at permissive types — which is exactly why it has to restate their constraints.
    assert len(pairs) == 7
    redeclared = {name for p, s in pairs for name in p.model_fields
                  if p.model_fields[name].annotation != s.model_fields[name].annotation}
    assert redeclared == {"model", "questions", "summary", "decisions", "challenges", "opportunities"}

    drift = []
    for permissive, strict in pairs:
        assert set(permissive.model_fields) == set(strict.model_fields), permissive.__name__
        for name, pf in permissive.model_fields.items():
            sf = strict.model_fields[name]
            if (pf.metadata != sf.metadata                                     # min/max length, ge/le…
                    or pf.is_required() != sf.is_required()
                    or (pf.default_factory is None) != (sf.default_factory is None)
                    or pf.default != sf.default):
                drift.append(f"{permissive.__name__}.{name}: {sf.metadata} vs {pf.metadata}")
    assert drift == []


def test_update_missing_session_raises(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().update_model("ghost", _full_model())


# ── legacy migration on first mutation ──────────────────────────────────────────


def test_a_legacy_session_is_named_in_the_error_rather_than_migrated_behind_your_back(workspace):
    """`out/` was the store until 0.8.0, and until 0.9.8 every read silently fell back to it and every
    mutation migrated one in place. That kept old sessions working without the user knowing, which is
    also what was wrong with it: the fallback ran on every read of every session for a layout nothing
    has written in two minor versions, and "where does this session live?" had two answers throughout
    the code. Migration is explicit now — so the one thing this layer still owes is an error that says
    which command to run, instead of a bare "no session"."""
    legacy = store.legacy_dir("old")
    legacy.mkdir(parents=True)
    (legacy / "model.json").write_text(json.dumps(_full_model()))
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")

    svc = SessionService()
    assert not svc.exists("old")
    with pytest.raises(SessionNotFoundError) as e:
        svc.load_model("old")
    assert e.value.details.get("legacy") is True
    assert "session migrate" in str(e.value)

    # And the explicit migration is intact: the model becomes revision 1, artifacts come with it,
    # and the originals are left where they were.
    store.migrate_legacy("old")
    assert store.session_exists("old")
    assert (legacy / "model.json").exists()
    result = svc.update_model("old", _full_model(**{"problem": _slot(90, "explicit", "high", "P")}))
    assert result.revision == 2
    d = store.canonical_dir("old")
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"


# ── SessionRepository: the storage seam (proves the service is backing-agnostic) ──
# The point of the seam is a non-filesystem backing: the same SessionService orchestration must run on
# backing, not just files. This in-memory repository stands in for that non-file backing — if the
# service works against it with zero filesystem, the orchestration is genuinely storage-agnostic.
#
# The behavioural assertions that used to live in one bespoke test here now live in
# `requivo.testing.repository_conformance.SessionRepositoryConformance` (#424) — importable and
# runnable by an out-of-repo `SessionRepository` implementation, not just this module. What remains
# here is the factory wiring (`make_repository`, below) plus the one test that is specific to *this*
# repo's own services, `test_session_service_runs_unchanged_on_a_non_file_repository`: it exercises
# `ArtifactService`'s dependency-graph staleness and provenance recording end to end, which is a claim
# about `SessionService`/`ArtifactService` orchestration rather than about the storage seam's own
# contract, so it does not belong in a suite an external backing is meant to run against itself.
from requivo.core.errors import RevisionConflictError as _RevConflict  # noqa: E402
from requivo.core.errors import SessionExistsError as _SessionExists  # noqa: E402
from requivo.core.errors import SessionNotFoundError as _NotFound  # noqa: E402
from requivo.core.persistence import ArtifactStatus, RevisionRecord, SessionMeta  # noqa: E402
from requivo.services.repository import FileSessionRepository, SessionRepository  # noqa: E402
from requivo.testing.repository_conformance import SessionRepositoryConformance  # noqa: E402


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory. A faithful stand-in
    for a Postgres backing (everything is mutation-backed, so has_meta == exists, ensure_writable is a
    no-op check)."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}
        self._locks: dict = {}                 # slug -> threading.RLock, created on first use
        self._locks_guard = threading.Lock()    # protects _locks itself, not any session's data

    def _lock_for(self, slug):
        # threading.RLock is re-entrant *per thread* by construction -- the same primitive this
        # backing needs for invariant 9's two halves at once (mutual exclusion across threads,
        # re-entrancy within one). A bare `yield` here used to be the whole implementation, on the
        # reasoning that a single dict mutation needs no lock -- true, and beside the point: the
        # *caller* (SessionService) takes this lock around several such mutations and depends on
        # nothing else interleaving with the whole sequence, which a no-op cannot provide. Found by
        # `requivo.testing.repository_conformance.SessionRepositoryConformance` (#424), which this
        # fake did not pass before this fix.
        with self._locks_guard:
            if slug not in self._locks:
                self._locks[slug] = threading.RLock()
            return self._locks[slug]

    @contextlib.contextmanager
    def lock(self, slug):
        lk = self._lock_for(slug)
        lk.acquire()
        try:
            yield
        finally:
            lk.release()

    def exists(self, slug): return slug in self._meta
    def has_meta(self, slug): return slug in self._meta

    def ensure_writable(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None):
        # Invariant 11, at this backing's own layer: the claim is a dict-key collision check rather
        # than a rename, but it owes the same answer -- a second create() on a slug already in the
        # store must not silently overwrite it. Added for the conformance suite (#424); before it
        # this method had no such check and lost the first session's identity on a collision, the
        # exact bug invariant 11 documents for the file backing's pre-fix `has_meta`-then-create.
        if slug in self._meta:
            raise _SessionExists(f"session '{slug}' already exists", details={"slug": slug})
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards)
        self._meta[slug] = meta
        self._req[slug] = request
        self._art[slug] = {}
        return meta

    def read_meta(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        return self._meta[slug]

    def delete(self, slug):
        # The dict-backed analogue of the file backing's lock-then-remove: a Postgres row delete has
        # no lock file to unlink, but it owes the identical release-the-slug guarantee the conformance
        # suite checks (#238) -- a stale key left in any of these dicts would make a later create()
        # for the same slug collide with residue this session left behind.
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        with self.lock(slug):
            self._meta.pop(slug, None)
            self._model.pop(slug, None)
            self._req.pop(slug, None)
            self._art.pop(slug, None)
            for key in [k for k in self._revs if k[0] == slug]:
                self._revs.pop(key, None)

    def write_meta(self, slug, meta): self._meta[slug] = meta
    def list_slugs(self): return sorted(self._meta)

    def list_unexaminable(self):
        # `[]`, and it is a real answer rather than a stub: a dict key either is a session or is not
        # there, so the question this method exists for cannot arise on this backing (#80). What
        # would be wrong is dropping a row that *was* enumerated and could not be decoded — see the
        # protocol's docstring; there are none here to drop.
        return []

    def load_model(self, slug):
        if slug not in self._model:
            raise _NotFound(f"no model '{slug}'", details={"slug": slug})
        return self._model[slug]

    def load_revision(self, slug, revision):
        if (slug, revision) not in self._revs:
            raise _NotFound(f"no revision {revision}", details={"slug": slug, "revision": revision})
        return self._revs[(slug, revision)]

    def save_revision(self, slug, model, *, expected_revision=None, provenance=None):
        meta = self.read_meta(slug)
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise _RevConflict("conflict", details={"expected": expected_revision,
                                                    "actual": meta.current_revision})
        rev = meta.current_revision + 1
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev, created_at="t", previous_revision=meta.current_revision or None,
            model_hash="sha256:mem", provider=prov.get("provider"), model_name=prov.get("model_name"),
            surface=prov.get("surface"), prompt_version=prov.get("prompt_version")))
        meta.current_revision = rev
        self._model[slug] = model
        self._revs[(slug, rev)] = model
        return rev, meta

    def request_text(self, slug): return self._req.get(slug, "")
    def context_cards(self, slug): return self._meta[slug].context_cards if slug in self._meta else None

    def save_artifact(self, slug, artifact_type, filename, content, *, source_revision, stale=False):
        self._art[slug][filename] = content
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at="t", stale=stale)
        self._meta[slug].artifact_status[artifact_type] = st
        return st

    def load_artifact(self, slug, filename): return self._art.get(slug, {}).get(filename)


class TestInMemoryRepositoryConformance(SessionRepositoryConformance):
    """The non-file backing above, proven against the shared suite -- the factory wiring #424's
    acceptance criteria ask this module to shrink to."""

    def make_repository(self):
        return InMemorySessionRepository()


class TestFileRepositoryConformance(SessionRepositoryConformance):
    """The shipped file backing, against the same shared suite -- both implementations this repo
    carries run identically against `SessionRepositoryConformance`, which is the point: a third
    implementation (Postgres, out of this repo) has the same bar to clear."""

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path):
        self._root = tmp_path

    def make_repository(self):
        return FileSessionRepository(root=self._root)


def test_session_service_runs_unchanged_on_a_non_file_repository():
    from requivo.services.sessions import SessionService
    repo = InMemorySessionRepository()
    assert isinstance(repo, SessionRepository)          # satisfies the protocol (runtime-checkable)
    svc = SessionService(repo)

    svc.create_session("a leave request", slug="leave-mem")
    r1 = svc.update_model("leave-mem", out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                          provenance={"provider": "anthropic", "surface": "cli-discover"})
    assert r1.revision == 1

    # artifact tracking + dependency-graph staleness, entirely in memory
    ArtifactService(repo).save("leave-mem", "criteria", "# c", source_revision=1)   # criteria consumes workflow
    r2 = svc.update_model(
        "leave-mem", out({"workflow": {**slot(95, "explicit", "high"), "value": "a → b"}}).model_dump())
    assert r2.revision == 2
    assert ArtifactService(repo).list("leave-mem")["criteria"]["stale"] is True

    # optimistic locking is enforced by the backing, not the file layout
    with pytest.raises(_RevConflict):
        svc.update_model("leave-mem", out({"workflow": slot(95, "explicit", "high")}).model_dump(),
                         expected_revision=0)

    # the rich status projection needs no filesystem either
    st = svc.status("leave-mem")
    assert st["revision"] == 2 and "understanding" in st
    # provenance is recorded per revision on the non-file backing
    assert [rr.surface for rr in svc.meta("leave-mem").revisions] == ["cli-discover", None]
