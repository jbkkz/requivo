"""Where a session stands on return (#785): what is decided, what is open, what moved since the
documents were written, which of them are stale and on which topics. Computed from the model, the
revisions the documents were written from and their recorded `stale` flags, never from a provider,
and pure: the service hands in what it read under one lock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from requivo.core.analysis import ranked_questions, readiness_blockers, slot_label, slot_meta
from requivo.core.contracts import Confidence, DecisionSource, EngineOutput, Impact
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
    assumed: list[dict[str, str]] = field(default_factory=list[dict[str, str]])  # inferred, high impact
    proposed: list[str] = field(default_factory=list[str])   # decisions proposed for the client to own
    since_revision: Optional[int] = None     # the oldest document source revision; None: no document
    changed: Optional[list[dict[str, str]]] = None   # {topic, value}; None: no document, or unreadable
    documents: list[DocumentState] = field(default_factory=list[DocumentState])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _moved(then: EngineOutput, now: EngineOutput, perimeter: str) -> list[str]:
    """The slots `diff_models` reports, in schema order."""
    moved = set(diff_models(then, now))
    return [sid for sid in slot_meta(perimeter)[1] if sid in moved]


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
        assumed=[{"topic": slot_label(sid, perimeter), "value": s.value} for sid, s in slots
                 if s.confidence is Confidence.inferred and s.impact is Impact.high and s.value.strip()],
        proposed=[d.decision for d in now.decisions if d.source is DecisionSource.proposed],
    )
    if artifacts:
        recap.since_revision = min(st.revision for st in artifacts.values())
        base = revisions.get(recap.since_revision)
        if base is not None:
            recap.changed = [{"topic": slot_label(sid, perimeter),
                              "value": now.model[sid].value if sid in now.model else ""}
                             for sid in _moved(base, now, perimeter)]
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
