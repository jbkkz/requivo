from __future__ import annotations

import functools
import hashlib
import json
from enum import Enum
from typing import Annotated, Any, Optional, TypeVar, Union, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

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


def _without(data: dict[str, Any], **absent: object) -> dict[str, Any]:
    """`data` minus each key holding its absent value: a field this version added stays off the wire
    until it says something (`test_a_claimless_model_is_written_exactly_as_before`)."""
    return {k: v for k, v in data.items() if k not in absent or v != absent[k]}


def _context_perimeter(info: Optional[ValidationInfo]) -> str:
    """The perimeter a validation runs against, from `model_validate(..., context=...)`; software by default."""
    ctx: object = info.context if info is not None else None
    if isinstance(ctx, dict) and cast("dict[str, Any]", ctx).get("perimeter"):
        return cast("dict[str, Any]", ctx)["perimeter"]
    return DEFAULT_PERIMETER


def _reject_duplicate_ids(label: str, ids: list[str]) -> None:
    """Ids are pointers (an estimate cites a story, a `depends_on` an issue); a repeat reads as one item."""
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{label} repeat the same id: {dupes}")


@functools.cache
def schema_slots(perimeter: str = DEFAULT_PERIMETER) -> tuple[dict[str, Any], ...]:
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


IMPACT_RANK = {Impact.low: 0, Impact.medium: 1, Impact.high: 2}

# The cap on claims per slot (#751), refused rather than truncated (invariant 3).
MAX_CLAIMS_PER_SLOT = 8


class ClaimSource(str, Enum):
    """Where a claim's statement came from (#751); it never changes under one claim id."""

    requester = "requester"  # said by the requester, or a third party's own word they relay
    artifact = "artifact"    # read from something the requester owns: code, data, documents
    evidence = "evidence"    # third-party material: public threads, interviews, market data
    proposed = "proposed"    # a choice proposed for the requester to own
    domain = "domain"        # domain knowledge: a regulation, a norm, a known trap
    assumed = "assumed"      # inferred, with no stated basis


class Confirmation(str, Enum):
    """What an answerer has done with a claim: the one thing about it that moves."""

    open = "open"
    let_stand = "let_stand"  # shown as a default at a checkpoint, not overturned
    confirmed = "confirmed"
    to_test = "to_test"      # only a real test settles it; `test_plan` required


class Answerer(str, Enum):
    requester = "requester"
    delegate = "delegate"
    agent = "agent"          # answering from documents on the requester's behalf (#724)


class Claim(StrictModel):
    """One independently confirmable statement inside a slot (#751). Informational in 3.x: readiness
    still reads the slot's `confidence` (`decision: claims-carry-provenance`)."""

    id: str = ""                              # derived from `text`; see _stable_id
    text: NonEmpty
    source: ClaimSource
    confirmation: Confirmation = Confirmation.open
    answered_by: Optional[Answerer] = None    # required when let_stand or confirmed
    impact: Optional[Impact] = None           # None inherits the slot's; never above it (see Slot)
    evidence: str = ""
    test_plan: str = ""                       # required exactly when to_test

    @model_validator(mode="after")
    def _assign_id_and_check_settlement(self) -> Claim:
        # Refused, never repaired, so it rides the retry loop: `test_a_claim_is_refused_rather_than_repaired`.
        object.__setattr__(self, "id", _stable_id("clm", self.text))
        to_test = self.confirmation is Confirmation.to_test
        if to_test and not self.test_plan.strip():
            raise ValueError(f"claim {self.text!r} is to_test and names no test_plan")
        # Named rather than `not to_test`: an unknown confirmation off disk is not judged here (invariant 8).
        if self.confirmation in (Confirmation.open, Confirmation.let_stand, Confirmation.confirmed) and self.test_plan.strip():
            raise ValueError(f"claim {self.text!r} carries a test_plan but is not to_test")
        if self.confirmation in (Confirmation.let_stand, Confirmation.confirmed) and self.answered_by is None:
            raise ValueError(f"claim {self.text!r} is {self.confirmation.value} and names no answered_by")
        return self

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Every turn re-sends the model, so an unset field costs tokens on each one.
        return _without(handler(self), answered_by=None, impact=None, evidence="", test_plan="")


