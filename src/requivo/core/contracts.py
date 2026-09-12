from __future__ import annotations

import functools
import hashlib
import json
from enum import Enum
from typing import Annotated, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from requivo.paths import FRAMEWORK

SOFT_COMPLETENESS = 70  # below this a slot is "soft" (tunable)

# The element type of one tri-state reasoning collection. It exists so `ModelProposal.resolve`'s inner
# `keep` helper can be annotated per call site rather than inferred across all three of them — see the
# comment at its definition. Runtime behaviour is unchanged; this is a name for the checker.
_Item = TypeVar("_Item")

# The engine asks at most this many questions per turn. A constant rather than a literal because two
# contracts carry the cap — `ModelProposal` and its persisted mirror — and pydantic does **not** let
# the mirror inherit it: re-annotating a field in a subclass without restating `Field(...)` drops the
# parent's constraints *and* its default, which would silently make `questions` required. So the
# mirror is forced to say it again, and two hand-written numbers that must agree and are checked by
# nothing is the defect class #14 exists to remove. The general property is pinned by
# `test_the_persisted_mirror_copies_every_constraint_it_restates`; this name removes the instance.
# The prompt asset says 3–6 in prose and cannot read this, so `engine.md` is the one place the number
# is genuinely duplicated — deliberately, and it is a floor-and-ceiling hint there rather than a cap.
MAX_QUESTIONS = 6

# The ceiling on a raw text input the engine reasons over: a discovery request, or the answers a
# refinement turn folds in. Enforced by `require_input_within_bounds` in `core/validation.py`, not
# only by the Web routes as it once was (#255) — a rule enforced by one interface is not enforced
# (invariant 14), and the cost of the gap was unbounded text billed on every turn of the interactive
# loop; `test_an_oversized_request_is_refused_before_any_provider_call` and its siblings are the
# guard. `web/config.py` re-exports it under its own names so the routes keep their re-render on top.
MAX_INPUT_CHARS = 20_000


class StrictModel(BaseModel):
    """Base for every contract an LLM fills.

    `extra="forbid"` rather than Pydantic's default of dropping unknown keys. The product's promise is
    a *validated* model, and silently discarding a field the model invented breaks that twice: the
    output looks conformant while carrying less than the model produced, and a prompt that has drifted
    away from its contract (a renamed field, an extra section) reads as a clean success instead of the
    loud failure it is. Rejecting is also self-healing here — `_complete()` retries with a corrective
    nudge, so the model is told what it got wrong rather than having it quietly deleted.

    This is the boundary contract only. Internal partial projections (a diff, a propagate basis) build
    `EngineOutput`s directly and are unaffected; the *completeness* rules that a real discovery reply
    must satisfy still live at the discovery boundary, not here.
    """

    model_config = ConfigDict(extra="forbid")


# A text field that must actually say something. Used where an empty string is not a legal value but a
# silently-degraded output: an unanswerable question, a nameless story, a challenge with no premise.
NonEmpty = Annotated[str, Field(min_length=1)]


def _stable_id(prefix: str, *parts: str) -> str:
    """A content-derived identifier: `<prefix>_<10 hex>` over the parts that carry the item's identity.

    Derived rather than assigned, because there is no authority to assign one. The model does not
    return ids (and could not return *stable* ones), and a counter kept in session metadata would have
    to be reconciled on every apply. Hashing the statement itself gives an id that is identical across
    revisions, surfaces and machines for as long as the statement is unchanged — which is exactly the
    span over which "the same decision" means anything. A reworded decision gets a new id, and that is
    honest: nothing in the data says the rewording preserved the intent.
    """
    digest = hashlib.sha256("␟".join(p.strip() for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:10]}"


def _reject_duplicate_ids(label: str, ids: list[str]) -> None:
    """Ids are pointers — a story id is cited by an estimate, an issue id by a `depends_on`, a scenario
    id by a test run. A repeated one does not read as a duplicate downstream; it reads as one item,
    and whichever copy is found first wins silently."""
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{label} repeat the same id: {dupes}")


