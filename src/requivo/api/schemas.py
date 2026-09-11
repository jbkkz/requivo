"""Request body models for the API's write routes (#425, slice 2).

Small, and deliberately thin: per `docs/decisions/0004-the-http-api-facade.md` §4, "request bodies
are declared as real models (they are small); response models start as documented dicts" -- the
services' own dataclasses already own `to_dict()`/`model_dump()`, and hand-mirroring them into a
second pydantic layer is the drift surface invariant 8's mirror rule names. So only the *inbound*
shapes get a model here; every response in `api/routes/` is the service's own return value, selected
and serialized, never re-declared.

Every field a route hands a service is passed through unvalidated beyond its JSON type -- the
service is the integrity boundary (invariant 14), not this layer. `context_cards`, `proposal` and
`slug` are exactly as untrusted here as a value read back off disk; the service resolves and refuses
them, this module only shapes the JSON.
"""

from __future__ import annotations

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    """`POST /sessions` -- `SessionService.create_session_report`'s own three arguments, still
    unvalidated: an unknown context card, an oversized request or slug are the service's refusals to
    make, not this model's."""

    request: str
    context_cards: list[str] | None = None
    slug: str | None = None


class AnswersRequest(BaseModel):
    """`POST /sessions/{slug}/answers`. `expected_revision` is **required** here -- stricter than
    `DiscoveryService.answer`'s own optional default, deliberately: an API is a concurrent surface by
    definition, and the Web's own form already carries it (§1 of the decision record)."""

    answers: str
    expected_revision: int


class ApplyRevisionRequest(BaseModel):
    """`POST /sessions/{slug}/revisions` -- the apply. `proposal` is passed to
    `SessionService.update_model` exactly as received; a shape it does not like is the service's own
    `invalid_model` family of refusals, not a 422 minted here."""

    proposal: dict
    expected_revision: int | None = None


class PreviewRevisionRequest(BaseModel):
    """`POST /sessions/{slug}/revisions/preview` -- the dry run. `SessionService.diff` takes no
    `expected_revision` (nothing is written, so there is no precondition to hold), which is why this
    model carries one field where `ApplyRevisionRequest` carries two."""

    proposal: dict


class ArtifactSaveRequest(BaseModel):
    """`PUT /sessions/{slug}/artifacts/{type}` -- the external-reasoner save.
    `source_revision` keeps `ArtifactService.save`'s own optional default (`None`) rather than being
    required here: omitting it is the service's own `unstated_source_revision` refusal (400),
    unchanged, and duplicating that requiredness at this layer would be a second, weaker copy of it."""

    content: str
    source_revision: int | None = None


class ContextCardsRequest(BaseModel):
    """`PUT /sessions/{slug}/context-cards` -- the rescope. `None`/absent means "every card", exactly
    as `SessionService.rescope`'s own parameter does."""

    context_cards: list[str] | None = None
