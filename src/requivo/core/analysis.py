from __future__ import annotations

import functools

from requivo.core.contracts import (
    SOFT_COMPLETENESS,
    Confidence,
    EngineOutput,
    Impact,
    Slot,
    schema_slot_ids,
    schema_slots,
)
from requivo.core.perimeters import DEFAULT_PERIMETER


@functools.cache
def slot_meta(perimeter: str = DEFAULT_PERIMETER) -> tuple[dict, dict]:
    """`(pillars, labels)` keyed by slot id, projected from `perimeter`'s schema and cached (#608),
    reading `schema_slots()` (#301: `test_the_four_slot_projections_all_read_from_one_schema_parse`)."""
    slots = schema_slots(perimeter)
    return ({s["id"]: s["pillar"] for s in slots}, {s["id"]: s["label"] for s in slots})


@functools.cache
def _default_impacts(perimeter: str = DEFAULT_PERIMETER) -> dict[str, Impact]:
    """Each slot's baseline impact from the schema, for a slot the model omitted entirely."""
    return {s["id"]: Impact(s["impact_default"]) for s in schema_slots(perimeter)}


def slot_label(slot_id: str, perimeter: str = DEFAULT_PERIMETER) -> str:
    """The human label for one slot id."""
    return slot_meta(perimeter)[1].get(slot_id, slot_id)


def slot_labels(slot_ids: list[str], perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Human labels for slot ids, in the order given: the one translation that keeps a slot id out of a reader's prose."""
    return [slot_label(sid, perimeter) for sid in slot_ids]


def soft_slots(out: EngineOutput) -> list[str]:
    """Slots that still carry real uncertainty AND move the solution: medium/high impact and (low completeness or not explicit)."""
    soft = []
    for slot_id, s in out.model.items():
        if s.impact in (Impact.medium, Impact.high) and (
            s.completeness < SOFT_COMPLETENESS or s.confidence is not Confidence.explicit
        ):
            soft.append(slot_id)
    return soft


def estimate_confidence(n_soft: int) -> str:
    """Estimate confidence derived from how many high-impact slots are still soft."""
    if n_soft <= 1:
        return "high"
    if n_soft <= 3:
        return "medium"
    return "low"


def readiness_blockers(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """High-impact slots not yet confirmed AND covered: what stands between here and build. Iterates
    the schema's required slots, so an omitted one blocks at its baseline impact; confirmation is
    `explicit` *and* completeness at the soft boundary, so a one-word reply cannot read as confirmed."""
    _, required = schema_slot_ids(perimeter)
    blockers = []
    for sid in required:
        s = out.model.get(sid)
        impact = s.impact if s is not None else _default_impacts(perimeter).get(sid, Impact.low)
        confirmed = (
            s is not None
            and s.confidence is Confidence.explicit
            and s.completeness >= SOFT_COMPLETENESS
        )
        # A slot deferred to a named test (#610) is a known unknown and does not block readiness.
        deferred_to_test = s is not None and s.confidence is Confidence.testable
        if impact is Impact.high and not confirmed and not deferred_to_test:
            blockers.append(sid)
    return [sid for sid in slot_meta(perimeter)[1] if sid in set(blockers)]  # schema order


def state_of(s: Slot) -> str:
    """`confirmed` / `inferred` / `to_test` / `unknown` for one slot's confidence; `to_test` is its own
    bucket, never folded into `unknown` (#610)."""
    if s.confidence is Confidence.explicit:
        return "confirmed"
    if s.confidence is Confidence.inferred:
        return "inferred"
    if s.confidence is Confidence.testable:
        return "to_test"
    return "unknown"


def model_status(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> dict:
    """The model-derived half of a status snapshot (readiness, understanding, questions, summary,
    gaps), the one projection `status --json` and `SessionService.status` both build on."""
    blockers = readiness_blockers(out, perimeter)
    gaps = [{"slot": s, "label": slot_label(s, perimeter)} for s in blockers]
    return {
        "readiness": {"ready": not blockers, "blocking_slots": gaps},
        "understanding": understanding_view(out, perimeter),
        "questions": [{"q": q.q, "slot": q.slot, "label": slot_label(q.slot, perimeter), "why": q.why}
                      for q in out.questions],
        "summary": out.summary.model_dump(),
        "remaining_gaps": gaps,
    }


def understanding_view(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> dict[str, list[dict]]:
    """The per-slot understanding grouped by state, each entry with pillar, label, completeness,
    impact and the `thin` flag (confirmed but below coverage)."""
    pillars, _labels = slot_meta(perimeter)
    groups: dict[str, list[dict]] = {"confirmed": [], "inferred": [], "to_test": [], "unknown": []}
    for sid, s in out.model.items():
        groups[state_of(s)].append({
            "slot": sid,
            "label": slot_label(sid, perimeter),
            "pillar": pillars.get(sid),
            "completeness": s.completeness,
            "impact": s.impact.value,
            "thin": s.confidence is Confidence.explicit and s.completeness < SOFT_COMPLETENESS,
        })
    return groups