@functools.lru_cache(maxsize=1)
def schema_slots() -> tuple[dict, ...]:
    """`framework/model_schema.json`'s `slots` list, parsed once and cached.

    Public since #301: this file and `core/analysis.py` each read and `json.loads`'d the same file
    independently, at four call sites (`schema_slot_ids`/`_schema_order` here, `slot_meta`/
    `_default_impacts` there) -- one file, parsed four times, on every cold cache. This is the one
    parse every projection below now reads from; each stays its own cached projection (allowed/
    required ids, schema order, pillar+label, baseline impact) rather than folding into a single
    shape, because they slice the same rows differently and a caller wanting `_schema_order()`'s
    tuple should not have to reconstruct it from `slot_meta()`'s dicts.

    Returned as a tuple of the raw dicts, in file order -- not keyed by id -- because two of the
    four projections need that order preserved, and a dict comprehension would have already lost it.
    """
    return tuple(json.loads((FRAMEWORK / "model_schema.json").read_text(encoding="utf-8"))["slots"])


@functools.lru_cache(maxsize=1)
def schema_slot_ids() -> tuple[frozenset[str], frozenset[str]]:
    """(allowed, required) slot ids from framework/model_schema.json. `required` excludes any slot
    flagged `optional`. Cached — the schema is read once. This is the single source of the slot
    vocabulary the model must speak; the contract and readiness both defer to it."""
    slots = schema_slots()
    allowed = frozenset(s["id"] for s in slots)
    required = frozenset(s["id"] for s in slots if not s.get("optional", False))
    return allowed, required


def missing_required_slots(present: set[str]) -> list[str]:
    """Required slot ids absent from `present`, in schema order — what a complete model still owes."""
    _, required = schema_slot_ids()
    return [sid for sid in _schema_order() if sid in required and sid not in present]


def unknown_slots(present: set[str]) -> list[str]:
    """Slot ids in `present` that the schema does not define — hallucinated / typo'd keys."""
    allowed, _ = schema_slot_ids()
    return sorted(present - allowed)


@functools.lru_cache(maxsize=1)
def _schema_order() -> tuple[str, ...]:
    return tuple(s["id"] for s in schema_slots())


class Confidence(str, Enum):
    explicit = "explicit"
    inferred = "inferred"
    empty = "empty"


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


class Question(StrictModel):
    # A question with no text, or aimed at nothing, is not a question — and it would still be rendered,
    # counted and answered against. The slot id is checked against the schema vocabulary below.
    q: NonEmpty
    slot: NonEmpty
    why: NonEmpty


class Summary(StrictModel):
    objective: str = ""
    scope: str = ""
    assumptions: list[str] = Field(default_factory=list)
    blind_spot: str = ""


