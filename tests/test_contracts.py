"""The boundary contracts: the slot vocabulary, and the pointers that have to point at something (#72)."""
import pytest
from _fakes import out, slot
from pydantic import ValidationError

from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    DesignDecision,
    EngineOutput,
    Epic,
    EstimateDraft,
    Exclusion,
    Opportunity,
    Threshold,
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
    # A slot id the schema doesn't define (typo / hallucination) is rejected at the contract.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"real_problem": slot(80, "explicit", "high")},
            "questions": [], "summary": {},
        })


def test_output_allows_a_partial_but_known_model():
    # Completeness (the full required set) is enforced at the discovery boundary, NOT the contract.
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
    ("thresholds", [{"condition": "c", "measure": "m", "action": "a", "rests_on": ["not_a_slot"]}]),
], ids=["question", "decision", "challenge", "exclusion", "threshold"])
def test_output_rejects_a_pointer_to_an_unknown_slot(extra_key, extra_value):
    # A question/decision/challenge must point at a slot the schema defines.
    base = {"model": {"workflow": slot(60, "inferred", "high")}, "questions": [], "summary": {}}
    base[extra_key] = extra_value
    with pytest.raises(ValidationError):
        EngineOutput.model_validate(base)


def test_contracts_reject_a_field_the_schema_does_not_define():
    """Invariant 4: boundary contracts are strict (#286)."""
    # Pydantic's default is to drop unknown keys.
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
    # A challenge without its alternative or recommendation is an objection with nowhere to go.
    base = {"headline": "h", "premise": "p", "alternative": "a", "consequence": "c", "recommendation": "r"}
    for missing in ("premise", "alternative", "consequence", "recommendation"):
        with pytest.raises(ValidationError):
            Challenge.model_validate({**base, missing: ""})


def test_a_testable_slot_with_no_test_plan_is_refused():
    """#610: "testable" with nothing naming what would settle it is `empty` with better manners."""
    with pytest.raises(ValidationError):
        out({"problem": {"completeness": 0, "confidence": "testable", "impact": "high"}})


def test_a_testable_slot_naming_its_test_plan_is_accepted():
    # The positive control: a settling condition is all "testable" asks for.
    model = out({"problem": {"completeness": 0, "confidence": "testable", "impact": "high",
                             "test_plan": "Ship a waitlist page and see whether 20 people sign up."}})
    assert model.model["problem"].confidence.value == "testable"


def test_reasoning_items_carry_a_stable_content_derived_id():
    """Invariant 5: `DesignDecision`, `Challenge` and `Opportunity` carry an `id` recomputed from their own
    text on every validation (#286)."""
    # A consumer will want to refer back to a decision.
    d1 = DesignDecision.model_validate({"decision": "Draft-first", "derived_from": ["permissions"]})
    d2 = DesignDecision.model_validate({"decision": "Draft-first", "why": "different rationale"})
    assert d1.id.startswith("dec_") and d1.id == d2.id            # same statement → same handle
    assert d1.id != DesignDecision.model_validate({"decision": "Approve-first"}).id
    # Survives the round-trip through model.json unchanged…
    assert DesignDecision.model_validate_json(d1.model_dump_json()).id == d1.id
    # …and a supplied id is never trusted: it is recomputed from the content.
    assert DesignDecision.model_validate({"decision": "Draft-first", "id": "dec_forged"}).id == d1.id


def test_reasoning_ids_are_distinct_per_kind():
    c = Challenge.model_validate({"headline": "h", "premise": "p", "alternative": "a",
                                  "consequence": "c", "recommendation": "r"})
    o = Opportunity.model_validate({"text": "reuse the notification service", "leverage": "high"})
    e = Exclusion.model_validate({"option": "Bulk import", "reason": "out of scope for v1"})
    t = Threshold.model_validate({"condition": "CAC exceeds 40", "measure": "CAC",
                                  "action": "stop the paid channel", "rests_on": ["workflow"]})
    assert (c.id.startswith("chl_") and o.id.startswith("opp_") and e.id.startswith("exc_")
           and t.id.startswith("thr_"))


def test_an_exclusion_is_a_fourth_reasoning_item_with_a_stable_content_derived_id():
    """#599: an excluded option gets the same identity treatment as its three siblings (invariant 5)."""
    e1 = Exclusion.model_validate({"option": "Bulk import", "reason": "Out of scope for v1",
                                   "rests_on": ["workflow"]})
    e2 = Exclusion.model_validate({"option": "Bulk import", "reason": "Out of scope for v1",
                                   "id": "exc_forged"})
    assert e1.id == e2.id
    assert e1.id != Exclusion.model_validate({"option": "SSO", "reason": "Not asked for"}).id


