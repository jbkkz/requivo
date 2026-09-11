"""Status view models — `SessionService.status()` and `UpdateResult`, reshaped for the screen.

These are projections, never computations. Readiness, coverage, the blast radius of a change and the
freshness of a document are all decided in the Core; if one of them were re-derived here the Web would
be a second engine, and the two would disagree the first time either changed. What this module adds is
translation (slot ids → the labels a reader sees, see `labels.py`) and *selection* — which few of the
many true things a screen shows first.
"""

from __future__ import annotations

from typing import Any

from requivo.core.analysis import slot_labels
from requivo.web.viewmodels.labels import artifact_labels

# The three understanding states the Core emits (evidence, NOT coverage — coverage is the separate
# `thin` flag each entry carries), each with its display tag and dot colour class. The tags read as
# the vocabulary the page uses in prose: what we know / what we are assuming / open question.
UNDERSTANDING_STATES = [
    ("confirmed", "KNOWN", "fact"),
    ("inferred", "ASSUMED", "assum"),
    ("unknown", "OPEN", "unkwn"),
]

# How many questions the default view shows. The engine already caps its reply at 6, so this is not a
# truncation of the reasoning — it is the difference between a list to work through and a list to
# read. The rest stay one disclosure away, and the count is always stated.
PRIORITY_QUESTIONS = 5


def readiness_view(status: dict) -> dict:
    """Readiness as one action state plus the reasons behind it.

    The Core's readiness is binary — a high-impact topic is either confirmed and covered or it blocks
    — so there are exactly two headlines here and no invented middle ground ('nearly ready'). The
    coverage count feeds the segmented bar, which now lives inside the traceability disclosure: a bar
    with no explanation is a score, and a score a reader cannot act on is noise on the primary screen.
    """
    rd = status.get("readiness", {})
    blocking = rd.get("blocking_slots", [])
    groups = status.get("understanding", {})
    total = sum(len(v) for v in groups.values())
    resolved = len(groups.get("confirmed", []))
    ready = rd.get("ready", False)
    return {
        "ready": ready,
        "blocking": blocking,
        "headline": "Ready for a first decision brief" if ready
        else "Not ready to produce a reliable scope",
        "lead": ("The main workflow, integrations, roles and blocking business rules are sufficiently "
                 "covered." if ready else "Still unresolved:"),
        "blocking_labels": [b["label"] for b in blocking],
        "resolved": resolved,
        "total": total,
    }


def understood_view(status: dict) -> dict:
    """'What Requivo understood' — the human read of the request, before any per-topic detail.

    Drawn from the engine's own `summary`, which until now the Web used only for its objective line.
    `scope`, `assumptions` and `blind_spot` were being produced on every turn and thrown away — they
    are the paragraph a reader needs to decide whether the engine understood them at all."""
    summary = status.get("summary", {}) or {}
    return {
        "objective": summary.get("objective", ""),
        "scope": summary.get("scope", ""),
        "assumptions": summary.get("assumptions", []) or [],
        "blind_spot": summary.get("blind_spot", ""),
        "has_content": any((summary.get("objective"), summary.get("scope"),
                            summary.get("assumptions"), summary.get("blind_spot"))),
    }