class ModelProposal(StrictModel):
    """A *proposed* model — what a surface sends to be applied: a discovery reply, a Claude Code
    proposal file, a Web form. Identical to the `EngineOutput` it resolves into, except in one
    load-bearing way: the three reasoning collections are **tri-state**.

        absent   the proposal says nothing about them — the established reasoning stands
        []       an explicit removal — the established reasoning is dropped
        [ … ]    a replacement

    That distinction has to live in the contract, not in a prompt. A refinement turn answers a
    question; it does not re-derive the brief, so `engine.md` asks only for `model`, `questions` and
    `summary`. Read as a whole `EngineOutput`, such a reply silently *deleted* every decision,
    challenge and opportunity the assessment had established — the one part of the model that carries
    the reasoning behind the facts — and, because the deletion arrived as an omission, nothing
    downstream could tell it apart from "nothing changed": the diff reported no reasoning movement and
    every artifact stayed marked fresh. `resolve()` is where the three states are collapsed against
    the model being refined, once, for every surface.
    """

    # protected_namespaces=() lets us keep the field literally named `model`; extra="forbid" is
    # inherited from StrictModel and restated here so the whole config is visible in one place.
    model_config = ConfigDict(protected_namespaces=(), extra="forbid")
    model: dict[str, Slot]
    # The engine asks at most MAX_QUESTIONS (the prompt says 3–6; the stop signal is []). The cap is an
    # invariant, not a suggestion — a turn that floods 12 questions has stopped prioritising by
    # information value. The persisted mirror restates this field and must restate the cap with it.
    questions: list[Question] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    summary: Summary
    # The reasoning layer — persisted so generators inherit it, not just the facts. Filled at discovery
    # finalization by absorbing advise()'s Brief. These types are defined below (Brief section);
    # forward-referenced here, resolved by model_rebuild() at the end of this module.
    #
    # `SerializeAsAny` on these three and nowhere else, because these three are the only fields a
    # value read off disk survives into: `resolve()` carries an unstated collection forward from the
    # model being refined (invariant 10), so a `PersistedDesignDecision` loaded from a newer
    # Requivo ends up sitting under this strict annotation. Pydantic serializes by the *annotated*
    # type, so without this the item keeps its unknown key in memory and loses it on the very next
    # write — the loud failure #14 removed, coming back as a quiet one. Validation is untouched: an
    # invented field in a provider reply is still refused here, because that arrives as a dict and
    # is validated, not carried.
    decisions: Optional[list[SerializeAsAny[DesignDecision]]] = None
    challenges: Optional[list[SerializeAsAny[Challenge]]] = None
    opportunities: Optional[list[SerializeAsAny[Opportunity]]] = None

    def resolve(self, current: Optional[EngineOutput] = None) -> EngineOutput:
        """Collapse the proposal onto the model it refines, yielding a complete `EngineOutput`.

        Every collection the proposal left unstated is carried forward from `current`; every one it
        stated — including as an empty list — replaces what was there. With no `current` (a first
        discovery) an unstated collection is simply empty: there is nothing to carry.

        A key `current` carries that this version cannot name is carried the same way, and for the
        same reason (#14): a proposal is `extra="forbid"`, so its silence about a field a newer
        Requivo added is not a decision to delete it. The result is then a `PersistedEngineOutput`,
        the only kind of `EngineOutput` that can hold such a key, re-admitted through `model_dump()`
        at the cost of one extra validation on the rare path. Dropping it here would turn the refusal
        #14 removed into a silent loss on the first refinement turn —
        `test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save` is the guard.

        What is *not* carried, and is a real narrowing rather than an oversight: the slots, the
        summary and the questions come from the proposal, which replaces them wholesale. An unknown
        key inside a slot written by a newer Requivo does not survive an apply, because the apply
        supersedes that slot with one this version built."""
        prior = current or EngineOutput(model={}, summary=Summary())

        # Annotated, and it has to be (#78): unannotated, pyright infers one return type across all
        # three call sites and hands `challenges` a `list[DesignDecision | Challenge]`. The union is
        # an artefact of the helper being shared, not of anything this function does — the checker
        # was right that the signature said nothing, and wrong about the code. `_Item` keeps each
        # call site's own element type.
        def keep(stated: Optional[list[_Item]], established: list[_Item]) -> list[_Item]:
            return list(established) if stated is None else list(stated)

        resolved = EngineOutput(
            model=self.model,
            questions=self.questions,
            summary=self.summary,
            decisions=keep(self.decisions, prior.decisions),
            challenges=keep(self.challenges, prior.challenges),
            opportunities=keep(self.opportunities, prior.opportunities),
        )
        carried = getattr(prior, "__pydantic_extra__", None) or {}
        if not carried:
            return resolved
        return PersistedEngineOutput.model_validate({**resolved.model_dump(), **carried})

    @model_validator(mode="after")
    def _validate_slot_vocabulary(self):
        # Every slot id the output names — in the model AND in the questions it targets — must be one
        # the schema defines. A hallucinated or typo'd key would otherwise sit unseen by every
        # schema-driven view, or point a question at a slot that doesn't exist. Completeness (the full
        # required set) is enforced at the discovery boundary, not here, so internal partial
        # projections (diff/propagate) stay constructable; the vocabulary check is safe everywhere.
        bad_model = unknown_slots(set(self.model))
        if bad_model:
            raise ValueError(f"unknown slots (not in schema): {bad_model}")
        allowed, _ = schema_slot_ids()
        bad_questions = sorted({q.slot for q in self.questions if q.slot not in allowed})
        if bad_questions:
            raise ValueError(f"questions target unknown slots (not in schema): {bad_questions}")
        # The reasoning layer carries DAG edges into the slots: a decision rests on `derived_from`, a
        # challenge contests `contests`. An edge to a slot the schema does not define would let the
        # dependency graph (propagate / impact) look rigorous while pointing at nothing — so the same
        # vocabulary rule applies to every reference, not just the model and the questions.
        bad_refs = sorted(
            {sid for d in (self.decisions or []) for sid in d.derived_from if sid not in allowed}
            | {sid for c in (self.challenges or []) for sid in c.contests if sid not in allowed}
        )
        if bad_refs:
            raise ValueError(f"reasoning references unknown slots (not in schema): {bad_refs}")
        # Ids are content-derived, so two identical items collide on one id. That is not a harmless
        # duplicate: the id is what a diff keys on and what a user refers a decision back by, so a
        # collision makes one of the pair invisible to change detection and ambiguous to cite. The
        # engine restating the same decision twice is the realistic cause, and it is a defect in the
        # reply — the retry loop can fix it, silently dropping one cannot.
        for label, items in (("decisions", self.decisions), ("challenges", self.challenges),
                             ("opportunities", self.opportunities)):
            ids = [i.id for i in (items or [])]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(
                    f"{label} contains repeated entries (identical content yields one id): {dupes}")
        return self


