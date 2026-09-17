"""The boundary contracts, the driver `uncertainty × impact` feeds, and readiness as one boolean (#72, #165)."""
import re

import pytest
from _fakes import out, printed, slot
from pydantic import ValidationError

from requivo.core.analysis import (
    estimate_confidence,
    model_status,
    readiness_blockers,
    soft_slots,
    state_of,
    understanding_view,
)
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    ContextDecision,
    ContextJudgment,
    DesignDecision,
    EngineOutput,
    EnvelopeElement,
    Epic,
    EpicIssue,
    EstimateDraft,
    EstimateItem,
    Exclusion,
    Feature,
    Opportunity,
    Requirement,
    Scenario,
    Slot,
    Stories,
    Story,
    Threshold,
    _schema_order,
    schema_slot_ids,
)
from requivo.render.markdown import brief_markdown
from requivo.render.terminal import render_readiness, render_turn
from requivo.web.viewmodels.status import readiness_view

# ── the engine output ──────────────────────────────────────────────────────────

_ONE_SLOT = {"model": {"workflow": slot(60, "inferred", "high")}, "questions": [], "summary": {}}
_CHALLENGE = {"headline": "h", "premise": "p", "alternative": "a", "consequence": "c", "recommendation": "r"}
_THRESHOLD = {"condition": "CAC exceeds 40", "measure": "CAC", "action": "stop the paid channel", "rests_on": ["workflow"]}
_SCENARIO = Scenario(id="AC-1", title="Happy", when="the manager approves", then=["it is approved"])
_ITEM = dict(story_id="S1", title="One", complexity="M", days_low=1, days_high=2)


def _q(slot_id, i=0):
    return {"q": f"question {i}", "slot": slot_id, "why": "uncertainty × impact"}


@pytest.mark.parametrize("payload", [
    {"questions": [], "summary": {}},
    {"model": {"real_problem": slot(80, "explicit", "high")}, "questions": [], "summary": {}},
    {**_ONE_SLOT, "model": {"problem": slot(150, "explicit", "high")}},
    {**_ONE_SLOT, "model": {"problem": slot(10, "maybe", "high")}},
    {**_ONE_SLOT, "questions": [_q("workflow", i) for i in range(7)]},
    {**_ONE_SLOT, "questions": [{"q": "", "slot": "workflow", "why": "w"}]},
    {**_ONE_SLOT, "questions": [{"q": "Q?", "slot": "workflow", "why": ""}]},
    {**_ONE_SLOT, "questions": [_q("not_a_slot")]},
    {**_ONE_SLOT, "decisions": [{"decision": "X", "derived_from": ["not_a_slot"]}]},
    {**_ONE_SLOT, "challenges": [{**_CHALLENGE, "contests": ["not_a_slot"]}]},
    {**_ONE_SLOT, "exclusions": [{"option": "Bulk import", "reason": "r", "rests_on": ["not_a_slot"]}]},
    {**_ONE_SLOT, "thresholds": [{**_THRESHOLD, "rests_on": ["not_a_slot"]}]},
], ids=["no-model", "unknown-slot", "completeness-out-of-range", "unknown-confidence", "seven-questions",
        "empty-question", "empty-why", "question-pointer", "decision-pointer", "challenge-pointer",
        "exclusion-pointer", "threshold-pointer"])
def test_output_refuses_a_malformed_payload_or_a_pointer_to_nothing(payload):
    with pytest.raises(ValidationError):
        EngineOutput.model_validate(payload)


def test_output_allows_a_partial_but_known_model_and_six_questions():
    """Completeness is enforced at the discovery boundary, not the contract (invariant 4); six is the cap."""
    ok = EngineOutput.model_validate({**_ONE_SLOT, "questions": [_q("workflow", i) for i in range(6)]})
    assert set(ok.model) == {"workflow"} and len(ok.questions) == 6


def test_contracts_reject_a_field_the_schema_does_not_define():
    """Invariant 4: boundary contracts are strict (#286), at the top level and inside a slot."""
    for payload in ({**_ONE_SLOT, "confidence_score": 0.8}, {**_ONE_SLOT, "model": {"workflow": {**slot(60, "inferred", "high"), "source": "guessed"}}}):
        with pytest.raises(ValidationError):
            EngineOutput.model_validate(payload)


def test_a_testable_slot_with_no_test_plan_is_refused():
    """#610: "testable" with nothing naming what would settle it is `empty` with better manners."""
    with pytest.raises(ValidationError):
        out({"problem": slot(0, "testable", "high")})
    plan = "Ship a waitlist page and see whether 20 people sign up."
    assert out({"problem": slot(0, "testable", "high", test_plan=plan)}).model["problem"].confidence.value == "testable"


