"""Status view models: `SessionService.status()` and `UpdateResult` reshaped for the screen.
Projections, never computations: translation (`labels.py`) and selection of what a screen shows first.
"""

from __future__ import annotations

from typing import Any

from requivo.core.analysis import slot_labels
from requivo.web.viewmodels.labels import artifact_labels

# The four understanding states the Core emits (evidence, not coverage), each with its tag and dot colour.
UNDERSTANDING_STATES = [
    ("confirmed", "KNOWN", "fact"),
    ("inferred", "ASSUMED", "assum"),
    ("to_test", "TO TEST", "test"),
    ("unknown", "OPEN", "unkwn"),
]

# How many questions the default view shows; the rest stay one disclosure away, the count stated.
PRIORITY_QUESTIONS = 5


def readiness_view(status: dict) -> dict:
    """Readiness as one action state plus the reasons: the Core's readiness is binary, so two
    headlines and no invented middle ground."""
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
    """'What Requivo understood': the engine's own `summary`, the paragraph a reader needs first."""
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
    """The product context this session's estimates were scored against: a naming, never a verdict
    (#492, `test_no_surface_claims_a_relevance_verdict_it_cannot_reach`). `narrowed=False` is every
    card in the install, re-resolved now; an unreadable card directory is the third state,
    `readable=False` (#518, `test_an_unreadable_card_directory_degrades_the_grounding_line_rather_than_the_verb`)."""
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
    """The per-topic understanding as a flat, dot-coded row list (known, assumed, open), shown under traceability."""
    groups = status.get("understanding", {})
    rows = []
    for key, tag, dot in UNDERSTANDING_STATES:
        for e in groups.get(key, []):
            rows.append({"tag": tag, "dot": dot, "name": e["label"],
                         "pillar": e["pillar"], "thin": e.get("thin", False)})
    return rows


def impact_view(result: Any) -> dict:
    """'What changed': an `UpdateResult` read as a scope statement. Everything is decided by the Core;
    this translates and groups, and never asks the provider. `invalidated_*` rather than `changed_*`,
    since the invalidated ones carry the text a reader re-examines."""
    changed = slot_labels(result.changed_slots)
    decisions = list(result.invalidated_decisions)
    assumptions = list(result.invalidated_challenges)
    exclusions = list(result.invalidated_exclusions)
    thresholds = list(result.invalidated_thresholds)
    documents = artifact_labels(result.stale_artifacts)
    return {
        "revision": result.revision,
        "headline": _impact_headline(changed),
        "changed_areas": changed,
        "decisions_to_review": decisions,
        "assumptions_to_review": assumptions,
        "exclusions_to_review": exclusions,
        "thresholds_to_review": thresholds,
        "documents_to_update": documents,
        "needs_review": bool(decisions or assumptions or exclusions or thresholds or documents),
        "ready": result.readiness.ready,
    }


def evidence_view(report: Any) -> dict:
    """Decisions worth re-reading because the evidence under them thickened (#493), keyed by
    decision id: a relabelling of `EvidenceReport`, saying *worth re-reading*, never *contradicted*.
    `reviewed=False` is no report at all; `unchecked` carries what the review could not decide."""
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