class EngineOutput(ModelProposal):
    """A *resolved* model — the durable product, and what every reader downstream sees.

    The difference from `ModelProposal` is exactly the tri-state: here the three reasoning collections
    are always concrete lists, so no renderer, generator or diff has to ask whether "no decisions"
    means none or means unstated. A proposal becomes one through `resolve()`, which is the only place
    that question is answered."""

    decisions: list[SerializeAsAny[DesignDecision]] = Field(default_factory=list)
    challenges: list[SerializeAsAny[Challenge]] = Field(default_factory=list)
    opportunities: list[SerializeAsAny[Opportunity]] = Field(default_factory=list)


class Story(StrictModel):
    id: NonEmpty
    title: NonEmpty
    as_a: str = ""
    i_want: str = ""
    so_that: str = ""
    acceptance: list[str] = Field(default_factory=list)
    # Which slots this story is traceable to. Checked against the schema like every other slot
    # reference: an id that names nothing makes the story *look* grounded in the model while linking
    # to nothing, and the trace is the reason the field exists.
    slots: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_slot_references(self):
        allowed, _ = schema_slot_ids()
        bad = sorted({sid for sid in self.slots if sid not in allowed})
        if bad:
            raise ValueError(f"story {self.id!r} references unknown slots (not in schema): {bad}")
        return self


class Stories(StrictModel):
    stories: list[Story] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_ids(self):
        # Ids are how an estimate item points back at a story. Two stories sharing one make that
        # pointer ambiguous, and the estimate silently attaches to whichever came first.
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
        # An inverted range is not a wide estimate, it is a broken one: the totals sum both ends, so
        # `5–1` quietly drags the project low bound above its high bound, and the spread — which is
        # how uncertainty is *communicated* here — reads backwards.
        if self.days_low > self.days_high:
            raise ValueError(
                f"estimate for {self.story_id!r} has days_low ({self.days_low}) above days_high "
                f"({self.days_high}) — low is the optimistic end")
        return self


class EstimateDraft(StrictModel):
    # What the LLM produces. Totals, confidence and spread_drivers are computed in Python
    # (from real slot data) so they can't be hallucinated.
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


# The reasoning layer carries identity. A decision, a challenge and an opportunity are each things a
# reader will want to refer back to — comment on, mark as accepted, follow across revisions — and text
# is a poor handle for that. Each therefore carries a stable `id`, always *recomputed* from its own
# content on validation: whatever a model put in the field is overwritten, so an id can never be
# hallucinated, and a round-trip through JSON yields the identical value.

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
    # Not "what did we learn" but "what should we contest" — the senior-PM pushback on the premise.
    # All five parts are load-bearing: a challenge missing its alternative or its recommendation is an
    # objection with nowhere to go, and it would still be rendered as if it were actionable.
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
    # A settled decision. why/alternative/tradeoff are filled only where there was a real fork —
    # trivial sourcing facts stay a bare `decision` line.
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


class Brief(StrictModel):
    # The advisory layer: what a senior consultant would add on top of the discovery.
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
    open_decisions: list[str] = Field(default_factory=list)  # decisions still to make


class Priority(str, Enum):
    must = "must"
    should = "should"
    could = "could"


class Requirement(StrictModel):
    # A requirement with no text is a priority attached to nothing, and it still renders as a numbered
    # line in the PRD — an empty row a reader has to decide the meaning of.
    id: NonEmpty
    requirement: NonEmpty
    priority: Priority


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

    @model_validator(mode="after")
    def _validate_unique_requirement_ids(self):
        _reject_duplicate_ids("requirements", [r.id for r in self.requirements])
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
    # A feature *is* its scenarios here — the document is a recette checklist, and a feature with none
    # is a heading someone has to test by intuition.
    scenarios: list[Scenario] = Field(min_length=1)


