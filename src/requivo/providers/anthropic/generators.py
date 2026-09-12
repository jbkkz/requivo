"""Model → artifact, and request → model: every operation this provider can be asked for.

The discovery turn (`run`/`answer_turn`) and the seven generators, plus the two tables that make
them reachable through the seam — `_GENERATORS` and `_OP_PROMPTS` — and `prompt_version`, which
hashes the exact system prompt an operation sends.

`_GENERATORS` and `_OP_PROMPTS` stay **one table each**, which #74 asks for by name: they are the
registry every surface reaches through, and a registry split across two modules is a registry with
two answers. Adding a generator is still a prompt asset + a contract + a function here + a writer in
`render/markdown.py` + a subcommand in `cli.py`.

Every function here is one `_complete()` call in `completion.py`; nothing in this module talks to
the SDK directly.
"""

from __future__ import annotations

import hashlib

from requivo.core.analysis import estimate_confidence, soft_slots
from requivo.core.context import build_prompt
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    EngineOutput,
    Epic,
    EstimateDraft,
    ModelProposal,
    ReleaseNotes,
    Stories,
)
from requivo.core.validation import completeness_gap
from requivo.providers.anthropic.completion import _complete

# ── Discovery ─────────────────────────────────────────────────────────────────


def _require_complete_model(out: ModelProposal) -> None:
    """A discovery turn must return the whole required slot set, and must say what the thing is for.

    The rules themselves live in `core.validation.completeness_gap`, shared with the deterministic
    apply path so the two boundaries cannot drift. What is local here is the *shape* of the failure:
    a plain `ValueError`, which `_complete()`'s retry loop feeds back to the model as a corrective
    nudge, so it self-corrects instead of the turn dying. Neither rule is in the contract itself,
    because a partial model is a legitimate internal object (a diff basis, a projection) — it is only
    a *discovery reply* that owes completeness.
    """
    gap = completeness_gap(out)
    if gap is not None:
        raise ValueError(gap.message)


def run(client, messages: list[dict], retries: int = 2, only: list[str] | None = None,
        carry_from: EngineOutput | None = None, *, reuse_system: bool = True,
        model: str | None = None) -> EngineOutput:
    """Engine turn: request/answers → filled model. `only` restricts which context cards inform the
    turn (defaults to all); keep it constant across a session's turns so the prompt cache holds.

    The reply is parsed as a `ModelProposal`, not an `EngineOutput`, because `engine.md` asks for
    `model`/`questions`/`summary` and nothing else: a turn that says nothing about decisions or
    challenges is *quiet*, not deleting them. `carry_from` is the model being refined — the established
    reasoning is carried onto the reply, so what leaves this function is a complete model again.

    `reuse_system` is the one thing this function cannot decide, so it is the caller's: only the
    interactive `discover` loop (`DiscoveryService.draft_turn`) genuinely re-sends this prompt and
    earns the breakpoint, while `start`/`run_discovery`/`answer` take the one-shot default (#58, #77).
    Pinned by `test_the_provider_seam_is_single_call_on_both_analyze_branches`. The remaining direct
    callers of this function are `answer_turn` and `scripts/golden_run.py`; no interface reaches it."""
    proposal = _complete(client, build_prompt("engine.md", only), messages, ModelProposal, retries,
                         validate=_require_complete_model, reuse_system=reuse_system, model=model,
                         operation="analyze")
    return proposal.resolve(carry_from)


def answer_turn(client, out: EngineOutput, request: str, answers: str,
                only: list[str] | None = None, *, reuse_system: bool = False,
                model: str | None = None) -> EngineOutput:
    """One stateless discovery turn: refine the model with new answers.

    The model IS the accumulated state, so a turn needs only the original request (for context),
    the current model, and the new answers — no live conversation loop. This is what lets any
    interface (Claude Code, an API, an MCP) drive discovery turn by turn instead of a blocking TTY.

    `only` is the context-card selection the original discovery used (from its session.json) — passing
    it keeps a refinement turn reasoning over the same cards, not silently the full set.

    **Single-call by default, hence `reuse_system=False`.** This function *is* the whole turn: it
    assembles a fresh message list, makes one call and returns, so on its own there is no loop for a
    cached system block to be read back by -- most callers make one call per operation (`requivo
    answer`, `POST /sessions/{slug}/answer`, one Claude Code turn). One caller genuinely loops it and
    passes True to say so: `DiscoveryService.draft_turn`, the interactive `discover` loop, on every
    turn after the first (#9, #58, #77). Pinned by
    `test_a_looping_caller_can_still_ask_for_the_breakpoint_back`."""
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": out.model_dump_json()},
        {"role": "user", "content": "Client answers:\n" + answers},
    ]
    return run(client, messages, only=only, carry_from=out, reuse_system=reuse_system, model=model)