# ── reasoning items and their ids (invariant 5) ────────────────────────────────

_KINDS = {
    "decision": (DesignDecision, "dec_", {"decision": "Draft-first", "derived_from": ["permissions"]},
                 {"decision": "Draft-first", "why": "different rationale"}, {"decision": "Approve-first"}),
    "challenge": (Challenge, "chl_", _CHALLENGE, {**_CHALLENGE, "alternative": "other"}, {**_CHALLENGE, "headline": "g"}),
    "opportunity": (Opportunity, "opp_", {"text": "reuse the notification service", "leverage": "high"},
                    {"text": "reuse the notification service", "leverage": "future"}, {"text": "other", "leverage": "high"}),
    "exclusion": (Exclusion, "exc_", {"option": "Bulk import", "reason": "Out of scope for v1", "rests_on": ["workflow"]},
                  {"option": "Bulk import", "reason": "Out of scope for v1"}, {"option": "SSO", "reason": "Not asked for"}),
    "threshold": (Threshold, "thr_", _THRESHOLD, {**_THRESHOLD, "measure": "different", "rests_on": ["problem"]},
                  {**_THRESHOLD, "condition": "Rate limit drops below 100"}),
}


@pytest.mark.parametrize("kind", list(_KINDS))
def test_reasoning_items_carry_a_stable_content_derived_id(kind):
    """Invariant 5 (#286, #599, #604): the id is recomputed from the item's own text; a supplied one is never trusted."""
    cls, prefix, a, same, other = _KINDS[kind]
    i1, i2 = cls.model_validate(a), cls.model_validate({**same, "id": f"{prefix}forged"})
    assert i1.id.startswith(prefix) and i1.id == i2.id
    assert i1.id != cls.model_validate(other).id
    assert cls.model_validate_json(i1.model_dump_json()).id == i1.id


@pytest.mark.parametrize("field, item", [
    ("exclusions", Exclusion.model_validate({"option": "A full audit-trail UI", "reason": "The timeline funds the workflow only", "rests_on": ["constraints"]})),
    ("thresholds", Threshold.model_validate({**_THRESHOLD, "rests_on": ["constraints"]})),
], ids=["exclusions-600", "thresholds-604"])
def test_brief_carries_typed_exclusions_it_can_propose_600(field, item):
    """#600, #604: a generator populates exclusions and thresholds through `Brief` as typed items, not prose."""
    assert getattr(Brief(complexity="low"), field) == []
    [carried] = getattr(Brief(complexity="low", **{field: [item]}), field)
    assert carried.id == item.id and carried.rests_on == ["constraints"]


# ── artifact contracts: structural rules, never judgments about content ────────


@pytest.mark.parametrize("build", [
    lambda: Challenge.model_validate({**_CHALLENGE, "premise": ""}),
    lambda: Challenge.model_validate({**_CHALLENGE, "recommendation": ""}),
    lambda: Threshold.model_validate({**_THRESHOLD, "action": ""}),
    lambda: Threshold.model_validate({**_THRESHOLD, "rests_on": []}),
    lambda: Story(id="S1", title="Approve leave", slots=["not-a-slot"]),
    lambda: Stories(stories=[Story(id="S1", title="One"), Story(id="S1", title="Two")]),
    lambda: EstimateDraft(items=[EstimateItem(**_ITEM), EstimateItem(**{**_ITEM, "title": "Two"})]),
    lambda: EstimateItem(**{**_ITEM, "days_low": 5, "days_high": 1}),
    lambda: Epic(title="E", issues=[EpicIssue(id="#2", title="Second", depends_on=["#404"])]),
    lambda: Epic(title="E", issues=[EpicIssue(id="#1", title="First", depends_on=["#1"])]),
    lambda: Feature(name="Approval", scenarios=[]),
    lambda: AcceptanceCriteria(title="T", features=[Feature(name="A", scenarios=[_SCENARIO]), Feature(name="B", scenarios=[_SCENARIO])]),
    lambda: Requirement(id="", requirement="", priority="must"),
    lambda: EnvelopeElement(kind="Budget", value="50k", origin="slot"),
    lambda: EnvelopeElement(kind="Team size", value="3 developers", origin="assumption", source_slot="constraints"),
    lambda: PRD(title="X", problem="P", envelope=[{"kind": "Budget", "value": "50k", "origin": "slot", "source_slot": "not_a_slot"}]),
], ids=["challenge-no-premise", "challenge-no-recommendation", "threshold-no-action", "threshold-rests-on-nothing",
        "story-unknown-slot", "stories-duplicate-id", "estimate-duplicate-id", "estimate-inverted-range",
        "epic-missing-dependency", "epic-self-dependency", "feature-no-scenario", "criteria-duplicate-scenario",
        "prd-empty-requirement", "envelope-slot-without-source", "envelope-assumption-with-source",
        "envelope-unknown-slot"])
