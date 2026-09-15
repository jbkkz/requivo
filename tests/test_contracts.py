"""The boundary contracts: the slot vocabulary, and the pointers that have to point at something.

Split out of `test_engine.py` (#72). Two groups, both about `core/contracts.py` and neither of them
needing a provider, a session or a filesystem:

* **Vocabulary and strictness** — invariant 4. Everything an LLM fills inherits `StrictModel`, every
  slot id is checked against the schema, and a field the model invented fails loudly instead of being
  dropped.
* **References that resolve** — the structural rules at the foot of the file, each one guarding a
  field something downstream follows.
"""
import pytest
from _fakes import out, slot
from pydantic import ValidationError

from requivo.core.contracts import (
    AcceptanceCriteria,
    Challenge,
    DesignDecision,
    EngineOutput,
    Epic,
    EstimateDraft,
    Exclusion,
    Opportunity,
)

# ── Contract validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("completeness, confidence", [
    (150, "explicit"),
    (10, "maybe"),
], ids=["out-of-range-completeness", "unknown-confidence"])
def test_output_rejects_a_slot_with_an_invalid_field(completeness, confidence):
    with pytest.raises(ValidationError):
        out({"problem": slot(completeness, confidence, "high")})


def test_output_requires_model():
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({"questions": [], "summary": {}})


def test_output_rejects_unknown_slots():
    # A slot id the schema doesn't define (typo / hallucination) is rejected at the contract, so it
    # can never sit in the model unseen by the schema-driven views. `real_problem` is the classic typo.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"real_problem": slot(80, "explicit", "high")},
            "questions": [], "summary": {},
        })


def test_output_allows_a_partial_but_known_model():
    # Completeness (the full required set) is enforced at the discovery boundary, NOT the contract —
    # internal projections (diff/propagate) legitimately carry a subset of *known* slots.
    part = EngineOutput.model_validate({
        "model": {"workflow": slot(60, "inferred", "high")},
        "questions": [], "summary": {},
    })
    assert set(part.model) == {"workflow"}


def _q(slot_id, i=0):
    return {"q": f"question {i}", "slot": slot_id, "why": "uncertainty × impact"}


def test_output_caps_questions_at_six():
    # The engine asks at most 6, sorted by information value; a 7th means it stopped prioritising.
    base = {"model": {"workflow": slot(60, "inferred", "high")}, "summary": {}}
    ok = EngineOutput.model_validate({**base, "questions": [_q("workflow", i) for i in range(6)]})
    assert len(ok.questions) == 6
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({**base, "questions": [_q("workflow", i) for i in range(7)]})


@pytest.mark.parametrize("extra_key, extra_value", [
    ("questions", [_q("not_a_slot")]),
    ("decisions", [{"decision": "X", "derived_from": ["not_a_slot"]}]),
    ("challenges", [{"headline": "h", "premise": "p", "alternative": "a", "consequence": "c",
                     "recommendation": "r", "contests": ["not_a_slot"]}]),
    ("exclusions", [{"option": "Bulk import", "reason": "r", "rests_on": ["not_a_slot"]}]),
], ids=["question", "decision", "challenge", "exclusion"])
def test_output_rejects_a_pointer_to_an_unknown_slot(extra_key, extra_value):
    # A question/decision/challenge must point at a slot the schema defines — a dangling pointer would
    # make the dependency graph look rigorous while pointing at nothing.
    base = {"model": {"workflow": slot(60, "inferred", "high")}, "questions": [], "summary": {}}
    base[extra_key] = extra_value
    with pytest.raises(ValidationError):
        EngineOutput.model_validate(base)


def test_contracts_reject_a_field_the_schema_does_not_define():
    """Invariant 4: boundary contracts are strict — everything an LLM fills inherits `StrictModel`;
    an invented field must fail loudly and ride the retry loop, not be silently discarded.
    Completeness lives at the discovery boundary instead, since a partial `EngineOutput` is legitimate
    (`test_output_allows_a_partial_but_known_model` is that half; moved here from CLAUDE.md by
    #286)."""
    # Pydantic's default is to drop unknown keys. For an LLM boundary that is the wrong default: the
    # output reads as conformant while carrying less than the model produced, and a prompt that has
    # drifted from its contract looks like a clean success. Rejecting also lets the retry loop tell the
    # model what it got wrong.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"workflow": slot(60, "inferred", "high")},
            "questions": [], "summary": {}, "confidence_score": 0.8,   # invented field
        })
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"workflow": {**slot(60, "inferred", "high"), "source": "guessed"}},
            "questions": [], "summary": {},
        })