# ── Generators (model → artifact) ───────────────────────────────────────────────
# Every generator threads `only` — the context-card selection its discovery ran against, read from
# session.json by the CLI — so an artifact is grounded in the same cards discovery used, not silently
# the full set. None means all cards (the default and the pre-0.6.1 behaviour).


# Every generator below is **one** `_complete` call, so its system prompt was being written to cache
# and never read back — `reuse_system=False` is the default here for that reason (#9). It stays a
# parameter rather than a constant because the same function is single-call in production and
# multi-call in the harness: `scripts/golden_run.py --brief` calls `advise()` K times off one prompt,
# and that caller should pass `reuse_system=True`. `AnthropicProvider.generate` threads it through
# `**kwargs`, so a future looping caller has the same escape hatch without another signature change.


def derive_stories(client, out: EngineOutput, only: list[str] | None = None, *,
                   reuse_system: bool = False, model: str | None = None) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md", only)
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Stories,
                     reuse_system=reuse_system, model=model, operation="stories")


def advise(client, out: EngineOutput, only: list[str] | None = None, *,
           reuse_system: bool = False, model: str | None = None) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities.

    `brief.md` still says "solution assessment", not "Decision brief" — deliberately, not missed
    (#166): it is fed into the system prompt verbatim, so renaming it is a `scripts/golden_run.py`
    spend decision, not a caption fix. See
    `test_the_declared_exception_records_its_reason_at_the_call_site` in
    tests/test_vocabulary_boundary.py for the guard and the full reasoning."""
    system = build_prompt("brief.md", only)
    user = "Completed requirements model to advise on:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Brief,
                     reuse_system=reuse_system, model=model, operation="brief")


def generate_prd(client, out: EngineOutput, only: list[str] | None = None, *,
                 reuse_system: bool = False, model: str | None = None) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_prompt("prd.md", only)
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], PRD,
                     reuse_system=reuse_system, model=model, operation="prd")


def generate_criteria(client, out: EngineOutput, only: list[str] | None = None, *,
                      reuse_system: bool = False, model: str | None = None) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_prompt("criteria.md", only)
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria,
                     reuse_system=reuse_system, model=model, operation="criteria")


def generate_epic(client, out: EngineOutput, only: list[str] | None = None, *,
                  reuse_system: bool = False, model: str | None = None) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_prompt("epic.md", only)
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json()
    return _complete(client, system, [{"role": "user", "content": user}], Epic,
                     reuse_system=reuse_system, model=model, operation="epic")


def generate_release(client, out: EngineOutput, version: str = "",
                     only: list[str] | None = None, *,
                     reuse_system: bool = False, model: str | None = None) -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_prompt("release.md", only)
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
    system = build_prompt("estimate.md", only)
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

# Every operation reachable through `ReasoningProvider.generate` -- the registry that keeps a surface
# from reasoning its own pipeline outside the seam (#77). Pinned by
# `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns`.
#
# `estimate` is the one entry that does not fit the plain model → contract shape, and it is listed
# rather than hidden: it takes the prior `stories` through `**kwargs`, and it returns
# `(EstimateDraft, soft_slots, confidence)` — the last two computed in core from the same model, so a
# caller cannot get the draft and the confidence out of step. Since #519 it is a saved artifact
# like the rest: `DiscoveryService.generate("estimate")` reasons the stories first from the same
# snapshot, saves both, and files them against one revision (`_generate_estimate`).
_GENERATORS = {
    "brief": advise,
    "stories": derive_stories,
    "prd": generate_prd,
    "criteria": generate_criteria,
    "epic": generate_epic,
    "release": generate_release,
    "estimate": estimate,
}

# The prompt file behind each operation — what `prompt_version()` hashes to identify the reasoning that
# produced a revision. `analyze` is the discovery turn; the rest are the artifact types.
_OP_PROMPTS = {
    "analyze": "engine.md", "brief": "brief.md", "stories": "stories.md", "estimate": "estimate.md",
    "prd": "prd.md", "criteria": "criteria.md", "epic": "epic.md", "release": "release.md",
}


def prompt_version(op: str, only: list[str] | None = None) -> str:
    """`"sha256:…"` over the exact system prompt an operation sends — the prompt file, the schema, and
    the selected context cards, byte for byte.

    This is what makes a revision traceable rather than merely timestamped. Behaviour here is tuned by
    editing Markdown and JSON assets, so "which model produced this" answers half the question; the
    other half is "against which prompt and which context cards", and that is exactly what changes
    between two runs that look identical in the log. A card added to the set moves the hash, because it
    genuinely moved the reasoning."""
    return "sha256:" + hashlib.sha256(build_prompt(_OP_PROMPTS[op], only).encode("utf-8")).hexdigest()
