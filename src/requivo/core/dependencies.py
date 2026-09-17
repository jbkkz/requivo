"""The dependency DAG: impact propagation over a saved model. Pure (no I/O, no LLM, no argv). Two
edge sets: slot → decision from `DesignDecision.derived_from`, slot → artifact from `ARTIFACT_SLOTS`.

    change these slots → which decisions must be re-validated, and which artifacts go stale?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from requivo.core.analysis import slot_label, slot_meta
from requivo.core.contracts import Confidence, EngineOutput
from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter
from requivo.core.selectors import normalize_tokens


def _all_slot_ids(perimeter: str = DEFAULT_PERIMETER) -> set[str]:
    return set(slot_meta(perimeter)[1])  # (pillars, labels) — labels is keyed by every slot id


# Which slots materially shape each artifact; deliberate, not "everything". `brief` maps to `*`: it is
# a judgment over the whole model, and a saved copy stays marked fresh otherwise (invariant 1).
_ARTIFACT_SLOTS_RAW: dict[str, set[str] | str] = {
    "brief": "*",
    "prd": {"problem", "success_metrics", "actors", "business_objects", "business_rules",
            "workflow", "integrations", "permissions", "constraints", "edge_cases",
            "acceptance", "risks"},
    "stories": {"actors", "business_objects", "business_rules", "workflow", "permissions"},
    "estimate": {"business_objects", "business_rules", "workflow", "integrations",
                 "permissions", "config_vs_custom", "constraints"},
    "criteria": {"workflow", "business_rules", "permissions", "edge_cases", "acceptance"},
    "epic": {"actors", "business_objects", "business_rules", "workflow", "integrations",
             "permissions", "config_vs_custom", "constraints"},
    "release": {"problem", "success_metrics", "workflow", "risks"},
    # The go-to-market perimeter's one artifact (#609), `*` for the same reason `brief` is.
    "gtm_plan": "*",
}

# type → filename under <session>/artifacts/, for everything that can be persisted: the one table three
# layers read (the service that saves, the CLI, the integrity checker). `ARTIFACT_FILES` was a second
# one until #556 (`decision: the-estimate-graduates` made the two identical);
# `test_the_real_artifact_registries_agree_on_their_key_sets` catches a type missing here.
ARTIFACT_FILENAMES: dict[str, str] = {
    "brief": "solution-assessment.md",
    "prd": "prd.md",
    "stories": "stories.md",
    "estimate": "estimate.md",
    "criteria": "acceptance-criteria.md",
    "epic": "epic.md",
    "release": "release-notes.md",
    "gtm_plan": "go-to-market-plan.md",  # #609
}

# Artifacts that rest on the *reasoning* layer: every generator, since each is prompted with the complete
# EngineOutput.
REASONING_CONSUMERS: frozenset[str] = frozenset(_ARTIFACT_SLOTS_RAW)


def artifact_slots(perimeter: str = DEFAULT_PERIMETER) -> dict[str, set[str]]:
    """The artifact→slots map with `*` expanded, narrowed to the types `perimeter` may produce (#608)."""
    every = _all_slot_ids(perimeter)
    owned = get_perimeter(perimeter).artifact_types
    return {name: (set(every) if slots == "*" else set(slots))
            for name, slots in _ARTIFACT_SLOTS_RAW.items() if name in owned}


def resolve_slots(tokens: list[str], perimeter: str = DEFAULT_PERIMETER) -> tuple[list[str], list[str]]:
    """Map user-typed tokens (slot ids or label substrings) to slot ids: (resolved ids in schema
    order, unmatched tokens). An unmatched token is reported, not dropped; an empty one is refused by
    `normalize_tokens`, since `"" in label` matches every label."""
    _, labels = slot_meta(perimeter)
    # Materialised before the helper iterates it: a generator would be exhausted by `normalize_tokens`.
    tokens = list(tokens)
    keys = normalize_tokens(tokens, what="slot")
    resolved, unmatched = [], []
    for raw, key in zip(tokens, keys):
        if key in labels:  # exact slot id
            hit = [key]
        else:  # label substring, e.g. "permission" → permissions
            hit = [sid for sid, lab in labels.items() if key in lab.lower()]
        if hit:
            resolved.extend(hit)
        else:
            # `raw.strip()`: echo the token the guard checked, or a leading newline forges a line (#40).
            unmatched.append(raw.strip())
    ordered = [sid for sid in labels if sid in set(resolved)]  # schema order, de-duped
    return ordered, unmatched


@dataclass
class DecisionImpact:
    decision: str
    rests_on: list[str]  # labels of the changed slots this decision was derived from

    def to_dict(self) -> dict:
        return {"decision": self.decision, "rests_on": self.rests_on}


@dataclass
class ChallengeImpact:
    headline: str
    rests_on: list[str]  # labels of the changed slots whose premise this challenge contests

    def to_dict(self) -> dict:
        return {"headline": self.headline, "rests_on": self.rests_on}


@dataclass
class ExclusionImpact:
    option: str
    rests_on: list[str]  # labels of the changed slots this exclusion rests on

    def to_dict(self) -> dict:
        return {"option": self.option, "rests_on": self.rests_on}


@dataclass
class ThresholdImpact:
    condition: str
    rests_on: list[str]  # labels of the changed slots this threshold rests on

    def to_dict(self) -> dict:
        return {"condition": self.condition, "rests_on": self.rests_on}


@dataclass
class ImpactReport:
    changed: list[str]  # labels of the slots in question
    decisions: list[DecisionImpact] = field(default_factory=list)
    challenges: list[ChallengeImpact] = field(default_factory=list)
    exclusions: list[ExclusionImpact] = field(default_factory=list)
    thresholds: list[ThresholdImpact] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # artifact names whose slot set is touched
    # `None` is *not reviewed* (#493): `propagate` has no revision history; an empty report ran and found nothing.
    evidence: Optional[EvidenceReport] = None

    @property
    def reasoning_hit(self) -> bool:
        """True if the change unseats a piece of baked-in reasoning, so the saved assessment no longer holds."""
        return bool(self.decisions or self.challenges or self.exclusions or self.thresholds)

    @property
    def empty(self) -> bool:
        return (not self.decisions and not self.challenges and not self.exclusions
               and not self.thresholds and not self.artifacts)

    def to_dict(self) -> dict:
        """The wire shape for the API's `/impact` route (#425); the field names are this dataclass's own."""
        return {"changed": self.changed,
                "decisions": [d.to_dict() for d in self.decisions],
                "challenges": [c.to_dict() for c in self.challenges],
                "exclusions": [e.to_dict() for e in self.exclusions],
                "thresholds": [t.to_dict() for t in self.thresholds],
                "artifacts": self.artifacts,
                "evidence": None if self.evidence is None else self.evidence.to_dict()}


def propagate(out: EngineOutput, changed: list[str], perimeter: str = DEFAULT_PERIMETER) -> ImpactReport:
    """What rests on the changed slots: decisions (`derived_from`), challenges (`contests`),
    exclusions and thresholds (`rests_on`), and the artifacts that go stale, narrowed to `perimeter`'s (#608)."""
    changed_set = set(changed)
    report = ImpactReport(changed=[slot_label(sid, perimeter) for sid in changed])

    for d in out.decisions:
        hit = [sid for sid in d.derived_from if sid in changed_set]
        if hit:
            report.decisions.append(
                DecisionImpact(d.decision, [slot_label(sid, perimeter) for sid in hit]))

    for c in out.challenges:
        hit = [sid for sid in c.contests if sid in changed_set]
        if hit:
            report.challenges.append(
                ChallengeImpact(c.headline, [slot_label(sid, perimeter) for sid in hit]))

    for e in out.exclusions:
        hit = [sid for sid in e.rests_on if sid in changed_set]
        if hit:
            report.exclusions.append(
                ExclusionImpact(e.option, [slot_label(sid, perimeter) for sid in hit]))

    for t in out.thresholds:
        hit = [sid for sid in t.rests_on if sid in changed_set]
        if hit:
            report.thresholds.append(
                ThresholdImpact(t.condition, [slot_label(sid, perimeter) for sid in hit]))

    amap = artifact_slots(perimeter)
    report.artifacts = [name for name in amap if amap[name] & changed_set]
    return report


@dataclass
class ReasoningDiff:
    """What moved in the reasoning layer between two model versions — ids, per collection."""
    decisions: list[str] = field(default_factory=list)
    challenges: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    thresholds: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.decisions or self.challenges or self.opportunities or self.exclusions
                    or self.thresholds)

    def to_dict(self) -> dict:
        return {"decisions": self.decisions, "challenges": self.challenges,
                "opportunities": self.opportunities, "exclusions": self.exclusions,
                "thresholds": self.thresholds}


