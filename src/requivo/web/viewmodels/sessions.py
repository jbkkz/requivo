"""Session view models: the home page's rows and the session screen, pure projections over
`SessionService` that decide what a screen shows first and re-derive nothing.
"""

from __future__ import annotations

from requivo.core.errors import SessionNotFoundError
from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter, resolve_perimeter
from requivo.services.discovery import GENERATABLE
from requivo.services.sessions import SessionService
from requivo.web.example import is_example
from requivo.web.viewmodels.labels import UNREADABLE_BADGE, artifact_label, unreadable_hint
from requivo.web.viewmodels.status import (
    PRIORITY_QUESTIONS,
    evidence_view,
    grounding_view,
    readiness_view,
    understanding_view,
    understood_view,
)

# How much of the request a home-page row shows; a session is recognised by what was asked.
TITLE_CHARS = 110


def _title(request_text: str, slug: str) -> str:
    """A row's human title: the opening of the request, or the slug when there is no request."""
    text = " ".join(request_text.split())
    if not text:
        return slug
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS].rstrip() + "…"


def generatable_view(perimeter: str = DEFAULT_PERIMETER) -> list[dict]:
    """Every document the service can produce for a session running `perimeter`, from its own
    vocabulary (#609: the global set offered every session every type)."""
    owned = get_perimeter(perimeter).artifact_types
    return [{"type": t, "label": artifact_label(t)} for t in GENERATABLE if t in owned]


def _artifacts_view(status: dict) -> list[dict]:
    arts = status.get("artifacts", {})
    return [
        {"type": t, "label": artifact_label(t), "revision": a["revision"],
         "filename": a["filename"], "stale": a["stale"]}
        for t, a in sorted(arts.items())
    ]


def _unreadable_row(slug: str, error: str | None) -> dict:
    """A row for a session nobody could read: the third state, stating no fact it does not have
    (`updated_at` empty, `open_questions` None, `is_example` False). `error` is the failure's own
    text; `hint` is the one human line the home page shows (#240).
    `test_a_degraded_row_shows_one_human_line_and_no_engine_internals`,
    `test_humanising_the_row_did_not_flatten_the_third_state`."""
    return {"slug": slug, "title": slug, "updated_at": "", "state": "unreadable",
            "status_label": UNREADABLE_BADGE, "open_questions": None, "needs_update": False,
            "error": error or "no further detail", "hint": unreadable_hint(error),
            "is_example": False}


def _readable_row(sessions: SessionService, meta) -> dict:
    """The ordinary row; raises whatever its reads raise, since `session_list` owns the degradation."""
    # One read of the request, two uses: two reads would be two instants.
    request_text = sessions.request_text(meta.slug)
    row = {
        "slug": meta.slug,
        "title": _title(request_text, meta.slug),
        "updated_at": meta.updated_at,
        "error": None,
        # Present on every row, so a template never asks which shape it was handed (#240).
        "hint": None,
        "is_example": is_example(request_text),
    }
    try:
        status = sessions.status(meta.slug)
    except SessionNotFoundError:
        # Not a failure: 'capture now, analyse later' has no model yet. Kept narrow, or an un-analysed
        # session and an unreadable one render identically.
        return {**row, "state": "awaiting", "status_label": "Awaiting analysis",
                "open_questions": 0, "needs_update": False}
    arts = status.get("artifacts", {})
    open_questions = len(status.get("questions", []))
    ready = status["readiness"]["ready"]
    return {
        **row,
        "state": "ready" if ready else "in_progress",
        "status_label": "Ready for a first decision brief" if ready
        else (f"{open_questions} open question{'' if open_questions == 1 else 's'}"
              if open_questions else "In progress"),
        "open_questions": open_questions,
        "needs_update": any(a["stale"] for a in arts.values()),
    }


def _most_recent_first(items: list[dict]) -> list[dict]:
    """Order the rows the way the heading claims: the session that moved last, first (#237;
    ordering is presentation, so the service's slug order is untouched:
    `test_the_cli_listing_order_is_not_what_changed`). Two stable sorts, so equal instants keep the
    slug tie-break; an unreadable row's empty `updated_at` is pinned last by `bool(...)`:
    `test_a_row_nobody_could_read_sorts_last_rather_than_first`."""
    items.sort(key=lambda r: r["slug"])
    items.sort(key=lambda r: (bool(r["updated_at"]), r["updated_at"]), reverse=True)
    return items


def session_list(sessions: SessionService) -> list[dict]:
    """One row per local session for the home page: what was asked, whether it waits on the reader,
    whether its brief drifted. The listing survives its own members (invariant 15, #7): the source
    is `list_entries()` and everything read on a row is inside one bare `except Exception`.
    `test_the_home_page_renders_every_row_when_three_are_broken`."""
    items = []
    for entry in sessions.list_entries():
        if not entry.readable:
            items.append(_unreadable_row(entry.slug, entry.error))
            continue
        try:
            items.append(_readable_row(sessions, entry.meta))
        except Exception as e:  # noqa: BLE001 - one member must not take the listing down
            items.append(_unreadable_row(entry.slug, str(e)))
    return _most_recent_first(items)


def session_detail(sessions: SessionService, slug: str) -> dict:
    """The session screen in reading order: the request, the understanding, the few questions that
    could change the solution, readiness, the brief. Everything else sits behind the traceability
    disclosure, counts always stated."""
    status = sessions.status(slug)
    model = sessions.load_model(slug)
    # Decisions derived while a topic under them was thinner (#493), computed by the service.
    evidence = evidence_view(sessions.thinner_evidence(slug))
    questions = status.get("questions", [])
    artifacts = _artifacts_view(status)
    perimeter = resolve_perimeter(status.get("perimeter"))
    generatable = generatable_view(perimeter)
    # The perimeter's own primary artifact, never the software constant (#609).
    primary_type = get_perimeter(perimeter).primary_artifact
    request_text = sessions.request_text(slug)
    return {
        "slug": slug,
        "revision": status.get("revision"),
        "request_text": request_text,
        # Decided from the request, never from the slug (#226, `web/example.py`).
        "is_example": is_example(request_text),
        "understood": understood_view(status),
        "readiness": readiness_view(status),
        # The few that lead the page; the rest are one disclosure away.
        "questions": questions[:PRIORITY_QUESTIONS],
        "more_questions": questions[PRIORITY_QUESTIONS:],
        "understanding": understanding_view(status),
        "context_cards": status.get("context_cards"),
        # What the readiness and questions were scored against (#492), on the primary screen.
        "grounding": grounding_view(status),
        # Only the splits are handed to a template (#300): a dead key on the hottest view model is an invitation.
        "primary_artifact": next((a for a in artifacts if a["type"] == primary_type), None),
        "other_artifacts": [a for a in artifacts if a["type"] != primary_type],
        "primary_generatable": next((g for g in generatable if g["type"] == primary_type), None),
        "more_generatable": [g for g in generatable if g["type"] != primary_type],
        # `mode="json"` so enums arrive as their value, not their repr.
        "decisions": [{**d.model_dump(mode="json"),
                       "reread": evidence["reread"].get(d.id),
                       "unchecked": evidence["unchecked"].get(d.id)}
                      for d in model.decisions],
        "evidence_reviewed": evidence["reviewed"],
        "challenges": [c.model_dump(mode="json") for c in model.challenges],
        "opportunities": [o.model_dump(mode="json") for o in model.opportunities],
        "exclusions": [e.model_dump(mode="json") for e in model.exclusions],
    }
