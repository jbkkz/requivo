"""The card-selection remedy: one finding, shared by `doctor` and `session verify` (#556), so the
two cannot print different advice for the same state."""

from __future__ import annotations

from requivo.core.context import check_selection
from requivo.services.sessions import SessionService

# Which card findings are repaired by restoring a file, and which by fixing the stored selection.
# `context_unreadable` is deliberately absent: `check_selection` lets it propagate, so it never
# arrives as a `problem["code"]`. `test_the_two_card_code_tables_agree`.
_RESTORABLE_CARD_CODES = frozenset({"unknown_context_card", "no_context_cards"})

_RESTORE_HINT = ("Put the card back, or point REQUIVO_CONTEXT_DIR at where it now lives — until "
                 "then these sessions refuse their next reasoning turn.")
_REPAIR_HINT = ("Repair the `context_cards` list in the session's session.json — the selection "
                "itself is malformed, so no card you install will resolve it.")


def _card_health(slug: str) -> dict:
    """Does this session's persisted card selection still load here? Three states: `{"checked":
    True, "problem": None}`, `{"checked": True, "problem": {…}}`, `{"checked": False, "error": …}`.
    An environment finding, not an integrity code: a card lives outside the directory, and
    `session import` must not refuse a colleague's archive for a card you lack."""
    try:
        # `SessionService.meta`, not `repo.context_cards`: the latter answers None (every card) for a
        # session it cannot find (#80, #86).
        problem = check_selection(SessionService().meta(slug).context_cards)
    except Exception as e:  # noqa: BLE001 - a health check reports that it could not look; it never raises
        return {"checked": False, "problem": None, "error": str(e)}
    return {"checked": True, "problem": problem.to_dict() if problem else None, "error": None}