def test_an_artifact_contract_refuses_a_reference_that_points_at_nothing(build):
    """A pointer has to point at something, a range cannot invert, a provenance claim cannot contradict itself (#603)."""
    with pytest.raises(ValidationError):
        build()


def test_the_well_formed_artifact_items_all_validate():
    """The must-fire control: a contract that refused everything would pass the test above."""
    assert Story(id="S1", title="Approve leave", slots=["workflow"]).slots == ["workflow"]
    assert EstimateItem(**{**_ITEM, "days_high": 5}).days_high == 5
    assert len(Epic(title="E", issues=[EpicIssue(id="#1", title="First"), EpicIssue(id="#2", title="Second", depends_on=["#1"])]).issues) == 2
    assert EnvelopeElement(kind="Budget", value="50k", origin="slot", source_slot="constraints").source_slot
    assert EnvelopeElement(kind="Team size", value="3 developers", origin="assumption").source_slot is None
    assert PRD(title="X", problem="P").envelope == [], "nothing said about the envelope means none invented (#603)"


# ── the grounding judgment (#593) ───────────────────────────────────────────────


@pytest.mark.parametrize("payload", [
    {"decision": "installed", "reason": "r"},
    {"decision": "none", "reason": "r", "cards": ["b2b-platform"]},
    {"decision": "uncovered", "reason": "r", "cards": ["b2b-platform"]},
], ids=["installed-names-nothing", "none-names-a-card", "uncovered-names-a-card"])
def test_a_judgment_whose_payload_contradicts_its_decision_is_refused(payload):
    """A verdict and a payload that disagree is what this contract exists to catch (#593)."""
    with pytest.raises(ValidationError):
        ContextJudgment.model_validate(payload)


def test_the_three_judgments_that_agree_with_themselves_all_validate():
    """The must-fire control: a validator that refused everything would pass the test above."""
    assert all(ContextJudgment.model_validate({"decision": d, "reason": "r"}).cards == [] for d in ("none", "uncovered"))
    assert ContextJudgment.model_validate({"decision": "installed", "reason": "r", "cards": ["b2b-platform"]}).decision is ContextDecision.installed


# ── the driver: information_value = uncertainty × impact ───────────────────────

_TESTABLE = slot(0, "testable", "high", test_plan="Ship a waitlist and see if 20 people sign up.")


def test_soft_slots_are_medium_or_high_and_unresolved():
    # Padded slots stay empty/low, so never soft; the result comes back in schema order.
    model = out({"problem": slot(90, "explicit", "high"), "business_rules": slot(30, "inferred", "high"),
                 "business_objects": slot(50, "inferred", "medium"), "reporting": slot(10, "empty", "low")})
    assert soft_slots(model) == ["business_objects", "business_rules"]


def test_estimate_confidence_tiers():
    assert [estimate_confidence(n) for n in (0, 1, 3, 5)] == ["high", "high", "medium", "low"]


@pytest.mark.parametrize("overrides, blocked, cleared", [
    ({"problem": slot(90, "explicit", "high"), "business_rules": slot(80, "inferred", "high"),
      "success_metrics": slot(0, "empty", "medium")}, ["business_rules"], ["problem", "success_metrics"]),
    ({"business_rules": slot(5, "explicit", "high"), "problem": slot(90, "explicit", "high")}, ["business_rules"], ["problem"]),
    ({"problem": slot(90, "explicit", "high"), "business_rules": _TESTABLE}, [], ["business_rules"]),
    ({"constraints": slot(90, "explicit", "high"), "workflow": slot(90, "explicit", "high")}, [], ["constraints", "workflow"]),
], ids=["inferred-high-blocks", "thin-explicit-still-blocks-#610", "deferred-to-a-test-does-not-#610", "own-intent-is-explicit-#611"])
def test_readiness_blockers_are_high_impact_unconfirmed(overrides, blocked, cleared):
    blockers = readiness_blockers(out(overrides))
    assert all(s in blockers for s in blocked) and not any(s in blockers for s in cleared), blockers


def test_readiness_flags_a_missing_high_impact_slot_as_blocker():
    # A required high-impact slot the model omitted entirely must not vanish from readiness.
    model_dict = {sid: slot(90, "explicit", "high") for sid in schema_slot_ids()[1] if sid != "business_rules"}
    assert "business_rules" in readiness_blockers(EngineOutput.model_validate({"model": model_dict, "questions": [], "summary": {}}))


def test_readiness_is_not_reached_on_self_asserted_beliefs_about_the_world_alone():
    """#611: with no client, `explicit` means the requester is the authority for the fact."""
    assert readiness_blockers(out({"success_metrics": slot(90, "inferred", "high"), "problem": slot(90, "inferred", "high")}))


