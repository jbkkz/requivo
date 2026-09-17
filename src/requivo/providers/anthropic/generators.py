"""Model → artifact, and request → model: every operation this provider can be asked for, and the
two registries (`_GENERATORS`, `_OP_PROMPTS`, one table each) every surface reaches them through.
Every function is one `_complete()` call; nothing here talks to the SDK.
"""

from __future__ import annotations

import hashlib

from requivo.core.analysis import estimate_confidence, soft_slots
from requivo.core.context import CardSummary, build_prompt, build_standalone_prompt, build_system_prompt
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    ContextJudgment,
    EngineOutput,
    Epic,
    EstimateDraft,
    GoToMarketPlan,
    ModelProposal,
    PerimeterJudgment,
    ReleaseNotes,
    Stories,
)
from requivo.core.perimeters import DEFAULT_PERIMETER, GO_TO_MARKET, PerimeterSummary
from requivo.core.validation import completeness_gap
from requivo.providers.anthropic.completion import _complete

# ── Discovery ─────────────────────────────────────────────────────────────────


def _require_complete_model(out: ModelProposal, perimeter: str = DEFAULT_PERIMETER) -> None:
    """A discovery reply owes the whole required slot set and a non-empty objective
    (`core.validation.completeness_gap`), raised as a `ValueError` so the retry loop nudges the model.
    `perimeter` (#608) must be the one the reply was reasoned against; `run()` closes over its own."""
    gap = completeness_gap(out, perimeter)
    if gap is not None:
        raise ValueError(gap.message)


def run(client, messages: list[dict], retries: int = 2, only: list[str] | None = None,
        carry_from: EngineOutput | None = None, *, reuse_system: bool = True,
        model: str | None = None, perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Engine turn: request/answers → filled model, parsed as a `ModelProposal` (a quiet reply is not
    a deletion) and resolved against `carry_from`. `only` restricts the cards; `perimeter` (#608)
    grounds the prompt and the validation context. `reuse_system` is the caller's: only the
    interactive loop re-sends this prompt (#9, #58, #77).
    `test_the_provider_seam_is_single_call_on_both_analyze_branches`."""
    proposal = _complete(
        client, build_system_prompt("engine.md", only, perimeter=perimeter), messages, ModelProposal,
        retries, validate=lambda o: _require_complete_model(o, perimeter), reuse_system=reuse_system,
        model=model, operation="analyze", context={"perimeter": perimeter})
    return proposal.resolve(carry_from, perimeter=perimeter)


def judge_context(client, request: str, cards: list[CardSummary], *,
                  model: str | None = None) -> ContextJudgment:
    """Does any installed card describe this request's domain? One cheap standalone call
    (`decision: the-engine-writes-the-missing-card`), not grounded in the schema and the cards, and
    `reuse_system=False`. The card names are validated against the install:
    `test_a_judgment_naming_a_card_the_install_does_not_have_is_refused`."""
    known = {c.stem for c in cards}

    def _names_only_installed_cards(judgment: ContextJudgment) -> None:
        unknown = sorted(set(judgment.cards) - known)
        if unknown:
            raise ValueError(
                f"cards {unknown} are not installed; name only the cards listed in the prompt, "
                f"spelled exactly, or use decision 'uncovered' if none of them describes this domain")

    listing = "\n".join(
        f"- {c.stem}: " + ("(this card could not be read)" if c.unreadable else c.domain or "(no domain stated)")
        for c in cards)
    system = build_standalone_prompt(
        "context_judgment.md", {"{{REQUEST}}": request, "{{CARDS}}": listing})
    return _complete(client, system, [{"role": "user", "content": "Judge this request's grounding."}],
                     ContextJudgment, validate=_names_only_installed_cards,
                     reuse_system=False, model=model, operation="judge_context")


def judge_perimeter(client, request: str, perimeters: list[PerimeterSummary], *,
                    model: str | None = None) -> PerimeterJudgment:
    """Which installed perimeter, if any, this request's shape belongs to (#601): `judge_context`'s
    seam, a standalone call before a session has claimed a perimeter. The ids are validated against
    the install rather than trusted."""
    known = {p.id for p in perimeters}

    def _names_only_installed_perimeters(judgment: PerimeterJudgment) -> None:
        named = ({judgment.perimeter} if judgment.perimeter else set()) | set(judgment.candidates)
        unknown = sorted(named - known)
        if unknown:
            raise ValueError(
                f"perimeter(s) {unknown} are not installed; name only the perimeters listed in the "
                f"prompt, spelled exactly, or use decision 'none' if none of them fits")

    listing = "\n".join(
        f"- {p.id}: " + ("(this perimeter's hint could not be read)" if p.unreadable else p.hint)
        for p in perimeters)
    system = build_standalone_prompt(
        "perimeter_judgment.md", {"{{REQUEST}}": request, "{{PERIMETERS}}": listing})
    return _complete(
        client, system, [{"role": "user", "content": "Judge which perimeter this request fits."}],
        PerimeterJudgment, validate=_names_only_installed_perimeters, reuse_system=False,
        model=model, operation="judge_perimeter")


def answer_turn(client, out: EngineOutput, request: str, answers: str,
                only: list[str] | None = None, *, reuse_system: bool = False,
                model: str | None = None, perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """One stateless discovery turn: the request, the current model and the new answers, so any
    interface drives discovery turn by turn. `only` keeps a refinement on the same cards.
    `reuse_system=False` by default: single-call; the interactive loop passes True (#9, #58, #77).
    `test_a_looping_caller_can_still_ask_for_the_breakpoint_back`."""
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": out.model_dump_json()},
        {"role": "user", "content": "Client answers:\n" + answers},
    ]
    return run(client, messages, only=only, carry_from=out, reuse_system=reuse_system, model=model,
              perimeter=perimeter)


# ── Generators (model → artifact) ───────────────────────────────────────────────
# Every generator threads `only`, the card selection its discovery ran against; None means all cards.


# Every generator is one `_complete` call, so `reuse_system=False` is the default (#9); it stays a
# parameter because `scripts/golden_run.py --brief` calls `advise()` K times off one prompt.


def derive_stories(client, out: EngineOutput, only: list[str] | None = None, *,
                   reuse_system: bool = False, model: str | None = None) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_system_prompt("stories.md", only)
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Stories,
                     reuse_system=reuse_system, model=model, operation="stories")


def advise(client, out: EngineOutput, only: list[str] | None = None, *,
           reuse_system: bool = False, model: str | None = None) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities.
    `brief.md` still says "solution assessment" (#166): renaming it is a golden-capture spend decision."""
    system = build_system_prompt("brief.md", only)
    user = "Completed requirements model to advise on:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Brief,
                     reuse_system=reuse_system, model=model, operation="brief")


def advise_gtm(client, out: EngineOutput, only: list[str] | None = None, *,
               reuse_system: bool = False, model: str | None = None) -> GoToMarketPlan:
    """The go-to-market perimeter's one artifact (#609): `advise()` over its own schema and prompt.
    `perimeter=GO_TO_MARKET` is hardcoded, since `_require_owned_artifact_type` only reaches this
    for such a session, and the prompt and the validation context must agree on it."""
    system = build_system_prompt("gtm_plan.md", only, perimeter=GO_TO_MARKET)
    user = "Completed go-to-market model to advise on:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], GoToMarketPlan,
                     reuse_system=reuse_system, model=model, operation="gtm_plan",
                     context={"perimeter": GO_TO_MARKET})


def generate_prd(client, out: EngineOutput, only: list[str] | None = None, *,
                 reuse_system: bool = False, model: str | None = None) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_system_prompt("prd.md", only)
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], PRD,
                     reuse_system=reuse_system, model=model, operation="prd")