def test_contracts_reject_an_empty_question():
    # A question with no text, or no rationale, would still be rendered, counted and answered against.
    for bad in ({"q": "", "slot": "workflow", "why": "w"}, {"q": "Q?", "slot": "workflow", "why": ""}):
        with pytest.raises(ValidationError):
            EngineOutput.model_validate({
                "model": {"workflow": slot(60, "inferred", "high")},
                "questions": [bad], "summary": {},
            })


def test_contracts_reject_a_challenge_missing_a_load_bearing_part():
    # A challenge without its alternative or recommendation is an objection with nowhere to go — and
    # it renders in the assessment as though it were actionable.
    base = {"headline": "h", "premise": "p", "alternative": "a", "consequence": "c", "recommendation": "r"}
    for missing in ("premise", "alternative", "consequence", "recommendation"):
        with pytest.raises(ValidationError):
            Challenge.model_validate({**base, missing: ""})


def test_reasoning_items_carry_a_stable_content_derived_id():
    """Invariant 5: `DesignDecision`, `Challenge` and `Opportunity` carry an `id` recomputed from
    their own text on every validation. A supplied one is never trusted — an LLM that echoes a
    stale id back would otherwise let two different decisions share a handle (moved here from
    CLAUDE.md by #286)."""
    # A consumer will want to refer back to a decision — comment on it, mark it accepted, follow it across
    # revisions — and text is a poor handle. The id is derived from the content, so it is identical
    # across revisions, surfaces and machines for as long as the statement is unchanged.
    d1 = DesignDecision.model_validate({"decision": "Draft-first", "derived_from": ["permissions"]})
    d2 = DesignDecision.model_validate({"decision": "Draft-first", "why": "different rationale"})
    assert d1.id.startswith("dec_") and d1.id == d2.id            # same statement → same handle
    assert d1.id != DesignDecision.model_validate({"decision": "Approve-first"}).id
    # Survives the round-trip through model.json unchanged…
    assert DesignDecision.model_validate_json(d1.model_dump_json()).id == d1.id
    # …and a supplied id is never trusted: it is recomputed from the content, so a model (or a
    # hand-edited session file) cannot invent an identity for a statement.
    assert DesignDecision.model_validate({"decision": "Draft-first", "id": "dec_forged"}).id == d1.id


def test_reasoning_ids_are_distinct_per_kind():
    c = Challenge.model_validate({"headline": "h", "premise": "p", "alternative": "a",
                                  "consequence": "c", "recommendation": "r"})
    o = Opportunity.model_validate({"text": "reuse the notification service", "leverage": "high"})
    e = Exclusion.model_validate({"option": "Bulk import", "reason": "out of scope for v1"})
    assert c.id.startswith("chl_") and o.id.startswith("opp_") and e.id.startswith("exc_")


def test_an_exclusion_is_a_fourth_reasoning_item_with_a_stable_content_derived_id():
    """#599: an excluded option gets the same identity treatment as its three siblings
    (invariant 5) — a supplied id is never trusted, and the same option/reason yields the
    same id regardless of `rests_on`."""
    e1 = Exclusion.model_validate({"option": "Bulk import", "reason": "Out of scope for v1",
                                   "rests_on": ["workflow"]})
    e2 = Exclusion.model_validate({"option": "Bulk import", "reason": "Out of scope for v1",
                                   "id": "exc_forged"})
    assert e1.id == e2.id
    assert e1.id != Exclusion.model_validate({"option": "SSO", "reason": "Not asked for"}).id


# ── artifact contracts: references that point at something ───────────────────
# These are *structural* rules, never judgments about content. Each one exists because the field is a
# pointer that something downstream follows: an estimate finds its story by id, a tracker turns
# `depends_on` into a real link, a story's `slots` is the trace back to the model. A pointer that
# resolves to nothing survives every render looking exactly like one that resolves.


