"""The single model-validation entry point, provider-agnostic: Pydantic enforces shape and vocabulary,
this layer adds the completeness boundary and translates every failure into a structured
`RequivoError` with a stable `code`, the same rule the provider's `validate` hook enforces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, cast, get_args, get_origin

from pydantic import BaseModel, ValidationError

from requivo.core.contracts import (
    MAX_INPUT_CHARS,
    Challenge,
    ClaimSource,
    Confidence,
    Confirmation,
    DesignDecision,
    EngineOutput,
    Exclusion,
    ModelProposal,
    Opportunity,
    Summary,
    Threshold,
    missing_required_slots,
    schema_slot_ids,
    schema_slots,
    unknown_slots,
)
from requivo.core.errors import (
    InputTooLargeError,
    InvalidModelError,
    MissingRequiredSlotError,
    RequivoError,
    UnknownSlotError,
)
from requivo.core.perimeters import DEFAULT_PERIMETER


@dataclass(frozen=True)
class Incompleteness:
    """A completeness rule a proposal breaks: the message plus what a structured error needs."""
    message: str
    path: str
    details: dict[str, Any] = field(default_factory=dict[str, Any])


def completeness_gap(out: ModelProposal, perimeter: str = DEFAULT_PERIMETER) -> Incompleteness | None:
    """The first completeness rule `out` breaks, or None: one definition, shared by the provider's
    retry hook and `validate_proposal`, which used to state it separately and drifted. A missing
    required slot is invisible to readiness; an empty objective renders as a blank heading."""
    missing = missing_required_slots(set(out.model), perimeter)
    if missing:
        return Incompleteness(
            f"model is missing required slots: {missing}. Emit every schema slot.",
            path=f"model.{missing[0]}", details={"slots": missing})
    if not out.summary.objective.strip():
        return Incompleteness(
            "summary.objective is empty — state in one line what this is meant to achieve.",
            path="summary.objective")
    return None


def unearned_confirmation(out: ModelProposal | EngineOutput) -> Incompleteness | None:
    """A turn no answer has reached can settle no claim (#751): what the request states is `source: requester`,
    still `open`. Judged on the provider's first call, the one place that knows no answer exists — revision 0
    cannot tell (`test_a_first_model_cannot_carry_a_claim_nobody_answered`)."""
    for sid, s in out.model.items():
        for c in s.claims:
            if c.confirmation in (Confirmation.let_stand, Confirmation.confirmed):
                return Incompleteness(
                    f"claim {c.text!r} on {sid} is {c.confirmation.value}, but nothing has been asked yet: "
                    "on a first model every claim is `open` or `to_test` — what the request states is "
                    "`source: requester`, not a confirmation.",
                    path=f"model.{sid}.claims", details={"slot": sid, "claim": c.id})
    return None


def require_input_within_bounds(text: str, *, field: str, limit: int = MAX_INPUT_CHARS) -> None:
    """Refuse `text` over `limit` characters before a provider call or a persist (invariant 3, never
    truncate; invariant 14, the service is the boundary, #255). `field` names what to shorten."""
    if len(text) > limit:
        raise InputTooLargeError(
            f"{field} exceeds {limit:,} characters", details={"limit": limit, "field": field})


def validate_proposal(data: dict[str, Any] | str, *, require_complete: bool = True,
                      current: EngineOutput | None = None,
                      perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Validate a proposed model (dict or JSON string) into an `EngineOutput`, raising a structured
    `RequivoError`. `require_complete` gates the completeness boundary; the vocabulary check always
    runs. `current` is the model being refined, which makes the reasoning tri-state real (`ModelProposal`).
    No claim rule here: revision 0 can land after answered turns (`test_a_first_apply_may_carry_a_claim_answered_in_the_conversation`)."""
    parsed: object = data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidModelError(f"proposal is not valid JSON: {e}", path="model") from e
    if not isinstance(parsed, dict):
        raise InvalidModelError("proposal must be a JSON object", path="model")
    data = cast("dict[str, Any]", parsed)

    # A precise `unknown_slot` before Pydantic's generic dump.
    model_slots: object = data.get("model")
    if isinstance(model_slots, dict):
        bad = unknown_slots(set(cast("dict[str, object]", model_slots)), perimeter)
        if bad:
            raise UnknownSlotError(
                f"model names slots the schema does not define: {bad}",
                path="model",
                details={"slots": bad},
            )

    try:
        proposal = ModelProposal.model_validate(data, context={"perimeter": perimeter})
        # Resolved before the completeness check, so the rules judge the model that would be stored.
        out = proposal.resolve(current, perimeter=perimeter)
    except ValidationError as e:
        raise InvalidModelError(f"proposal does not match the schema: {e}", path="model") from e

    if require_complete:
        gap = completeness_gap(out, perimeter)
        if gap is not None:
            error = MissingRequiredSlotError if gap.details.get("slots") else InvalidModelError
            raise error(gap.message, path=gap.path, details=gap.details)
    return out


# What `schema --proposal` fills per grade, keyed by the enum so a new grade cannot go unshown (#770).
_SLOT_EXAMPLES: dict[Confidence, dict[str, Any]] = {
    Confidence.explicit: {
        "completeness": 90, "impact": "high", "value": "<what the requester stated>", "evidence": "request: <their words>",
        "claims": [{"text": "<one statement they can confirm on its own>", "source": ClaimSource.requester.value,
                    "confirmation": Confirmation.open.value}]},
    Confidence.inferred: {"completeness": 60, "impact": "medium", "value": "<your assumption>",
                          "evidence": "proposed: <one-line rationale>"},
    Confidence.empty: {"completeness": 0, "impact": "high", "value": "", "evidence": ""},
    Confidence.testable: {"completeness": 30, "impact": "high", "value": "<the bet>", "evidence": "request: <their words>",
                          "test_plan": "<the test or experiment that would settle it>"},
}
_SUMMARY_EXAMPLE = {"objective": "<one line: what this is for>", "scope": "<what is in, and what is out>",
                    "assumptions": ["<an assumption the requester should review>"],
                    "blind_spot": "<what nobody has looked at yet>"}
_REASONING_KINDS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("decisions", DesignDecision), ("challenges", Challenge), ("opportunities", Opportunity),
    ("exclusions", Exclusion), ("thresholds", Threshold))
_EDGES = frozenset({"derived_from", "contests", "rests_on"})


def _item_example(kind: type[BaseModel], slot: str) -> dict[str, Any]:
    """One reasoning item from its contract's own fields: an edge names `slot`, an enum its first value."""
    item: dict[str, Any] = {}
    for name, info in kind.model_fields.items():
        if name == "id":   # derived from the item's text (invariant 5), so never part of what is sent
            continue
        enums = [a for a in (get_args(info.annotation) or (info.annotation,)) if isinstance(a, type) and issubclass(a, Enum)]
        if name in _EDGES:
            item[name] = [slot]
        elif enums:
            item[name] = next(iter(enums[0])).value
        else:
            item[name] = [f"<{name}>"] if get_origin(info.annotation) is list else f"<{name}>"
    return item


def _refusal(data: dict[str, Any], **kwargs: Any) -> Optional[dict[str, Any]]:
    try:
        validate_proposal(data, **kwargs)
    except RequivoError as e:
        return e.to_dict()
    return None


def proposal_shape(perimeter: str = DEFAULT_PERIMETER) -> dict[str, Any]:
    """The proposal `model apply` reads, built from the contracts (#770): a slot per confidence, the whole
    summary and one item per reasoning kind, accepted by `ModelProposal` before it is returned; the schema's
    required and optional slots; and the envelopes two broken copies of it really raise here."""
    ids = [s["id"] for s in schema_slots(perimeter)]
    _, required = schema_slot_ids(perimeter)
    graded = dict(zip(ids, Confidence))
    model = {sid: {"confidence": grade.value, **_SLOT_EXAMPLES[grade]} for sid, grade in graded.items()}
    empty = next(sid for sid, grade in graded.items() if grade is Confidence.empty)
    proposal: dict[str, Any] = {
        "model": model,
        "questions": [{"q": "<one decision, in the requester's words>", "slot": empty, "why": "<what the answer changes>"}],
        "summary": {name: _SUMMARY_EXAMPLE[name] for name in Summary.model_fields},
        **{key: [_item_example(kind, ids[0])] for key, kind in _REASONING_KINDS},
    }
    ModelProposal.model_validate(proposal, context={"perimeter": perimeter})   # a drifted example fails here
    nulled = {**proposal, "model": {**model, empty: {**model[empty], "value": None}}}
    refusals = (_refusal(nulled, require_complete=False, perimeter=perimeter), _refusal(proposal, perimeter=perimeter))
    return {"perimeter": perimeter, "proposal": proposal, "required": [s for s in ids if s in required],
            "optional": [s for s in ids if s not in required], "refusals": [r for r in refusals if r]}