def grounding_view(status: dict) -> dict:
    """What this session's impact estimates were scored against — the product context, named.

    A **naming, never a verdict** (#492). Cards can be present, readable and about a different
    product entirely, and that state renders identically to `ok`; since `information_value =
    uncertainty x impact` is the whole driver and the cards are what the impact half is read
    against, a session grounded on the wrong product reaches *ready* with a question selection
    nothing on screen accounts for. There is no `mismatched` status and this does not invent one:
    relevance is a judgment, every other `context.status` value is decidable from the filesystem,
    and the two ways to automate the judgment are a paid call in the free deterministic path or a
    keyword heuristic that is wrong silently. The human is the detector; this is the fact they
    judge.

    It is on the **primary** screen rather than under *Traceability details*, unlike the history
    line that also names the cards. The rule this repo applies is that the primary screen shows what
    a reader must act on — and the whole of #492 is that this is exactly such a fact, since nothing
    else on the page can tell them the grounding was wrong.

    `narrowed=False` is not "no cards": it is *every card in the install*, re-resolved on each turn,
    which is why the names are read now rather than taken from the session. An explicit selection
    was fixed at creation and is true of every turn this session has taken; an unnarrowed one is
    only a claim about the install as it stands today.

    `available_cards()` is a read of the install, not a re-derivation of anything the Core decides,
    so it stays inside this module's stated remit: translation and selection.

    **It can fail, and the failure must not reach the page.** Enumerating the card directory raises
    `ContextUnreadableError` when permissions refuse it — an install-level fact with nothing to do
    with the session being rendered. Uncaught, it would reach `session_page`'s broad `except
    Exception` and render `sessions/unreadable.html`, whose copy says *Requivo found this session on
    disk and could not open it* and whose remedies are `session verify` and recovering from
    `revisions/`: every word of that is wrong here, and a reader could not tell a corrupt session
    from a permissions problem next door. `readable=False` is the third state (invariant 15's rule,
    one row up: a row that could not be read renders differently from one with nothing in it), and
    #518's review is where this was found.
    """
    from requivo.core.context import available_cards
    from requivo.core.errors import ContextUnreadableError
    cards = status.get("context_cards")
    if cards:
        return {"narrowed": True, "readable": True, "cards": list(cards)}
    try:
        return {"narrowed": False, "readable": True, "cards": available_cards()}
    except ContextUnreadableError:
        return {"narrowed": False, "readable": False, "cards": []}


def understanding_view(status: dict) -> list[dict]:
    """The per-topic understanding as a flat, dot-coded row list (known, then assumed, then open) —
    the model view, now shown under traceability rather than on the primary screen. Each row carries
    its tag, dot colour, topic label, pillar, and the `thin` coverage flag."""
    groups = status.get("understanding", {})
    rows = []
    for key, tag, dot in UNDERSTANDING_STATES:
        for e in groups.get(key, []):
            rows.append({"tag": tag, "dot": dot, "name": e["label"],
                         "pillar": e["pillar"], "thin": e.get("thin", False)})
    return rows


def impact_view(result: Any) -> dict:
    """'What changed' — an `UpdateResult` read as a scope statement rather than a diff.

    Everything here is already decided by the Core: which topics materially moved (`diff_models`),
    which established reasoning the change unseats (`propagate` over the *prior* model), and which
    saved documents fall in the blast radius (`ARTIFACT_SLOTS` + `REASONING_CONSUMERS`). This view
    translates and groups it; it never asks a second time, and it must never ask the provider — a
    generated list of documents needing an update would be a plausible guess where a computed one is
    an answer.

    `invalidated_*` rather than `changed_*` is deliberate: the changed collections are content-derived
    ids, which mean nothing on screen, while the invalidated ones carry the decision text and the
    challenge headline — the thing a reader has to go and re-examine."""
    changed = slot_labels(result.changed_slots)
    decisions = list(result.invalidated_decisions)
    assumptions = list(result.invalidated_challenges)
    documents = artifact_labels(result.stale_artifacts)
    return {
        "revision": result.revision,
        "headline": _impact_headline(changed),
        "changed_areas": changed,
        "decisions_to_review": decisions,
        "assumptions_to_review": assumptions,
        "documents_to_update": documents,
        "needs_review": bool(decisions or assumptions or documents),
        "ready": result.readiness.ready,
    }


def _impact_headline(changed: list[str]) -> str:
    if not changed:
        return "Your answers were folded in — no part of the solution moved."
    if len(changed) == 1:
        return f"One part of the solution moved: {changed[0]}."
    return f"{len(changed)} parts of the solution moved."