def test_brief_carries_typed_exclusions_it_can_propose_600():
    """#600: a generator populates exclusions through `Brief`, the same typed `Exclusion` #599 gave a home to
    model.json — not prose."""
    assert Brief(complexity="low").exclusions == []
    brief = Brief(complexity="low", exclusions=[Exclusion.model_validate(
        {"option": "A full audit-trail UI", "reason": "The stated timeline funds the approval "
         "workflow only", "rests_on": ["constraints"]})])
    assert brief.exclusions[0].id.startswith("exc_")
    assert brief.exclusions[0].rests_on == ["constraints"]


def test_a_threshold_is_a_fifth_reasoning_item_with_a_stable_content_derived_id():
    """#604: a decision threshold gets the same identity treatment as its four siblings (invariant 5)."""
    t1 = Threshold.model_validate({"condition": "CAC exceeds 40", "measure": "CAC",
                                   "action": "stop the paid channel", "rests_on": ["workflow"]})
    t2 = Threshold.model_validate({"condition": "CAC exceeds 40", "measure": "different measure",
                                   "action": "stop the paid channel", "rests_on": ["problem"],
                                   "id": "thr_forged"})
    assert t1.id == t2.id
    assert t1.id != Threshold.model_validate(
        {"condition": "Rate limit drops below 100", "measure": "vendor API rate limit",
         "action": "reopen build-versus-buy", "rests_on": ["workflow"]}).id


def test_a_threshold_with_no_action_or_no_slot_it_rests_on_is_refused():
    """#604 acceptance criterion: "a condition with no action, or no slot it rests on, is refused"."""
    base = {"condition": "CAC exceeds 40", "measure": "CAC", "action": "stop the paid channel",
           "rests_on": ["workflow"]}
    with pytest.raises(ValidationError):
        Threshold.model_validate({**base, "action": ""})
    with pytest.raises(ValidationError):
        Threshold.model_validate({**base, "rests_on": []})


def test_brief_carries_typed_thresholds_it_can_propose_604():
    """#604, mirroring #600: a generator populates thresholds through `Brief`, the same typed `Threshold` this
    issue gave a home to model.json — not prose."""
    assert Brief(complexity="low").thresholds == []
    brief = Brief(complexity="low", thresholds=[Threshold.model_validate(
        {"condition": "CAC exceeds the stated budget ceiling", "measure": "cost per paid signup",
         "action": "stop the paid channel", "rests_on": ["constraints"]})])
    assert brief.thresholds[0].id.startswith("thr_")
    assert brief.thresholds[0].rests_on == ["constraints"]


# ── artifact contracts: references that point at something ───────────────────
# These are *structural* rules, never judgments about content.


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
        # Not a wide estimate — a broken one.
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


# -- the PRD envelope (#603) ---------------------------------------------------
# The same honesty split `confidence` already draws for slots.


def test_an_envelope_element_s_origin_and_source_slot_must_agree():
    """A slot-sourced element with no slot, or an assumed one that names one anyway, is a provenance claim
    that contradicts itself -- the exact confusion this field exists to end."""
    from requivo.core.contracts import EnvelopeElement

    EnvelopeElement(kind="Budget", value="50k", origin="slot", source_slot="constraints")
    EnvelopeElement(kind="Team size", value="3 developers", origin="assumption")
    with pytest.raises(ValidationError):
        EnvelopeElement(kind="Budget", value="50k", origin="slot")
    with pytest.raises(ValidationError):
        EnvelopeElement(kind="Team size", value="3 developers", origin="assumption",
                        source_slot="constraints")


def test_a_prd_envelope_cannot_cite_a_slot_the_schema_does_not_define():
    element = {"kind": "Budget", "value": "50k", "origin": "slot", "source_slot": "not_a_slot"}
    with pytest.raises(ValidationError):
        PRD(title="X", problem="P", envelope=[element])


def test_a_prd_with_no_constraint_content_has_an_empty_envelope_by_default():
    """Acceptance criterion (#603): a model with nothing to say about its envelope must not have one invented
    for it -- `envelope` defaults to empty rather than requiring a provider to fill it."""
    assert PRD(title="X", problem="P").envelope == []


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
    """A verdict and a payload that disagree is what this contract exists to catch (#593)."""
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
