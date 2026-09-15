"""The single model-validation entry point — provider-agnostic.

Every model update, whoever produces it (the Anthropic provider, a Claude Code proposal, a future
Web client), passes through here before it is applied. Pydantic already enforces the *shape* and the
slot *vocabulary* (`EngineOutput._validate_slot_vocabulary`); this layer adds the *completeness*
boundary (the full required slot set) and, crucially, translates every failure into a structured
`RequivoError` with a stable `code` — so the CLI's `model validate`/`model apply` can emit a machine
envelope and Claude Code can act on the `code` instead of scraping a Pydantic message.

This is the same guarantee `run()`'s `validate` hook enforces inside the provider; extracting it here
means the deterministic CLI path (which never calls an LLM) applies the identical rule.
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
    """A completeness rule a proposal breaks — the message plus what a structured error needs."""
    message: str
    path: str
    details: dict = field(default_factory=dict)


def completeness_gap(out: ModelProposal, perimeter: str = DEFAULT_PERIMETER) -> Incompleteness | None:
    """The first completeness rule `out` breaks, or None if it is a complete model.

    One definition of "complete", shared by the two boundaries that enforce it: the provider's retry
    hook (which raises a plain `ValueError` so `_complete()` can nudge the model into self-correcting)
    and `validate_proposal` (which raises a structured `RequivoError` so a CLI or Claude Code caller
    can act on the `code`). They used to state the rules separately, and drifted: the provider required
    an objective, the CLI did not, so the same model was complete on one surface and not the other.

    Both rules exist because their absence fails *silently*. A missing required slot becomes invisible
    to readiness and to every view, so a high-impact gap can pass as 'ready'; an empty objective leaves
    a set of slots with nothing naming what they are about, and renders as a blank heading everywhere.
    """
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
    """Refuse `text` over `limit` characters before it reaches a provider call or is persisted.

    Invariant 3 (refuse, don't truncate): a request or an answer silently cut mid-paste is reasoned
    over as if it were the whole thing, and the caller never learns which half the engine saw — so
    this raises rather than slicing anything. Invariant 14 (the service layer is the integrity
    boundary, not the interfaces): before #255 this cap existed only in `web/config.py`, checked by
    the Web routes alone, so `DiscoveryService`/`SessionService` called directly -- the CLI, Claude
    Code, a future consumer -- accepted unbounded text straight through to a billed provider call.

    `field` names what was too long (`"request"`, `"answers"`) so the message and the structured
    `details` tell a caller which one to shorten, rather than making them guess from a bare count.
    """
    if len(text) > limit:
        raise InputTooLargeError(
            f"{field} exceeds {limit:,} characters", details={"limit": limit, "field": field})


def validate_proposal(data: dict | str, *, require_complete: bool = True,
                      current: EngineOutput | None = None,
                      perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Validate a proposed model (a dict or a JSON string) into an `EngineOutput`, raising a
    structured `RequivoError` on any failure.

    `require_complete` gates the completeness boundary: True (the default, matching discovery) rejects
    a model missing any required slot or an empty objective; False allows a partial projection (used
    where a caller knowingly works with a subset). The vocabulary check (unknown slots) always runs —
    a hallucinated key is never acceptable.

    `current` is the model being refined, and it is what makes the reasoning layer's tri-state real
    (see `ModelProposal`): a proposal that does not mention `decisions` leaves the established ones
    standing, rather than deleting them by omission. Pass it wherever there is one — the callers that
    apply to a session always have it."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidModelError(f"proposal is not valid JSON: {e}", path="model") from e
    if not isinstance(data, dict):
        raise InvalidModelError("proposal must be a JSON object", path="model")

    # Surface a precise slot-vocabulary error *before* Pydantic, so the caller gets `unknown_slot`
    # with the offending ids rather than a generic validation dump.
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
        # Resolve against what is already there *before* the completeness check, so the rules judge the
        # model that would actually be stored, not the delta that was sent.
        out = proposal.resolve(current, perimeter=perimeter)
    except ValidationError as e:
        raise InvalidModelError(f"proposal does not match the schema: {e}", path="model") from e

    if require_complete:
        gap = completeness_gap(out, perimeter)
        if gap is not None:
            error = MissingRequiredSlotError if gap.details.get("slots") else InvalidModelError
            raise error(gap.message, path=gap.path, details=gap.details)
    return out