@pytest.mark.parametrize("s, state", [
    (Slot(completeness=90, confidence="explicit", impact="high"), "confirmed"),
    (Slot(completeness=50, confidence="inferred", impact="high"), "inferred"),
    (Slot(completeness=0, confidence="empty", impact="low"), "unknown"),
    (Slot(**_TESTABLE), "to_test"),
])
def test_state_of_maps_confidence(s, state):
    # #610: a gap deferred to a test is a fourth bucket, neither confirmed nor unknown.
    assert state_of(s) == state


def test_understanding_view_carries_a_to_test_group():
    groups = understanding_view(out({"business_rules": _TESTABLE}))
    assert [e["slot"] for e in groups["to_test"]] == ["business_rules"]


def test_the_four_slot_projections_all_read_from_one_schema_parse(monkeypatch):
    """#301: `schema_slot_ids`/`_schema_order` and `slot_meta`/`_default_impacts` used to each parse the schema."""
    from requivo.core import analysis, contracts

    for fn in (contracts.schema_slots, contracts.schema_slot_ids, contracts._schema_order, analysis.slot_meta, analysis._default_impacts):
        fn.cache_clear()
    calls = []
    original = contracts.json.loads
    monkeypatch.setattr(contracts.json, "loads", lambda *a, **kw: (calls.append(1), original(*a, **kw))[1])
    contracts.schema_slot_ids(), contracts._schema_order(), analysis.slot_meta(), analysis._default_impacts()
    assert len(calls) == 1, f"the schema file was parsed {len(calls)} time(s) across four projections"


# ── readiness is one boolean, on every surface (#165) ──────────────────────────

BLOCKER_COUNTS = (0, 1, 2, 3, 5)  # 1 and 2 are what a deleted `<= 2` arm used to call "nearly"
BRIEF_READINESS_HEADING = "## Are we ready?"


def _model_with(n_blockers: int):
    """A complete model whose only unresolved high-impact topics are the first `n_blockers` ones."""
    ordered = [sid for sid in _schema_order() if sid in schema_slot_ids()[1]]
    assert len(ordered) > max(BLOCKER_COUNTS), "the schema no longer has enough required slots"
    return out({sid: slot(0 if i < n_blockers else 90, "empty" if i < n_blockers else "explicit", "high")
                for i, sid in enumerate(ordered)})


def _after(text: str, marker: str, stop: str | None = None) -> str:
    """What follows `marker` on the first line carrying it, cut at `stop`."""
    tail = next(ln for ln in text.splitlines() if marker in ln).split(marker, 1)[1]
    return (tail.split(stop)[0] if stop else tail).strip()


def _brief_verdict(model) -> str:
    body = brief_markdown(model, Brief(complexity="low")).split(BRIEF_READINESS_HEADING, 1)[1]
    match = re.search(r"\*\*(.+?)\*\*", body)
    assert match, "the decision brief's readiness section states no verdict"
    return match.group(1)


SURFACES = [("terminal status", lambda m: _after(printed(render_readiness, m), "Status")),
            ("terminal turn", lambda m: _after(printed(render_turn, m), "Ready?", "→")), ("decision brief", _brief_verdict),
            ("web", lambda m: readiness_view(model_status(m))["headline"])]


def test_the_core_answers_readiness_with_one_boolean():
    # The reference the surfaces are graded against, and the positive control for `_model_with`.
    ready = {n: model_status(_model_with(n))["readiness"]["ready"] for n in BLOCKER_COUNTS}
    assert ready == {0: True, 1: False, 2: False, 3: False, 5: False}


@pytest.mark.parametrize("surface,extract", SURFACES, ids=[s for s, _ in SURFACES])
def test_readiness_renders_as_one_boolean_on_every_surface(surface, extract):
    verdicts = {n: extract(_model_with(n)) for n in BLOCKER_COUNTS}
    blocked = {v for n, v in verdicts.items() if n}
    assert len(blocked) == 1, f"{surface} grades readiness by blocker count: {verdicts}"
    assert verdicts[0] not in blocked, f"{surface} says the same thing ready and not: {verdicts}"
    assert not any(re.search(r"(?i)nearly", v) for v in verdicts.values()), f"{surface} invents a middle state: {verdicts}"


def test_every_surface_asks_the_same_readiness_question():
    model = _model_with(2)
    terminal = printed(render_readiness, model)
    markdown = brief_markdown(model, Brief(complexity="low"))
    assert "ARE WE READY?" in terminal and "READY FOR IMPLEMENTATION?" not in terminal
    assert BRIEF_READINESS_HEADING in markdown and "Ready to estimate?" not in markdown