def _diff_items(old_items: list, new_items: list) -> list[str]:
    """Ids added, removed or edited between two reasoning collections. Symmetric, populated → empty
    included, which is safe only because both sides are *resolved* models (`ModelProposal.resolve`
    carries omitted reasoning forward), so an empty side is a genuine deletion."""
    old_by_id = {i.id: i.model_dump_json() for i in old_items}
    new_by_id = {i.id: i.model_dump_json() for i in new_items}
    # Compare content, not ids: `id` is derived from a subset of the fields.
    return sorted(k for k in old_by_id.keys() | new_by_id.keys()
                  if old_by_id.get(k) != new_by_id.get(k))


def diff_reasoning(old: EngineOutput, new: EngineOutput) -> ReasoningDiff:
    """The reasoning-layer counterpart of `diff_models`: unchanged slots with changed decisions is a
    materially different model."""
    return ReasoningDiff(
        decisions=_diff_items(old.decisions, new.decisions),
        challenges=_diff_items(old.challenges, new.challenges),
        opportunities=_diff_items(old.opportunities, new.opportunities),
        exclusions=_diff_items(old.exclusions, new.exclusions),
        thresholds=_diff_items(old.thresholds, new.thresholds),
    )


def diff_models(old: EngineOutput, new: EngineOutput) -> list[str]:
    """Slot ids that materially changed (value, confidence, impact or test plan; completeness alone is noise)."""
    changed = []
    for sid in old.model.keys() | new.model.keys():
        old_slot = old.model.get(sid)
        new_slot = new.model.get(sid)
        if old_slot is None or new_slot is None:  # slot appeared or disappeared between versions
            changed.append(sid)
        elif (
            old_slot.value.strip() != new_slot.value.strip()
            or old_slot.confidence != new_slot.confidence
            or old_slot.impact != new_slot.impact
            # A re-planned test is a material change (#610): `test_a_re_planned_test_is_a_material_change`.
            or old_slot.test_plan.strip() != new_slot.test_plan.strip()
        ):
            changed.append(sid)
    return changed