class AcceptanceCriteria(StrictModel):
    title: NonEmpty
    features: list[Feature] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_scenario_ids(self):
        # Scenario ids are cited in test runs and bug reports, across features — so they are unique
        # across the document, not per feature.
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
        # `depends_on` is exported as a real link — a GitLab issue relation, a line in a GitHub body,
        # an ordering an n8n flow acts on. An edge to an id this epic does not contain is a dependency
        # on nothing, and it survives the export looking exactly like a real one.
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
#
# Two invariants ask opposite things of this one shape, and neither of them is negotiable.
#
#   Invariant 4 — what an LLM fills is `extra="forbid"`. A field the model invented must fail loudly
#   and ride `_complete()`'s retry loop, because a silently dropped key makes a prompt that has
#   drifted away from its contract read as a clean success. `StrictModel` says all of this already.
#
#   Invariant 8 — what is on disk is forward-compatible. `.requivo/sessions/` is public at
#   format_version 1 and `docs/compatibility.md` says adding a field, anywhere, needs no bump; so a
#   key written by a *newer* Requivo must survive a round-trip through an older one rather than
#   making the session unopenable. `SessionMeta` and `RevisionRecord` have said this since 0.9.4.
#
# The two are not inconsistent, and this comment exists so the next reader does not "fix" them into
# agreement. What differs is where the bytes came from, and therefore what an unknown key is
# evidence *of*. From a provider it means something is wrong now, and there is a retry that can put
# it right — so refusing is the cheap, self-healing answer. From disk it means something is newer,
# there is no retry, and refusing costs the user a session they can otherwise read perfectly well.
# Relaxing `StrictModel` to settle the argument would persist a hallucinated field as though it were
# part of the model; tightening the disk side is the bug this block was written to close (#14).
#
# Reading permissively is only half of it, and the first version of this fix stopped there. What a
# reader accepts and what a *writer* preserves are two questions, and getting the first right while
# the second silently drops the key is worse than the refusal it replaced: nothing fails and nothing
# says so. Two places had to move with it, both in `resolve()` — `SerializeAsAny` on the strict
# tree's three reasoning collections, because those are where a value read off disk survives into,
# and carrying `current`'s top-level unknown keys, because a proposal is `extra="forbid"` and so
# cannot speak to them at all. `resolve()` states the argument for each.
#
# What this genuinely does not promise, and the distinction is worth keeping sharp. An *apply*
# replaces the slots, the summary and the questions with the ones the proposal carries (invariant
# 10), so an unknown key inside one of those does not survive a refinement turn — it is superseded,
# not dropped. And an unknown *slot id* is still refused: that is `schema_version`'s frontier, which
# refuses a newer slot schema with a message naming the upgrade, and absorbing it here as an extra
# key would route it around that.
#
# The mirror is written out class by class rather than generated from the strict tree, so it greps
# and so nothing about it is clever. Its one failure mode — a nested contract nobody remembered to
# twin, which re-forbids extras one level down and says nothing — is guarded by a test that walks
# the field graph of both trees rather than listing them
# (`test_the_persisted_contract_is_permissive_all_the_way_down`).


class PersistedSlot(Slot):
    model_config = ConfigDict(extra="allow")


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


class PersistedEngineOutput(EngineOutput):
    """An `EngineOutput` as it is read back from `model.json` or `revisions/NNNN-model.json`.

    A subclass, so every reader annotated `EngineOutput` keeps working and every validator the
    strict tree carries — the slot vocabulary, the DAG edges, the derived ids — still runs. The only
    difference is that an unknown key is carried instead of refused, at every level: the nested
    fields are re-declared against the permissive twins because pydantic serializes by the
    *annotated* type, so a permissive value under a strict annotation would load fine and then lose
    its extras on the next write — which is the half of the promise that matters.

    Read the block above for why this and `StrictModel` disagree on purpose."""

    model_config = ConfigDict(protected_namespaces=(), extra="allow")
    # Every re-declared field has to restate its constraints, because pydantic drops the parent's
    # `FieldInfo` when a subclass re-annotates: annotation alone would lose the cap *and* the default,
    # making `questions` required. `MAX_QUESTIONS` is why the cap cannot drift by value, and
    # `test_the_persisted_mirror_copies_every_constraint_it_restates` is why it cannot drift at all.
    model: dict[str, PersistedSlot]
    questions: list[PersistedQuestion] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    summary: PersistedSummary
    decisions: list[PersistedDesignDecision] = Field(default_factory=list)
    challenges: list[PersistedChallenge] = Field(default_factory=list)
    opportunities: list[PersistedOpportunity] = Field(default_factory=list)
