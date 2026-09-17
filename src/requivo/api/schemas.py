"""Request body models for the API's write routes (#425): only the inbound shapes, every field passed
through to the service, which is the integrity boundary (invariant 14). Responses are the services'
own return values, never re-declared.
"""

from __future__ import annotations

# `Optional[...]`, not `X | None`: pydantic evaluates these at runtime, and 3.9 has no union operator.
from typing import Optional

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    """`POST /sessions`: `create_session_report`'s own arguments, still unvalidated."""

    request: str
    context_cards: Optional[list[str]] = None
    slug: Optional[str] = None


class AnswersRequest(BaseModel):
    """`POST /sessions/{slug}/answers`. `expected_revision` is required here: an API is a concurrent surface."""

    answers: str
    expected_revision: int


class ApplyRevisionRequest(BaseModel):
    """`POST /sessions/{slug}/revisions`, the apply; `proposal` is passed to `update_model` as received."""

    proposal: dict
    expected_revision: Optional[int] = None


class PreviewRevisionRequest(BaseModel):
    """`POST /sessions/{slug}/revisions/preview`, the dry run: no `expected_revision`, since nothing is written."""

    proposal: dict


class ArtifactSaveRequest(BaseModel):
    """`PUT /sessions/{slug}/artifacts/{type}`, the external-reasoner save; `source_revision` keeps
    the service's optional default, so the omission is its own `unstated_source_revision` refusal."""

    content: str
    source_revision: Optional[int] = None


class ContextCardsRequest(BaseModel):
    """`PUT /sessions/{slug}/context-cards`, the rescope; `None` means every card."""

    context_cards: Optional[list[str]] = None
