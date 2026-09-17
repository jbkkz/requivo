"""Discovery write routes (#425), through `DiscoveryService`; both paid, scoped in `track_api_usage()`."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from requivo.api.dependencies import get_discovery, safe_slug
from requivo.api.schemas import AnswersRequest
from requivo.api.usage import track_api_usage, usage_view
from requivo.services.discovery import DiscoveryService

router = APIRouter()


@router.post("/sessions/{slug}/discover")
def run_discovery(slug: str = Depends(safe_slug),
                  discovery: DiscoveryService = Depends(get_discovery)) -> dict:
    """Run the first discovery turn on a 'create session only' session: 409 above revision 0, before
    payment (invariant 13); 503 `session_locked` under a concurrent first discovery."""
    with track_api_usage("api-discover") as ledger:
        result = discovery.run_discovery(slug, surface="api-discover")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}


@router.post("/sessions/{slug}/answers")
def submit_answers(body: AnswersRequest, slug: str = Depends(safe_slug),
                   discovery: DiscoveryService = Depends(get_discovery)) -> dict:
    """Fold the caller's answers into the model as a new revision. `expected_revision` is required
    (an API is concurrent), and a stale one is refused before the provider is paid (#205)."""
    with track_api_usage("api-answer") as ledger:
        result = discovery.answer(slug, body.answers, expected_revision=body.expected_revision,
                                  surface="api-answer")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}
