"""Discovery write routes -- run the first turn on a captured request, and fold in answers (#425,
slice 2). Both go through `DiscoveryService`, the same validated apply path (reason -> apply ->
save) every other surface uses; no handler composes core calls or re-validates.

Both are paid, so both are scoped in a `track_api_usage()` ledger -- logged on the failure path too,
see `api/usage.py` -- and carry the `usage` object on the response; `usage_view` returns `None`
rather than a manufactured zero for a call the provider reported no figures for.
"""

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
    """Run the first discovery turn on a 'create session only' session (`DiscoveryService.run_discovery`).

    409 `revision_conflict` above revision 0 -- the gate is taken before payment (invariant 13); 503
    `session_locked` when a concurrent first discovery already holds the non-blocking guard."""
    with track_api_usage("api-discover") as ledger:
        result = discovery.run_discovery(slug, surface="api-discover")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}


@router.post("/sessions/{slug}/answers")
def submit_answers(body: AnswersRequest, slug: str = Depends(safe_slug),
                   discovery: DiscoveryService = Depends(get_discovery)) -> dict:
    """Fold the caller's answers into the model as a new revision -- 'answers-as-turns': a turn is not
    a stored object, it becomes a revision (`DiscoveryService.answer`).

    `expected_revision` is **required** by `AnswersRequest`, stricter than the service's own optional
    default -- an API is a concurrent surface by definition (§1 of the decision record). A stale
    precondition is refused here, before the provider is paid (#205,
    `DiscoveryService._require_no_conflict_yet`) -- 409 `revision_conflict`."""
    with track_api_usage("api-answer") as ledger:
        result = discovery.answer(slug, body.answers, expected_revision=body.expected_revision,
                                  surface="api-answer")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}