class Slot(StrictModel):
    completeness: int = Field(ge=0, le=100)
    confidence: Confidence
    impact: Impact
    value: str = ""
    evidence: str = ""
    # What would settle this slot; required exactly when `confidence` is `testable` (#610).
    test_plan: str = ""
    # Informational in 3.x (#751): recorded, diffed and shown; readiness still reads `confidence`.
    claims: list[Claim] = Field(default_factory=list[Claim], max_length=MAX_CLAIMS_PER_SLOT)

    @model_validator(mode="after")
    def _testable_names_its_settlement(self) -> Slot:
        # A `testable` slot with no plan is `empty` with better manners: `test_a_testable_slot_with_no_test_plan_is_refused`.
        if self.confidence is Confidence.testable and not self.test_plan.strip():
            raise ValueError(
                "confidence 'testable' names no test_plan -- state what would settle this slot, or "
                "grade it inferred/empty instead")
        # A claim outranking its slot, or two claims on one id: `test_a_claim_is_refused_rather_than_repaired`.
        above = [c.text for c in self.claims if c.impact is not None and IMPACT_RANK[c.impact] > IMPACT_RANK[self.impact]]
        if above:
            raise ValueError(f"claims rated above their slot's impact ({self.impact.value}): {above}")
        _reject_duplicate_ids("claims", [c.id for c in self.claims])
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_claims(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # No claims is absent, never `[]` (invariant 6): a claimless model is written as an older 3.x wrote it.
        return _without(handler(self), claims=[])


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

# A written card rides the system block of every later call (#598): one line per value and a byte cap
# under the largest bundled card, each refused rather than trimmed (invariant 3).
MAX_GENERATED_CARD_BYTES = 6_000
CardLine = Annotated[str, Field(min_length=1, max_length=300)]


def _breaks_a_line(value: str) -> bool:
    """C0, DEL, C1 and the Unicode line/paragraph separators: whatever can end a line or move a cursor."""
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" for c in value)


class GeneratedCard(StrictModel):
    """The card an `uncovered` verdict writes (#598, `decision: the-engine-writes-the-missing-card`): the
    engine fills fields, never Markdown, and `markdown()` is the one writer of the file.
    `test_a_generated_card_that_could_forge_a_line_is_refused_not_trimmed`."""

    stem: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=2, max_length=48)
    title: CardLine
    business_domain: CardLine
    typical_users: CardLine
    what_it_does: CardLine
    entities: list[CardLine] = Field(min_length=1, max_length=10)
    key_concepts: list[CardLine] = Field(min_length=1, max_length=10)
    regulatory: list[CardLine] = Field(default_factory=list, max_length=10)
    recurring_traps: list[CardLine] = Field(min_length=1, max_length=10)

    @field_validator("title", "business_domain", "typical_users", "what_it_does", "entities",
                     "key_concepts", "regulatory", "recurring_traps")
    @classmethod
    def _one_line_each(cls, value: str | list[str]) -> str | list[str]:
        for line in [value] if isinstance(value, str) else value:
            if _breaks_a_line(line) or not line.strip():
                raise ValueError(f"{line!r} is not one line of text; every card value is a single line, "
                                 "since it is written into the system prompt of every later call")
        return value

    @model_validator(mode="after")
    def _within_the_cap(self) -> GeneratedCard:
        size = len(self.markdown().encode("utf-8"))
        if size > MAX_GENERATED_CARD_BYTES:
            raise ValueError(f"the card renders to {size} bytes, over the {MAX_GENERATED_CARD_BYTES}-byte "
                             "cap; write fewer, shorter lines")
        return self

    def markdown(self) -> str:
        """The card file, in `_template.md`'s sections, so `card_summaries` reads its domain back."""
        def bullets(items: list[str]) -> str:
            return "\n".join(f"  - {item}" for item in items) or "  - none identified from the request"
        return (f"# Context card — {self.title}\n\n"
                "> Written by Requivo from a client request: its reading, not a source. Correct or delete this file.\n\n"
                f"## Who\n- Business domain: {self.business_domain}\n- Typical users / roles: {self.typical_users}\n\n"
                f"## The product / module\n- What it does: {self.what_it_does}\n"
                f"- Main business objects (entities):\n{bullets(self.entities)}\n"
                f"- Key domain concepts:\n{bullets(self.key_concepts)}\n\n"
                f"## Sensitivities & constraints\n- Regulatory:\n{bullets(self.regulatory)}\n"
                f"- Recurring traps:\n{bullets(self.recurring_traps)}\n")


