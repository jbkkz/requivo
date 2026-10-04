"""Where a session stands on return (#785): what is decided, what is open, what moved since the
documents were written, which of them are stale and on which topics. Computed from the model, the
revisions the documents were written from and their recorded `stale` flags, never from a provider,
and pure: the service hands in what it read under one lock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from requivo.core.analysis import (
    blocking_reason,
    ranked_questions,
    readiness_blockers,
    slot_label,
    slot_meta,
    veto_defaults,
)
from requivo.core.contracts import Confidence, DecisionSource, EngineOutput
from requivo.core.dependencies import ARTIFACT_FILENAMES, diff_models, diff_reasoning, propagate
from requivo.core.perimeters import DEFAULT_PERIMETER
from requivo.core.persistence.models import ArtifactStatus


@dataclass
class DocumentState:
    """One generated document: its recorded `stale` flag (invariant 1) and, when stale, the topics
    moved since the revision it was written from. `because` is `None` when that revision is unreadable."""
    type: str
    filename: str
    stale: bool
    because: Optional[list[str]] = None
    reasoning_moved: bool = False


@dataclass
class Recap:
    objective: str
    ready: bool
    decided: list[dict[str, str]] = field(default_factory=list[dict[str, str]])   # {topic, value}
    decisions: list[str] = field(default_factory=list[str])
    questions: list[str] = field(default_factory=list[str])                      # ranked (#771)
    assumed: list[dict[str, str]] = field(default_factory=list[dict[str, str]])  # the checkpoint defaults
    proposed: list[str] = field(default_factory=list[str])   # decisions proposed for the client to own
    to_test: list[dict[str, str]] = field(default_factory=list[dict[str, str]])   # {topic, test_plan}
    blocking: list[dict[str, str]] = field(default_factory=list[dict[str, str]])  # {topic, reason}, not assumed
    # What the change summary compares against: the oldest *current* document (a regeneration moves it),
    # `{type, revision}`, and that revision again; None: no document.
    since: Optional[dict[str, Any]] = None
    since_revision: Optional[int] = None
    changed: Optional[list[dict[str, str]]] = None   # {topic, value}; None: no document, or unreadable
    reasoning_changed: Optional[list[dict[str, str]]] = None   # {item, change}; None as `changed`
    documents: list[DocumentState] = field(default_factory=list[DocumentState])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _moved(then: EngineOutput, now: EngineOutput, perimeter: str) -> list[str]:
    """The slots `diff_models` reports, in schema order."""
    moved = set(diff_models(then, now))
    return [sid for sid in slot_meta(perimeter)[1] if sid in moved]


# The text each reasoning collection is read by, for a moved item named in plain words.
_REASONING_TEXT = {"decisions": "decision", "challenges": "headline", "opportunities": "text",
                   "exclusions": "option", "thresholds": "condition"}


def _reasoning_moves(then: EngineOutput, now: EngineOutput) -> list[dict[str, str]]:
    """Each reasoning item `diff_reasoning` reports, as `{item, change}`: added, edited or removed."""
    diff = diff_reasoning(then, now).to_dict()
    moves: list[dict[str, str]] = []
    for collection, attr in _REASONING_TEXT.items():
        before: dict[str, Any] = {i.id: i for i in getattr(then, collection)}
        after: dict[str, Any] = {i.id: i for i in getattr(now, collection)}
        for rid in diff[collection]:
            change = "added" if rid not in before else "removed" if rid not in after else "edited"
            moves.append({"item": str(getattr(after.get(rid) or before[rid], attr)), "change": change})
    return moves


def build_recap(now: EngineOutput, artifacts: Mapping[str, ArtifactStatus],
                revisions: Mapping[int, EngineOutput], perimeter: str = DEFAULT_PERIMETER) -> Recap:
    """The recap over `now`, the recorded `artifacts` and the frozen `revisions` they were written
    from (a missing one was unreadable). Staleness is each document flag; `because` names the
    topics `propagate` reaches that document through, never a revision-number comparison."""
    slots = [(sid, now.model[sid]) for sid in slot_meta(perimeter)[1] if sid in now.model]
    recap = Recap(
        objective=now.summary.objective,
        ready=not readiness_blockers(now, perimeter),
        decided=[{"topic": slot_label(sid, perimeter), "value": s.value} for sid, s in slots
                 if s.confidence is Confidence.explicit and s.value.strip()],
        decisions=[d.decision for d in now.decisions if d.source is not DecisionSource.proposed],
        questions=[q.q for q in ranked_questions(now, perimeter)],
        # Open is never only the questions: a turn may propose none while bets, defaults and blockers stand.
        assumed=[{"topic": slot_label(sid, perimeter), "value": now.model[sid].value}
                 for sid in veto_defaults(now, perimeter)],
        proposed=[d.decision for d in now.decisions if d.source is DecisionSource.proposed],
        to_test=[{"topic": slot_label(sid, perimeter), "test_plan": s.test_plan} for sid, s in slots
                 if s.confidence is Confidence.testable],
    )
    assumed = set(veto_defaults(now, perimeter))
    recap.blocking = [{"topic": slot_label(sid, perimeter), "reason": blocking_reason(now.model.get(sid))}
                      for sid in readiness_blockers(now, perimeter) if sid not in assumed]
    # `min` keeps the first of a tie, so the display order (ARTIFACT_FILENAMES) names it.
    oldest = min((t for t in ARTIFACT_FILENAMES if t in artifacts), key=lambda t: artifacts[t].revision, default=None)
    if oldest is not None:
        recap.since = {"type": oldest, "revision": artifacts[oldest].revision}
        recap.since_revision = artifacts[oldest].revision
        base = revisions.get(recap.since_revision)
        if base is not None:
            recap.changed = [{"topic": slot_label(sid, perimeter),
                              "value": now.model[sid].value if sid in now.model else ""}
                             for sid in _moved(base, now, perimeter)]
            recap.reasoning_changed = _reasoning_moves(base, now)
    # The documents in their one display order (ARTIFACT_FILENAMES), never the key order on disk.
    for doc_type in [t for t in ARTIFACT_FILENAMES if t in artifacts]:
        st = artifacts[doc_type]
        state = DocumentState(type=doc_type, filename=st.filename, stale=st.stale)
        then = revisions.get(st.revision) if st.stale else None
        if then is not None:
            state.because = [slot_label(sid, perimeter) for sid in _moved(then, now, perimeter)
                             if doc_type in propagate(now, [sid], perimeter).artifacts]
            state.reasoning_moved = diff_reasoning(then, now).changed
        recap.documents.append(state)
    return recap
