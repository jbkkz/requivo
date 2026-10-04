from __future__ import annotations

import functools
from typing import Any

from requivo.core.contracts import (
    IMPACT_RANK,
    SOFT_COMPLETENESS,
    Confidence,
    EngineOutput,
    Impact,
    Question,
    Slot,
    Stories,
    schema_slot_ids,
    schema_slots,
)
from requivo.core.perimeters import DEFAULT_PERIMETER


@functools.cache
def slot_meta(perimeter: str = DEFAULT_PERIMETER) -> tuple[dict[str, str], dict[str, str]]:
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
    soft: list[str] = []
    for slot_id, s in out.model.items():
        if s.impact in (Impact.medium, Impact.high) and (
            s.completeness < SOFT_COMPLETENESS or s.confidence is not Confidence.explicit
        ):
            soft.append(slot_id)
    return soft


def veto_defaults(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """The `inferred` slots worth a veto at a checkpoint (#731): `soft_slots`' rule narrowed to the
    inferred ones carrying a value, in schema order. A selection, never a second ranking."""
    soft = set(soft_slots(out))
    return [sid for sid in slot_meta(perimeter)[1] if sid in soft
            and out.model[sid].confidence is Confidence.inferred and out.model[sid].value.strip()]


def estimate_confidence(n_soft: int, n_uncovered: int = 0) -> str:
    """Estimate confidence from how many slots are still soft, never `high` while a slot is
    uncovered (`uncovered_slots`, #782): an estimate missing an area is not a confident one."""
    if n_soft <= 1:
        return "medium" if n_uncovered else "high"
    if n_soft <= 3:
        return "medium"
    return "low"


def uncovered_slots(out: EngineOutput, stories: Stories, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """High-impact What/How slots carrying a value that no story traces to through `Story.slots`,
    in schema order (#782): scope the model states and the estimate does not cost. Why/Validate
    slots frame a build rather than slice into it; an empty slot is already soft."""
    pillars, _ = slot_meta(perimeter)
    traced = {sid for st in stories.stories for sid in st.slots}
    return [sid for sid, pillar in pillars.items() if pillar in ("what", "how") and sid not in traced
            and sid in out.model and out.model[sid].impact is Impact.high and out.model[sid].value.strip()]


def readiness_blockers(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """High-impact slots not yet confirmed AND covered: what stands between here and build. Iterates
    the schema's required slots, so an omitted one blocks at its baseline impact; confirmation is
    `explicit` *and* completeness at the soft boundary, so a one-word reply cannot read as confirmed."""
    _, required = schema_slot_ids(perimeter)
    blockers: list[str] = []
    for sid in required:
        s = out.model.get(sid)
        impact = s.impact if s is not None else _default_impacts(perimeter).get(sid, Impact.low)
        confirmed = s is not None and s.confidence is Confidence.explicit and not is_thin(s)
        # A slot deferred to a named test (#610) is a known unknown and does not block readiness.
        deferred_to_test = s is not None and s.confidence is Confidence.testable
        if impact is Impact.high and not confirmed and not deferred_to_test:
            blockers.append(sid)
    return [sid for sid in slot_meta(perimeter)[1] if sid in set(blockers)]  # schema order


def is_thin(s: Slot) -> bool:
    """Confirmed (`explicit`) yet below the soft boundary: blocks readiness, and what it needs is
    more said, not a confirmation (#722)."""
    return s.confidence is Confidence.explicit and s.completeness < SOFT_COMPLETENESS


def blocking_reason(s: Slot | None) -> str:
    """Why a readiness blocker blocks (#722): `unknown` (empty or absent), `thin`, or `unconfirmed`.
    A naming of what `readiness_blockers` decided, never a second readiness rule."""
    if s is None or s.confidence is Confidence.empty:
        return "unknown"
    return "thin" if is_thin(s) else "unconfirmed"


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


def claims_below_slot_impact(out: EngineOutput) -> int:
    """Claims rated below their slot's impact (#751): counted and named in `status`, never capped,
    so a downgrade that would carry a slot stays visible (`decision: claims-carry-provenance`)."""
    return sum(1 for s in out.model.values() for c in s.claims
               if c.impact is not None and IMPACT_RANK[c.impact] < IMPACT_RANK[s.impact])


def model_status(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> dict[str, Any]:
    """The model-derived half of a status snapshot (readiness, understanding, questions, summary,
    gaps), the one projection `status --json` and `SessionService.status` both build on."""
    blockers = readiness_blockers(out, perimeter)
    # `reason` (#722) says why each one blocks: added to a nested object, so free (docs/compatibility.md).
    gaps = [{"slot": s, "label": slot_label(s, perimeter), "reason": blocking_reason(out.model.get(s))}
            for s in blockers]
    return {
        "readiness": {"ready": not blockers, "blocking_slots": gaps},
        "understanding": understanding_view(out, perimeter),
        # Ranked, each with its `information_value` (#771): nested and per-question, so additive.
        "questions": [{"q": q.q, "slot": q.slot, "label": slot_label(q.slot, perimeter), "why": q.why,
                       "information_value": information_value(out, q.slot, perimeter)}
                      for q in ranked_questions(out, perimeter)],
        "pillars": pillar_completeness(out, perimeter),
        "summary": out.summary.model_dump(),
        "remaining_gaps": gaps,
        "claims_below_slot_impact": claims_below_slot_impact(out),
    }


def understanding_view(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> dict[str, list[dict[str, Any]]]:
    """The per-slot understanding grouped by state, each entry with pillar, label, completeness,
    impact, the `thin` flag (confirmed but below coverage) and its informational `claims` (#751)."""
    pillars, _labels = slot_meta(perimeter)
    groups: dict[str, list[dict[str, Any]]] = {"confirmed": [], "inferred": [], "to_test": [], "unknown": []}
    for sid, s in out.model.items():
        groups[state_of(s)].append({
            "slot": sid,
            "label": slot_label(sid, perimeter),
            "pillar": pillars.get(sid),
            "completeness": s.completeness,
            "impact": s.impact.value,
            "thin": is_thin(s),
            "claims": [c.model_dump(mode="json") for c in s.claims],
        })
    return groups


# ── the driver, shown (#771) ────────────────────────────────────────────────────
# information_value = uncertainty × impact, both in [0, 1]: uncertainty = 1 − completeness/100 × the
# confidence's weight below, impact = (IMPACT_RANK + 1) / 3. An omitted slot is wholly uncertain at its
# schema baseline impact.
_KNOWN_WEIGHT = {Confidence.explicit: 1.0, Confidence.inferred: 0.5, Confidence.testable: 0.5, Confidence.empty: 0.0}


def information_value(out: EngineOutput, slot_id: str, perimeter: str = DEFAULT_PERIMETER) -> float:
    """One slot's `uncertainty × impact`, rounded to two places: the number a question is ranked by."""
    s = out.model.get(slot_id)
    if s is None:
        return round((IMPACT_RANK[_default_impacts(perimeter).get(slot_id, Impact.low)] + 1) / 3, 2)
    uncertainty = 1 - s.completeness / 100 * _KNOWN_WEIGHT.get(s.confidence, 0.0)
    return round(uncertainty * (IMPACT_RANK[s.impact] + 1) / 3, 2)


def ranked_questions(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[Question]:
    """The turn's questions by descending information value, the order every surface asks and shows
    them in (#771). `sorted` is stable, so a tie keeps the provider's order."""
    return sorted(out.questions, key=lambda q: -information_value(out, q.slot, perimeter))


def pillar_completeness(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> dict[str, int]:
    """Mean completeness per pillar, in schema order (#771), over the pillar's required slots (an
    omitted one counts 0) and any optional slot the model carries."""
    pillars, _labels = slot_meta(perimeter)
    _, required = schema_slot_ids(perimeter)
    seen: dict[str, list[int]] = {}
    for sid, pillar in pillars.items():
        s = out.model.get(sid)
        if s is not None or sid in required:
            seen.setdefault(pillar, []).append(s.completeness if s is not None else 0)
    return {pillar: round(sum(values) / len(values)) for pillar, values in seen.items()}