class ContextJudgment(StrictModel):
    """Whether the installed context cards cover this request's domain. `reason` is shown verbatim
    before it grounds anything (#492)."""

    decision: ContextDecision
    reason: NonEmpty
    cards: list[str] = Field(default_factory=list, max_length=MAX_JUDGED_CARDS)
    card: Optional[GeneratedCard] = None

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
        if (self.decision is ContextDecision.uncovered) != (self.card is not None):
            raise ValueError(
                f"decision {self.decision.value!r} {'carries' if self.card else 'writes no'} card; "
                "'uncovered' writes the missing card, and only 'uncovered' does")
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
    questions: list[Question] = Field(default_factory=list[Question], max_length=MAX_QUESTIONS)
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
        carried: dict[str, Any] = getattr(prior, "__pydantic_extra__", None) or {}
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

    # Narrowing the proposal's `Optional` lists is the point (invariant 10), which a checker reads as an
    # unsafe mutable override: `test_reasoning_merely_omitted_by_a_turn_is_preserved`. The factories
    # say `Any` because the item classes are defined below.
    decisions: list[SerializeAsAny[DesignDecision]] = Field(default_factory=list[Any])  # pyright: ignore[reportIncompatibleVariableOverride]
    challenges: list[SerializeAsAny[Challenge]] = Field(default_factory=list[Any])  # pyright: ignore[reportIncompatibleVariableOverride]
    opportunities: list[SerializeAsAny[Opportunity]] = Field(default_factory=list[Any])  # pyright: ignore[reportIncompatibleVariableOverride]
    exclusions: list[SerializeAsAny[Exclusion]] = Field(default_factory=list[Any])  # pyright: ignore[reportIncompatibleVariableOverride]
    thresholds: list[SerializeAsAny[Threshold]] = Field(default_factory=list[Any])  # pyright: ignore[reportIncompatibleVariableOverride]


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


class DecisionSource(str, Enum):
    """Who owns a decision (#751): the requester's own choice, or one proposed for them to own."""

    requester = "requester"
    proposed = "proposed"


