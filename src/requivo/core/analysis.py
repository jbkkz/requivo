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


@functools.lru_cache(maxsize=1)
def slot_meta() -> tuple[dict, dict]:
    """`(pillars, labels)`, both keyed by slot id, projected from the framework schema and cached.

    Public since #302: four modules outside this one already imported it under its underscore name,
    so the privacy marker was false everywhere it mattered. Reads `schema_slots()` rather than parsing
    the file itself (#301) — this and `_default_impacts` were two of four independent parses of the
    same `model_schema.json`; `test_the_four_slot_projections_all_read_from_one_schema_parse` is the
    guard.
    """
    slots = schema_slots()
    return ({s["id"]: s["pillar"] for s in slots}, {s["id"]: s["label"] for s in slots})


@functools.lru_cache(maxsize=1)
def _default_impacts() -> dict[str, Impact]:
    """Each slot's baseline impact from the schema — used to judge a slot the model omitted entirely
    (where there's no live impact to read)."""
    return {s["id"]: Impact(s["impact_default"]) for s in schema_slots()}


def slot_label(slot_id: str) -> str:
    """The human label for one slot id. Public since #302, for the same reason `slot_meta` is: four
    modules outside this one already called it under its underscore name."""
    return slot_meta()[1].get(slot_id, slot_id)


def slot_labels(slot_ids: list[str]) -> list[str]:
    """Human labels for slot ids, in the order given — the list-form convenience over `slot_label`.

    Every surface that reports a change reports it in slot ids (`UpdateResult.changed_slots`), and
    every surface that shows it to a reader has to translate. Doing that translation here rather than
    in each interface is what keeps a slot id out of the Web's prose: the schema's `label` is the one
    the engine's own Voice rule already writes in."""
    return [slot_label(sid) for sid in slot_ids]


def soft_slots(out: EngineOutput) -> list[str]:
    """Slots that still carry real uncertainty AND move the solution — the objective drivers of
    the estimate spread. Soft = medium/high impact and (low completeness or not yet explicit)."""
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


def readiness_blockers(out: EngineOutput) -> list[str]:
    """High-impact slots not yet confirmed AND covered — what stands between here and build.

    Public since #302, for the same reason `slot_meta` is: `render/terminal.py` and
    `render/markdown.py` already called this under its underscore name.

    Iterates the schema's required slots, not just the ones the model returned: a required slot the
    model omitted is treated as unknown at its baseline impact, so a missing high-impact dimension
    reads as a blocker instead of vanishing. This is the readiness guarantee — 'ready' can never be
    reached with a high-impact gap, whether the gap is empty-but-present or absent entirely.

    Confirmation is two-dimensional: a slot must be both `explicit` (provenance) *and* covered
    (completeness at/above the soft boundary). An `explicit` slot at completeness 5 is a stated-but-
    thin answer, not a resolved dimension — it still blocks. This keeps provenance and coverage from
    collapsing into one signal, so a high-impact topic can't read as 'confirmed' on a one-word reply."""
    _, required = schema_slot_ids()
    blockers = []
    for sid in required:
        s = out.model.get(sid)
        impact = s.impact if s is not None else _default_impacts().get(sid, Impact.low)
        confirmed = (
            s is not None
            and s.confidence is Confidence.explicit
            and s.completeness >= SOFT_COMPLETENESS
        )
        if impact is Impact.high and not confirmed:
            blockers.append(sid)
    return [sid for sid in slot_meta()[1] if sid in set(blockers)]  # schema order


def state_of(s: Slot) -> str:
    """`confirmed` / `inferred` / `unknown` for one slot's confidence. Public since #302:
    `render/terminal.py` already called this under its underscore name, and the alternative --
    inlining the confidence-to-state mapping there -- would duplicate a core classification rule in
    a render module rather than share it."""
    if s.confidence is Confidence.explicit:
        return "confirmed"
    if s.confidence is Confidence.inferred:
        return "inferred"
    return "unknown"


def model_status(out: EngineOutput) -> dict:
    """The model-derived half of a status snapshot — readiness, understanding, priority questions,
    summary, and remaining gaps — as one computed projection. Both `status --json` (a raw model or a
    session) and `SessionService.status` (a session) build on this, so the presentation logic lives in
    exactly one place; the session-only fields (revision, artifacts, context cards) are layered on by
    each caller. Everything here needs only the model, so it works for a bare model.json too."""
    blockers = readiness_blockers(out)
    gaps = [{"slot": s, "label": slot_label(s)} for s in blockers]
    return {
        "readiness": {"ready": not blockers, "blocking_slots": gaps},
        "understanding": understanding_view(out),
        "questions": [{"q": q.q, "slot": q.slot, "label": slot_label(q.slot), "why": q.why}
                      for q in out.questions],
        "summary": out.summary.model_dump(),
        "remaining_gaps": gaps,
    }


def understanding_view(out: EngineOutput) -> dict[str, list[dict]]:
    """The per-slot understanding grouped by state (confirmed / inferred / unknown), each entry carrying
    its pillar, label, completeness and impact. This is the machine form of the `render_turn` checklist:
    the JSON status and the Web read the same computed view rather than rebuilding the
    presentation logic. `thin` marks a confirmed-but-below-coverage slot — the exact case readiness now
    still blocks on, surfaced so a client can render 'stated but partial' without re-deriving it."""
    pillars, _labels = slot_meta()
    groups: dict[str, list[dict]] = {"confirmed": [], "inferred": [], "unknown": []}
    for sid, s in out.model.items():
        groups[state_of(s)].append({
            "slot": sid,
            "label": slot_label(sid),
            "pillar": pillars.get(sid),
            "completeness": s.completeness,
            "impact": s.impact.value,
            "thin": s.confidence is Confidence.explicit and s.completeness < SOFT_COMPLETENESS,
        })
    return groups
