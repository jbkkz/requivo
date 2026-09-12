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

    A **naming, never a verdict**: there is no `mismatched` status and this does not invent one --
    relevance is a human judgment, not something a free deterministic path or a keyword heuristic can
    safely automate, so the primary screen states the fact (which cards, narrowed or not) and leaves
    the verdict to the reader (#492). Pinned by `test_no_surface_claims_a_relevance_verdict_it_cannot_reach`.

    `narrowed=False` is not "no cards": it is *every card in the install*, re-resolved on each turn
    via `available_cards()` (a read of the install, not a re-derivation of anything Core decides),
    which is why the names are read now rather than taken from the session.

    **It can fail, and the failure must not reach the page.** Enumerating the card directory can
    raise `ContextUnreadableError` -- an install-level fact with nothing to do with the session being
    rendered -- and uncaught it would render `sessions/unreadable.html`'s session-corruption copy
    over what is actually a permissions problem next door. `readable=False` is the third state
    (invariant 15) (#518). Pinned by
    `test_an_unreadable_card_directory_degrades_the_grounding_line_rather_than_the_verb`.
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


def evidence_view(report: Any) -> dict:
    """Decisions worth re-reading because the evidence under them thickened (#493), keyed by
    decision id so the traceability panel can tag each decision row in place.

    A relabelling of `SessionService.thinner_evidence`'s `EvidenceReport` and nothing more: the
    Core decided which decisions were derived while a topic they rest on was still assumed or
    empty and is confirmed now; this turns that into one caption per decision. The caption says
    *worth re-reading* and never *contradicted* -- whether the confirmed value disagrees with the
    decision is a judgment, and a judgment is the assessment's to make, not a view model's.

    `reviewed=False` is the third state: no report at all (the page could not review), distinct
    from a report that reviewed every decision and flagged none. `unchecked` carries the decisions
    the review could not decide about, with the Core's reason, so a missing tag never reads as a
    clean one."""
    if report is None:
        return {"reviewed": False, "reread": {}, "unchecked": {}}
    reread = {}
    for f in report.flagged:
        when = f"at revision {f.derived_at}" if f.derived_at is not None else "when it was recorded"
        reread[f.id] = (f"Worth re-reading — {', '.join(f.thickened)}: assumed or empty {when}, "
                        "confirmed since.")
    return {
        "reviewed": True,
        "reread": reread,
        "unchecked": {u.id: u.reason for u in report.could_not_tell},
    }


def _impact_headline(changed: list[str]) -> str:
    if not changed:
        return "Your answers were folded in — no part of the solution moved."
    if len(changed) == 1:
        return f"One part of the solution moved: {changed[0]}."
    return f"{len(changed)} parts of the solution moved."
