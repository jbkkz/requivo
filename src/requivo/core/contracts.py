from __future__ import annotations

import functools
import hashlib
import json
from enum import Enum
from typing import Annotated, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, ValidationInfo, field_validator, model_validator

from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter

SOFT_COMPLETENESS = 70  # below this a slot is "soft" (tunable)

# The element type of one tri-state reasoning collection, so `resolve`'s `keep` is annotated per call site.
_Item = TypeVar("_Item")

# The cap on questions per turn, carried by `ModelProposal` and its persisted mirror, which pydantic
# makes restate it (#14): `test_the_persisted_mirror_copies_every_constraint_it_restates`.
MAX_QUESTIONS = 6

# The ceiling on a raw text input, enforced by `require_input_within_bounds` in core, not only by
# the Web (#255, invariant 14): `test_an_oversized_request_is_refused_before_any_provider_call`.
MAX_INPUT_CHARS = 20_000


class StrictModel(BaseModel):
    """Base for every contract an LLM fills: `extra="forbid"`, so an invented field fails loudly and
    rides `_complete()`'s retry loop (invariant 4). Completeness lives at the discovery boundary."""

    model_config = ConfigDict(extra="forbid")


# A text field that must actually say something.
NonEmpty = Annotated[str, Field(min_length=1)]


def _stable_id(prefix: str, *parts: str) -> str:
    """A content-derived identifier, `<prefix>_<10 hex>` over the parts that carry identity: stable
    across revisions and machines while the statement is unchanged, new when it is reworded."""
    digest = hashlib.sha256("␟".join(p.strip() for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:10]}"


def _context_perimeter(info: Optional[ValidationInfo]) -> str:
    """The perimeter a validation runs against, from `model_validate(..., context=...)`; software by default."""
    ctx = info.context if info is not None else None
    if isinstance(ctx, dict) and ctx.get("perimeter"):
        return ctx["perimeter"]
    return DEFAULT_PERIMETER


def _reject_duplicate_ids(label: str, ids: list[str]) -> None:
    """Ids are pointers (an estimate cites a story, a `depends_on` an issue); a repeat reads as one item."""
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{label} repeat the same id: {dupes}")


@functools.cache
def schema_slots(perimeter: str = DEFAULT_PERIMETER) -> tuple[dict, ...]:
    """One perimeter's `model_schema.json` `slots`, parsed once per perimeter and cached (#608, #301),
    as a tuple of raw dicts in file order. `perimeter` defaults to software; an unknown one raises
    `UnknownPerimeterError`."""
    schema_path = get_perimeter(perimeter).schema_path
    return tuple(json.loads(schema_path.read_text(encoding="utf-8"))["slots"])


@functools.cache
def schema_slot_ids(perimeter: str = DEFAULT_PERIMETER) -> tuple[frozenset[str], frozenset[str]]:
    """(allowed, required) slot ids for `perimeter`; `required` excludes `optional` slots. The single
    source of the slot vocabulary."""
    slots = schema_slots(perimeter)
    allowed = frozenset(s["id"] for s in slots)
    required = frozenset(s["id"] for s in slots if not s.get("optional", False))
    return allowed, required


