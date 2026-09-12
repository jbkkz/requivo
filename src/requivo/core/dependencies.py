"""The dependency DAG — impact propagation over a saved model.

The model is not a flat snapshot: its parts rest on each other. A design decision rests on the
slots it was derived from; a generated artifact consumes a known set of slots. When a slot changes,
the things that rest on it go stale. This module makes that graph explicit and answers one question:

    change these slots → which decisions must be re-validated, and which artifacts go stale?

It is **pure** (no I/O, no LLM, no argv/stdout): `render/` prints an `ImpactReport`, `cli.py` wires
it to a verb. The two edge sets are:

  slot ──derived_from──> decision   from DesignDecision.derived_from (filled by advise())
  slot ──consumed_by───> artifact   from ARTIFACT_SLOTS below (static, honest, coarse)

The artifact edges need no LLM, so propagation works even on a model whose decisions predate
`derived_from` — the decision layer just *explains* the staleness on top of the artifact backbone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from requivo.core.analysis import slot_label, slot_meta
from requivo.core.contracts import EngineOutput
from requivo.core.selectors import normalize_tokens


def _all_slot_ids() -> set[str]:
    return set(slot_meta()[1])  # (pillars, labels) — labels is keyed by every slot id


# Which slots materially shape each artifact. Deliberate, not "everything": an over-broad map makes
# every change invalidate everything, which is the same as saying nothing. The names match the
# buildable generators.
#
# The `brief`/assessment is the one entry mapped to `*`, and that is not laziness. It is a *judgment
# over the whole model* — the executive summary, the complexity verdict, the challenges and the
# understanding checklist are all read off the complete slot set — so any slot that materially moves
# does invalidate the copy on disk. It used to be absent from this map on the grounds that it is the
# live analysis layer rather than a deliverable; that stopped being true when it became a saved
# artifact, and the result was an assessment that stayed marked "fresh" after the problem statement
# under it had changed.
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
}

# The persisted file for each artifact. Used by change-detection to flag *existing* stale files on
# disk. `None` meant "rendered to the terminal only", which `stories` and `estimate` were until #519
# (`decision: the-estimate-graduates`); every type names a file now. The `str | None` type and the
# two arms that read a `None` (`render_impact`, `migrate_legacy`) are left as they are: folding this
# table into ARTIFACT_FILENAMES below is the merge #270 deferred, and it is a separate change.
ARTIFACT_FILES: dict[str, str | None] = {
    "brief": "solution-assessment.md", "prd": "prd.md", "stories": "stories.md",
    "estimate": "estimate.md", "criteria": "acceptance-criteria.md", "epic": "epic.md",
    "release": "release-notes.md",
}

# type → filename under <session>/artifacts/, for everything that can be *persisted*. Core holds it
# because three layers ask the same question — the service that saves, the CLI that offers `--type`,
# and the integrity checker that verifies what a session claims to hold — and a vocabulary that
# exists in two places drifts. It used to differ from ARTIFACT_FILES above in `stories` (saveable by
# Claude Code, unwritten by the provider path) and `estimate` (terminal-only on both counts); since
# #519 the two agree on every type, and `test_ARTIFACT_FILES_and_ARTIFACT_FILENAMES_agree_wherever_both_name_a_file`
# holds them there.
ARTIFACT_FILENAMES: dict[str, str] = {
    "brief": "solution-assessment.md",
    "prd": "prd.md",
    "stories": "stories.md",
    "estimate": "estimate.md",
    "criteria": "acceptance-criteria.md",
    "epic": "epic.md",
    "release": "release-notes.md",
}

# Artifacts that rest on the *reasoning* layer (decisions / challenges / opportunities), not only on
# slots. This is every generator, and deliberately so: each one is prompted with the complete
# EngineOutput — `model_dump_json()`, reasoning included — so a decision that changes can change the
# artifact even when no slot moved. The slot map above is a genuine narrowing because a generator
# reads only some *facts*; there is no comparable narrowing here, because they all read all of it.
REASONING_CONSUMERS: frozenset[str] = frozenset(_ARTIFACT_SLOTS_RAW)


def artifact_slots() -> dict[str, set[str]]:
    """Resolve the artifact→slots map, expanding `*` to every slot id."""
    every = _all_slot_ids()
    return {name: (set(every) if slots == "*" else set(slots))
            for name, slots in _ARTIFACT_SLOTS_RAW.items()}


def resolve_slots(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Map user-typed tokens (slot ids OR label substrings, PM-friendly) to slot ids.
    Returns (resolved ids in schema order, unmatched tokens).

    A token that matches nothing is *reported*, not dropped — that is what the second element is for,
    and the caller prints it. A token that is **empty** is refused outright, before any matching runs,
    because the substring arm makes it match everything: `"" in label` is true for every label, so
    `requivo impact <model> ""` (an unset shell variable, usually — the positional is named
    `session` since #248) or a caller splitting a
    comma-separated value on a trailing comma resolved to the *entire* schema with an empty unmatched
    list. An impact report claiming the whole model changed, carrying no complaint about its input,
    reads as a precise answer to a specific question rather than as a failure. The refusal is
    `normalize_tokens`, shared with the context-card selectors so the rule is stated once.
    """
    _, labels = slot_meta()
    # Materialised before the helper iterates it: a generator handed in here would be exhausted by
    # `normalize_tokens` and the `zip` below would then pair nothing, returning ([], []) — no slots
    # and no complaint, which is the same silent absence this function is being fixed for.
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
            # `raw.strip()`, not `raw` — echo the token the guard actually checked (#40 review).
            # `normalize_tokens` inspects the *stripped* token for control characters, and
            # `str.strip()` removes the ones Python classifies as whitespace, a newline among them.
            # So a token whose newline is leading or trailing passes the guard, and echoing the
            # unstripped original here put that newline into the line `cli.py` prints — the same
            # forged-receipt defect #40 is about, one selector over. The two card selectors already
            # echo `raw.strip()` for the sibling reason (a caller should see what they typed, not
            # the key it was matched by); this one had drifted.
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
class ImpactReport:
    changed: list[str]  # labels of the slots in question
    decisions: list[DecisionImpact] = field(default_factory=list)
    challenges: list[ChallengeImpact] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # artifact names whose slot set is touched

    @property
    def reasoning_hit(self) -> bool:
        """True if the change unseats a piece of baked-in reasoning (a decision or a challenge) — the
        signal that the saved assessment, which renders that reasoning, no longer holds."""
        return bool(self.decisions or self.challenges)

    @property
    def empty(self) -> bool:
        return not self.decisions and not self.challenges and not self.artifacts

    def to_dict(self) -> dict:
        """The wire shape for the API's `/impact` route (#425) -- the first `--json`-style payload
        this report has ever needed, since `requivo impact` has always been terminal-only. Not a
        second vocabulary: the field names are this dataclass's own, unrenamed."""
        return {"changed": self.changed,
                "decisions": [d.to_dict() for d in self.decisions],
                "challenges": [c.to_dict() for c in self.challenges],
                "artifacts": self.artifacts}


