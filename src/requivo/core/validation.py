"""The single model-validation entry point, provider-agnostic: Pydantic enforces shape and vocabulary,
this layer adds the completeness boundary and translates every failure into a structured
`RequivoError` with a stable `code`, the same rule the provider's `validate` hook enforces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from requivo.core.contracts import MAX_INPUT_CHARS, EngineOutput, ModelProposal, missing_required_slots, unknown_slots
from requivo.core.errors import InputTooLargeError, InvalidModelError, MissingRequiredSlotError, UnknownSlotError
from requivo.core.perimeters import DEFAULT_PERIMETER


@dataclass(frozen=True)
class Incompleteness:
    """A completeness rule a proposal breaks: the message plus what a structured error needs."""
    message: str
    path: str
    details: dict = field(default_factory=dict)


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


def require_input_within_bounds(text: str, *, field: str, limit: int = MAX_INPUT_CHARS) -> None:
    """Refuse `text` over `limit` characters before a provider call or a persist (invariant 3, never
    truncate; invariant 14, the service is the boundary, #255). `field` names what to shorten."""
    if len(text) > limit:
        raise InputTooLargeError(
            f"{field} exceeds {limit:,} characters", details={"limit": limit, "field": field})


def validate_proposal(data: dict | str, *, require_complete: bool = True,
                      current: EngineOutput | None = None,
                      perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Validate a proposed model (dict or JSON string) into an `EngineOutput`, raising a structured
    `RequivoError`. `require_complete` gates the completeness boundary; the vocabulary check always
    runs. `current` is the model being refined, which makes the reasoning tri-state real (`ModelProposal`)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidModelError(f"proposal is not valid JSON: {e}", path="model") from e
    if not isinstance(data, dict):
        raise InvalidModelError("proposal must be a JSON object", path="model")

    # A precise `unknown_slot` before Pydantic's generic dump.
    model_slots = data.get("model")
    if isinstance(model_slots, dict):
        bad = unknown_slots(set(model_slots), perimeter)
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