def missing_required_slots(present: set[str], perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Required slot ids absent from `present`, in schema order — what a complete model still owes."""
    _, required = schema_slot_ids(perimeter)
    return [sid for sid in _schema_order(perimeter) if sid in required and sid not in present]


def unknown_slots(present: set[str], perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Slot ids in `present` that the schema does not define — hallucinated / typo'd keys."""
    allowed, _ = schema_slot_ids(perimeter)
    return sorted(present - allowed)


@functools.cache
def _schema_order(perimeter: str = DEFAULT_PERIMETER) -> tuple[str, ...]:
    return tuple(s["id"] for s in schema_slots(perimeter))


class Confidence(str, Enum):
    explicit = "explicit"
    inferred = "inferred"
    empty = "empty"
    # Unknown, and NOT answerable by asking (#610): readiness exempts it, and a `test_plan` is required.
    testable = "testable"


class Impact(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Level(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Slot(StrictModel):
    completeness: int = Field(ge=0, le=100)
    confidence: Confidence
    impact: Impact
    value: str = ""
    evidence: str = ""
    # What would settle this slot; required exactly when `confidence` is `testable` (#610).
    test_plan: str = ""

    @model_validator(mode="after")
    def _testable_names_its_settlement(self) -> Slot:
        # A `testable` slot with no plan is `empty` with better manners: `test_a_testable_slot_with_no_test_plan_is_refused`.
        if self.confidence is Confidence.testable and not self.test_plan.strip():
            raise ValueError(
                "confidence 'testable' names no test_plan -- state what would settle this slot, or "
                "grade it inferred/empty instead")
        return self


class Question(StrictModel):
    # A question with no text, or aimed at nothing, would still be rendered and answered against.
    q: NonEmpty
    slot: NonEmpty
    why: NonEmpty


class ContextDecision(str, Enum):
    """What a first discovery decided about its own grounding (`decision: the-engine-writes-the-missing-card`).
    *No card is warranted* and *a card is warranted and none is installed* are opposite verdicts."""

    none = "none"
    installed = "installed"
    uncovered = "uncovered"


# A judgment names installed cards by stem; a shape check, not a policy.
MAX_JUDGED_CARDS = 16


class ContextJudgment(StrictModel):
    """Whether the installed context cards cover this request's domain. `reason` is shown verbatim
    before it grounds anything (#492)."""

    decision: ContextDecision
    reason: NonEmpty
    cards: list[str] = Field(default_factory=list, max_length=MAX_JUDGED_CARDS)

    @model_validator(mode="after")
    def _shape_matches_the_decision(self) -> ContextJudgment:
        """A decision and a payload that disagree are refused (invariant 3: `installed` with no cards
        would read as *every card*), riding the retry loop as a `ValueError`.
        `test_a_judgment_whose_payload_contradicts_its_decision_is_refused`."""
        if self.decision is ContextDecision.installed and not self.cards:
            raise ValueError(
                "decision 'installed' names no cards; an empty selection means *every* card "
                "downstream, which is the opposite of what this decision claims")
        if self.decision is not ContextDecision.installed and self.cards:
            raise ValueError(
                f"decision {self.decision.value!r} names cards {self.cards!r}; only 'installed' "
                f"selects, so this reply's verdict and its payload disagree")
        return self


class PerimeterDecision(str, Enum):
    """Which installed perimeter a request's shape belongs to (#601). *One clearly fits* and *two
    plausibly fit* are opposite kinds of uncertainty."""

    fits = "fits"
    ambiguous = "ambiguous"
    none = "none"


# A judgment naming more candidates than the install holds has stopped selecting; a shape check.
MAX_JUDGED_PERIMETERS = 8


class PerimeterJudgment(StrictModel):
    """Which installed perimeter, if any, a request's shape belongs to (#601). `reason` is shown
    verbatim before it routes anything."""

    decision: PerimeterDecision
    reason: NonEmpty
    perimeter: str = ""
    candidates: list[str] = Field(default_factory=list, max_length=MAX_JUDGED_PERIMETERS)

    @model_validator(mode="after")
    def _shape_matches_the_decision(self) -> PerimeterJudgment:
        """A decision and a payload that disagree are refused, as `ContextJudgment` does.
        `test_a_perimeter_judgment_whose_payload_contradicts_its_decision_is_refused`."""
        if self.decision is PerimeterDecision.fits and not self.perimeter:
            raise ValueError(
                "decision 'fits' names no perimeter; a verdict that fits must say which")
        if self.decision is not PerimeterDecision.fits and self.perimeter:
            raise ValueError(
                f"decision {self.decision.value!r} names perimeter {self.perimeter!r}; only "
                f"'fits' routes")
        if self.decision is PerimeterDecision.ambiguous and len(set(self.candidates)) < 2:
            raise ValueError(
                "decision 'ambiguous' names fewer than two distinct candidates; ambiguity needs at "
                "least two different perimeters, not the same one repeated")
        if self.decision is not PerimeterDecision.ambiguous and self.candidates:
            raise ValueError(
                f"decision {self.decision.value!r} names candidates {self.candidates!r}; only "
                f"'ambiguous' does")
        return self


class Summary(StrictModel):
    objective: str = ""
    scope: str = ""
    assumptions: list[str] = Field(default_factory=list)
    blind_spot: str = ""


class ModelProposal(StrictModel):
    """A *proposed* model: a discovery reply, a Claude Code proposal file, a Web form. Identical to
    the `EngineOutput` it resolves into, except that the five reasoning collections are tri-state:

        absent   the proposal says nothing about them; the established reasoning stands
        []       an explicit removal
        [ … ]    a replacement

    A refinement turn answers a question and does not re-derive the brief, so read as a whole
    `EngineOutput` it silently deleted every decision (invariant 10). `resolve()` collapses the
    three states, once, for every surface."""

    # protected_namespaces=() keeps the field literally named `model`; extra="forbid" restated for visibility.
    model_config = ConfigDict(protected_namespaces=(), extra="forbid")
    model: dict[str, Slot]
    # The cap is an invariant: a turn that floods questions has stopped prioritising by information value.
    questions: list[Question] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    summary: Summary
    # The reasoning layer, filled by absorbing the assessment's Brief, never by the discovery turn.
    # `SerializeAsAny` on these five and nowhere else: `resolve()` carries an unstated collection
    # forward off disk (invariant 10), and pydantic serializes by the annotated type, so without it a
    # `PersistedDesignDecision`'s unknown key is lost on the next write (#14).
    decisions: Optional[list[SerializeAsAny[DesignDecision]]] = None
    challenges: Optional[list[SerializeAsAny[Challenge]]] = None
    opportunities: Optional[list[SerializeAsAny[Opportunity]]] = None
    exclusions: Optional[list[SerializeAsAny[Exclusion]]] = None
    thresholds: Optional[list[SerializeAsAny[Threshold]]] = None

    def resolve(self, current: Optional[EngineOutput] = None, *,
               perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
        """Collapse the proposal onto the model it refines, yielding a complete `EngineOutput`: an
        unstated collection is carried forward from `current`, a stated one (even `[]`) replaces.
        An unknown top-level key of `current` is carried too (#14), so the result is a
        `PersistedEngineOutput` re-admitted through `model_dump()`:
        `test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save`. `perimeter` (#608)
        is threaded as validation context. Slots, summary and questions are replaced wholesale."""
        prior = current or EngineOutput(model={}, summary=Summary())

        # Annotated (#78): unannotated, pyright infers one return type across the call sites.
        def keep(stated: Optional[list[_Item]], established: list[_Item]) -> list[_Item]:
            return list(established) if stated is None else list(stated)

        resolved = EngineOutput.model_validate(
            {
                "model": self.model,
                "questions": self.questions,
                "summary": self.summary,
                "decisions": keep(self.decisions, prior.decisions),
                "challenges": keep(self.challenges, prior.challenges),
                "opportunities": keep(self.opportunities, prior.opportunities),
                "exclusions": keep(self.exclusions, prior.exclusions),
                "thresholds": keep(self.thresholds, prior.thresholds),
            },
            context={"perimeter": perimeter},
        )
        carried = getattr(prior, "__pydantic_extra__", None) or {}
        if not carried:
            return resolved
        return PersistedEngineOutput.model_validate(
            {**resolved.model_dump(), **carried}, context={"perimeter": perimeter})

    @model_validator(mode="after")
    def _validate_slot_vocabulary(self, info: ValidationInfo):
        # Every slot id named, in the model and in the questions, must be one the schema defines;
        # completeness is enforced at the discovery boundary. `perimeter` comes from the validation context (#608).
        perimeter = _context_perimeter(info)
        bad_model = unknown_slots(set(self.model), perimeter)
        if bad_model:
            raise ValueError(f"unknown slots (not in schema): {bad_model}")
        allowed, _ = schema_slot_ids(perimeter)
        bad_questions = sorted({q.slot for q in self.questions if q.slot not in allowed})
        if bad_questions:
            raise ValueError(f"questions target unknown slots (not in schema): {bad_questions}")
        # DAG edges (`derived_from`, `contests`, `rests_on`) obey the same vocabulary rule.
        bad_refs = sorted(
            {sid for d in (self.decisions or []) for sid in d.derived_from if sid not in allowed}
            | {sid for c in (self.challenges or []) for sid in c.contests if sid not in allowed}
            | {sid for e in (self.exclusions or []) for sid in e.rests_on if sid not in allowed}
            | {sid for t in (self.thresholds or []) for sid in t.rests_on if sid not in allowed}
        )
        if bad_refs:
            raise ValueError(f"reasoning references unknown slots (not in schema): {bad_refs}")
        # Two identical items collide on one content-derived id, which a diff keys on: a defect in the
        # reply the retry loop can fix.
        for label, items in (("decisions", self.decisions), ("challenges", self.challenges),
                             ("opportunities", self.opportunities), ("exclusions", self.exclusions),
                             ("thresholds", self.thresholds)):
            ids = [i.id for i in (items or [])]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(
                    f"{label} contains repeated entries (identical content yields one id): {dupes}")
        return self


class EngineOutput(ModelProposal):
    """A *resolved* model: the five reasoning collections are always concrete lists. A proposal
    becomes one through `resolve()`, the only place the tri-state question is answered."""

    decisions: list[SerializeAsAny[DesignDecision]] = Field(default_factory=list)
    challenges: list[SerializeAsAny[Challenge]] = Field(default_factory=list)
    opportunities: list[SerializeAsAny[Opportunity]] = Field(default_factory=list)
    exclusions: list[SerializeAsAny[Exclusion]] = Field(default_factory=list)
    thresholds: list[SerializeAsAny[Threshold]] = Field(default_factory=list)


class Story(StrictModel):
    id: NonEmpty
    title: NonEmpty
    as_a: str = ""
    i_want: str = ""
    so_that: str = ""
    acceptance: list[str] = Field(default_factory=list)
    # The slots this story is traceable to, checked against the schema like every other reference.
    slots: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_slot_references(self, info: ValidationInfo):
        allowed, _ = schema_slot_ids(_context_perimeter(info))
        bad = sorted({sid for sid in self.slots if sid not in allowed})
        if bad:
            raise ValueError(f"story {self.id!r} references unknown slots (not in schema): {bad}")
        return self


class Stories(StrictModel):
    stories: list[Story] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_ids(self):
        # An estimate item points back at a story by id.
        _reject_duplicate_ids("stories", [st.id for st in self.stories])
        return self


class Complexity(str, Enum):
    S = "S"
    M = "M"
    L = "L"


class EstimateItem(StrictModel):
    story_id: NonEmpty
    title: NonEmpty
    complexity: Complexity
    days_low: float = Field(ge=0)
    days_high: float = Field(ge=0)
    drives: list[str] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def _validate_range(self):
        # An inverted range is not a wide estimate, it is a broken one: the totals sum both ends.
        if self.days_low > self.days_high:
            raise ValueError(
                f"estimate for {self.story_id!r} has days_low ({self.days_low}) above days_high "
                f"({self.days_high}) — low is the optimistic end")
        return self


class EstimateDraft(StrictModel):
    # What the LLM produces; totals, confidence and spread_drivers are computed in Python.
    items: list[EstimateItem]
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_stories(self):
        _reject_duplicate_ids("estimate items", [i.story_id for i in self.items])
        return self


class Leverage(str, Enum):
    high = "high"
    medium = "medium"
    future = "future"


# Each reasoning item carries a stable `id`, recomputed from its own content on validation (invariant 5).

class Opportunity(StrictModel):
    id: str = ""           # derived from `text`; see _stable_id
    text: NonEmpty
    leverage: Leverage
    modules: list[str] = Field(default_factory=list)  # concrete modules the leverage reaches (grounds it)

    @model_validator(mode="after")
    def _assign_id(self):
        object.__setattr__(self, "id", _stable_id("opp", self.text))
        return self


class Challenge(StrictModel):
    # What to contest, not what we learned. All five parts are load-bearing.
    id: str = ""               # derived from headline + premise; see _stable_id
    headline: NonEmpty         # 3–6 words naming the thing being challenged
    premise: NonEmpty          # the assumption the request takes for granted
    alternative: NonEmpty      # a concrete, domain-grounded alternative worth weighing
    consequence: NonEmpty      # what the current premise risks or costs
    recommendation: NonEmpty   # what to do about it before build
    contests: list[str] = Field(default_factory=list)  # slot ids whose premise this contests

    @model_validator(mode="after")
    def _assign_id(self):
        object.__setattr__(self, "id", _stable_id("chl", self.headline, self.premise))
        return self


class DesignDecision(StrictModel):
    # A settled decision; why/alternative/tradeoff only where there was a real fork.
    id: str = ""           # derived from `decision`; see _stable_id
    decision: NonEmpty     # what was decided
    why: str = ""          # the rationale
    alternative: str = ""  # what was weighed instead
    tradeoff: str = ""     # the cost accepted for this choice
    derived_from: list[str] = Field(default_factory=list)  # slot ids the decision rests on (the DAG edge)

    @model_validator(mode="after")
    def _assign_id(self):
        object.__setattr__(self, "id", _stable_id("dec", self.decision))
        return self


class Exclusion(StrictModel):
    # An option deliberately ruled out (#599); `rests_on` is the DAG edge `propagate()` re-opens it on.
    id: str = ""            # derived from `option`; see _stable_id
    option: NonEmpty        # the option that was considered
    reason: NonEmpty        # why it lost
    rests_on: list[str] = Field(default_factory=list)  # slot ids this exclusion rests on (the DAG edge)

    @model_validator(mode="after")
    def _assign_id(self):
        object.__setattr__(self, "id", _stable_id("exc", self.option))
        return self


class Threshold(StrictModel):
    # A decision that has not fired yet, "at X, do Y" (#604); `rests_on` is required non-empty, unlike
    # Exclusion's, because a threshold with no slot it rests on has nowhere to go.
    id: str = ""             # derived from condition + action; see _stable_id
    condition: NonEmpty      # "at X" — the trigger, in plain terms
    measure: NonEmpty        # the fact or metric this reads to evaluate the condition
    action: NonEmpty         # "do Y" — what happens when it fires
    rests_on: list[str] = Field(min_length=1)  # slot ids this threshold rests on (the DAG edge)

    @model_validator(mode="after")
    def _assign_id(self):
        object.__setattr__(self, "id", _stable_id("thr", self.condition, self.action))
        return self


class Brief(StrictModel):
    # The advisory layer.
    problem: str = ""                                   # one-line problem statement (exec summary)
    solution: str = ""                                  # one-line solution statement (exec summary)
    introduces: list[str] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)  # premises worth contesting before build
    complexity: Level
    complexity_reasons: list[str] = Field(default_factory=list)  # the "because …" behind the verdict
    cost_driver: str = ""
    risks: list[str] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)  # ranked by leverage
    next_steps: list[str] = Field(default_factory=list)
    decisions: list[DesignDecision] = Field(default_factory=list)  # settled decisions, with tradeoffs
    # Typed exclusions the brief proposes (#600): `test_brief_carries_typed_exclusions_it_can_propose_600`.
    exclusions: list[Exclusion] = Field(default_factory=list)
    # Typed thresholds (#604): `test_brief_carries_typed_thresholds_it_can_propose_604`.
    thresholds: list[Threshold] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)  # decisions still to make


class Priority(str, Enum):
    must = "must"
    should = "should"
    could = "could"


class Requirement(StrictModel):
    # A requirement with no text is a priority attached to nothing.
    id: NonEmpty
    requirement: NonEmpty
    priority: Priority


class EnvelopeOrigin(str, Enum):
    slot = "slot"
    assumption = "assumption"


class EnvelopeElement(StrictModel):
    # One resource constraint an artifact worked within (#603); `origin` names the slot it came from,
    # or says the generator assumed it.
    kind: NonEmpty
    value: NonEmpty
    origin: EnvelopeOrigin
    source_slot: Optional[str] = None

    @model_validator(mode="after")
    def _source_slot_matches_origin(self):
        if self.origin is EnvelopeOrigin.slot and not self.source_slot:
            raise ValueError("an envelope element read from the model must name its source slot")
        if self.origin is EnvelopeOrigin.assumption and self.source_slot:
            raise ValueError("an assumed envelope element must not name a source slot")
        return self


class GoToMarketPlan(StrictModel):
    """The go-to-market perimeter's one artifact (#609): `plan`, `rationale`, `risks` and
    `open_decisions` are judged by the provider; `exclusions`, `thresholds` and `envelope` are typed
    items projected off the model, as the software brief's are. Its slot vocabulary is go-to-market's."""

    plan: list[str] = Field(default_factory=list)        # the chosen set -- not a ranking (#600)
    rationale: str = ""                                    # why this set, given the envelope below
    risks: list[str] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)      # #599
    thresholds: list[Threshold] = Field(default_factory=list)      # #604
    envelope: list[EnvelopeElement] = Field(default_factory=list)  # #603
    open_decisions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_slot_vocabulary(self, info: ValidationInfo):
        # The same DAG-edge rule as `ModelProposal._validate_slot_vocabulary`; `_context_perimeter`
        # keeps it on go-to-market's vocabulary (the generator passes `context={"perimeter": GO_TO_MARKET}`).
        allowed, _ = schema_slot_ids(_context_perimeter(info))
        bad = sorted(
            {e.source_slot for e in self.envelope if e.source_slot and e.source_slot not in allowed}
            | {sid for e in self.exclusions for sid in e.rests_on if sid not in allowed}
            | {sid for t in self.thresholds for sid in t.rests_on if sid not in allowed}
        )
        if bad:
            raise ValueError(f"go-to-market plan references unknown slots (not in schema): {bad}")
        return self