# ── Evidence since derivation (#493) ──────────────────────────────────────────
# A decision derived while a slot it rests on was `empty`, `inferred` or `testable` (#610), and that slot
# is `explicit` now: *worth re-reading*, never "contradicted", which is the assessment's judgment.
# `test_a_decision_derived_from_a_thin_slot_that_is_now_explicit_is_flagged`.

_THIN = frozenset({Confidence.empty, Confidence.inferred, Confidence.testable})


@dataclass
class ThinnerEvidence:
    """One decision recorded while a slot it rests on carried thinner evidence than it does now."""
    decision: str
    id: str
    thickened: list[str]  # labels of the derived_from slots that were empty/inferred then, explicit now
    # The revision the decision was first recorded at; the pure comparison leaves it None.
    derived_at: Optional[int] = None

    def to_dict(self) -> dict:
        return {"decision": self.decision, "id": self.id, "thickened": self.thickened,
                "derived_at": self.derived_at}


@dataclass
class EvidenceUnknown:
    """A decision the review could not decide about, and why: the third state."""
    decision: str
    id: str
    reason: str

    def to_dict(self) -> dict:
        return {"decision": self.decision, "id": self.id, "reason": self.reason}


@dataclass
class EvidenceReport:
    """What `thinner_evidence` found. `reviewed` counts every decision examined, so an empty `flagged`
    on a model with decisions reads as *checked, none*."""
    reviewed: int = 0
    flagged: list[ThinnerEvidence] = field(default_factory=list)
    could_not_tell: list[EvidenceUnknown] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.flagged and not self.could_not_tell

    def to_dict(self) -> dict:
        return {"reviewed": self.reviewed,
                "flagged": [f.to_dict() for f in self.flagged],
                "could_not_tell": [u.to_dict() for u in self.could_not_tell]}


def thinner_evidence(then: EngineOutput, now: EngineOutput,
                     perimeter: str = DEFAULT_PERIMETER) -> EvidenceReport:
    """Decisions in `now` recorded, in `then`, against thinner evidence than `now` holds: pure, two
    models in, a report out, matched by content-derived id (invariant 5). Three states per decision:
    flagged, clean, or `could_not_tell` (not in `then`, no `derived_from`, or a slot one model does
    not carry); a firm flag outranks a partial look."""
    then_ids = {d.id for d in then.decisions}
    report = EvidenceReport()
    for d in now.decisions:
        report.reviewed += 1
        if d.id not in then_ids:
            report.could_not_tell.append(EvidenceUnknown(
                d.decision, d.id, "not recorded in the model it is being compared against"))
            continue
        if not d.derived_from:
            report.could_not_tell.append(EvidenceUnknown(
                d.decision, d.id, "records no slots it was derived from"))
            continue
        thickened: list[str] = []
        unresolved: list[str] = []
        for sid in d.derived_from:
            before, after = then.model.get(sid), now.model.get(sid)
            if before is None or after is None:
                unresolved.append(sid)
            elif before.confidence in _THIN and after.confidence == Confidence.explicit:
                thickened.append(sid)
        if thickened:
            report.flagged.append(ThinnerEvidence(
                d.decision, d.id, [slot_label(sid, perimeter) for sid in thickened]))
        elif unresolved:
            report.could_not_tell.append(EvidenceUnknown(
                d.decision, d.id,
                "not in both models: " + ", ".join(slot_label(sid, perimeter) for sid in unresolved)))
    return report