class DesignDecision(StrictModel):
    # A settled decision; why/alternative/tradeoff only where there was a real fork.
    id: str = ""           # derived from `decision`; see _stable_id
    decision: NonEmpty     # what was decided
    why: str = ""          # the rationale
    alternative: str = ""  # what was weighed instead
    tradeoff: str = ""     # the cost accepted for this choice
    derived_from: list[str] = Field(default_factory=list)  # slot ids the decision rests on (the DAG edge)
    source: Optional[DecisionSource] = None  # informational (#751); None is unstated, never guessed

    @model_serializer(mode="wrap")
    def _omit_unstated_source(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without(handler(self), source=None)

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
    challenges: list[Challenge] = Field(default_factory=list[Challenge])  # premises worth contesting before build
    complexity: Level
    complexity_reasons: list[str] = Field(default_factory=list)  # the "because …" behind the verdict
    cost_driver: str = ""
    risks: list[str] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list[Opportunity])  # ranked by leverage
    next_steps: list[str] = Field(default_factory=list)
    decisions: list[DesignDecision] = Field(default_factory=list[DesignDecision])  # settled decisions, with tradeoffs
    # Typed exclusions the brief proposes (#600): `test_brief_carries_typed_exclusions_it_can_propose_600`.
    exclusions: list[Exclusion] = Field(default_factory=list[Exclusion])
    # Typed thresholds (#604): `test_brief_carries_typed_exclusions_it_can_propose_600`.
    thresholds: list[Threshold] = Field(default_factory=list[Threshold])
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
    `open_decisions` are judged by the provider; `challenges` (#728), `exclusions` and `thresholds` are typed
    items absorbed into the model, as the software brief's are, beside a typed `envelope`. Its slot
    vocabulary is go-to-market's."""

    plan: list[str] = Field(default_factory=list)        # the chosen set -- not a ranking (#600)
    rationale: str = ""                                    # why this set, given the envelope below
    risks: list[str] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list[Challenge])     # #728, the brief's shape
    exclusions: list[Exclusion] = Field(default_factory=list[Exclusion])      # #599
    thresholds: list[Threshold] = Field(default_factory=list[Threshold])      # #604
    envelope: list[EnvelopeElement] = Field(default_factory=list[EnvelopeElement])  # #603
    open_decisions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_slot_vocabulary(self, info: ValidationInfo):
        # The same DAG-edge rule as `ModelProposal._validate_slot_vocabulary`; `_context_perimeter`
        # keeps it on go-to-market's vocabulary (the generator passes `context={"perimeter": GO_TO_MARKET}`).
        allowed, _ = schema_slot_ids(_context_perimeter(info))
        bad = sorted(
            {e.source_slot for e in self.envelope if e.source_slot and e.source_slot not in allowed}
            | {sid for c in self.challenges for sid in c.contests if sid not in allowed}
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
    requirements: list[Requirement] = Field(default_factory=list[Requirement])
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
    envelope: list[EnvelopeElement] = Field(default_factory=list[EnvelopeElement])

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


def _known_or_raw(enum: type[Enum], v: object) -> object:
    """`v` as a member of `enum` when this build knows it, else the raw string a newer Requivo wrote."""
    try:
        return enum(v) if isinstance(v, str) else v
    except ValueError:
        return v


class PersistedClaim(Claim):
    model_config = ConfigDict(extra="allow")
    # A provenance value this build does not know loads (invariant 8) and never reads as a known one:
    # `test_a_claim_value_this_version_does_not_know_round_trips`.
    source: Union[ClaimSource, str]  # pyright: ignore[reportIncompatibleVariableOverride]
    confirmation: Union[Confirmation, str] = Confirmation.open  # pyright: ignore[reportIncompatibleVariableOverride]
    answered_by: Union[Answerer, str, None] = None  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("source", "confirmation", "answered_by", mode="before")
    @classmethod
    def _value_or_raw(cls, v: object, info: ValidationInfo) -> object:
        enum = {"source": ClaimSource, "confirmation": Confirmation}.get(info.field_name or "", Answerer)
        return _known_or_raw(enum, v)


class PersistedSlot(Slot):
    model_config = ConfigDict(extra="allow")
    # A confidence value this build does not know must load (invariant 8) and never read as `explicit`:
    # `test_a_slot_confidence_this_version_does_not_know_survives_a_round_trip_unread_as_explicit`.
    # `Union`, never `Confidence | str`: pydantic evaluates this at class definition, which 3.9 cannot.
    confidence: Union[Confidence, str]  # pyright: ignore[reportIncompatibleVariableOverride]
    claims: list[PersistedClaim] = Field(default_factory=list[PersistedClaim], max_length=MAX_CLAIMS_PER_SLOT)  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_or_raw(cls, v: object) -> object:
        return _known_or_raw(Confidence, v)


class PersistedQuestion(Question):
    model_config = ConfigDict(extra="allow")


class PersistedSummary(Summary):
    model_config = ConfigDict(extra="allow")


class PersistedDesignDecision(DesignDecision):
    model_config = ConfigDict(extra="allow")
    source: Union[DecisionSource, str, None] = None  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("source", mode="before")
    @classmethod
    def _source_or_raw(cls, v: object) -> object:
        return _known_or_raw(DecisionSource, v)


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
    # `test_the_persisted_mirror_copies_every_constraint_it_restates`. Each re-declaration swaps in a
    # permissive twin, an unsafe mutable override to a checker and the point of invariant 8:
    # `test_the_persisted_contract_is_permissive_all_the_way_down`.
    model: dict[str, PersistedSlot]  # pyright: ignore[reportIncompatibleVariableOverride]
    questions: list[PersistedQuestion] = Field(default_factory=list[PersistedQuestion], max_length=MAX_QUESTIONS)  # pyright: ignore[reportIncompatibleVariableOverride]
    summary: PersistedSummary  # pyright: ignore[reportIncompatibleVariableOverride]
    decisions: list[PersistedDesignDecision] = Field(default_factory=list[PersistedDesignDecision])  # pyright: ignore[reportIncompatibleVariableOverride]
    challenges: list[PersistedChallenge] = Field(default_factory=list[PersistedChallenge])  # pyright: ignore[reportIncompatibleVariableOverride]
    opportunities: list[PersistedOpportunity] = Field(default_factory=list[PersistedOpportunity])  # pyright: ignore[reportIncompatibleVariableOverride]
    exclusions: list[PersistedExclusion] = Field(default_factory=list[PersistedExclusion])  # pyright: ignore[reportIncompatibleVariableOverride]
    thresholds: list[PersistedThreshold] = Field(default_factory=list[PersistedThreshold])  # pyright: ignore[reportIncompatibleVariableOverride]