def test_a_story_cannot_be_traced_to_a_slot_that_does_not_exist():
    from requivo.core.contracts import Story

    Story(id="S1", title="Approve leave", slots=["workflow"])           # a real slot is fine
    with pytest.raises(ValidationError):
        Story(id="S1", title="Approve leave", slots=["not-a-slot"])


def test_stories_and_estimate_items_cannot_repeat_an_id():
    from requivo.core.contracts import EstimateItem, Stories, Story

    with pytest.raises(ValidationError):
        Stories(stories=[Story(id="S1", title="One"), Story(id="S1", title="Two")])
    with pytest.raises(ValidationError):
        EstimateDraft(items=[
            EstimateItem(story_id="S1", title="One", complexity="M", days_low=1, days_high=2),
            EstimateItem(story_id="S1", title="Two", complexity="M", days_low=1, days_high=2)])


def test_an_estimate_range_cannot_be_inverted():
    from requivo.core.contracts import EstimateItem

    EstimateItem(story_id="S1", title="X", complexity="M", days_low=1, days_high=5)
    with pytest.raises(ValidationError):
        # Not a wide estimate — a broken one. The totals sum both ends, so this drags the project's
        # low bound above its high bound and the spread reads backwards.
        EstimateItem(story_id="S1", title="X", complexity="M", days_low=5, days_high=1)


def test_an_epic_cannot_depend_on_an_issue_it_does_not_contain():
    from requivo.core.contracts import EpicIssue

    ok = Epic(title="E", issues=[EpicIssue(id="#1", title="First"),
                                 EpicIssue(id="#2", title="Second", depends_on=["#1"])])
    assert len(ok.issues) == 2
    with pytest.raises(ValidationError):
        Epic(title="E", issues=[EpicIssue(id="#2", title="Second", depends_on=["#404"])])
    with pytest.raises(ValidationError):
        Epic(title="E", issues=[EpicIssue(id="#1", title="First", depends_on=["#1"])])


def test_acceptance_criteria_need_a_scenario_per_feature_and_unique_scenario_ids():
    from requivo.core.contracts import Feature, Scenario

    sc = Scenario(id="AC-1", title="Happy", when="the manager approves", then=["it is approved"])
    with pytest.raises(ValidationError):
        Feature(name="Approval", scenarios=[])           # a heading with nothing to test
    with pytest.raises(ValidationError):
        AcceptanceCriteria(title="T", features=[Feature(name="A", scenarios=[sc]),
                                                Feature(name="B", scenarios=[sc])])


def test_a_prd_requirement_cannot_be_an_empty_row():
    from requivo.core.contracts import Requirement

    with pytest.raises(ValidationError):
        Requirement(id="", requirement="", priority="must")


# ── the grounding judgment (#593) ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload, why", [
    ({"decision": "installed", "reason": "r"},
     "installed with no cards selects nothing, which reads as every card downstream"),
    ({"decision": "none", "reason": "r", "cards": ["b2b-platform"]},
     "none names a card, so the verdict and the payload disagree"),
    ({"decision": "uncovered", "reason": "r", "cards": ["b2b-platform"]},
     "uncovered names a card it has just said covers nothing"),
])
def test_a_judgment_whose_payload_contradicts_its_decision_is_refused(payload, why):
    """A verdict and a payload that disagree is what this contract exists to catch. The empty-
    `installed` case is invariant 3's "refuse, don't filter" one call earlier: an empty selection
    widens the context to everything instead of narrowing it (#593)."""
    from requivo.core.contracts import ContextJudgment

    with pytest.raises(ValidationError):
        ContextJudgment.model_validate(payload)


def test_the_three_judgments_that_agree_with_themselves_all_validate():
    """The must-fire control: a validator that refused everything would pass the test above."""
    from requivo.core.contracts import ContextDecision, ContextJudgment

    assert ContextJudgment.model_validate({"decision": "none", "reason": "r"}).cards == []
    assert ContextJudgment.model_validate({"decision": "uncovered", "reason": "r"}).cards == []
    installed = ContextJudgment.model_validate(
        {"decision": "installed", "reason": "r", "cards": ["b2b-platform"]})
    assert installed.decision is ContextDecision.installed