def generate_criteria(client, out: EngineOutput, only: list[str] | None = None, *,
                      reuse_system: bool = False, model: str | None = None) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_system_prompt("criteria.md", only)
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria,
                     reuse_system=reuse_system, model=model, operation="criteria")


def generate_epic(client, out: EngineOutput, only: list[str] | None = None, *,
                  reuse_system: bool = False, model: str | None = None) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_system_prompt("epic.md", only)
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Epic,
                     reuse_system=reuse_system, model=model, operation="epic")


def generate_release(client, out: EngineOutput, version: str = "",
                     only: list[str] | None = None, *,
                     reuse_system: bool = False, model: str | None = None) -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_system_prompt("release.md", only)
    user = "Completed requirements model to turn into release notes:\n" + out.model_dump_json()
    notes = _complete(client, system, [{"role": "user", "content": user}], ReleaseNotes,
                      reuse_system=reuse_system, model=model, operation="release")
    if version:
        notes.version = version
    return notes


def estimate(client, out: EngineOutput, stories: Stories,
             only: list[str] | None = None, *,
             reuse_system: bool = False, model: str | None = None) -> tuple[EstimateDraft, list[str], str]:
    """Pipeline stage: stories + the model's soft slots → a day-based estimate.
    Returns (draft, soft_slots, confidence) — the latter two are Python-authoritative."""
    soft = soft_slots(out)
    system = build_system_prompt("estimate.md", only)
    user = (
        "User stories to estimate:\n"
        + stories.model_dump_json()
        + "\n\nUnresolved (soft) slots — widen the range for any story that depends on one:\n"
        + (", ".join(soft) if soft else "(none — the model is solid)")
    )
    draft = _complete(client, system, [{"role": "user", "content": user}], EstimateDraft,
                      reuse_system=reuse_system, model=model, operation="estimate")
    return draft, soft, estimate_confidence(len(soft))


# ── The registry ────────────────────────────────────────────────────────────────

# Every operation reachable through `ReasoningProvider.generate` (#77):
# `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns`. `estimate` takes the
# prior `stories` through `**kwargs` and returns `(EstimateDraft, soft_slots, confidence)`; since
# #519 `DiscoveryService.generate("estimate")` reasons and saves both.
_GENERATORS = {
    "brief": advise,
    "stories": derive_stories,
    "prd": generate_prd,
    "criteria": generate_criteria,
    "epic": generate_epic,
    "release": generate_release,
    "estimate": estimate,
    "gtm_plan": advise_gtm,
}

# The prompt file behind each operation, what `prompt_version()` hashes; `analyze` is the discovery turn.
_OP_PROMPTS = {
    "analyze": "engine.md", "brief": "brief.md", "stories": "stories.md", "estimate": "estimate.md",
    "prd": "prd.md", "criteria": "criteria.md", "epic": "epic.md", "release": "release.md",
    "gtm_plan": "gtm_plan.md",
}


# Prompts that are not operations: no shared leading block, no revision to stamp. A second table so
# `test_every_prompt_asset_belongs_to_an_operation` still accounts for every file (#593).
_STANDALONE_PROMPTS = {"judge_context": "context_judgment.md",
                       "judge_perimeter": "perimeter_judgment.md"}


def prompt_version(op: str, only: list[str] | None = None, *,
                   perimeter: str = DEFAULT_PERIMETER) -> str:
    """`"sha256:…"` over the exact system prompt an operation sends (prompt file, schema, selected
    cards, and the perimeter, #608), which is what makes a revision traceable."""
    return "sha256:" + hashlib.sha256(
        build_prompt(_OP_PROMPTS[op], only, perimeter=perimeter).encode("utf-8")).hexdigest()