class PRD(StrictModel):
    title: NonEmpty
    summary: str = ""
    problem: NonEmpty
    goals: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    # The envelope this document was planned within (#603); empty when the model has no constraint content.
    envelope: list[EnvelopeElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_requirement_ids(self):
        _reject_duplicate_ids("requirements", [r.id for r in self.requirements])
        return self

    @model_validator(mode="after")
    def _validate_envelope_slot_vocabulary(self, info: ValidationInfo):
        # A `source_slot` naming nothing the schema defines would let the envelope look grounded.
        allowed, _ = schema_slot_ids(_context_perimeter(info))
        bad = sorted({e.source_slot for e in self.envelope if e.source_slot and e.source_slot not in allowed})
        if bad:
            raise ValueError(f"envelope references unknown slots (not in schema): {bad}")
        return self


class ScenarioKind(str, Enum):
    happy_path = "happy_path"
    edge_case = "edge_case"
    error = "error"
    permission = "permission"


class Scenario(StrictModel):
    id: NonEmpty
    title: NonEmpty
    kind: ScenarioKind = ScenarioKind.happy_path
    given: list[str] = Field(default_factory=list)
    when: NonEmpty
    then: list[str] = Field(min_length=1)


class Feature(StrictModel):
    name: NonEmpty
    # A feature *is* its scenarios here; one with none is a heading tested by intuition.
    scenarios: list[Scenario] = Field(min_length=1)


class AcceptanceCriteria(StrictModel):
    title: NonEmpty
    features: list[Feature] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_scenario_ids(self):
        # Scenario ids are cited across features, so they are unique across the document.
        _reject_duplicate_ids("scenarios", [sc.id for f in self.features for sc in f.scenarios])
        return self


class EpicIssue(StrictModel):
    id: NonEmpty
    title: NonEmpty
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Epic(StrictModel):
    title: NonEmpty
    goal: str = ""
    business_value: str = ""
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    milestone: str = ""
    issues: list[EpicIssue] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dependency_graph(self):
        # `depends_on` is exported as a real link; an edge to an unknown id survives the export looking real.
        ids = [i.id for i in self.issues]
        _reject_duplicate_ids("epic issues", ids)
        known = set(ids)
        dangling = sorted({dep for i in self.issues for dep in i.depends_on if dep not in known})
        if dangling:
            raise ValueError(
                f"epic issues depend on ids the epic does not define: {dangling}")
        self_dep = sorted({i.id for i in self.issues if i.id in i.depends_on})
        if self_dep:
            raise ValueError(f"epic issues depend on themselves: {self_dep}")
        return self


class ReleaseNotes(StrictModel):
    title: NonEmpty
    version: str = ""
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# The reasoning fields forward-reference the types defined above; resolve them on both contracts.
ModelProposal.model_rebuild()
EngineOutput.model_rebuild()


# ── The persisted model: the same shape, the opposite rule on unknown keys ────────────────────────
# What an LLM fills is `extra="forbid"` (invariant 4: a retry can put it right); what is read off disk
# is `extra="allow"` (invariant 8: a newer Requivo's key must survive a round-trip, #14). Reading
# permissively is half of it: `resolve()` carries the keys so a writer preserves them too. An apply
# still supersedes slots, summary and questions (invariant 10), and an unknown *slot id* is still
# refused (`schema_version`'s frontier). The mirror is written class by class so it greps; a nested
# contract nobody twinned is caught by `test_the_persisted_contract_is_permissive_all_the_way_down`.


class PersistedSlot(Slot):
    model_config = ConfigDict(extra="allow")
    # A confidence value this build does not know must load (invariant 8) and never read as `explicit`:
    # `test_a_slot_confidence_this_version_does_not_know_survives_a_round_trip_unread_as_explicit`.
    # `Union`, never `Confidence | str`: pydantic evaluates this at class definition, which 3.9 cannot.
    confidence: Union[Confidence, str]

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_or_raw(cls, v):
        if isinstance(v, str):
            try:
                return Confidence(v)
            except ValueError:
                return v
        return v


class PersistedQuestion(Question):
    model_config = ConfigDict(extra="allow")


class PersistedSummary(Summary):
    model_config = ConfigDict(extra="allow")


class PersistedDesignDecision(DesignDecision):
    model_config = ConfigDict(extra="allow")


class PersistedChallenge(Challenge):
    model_config = ConfigDict(extra="allow")


class PersistedOpportunity(Opportunity):
    model_config = ConfigDict(extra="allow")


class PersistedExclusion(Exclusion):
    model_config = ConfigDict(extra="allow")


class PersistedThreshold(Threshold):
    model_config = ConfigDict(extra="allow")


class PersistedEngineOutput(EngineOutput):
    """An `EngineOutput` as read back from disk: a subclass, so every reader and validator still
    works, with an unknown key carried instead of refused at every level (the nested fields are
    re-declared against the permissive twins because pydantic serializes by the annotated type)."""

    model_config = ConfigDict(protected_namespaces=(), extra="allow")
    # A re-declared field must restate its constraints: pydantic drops the parent's `FieldInfo`.
    # `test_the_persisted_mirror_copies_every_constraint_it_restates`.
    model: dict[str, PersistedSlot]
    questions: list[PersistedQuestion] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    summary: PersistedSummary
    decisions: list[PersistedDesignDecision] = Field(default_factory=list)
    challenges: list[PersistedChallenge] = Field(default_factory=list)
    opportunities: list[PersistedOpportunity] = Field(default_factory=list)
    exclusions: list[PersistedExclusion] = Field(default_factory=list)
    thresholds: list[PersistedThreshold] = Field(default_factory=list)