def propagate(out: EngineOutput, changed: list[str]) -> ImpactReport:
    """Given slot ids that changed (or are being probed), report what rests on them: the design
    decisions to re-validate, the challenges whose premise is now in question, and the artifacts that
    go stale. Decisions rest on slots via `derived_from`; challenges contest slots via `contests` —
    the same DAG edge, the other direction of reasoning."""
    changed_set = set(changed)
    report = ImpactReport(changed=[slot_label(sid) for sid in changed])

    for d in out.decisions:
        hit = [sid for sid in d.derived_from if sid in changed_set]
        if hit:
            report.decisions.append(DecisionImpact(d.decision, [slot_label(sid) for sid in hit]))

    for c in out.challenges:
        hit = [sid for sid in c.contests if sid in changed_set]
        if hit:
            report.challenges.append(ChallengeImpact(c.headline, [slot_label(sid) for sid in hit]))

    amap = artifact_slots()
    report.artifacts = [name for name in _ARTIFACT_SLOTS_RAW if amap[name] & changed_set]
    return report


@dataclass
class ReasoningDiff:
    """What moved in the reasoning layer between two model versions — ids, per collection."""
    decisions: list[str] = field(default_factory=list)
    challenges: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.decisions or self.challenges or self.opportunities)

    def to_dict(self) -> dict:
        return {"decisions": self.decisions, "challenges": self.challenges,
                "opportunities": self.opportunities}


def _diff_items(old_items: list, new_items: list) -> list[str]:
    """Ids that were added, removed, or edited between two reasoning collections.

    This is symmetric, including the populated → empty case, and that is only safe because both sides
    are *resolved* models. A refinement turn routinely replies without re-stating the reasoning it
    already established (the engine is answering a question, not re-deriving the brief) — but that
    omission is collapsed upstream, by `ModelProposal.resolve`, which carries the established
    reasoning forward. So by the time two models reach this function, an empty collection facing a
    populated one means the reasoning was genuinely dropped, and it should mark what rests on it
    stale. This function used to absorb that case itself, which made a real deletion indistinguishable
    from a turn that simply stayed quiet.
    """
    old_by_id = {i.id: i.model_dump_json() for i in old_items}
    new_by_id = {i.id: i.model_dump_json() for i in new_items}
    # Compare content, not just ids: `id` is derived from a *subset* of each item's fields (a
    # decision's text, a challenge's headline + premise), so an edit to a rationale or a tradeoff
    # keeps the id and would otherwise be invisible.
    return sorted(k for k in old_by_id.keys() | new_by_id.keys()
                  if old_by_id.get(k) != new_by_id.get(k))


def diff_reasoning(old: EngineOutput, new: EngineOutput) -> ReasoningDiff:
    """The reasoning-layer counterpart of `diff_models`. Slots carry the facts; decisions, challenges
    and opportunities carry the judgment over them, and both reach the generators. A model whose
    slots are untouched but whose design decisions changed is a materially different model."""
    return ReasoningDiff(
        decisions=_diff_items(old.decisions, new.decisions),
        challenges=_diff_items(old.challenges, new.challenges),
        opportunities=_diff_items(old.opportunities, new.opportunities),
    )


def diff_models(old: EngineOutput, new: EngineOutput) -> list[str]:
    """Slot ids that materially changed between two model versions — the trigger for staleness.
    A slot changed if its value, confidence or impact moved (completeness alone is noise)."""
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
        ):
            changed.append(sid)
    return changed
