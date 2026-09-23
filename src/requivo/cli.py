from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Callable, NamedTuple

from dotenv import load_dotenv

from requivo import __version__
from requivo.cli_support import (
    _announce_bind,
    _generator_service,
    _missing_extra_message,
    _print_session_candidates,
    _render_usage_safely,
    _resolve_optional_session,
    _wrote,
    _wrote_file,
)
from requivo.core import persistence as store
from requivo.core.adapters import epic_export_json, to_github_json, to_gitlab_json
from requivo.core.analysis import model_status, slot_label
from requivo.core.context import available_cards, average_card_byte_size, resolve_cards
from requivo.core.contracts import EngineOutput, Question
from requivo.core.dependencies import propagate, resolve_slots
from requivo.core.errors import AmbiguousPerimeterError, InvalidSlugError, RequivoError, SessionNotFoundError
from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter, resolve_perimeter
from requivo.core.persistence import load_model
from requivo.core.selectors import display_document, display_text, display_token
from requivo.deterministic import is_file_argument, print_json, read_source
from requivo.deterministic import register as register_deterministic
from requivo.paths import DEMO

# The only provider names this surface may take, each a surface concern (#77, #167); the list is
# guarded both ways by `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns`.
from requivo.providers.anthropic import new_client
from requivo.providers.errors import EngineError
from requivo.render.markdown import criteria_markdown, epic_markdown, gtm_plan_markdown, prd_markdown, release_markdown
from requivo.render.terminal import (
    DOC_TYPES,
    docs_menu_rows,
    render_brief,
    render_context_judgment,
    render_dependency_map,
    render_docs_menu,
    render_estimate,
    render_evidence,
    render_grounding,
    render_impact,
    render_next_command,
    render_session_cost,
    render_stale,
    render_stories,
    render_turn,
    render_turn_state,
)
from requivo.services.artifacts import ARTIFACT_FILENAMES
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.streams import configure_streams, safe_write
from requivo.usage import track_usage

MAX_TURNS = 8

# Questions asked per checkpoint, below `MAX_QUESTIONS` (the ceiling on what a turn *returns*); the
# rest are re-derived next turn, never carried (#592). `test_the_checkpoint_window_fits_inside_the_contract_cap`.
QUESTIONS_PER_CHECKPOINT = 4

# The work succeeded and the report could not be encoded: distinct from 1 so a script can tell them apart.
EXIT_RENDER_FAILED = 3

# 128 + SIGINT, reserved for Ctrl-C (#206); nothing else may claim it: `test_the_degraded_code_collides_with_nothing`.
EXIT_INTERRUPTED = 130

# Two messages: this arm cannot see how far the handler got; a non-empty ledger is the one fact it can read.
_RENDER_FAILED_HEAD = (
    "\n"
    "Requivo could not encode its output for this console: {error}\n"
    "\n"
    "This failed while *printing*, which happens after the command has done its work.\n"
)

_RENDER_FAILED_PAID = (
    "A provider call HAS completed and been billed on this run, and any revision it produced has\n"
    "already been applied. Do not re-run this command -- you would pay for a second call and stack\n"
    "a second revision on the first. Check with `requivo status <session>`.\n"
)

_RENDER_FAILED_UNPAID = (
    "No provider call was made on this run, so nothing has been billed. A local change may still\n"
    "have been written -- `model apply` and `artifact save` mutate without calling out -- so check\n"
    "with `requivo status <session>` before re-running rather than assuming either way.\n"
)



_RENDER_FAILED_TAIL = (
    "\n"
    "Set PYTHONIOENCODING=utf-8, or redirect to a file, to see the output itself.\n"
    "Run `requivo doctor` to see which stream could not be configured.\n"
)


class Drafted(NamedTuple):
    """The drafting loop's result: the model, and whether the *user* ended it (a converged loop goes
    on to the brief, a stopped one must not). `model` is None only when turn 1 produced nothing.
    Pinned by `test_stopping_early_stops_reasoning_and_says_so`."""

    model: EngineOutput | None
    stopped: bool


class DraftingFailed(Exception):
    """A provider failure or Ctrl-C during a draft turn, carrying `last`, the model of the last turn
    that succeeded (None when turn 1 failed), so the caller can persist it (#202).
    Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`."""

    def __init__(self, cause: BaseException, last: EngineOutput | None, turn: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.last = last
        self.turn = turn


def converse(disco: DiscoveryService, request: str, only: list[str] | None = None,
            perimeter: str = "software") -> Drafted:
    """Fill the model, ask, feed answers back, until no high-value question remains.

    Returns a `Drafted`, never None: read `.stopped`. The model is the carried state, not a
    transcript; the reasoning is `DiscoveryService.draft_turn`, so this holds no provider client.
    `only` is held constant across turns so the cached prefix survives. Pinned by
    `test_stopping_early_keeps_the_turns_it_paid_for` and
    `test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript`."""
    out = None
    answers = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        try:
            out = disco.draft_turn(request, current_model=out, answers=answers, cards=only,
                                   perimeter=perimeter)
        except (RequivoError, KeyboardInterrupt) as e:
            # `RequivoError`, not `EngineError`: `ProviderOutputError` is a sibling, and `out` still holds
            # the last turn that succeeded. `test_a_provider_output_failure_mid_turn_also_names_the_claimed_session`.
            raise DraftingFailed(e, out, turn) from e
        # The checkpoint; `_prompt_answers` asks the questions one at a time (#592).
        render_turn_state(out, perimeter)

        if not out.questions:
            break

        answers = _prompt_answers(out.questions, perimeter)
        if answers is None:
            return Drafted(out, stopped=True)
    else:
        print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")

    return Drafted(out, stopped=False)


def _prompt_answers(questions: list[Question], perimeter: str = DEFAULT_PERIMETER) -> str | None:
    """Prompt for one turn's answers, folded into the `[slot: ...] Q: ... → A: ...` shape the
    provider reads back. `None` means the user stopped, and this already printed why. Only the
    leading `QUESTIONS_PER_CHECKPOINT` are asked (#592)."""
    asked = questions[:QUESTIONS_PER_CHECKPOINT]
    print("\nEnter skips a question · 'q' stops.")
    replies = []
    try:
        for i, q in enumerate(asked, 1):
            # `q.q` is LLM-authored prose: `display_text` escapes control characters so a forged question
            # cannot write at column 0 of the prompt (#330).
            # `test_a_forged_question_cannot_write_a_line_at_column_zero_of_the_input_prompt`.
            safe_q = display_text(q.q)
            ans = input(
                f"\n  [{i}/{len(asked)}] {safe_q}\n"
                f"        ({slot_label(q.slot, perimeter)})\n"
                f"      > "
            ).strip()
            if ans.lower() == "q":
                print("Stopped.")
                return None
            if ans:
                # Same escaped text folded back: `test_a_forged_question_cannot_break_the_answer_folded_back_to_the_provider`.
                replies.append(f"[slot: {q.slot}] Q: {safe_q} → A: {ans}")
    except (EOFError, KeyboardInterrupt):
        print("\nStopped.")
        return None

    if not replies:
        print("No answer provided — stopping.")
        return None

    return "\n".join(replies)


def _prompt_perimeter_choice(e: AmbiguousPerimeterError) -> str | None:
    """Ask which perimeter candidate to use after an ambiguous route (#601); `None` means declined.
    `KeyboardInterrupt` is deliberately not caught, so a cancel exits 130 rather than 1:
    `test_an_interrupt_at_the_perimeter_prompt_exits_130_not_1`."""
    candidates = e.details.get("candidates", [])
    reason = display_text(e.details.get("reason", ""))
    print(f"\nMore than one installed perimeter could fit this request — {reason}")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c}")
    try:
        ans = input(f"      Which one? [1-{len(candidates)}, Enter to skip] > ").strip()
    except EOFError:
        print("\nStopped.")
        return None
    if not ans.isdigit() or not (1 <= int(ans) <= len(candidates)):
        return None
    chosen = candidates[int(ans) - 1]
    print(f"→ Continuing under {chosen}.")
    return chosen


# ── Subcommand CLI (`requivo`) ────────────────────────────────────────────────
# Each handler parses, calls the services, renders, writes. `app()` takes an optional client so tests
# can inject a stub; only the verbs that call the API build one.


def _why(e: BaseException) -> str:
    """A `KeyboardInterrupt` stringifies to '', so it gets a word of its own (#320)."""
    return "interrupted" if isinstance(e, KeyboardInterrupt) else str(e)


def _say_saved(slug: str) -> None:
    """`canonical_dir` direct, justified (#76): the path is the answer and the repository has none."""
    print(f"\nSaved session → {store.canonical_dir(slug)}")


def _say_nothing_drafted(slug: str) -> None:
    """A session was claimed and the paid call that would have drafted its first turn never returned."""
    print(f"\nSaved request → {store.canonical_dir(slug)}", file=sys.stderr)
    print("Nothing was drafted, so the session is unchanged — re-run `requivo discover` to try "
          "again.", file=sys.stderr)


def _rescue_drafted(disco, request: str, e: DraftingFailed, *, cards, slug: str,
                    perimeter: str = "software"):
    """Persist what an interrupted drafting loop had already paid for, then let the failure surface.
    Turn 1 failing has nothing to save, so it points at `discover` rather than `answer`. Never returns.
    Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`."""
    if e.last is None:
        _say_nothing_drafted(slug)
    else:
        kept = e.turn - 1
        # Guarded: the rescue's own save can fail, and it must still name the original failure.
        # `test_a_rescue_that_cannot_save_says_so_and_still_names_the_original_failure`.
        try:
            slug = disco.finalize_discovery(request, e.last, cards=cards, slug=slug,
                                            brief=None, surface="cli-discover", perimeter=perimeter)
        except (RequivoError, OSError, KeyboardInterrupt) as save_failed:
            # A second Ctrl-C on the rescue's save must not propagate silently (#206).
            print(f"\nTurn {e.turn} failed, and the {kept} turn(s) before it could NOT be saved: "
                  f"{_why(save_failed)}", file=sys.stderr)
            print(f"The request is still captured at {store.canonical_dir(slug)}.", file=sys.stderr)
            print(f"The failure that stopped the run was: {_why(e.cause)}", file=sys.stderr)
            if isinstance(save_failed, KeyboardInterrupt):
                # Re-raised bare so `app()` assigns the shared exit code (130).
                raise
            raise SystemExit(1) from save_failed
        print(f"\nTurn {e.turn} failed, so the {kept} turn(s) before it were saved rather than "
              f"discarded.", file=sys.stderr)
        print(f"Saved session → {store.canonical_dir(slug)}", file=sys.stderr)
        print(f'Continue where you left off with:\n  requivo answer {slug} "<your answers>"',
              file=sys.stderr)
    # Re-raised, not wrapped: `app()` decides the exit code and prints the usage tail (#206).
    raise e.cause


# The three shapes `discover`'s argument takes, said once so the two refusals agree (#360).
_REQUEST_SHAPES = ("a sentence describing what to build, a path to a file containing one, or '-' to "
                   "read one from stdin.")


def _cmd_discover(a, client) -> None:
    # `getattr`: `run`'s subparser reuses this function without defining `--perimeter` (#601).
    perimeter = getattr(a, "perimeter", None)
    if not a.request or not a.request.strip():
        print(f"discover needs a request: {_REQUEST_SHAPES}", file=sys.stderr)
        raise SystemExit(2)
    client = client or new_client()
    # `a.request != "-"` first, so this agrees with where `read_source` reads from.
    # `test_a_dash_is_stdin_even_when_a_file_of_that_name_exists`.
    is_file = a.request != "-" and is_file_argument(a.request)
    # `read_source`: the third shape is `-` for stdin (#360).
    # `test_discover_reads_the_request_from_stdin_when_the_argument_is_a_dash`.
    request = read_source(a.request)
    if not request.strip():
        # Same refusal as the blank argument, one noun different.
        source = "stdin" if a.request == "-" else "that file"
        print(f"discover needs a request and {source} is empty: {_REQUEST_SHAPES}", file=sys.stderr)
        raise SystemExit(2)

    # An unknown card is a hard error: `only = None` would mean *every* card (invariant 3).
    only = resolve_cards(a.context.split(",")) if a.context else None
    if only:
        print(f"Context cards: {', '.join(only)}")
    else:
        # Disclosure only, before the paid call (#257): `only` stays None.
        all_cards = available_cards()
        if all_cards:
            avg_bytes = average_card_byte_size()
            weight = f"~{avg_bytes:,} bytes each" if avg_bytes else "measurable weight"
            print(f"Context cards: all {len(all_cards)} ({', '.join(all_cards)}) — no --context "
                  f"given. Each card adds {weight} to every prompt and dilutes the others; narrow "
                  "with --context/--cards if this request is about one product area "
                  "(see docs/context-cards.md).")

    # Orchestration lives in DiscoveryService; the CLI owns the TTY loop and the rendering.
    disco = DiscoveryService(client=client)
    # A filename is a slug *suggestion*; it is validated strictly.
    slug_hint = SessionService.slug_hint(Path(a.request).stem) if is_file else None
    quick = a.once or not sys.stdin.isatty()
    if quick:
        # Claimed, routed, judged and re-claimed in one service call (#593, #601); `only`/`perimeter`
        # are rebound. The `except` below needs the slug to name.
        meta, grounding, only, routing = disco.claim_and_ground(request, cards=only, slug=slug_hint,
                                                                 perimeter=perimeter)
        perimeter = meta.perimeter
        render_context_judgment(grounding, routing)
        try:
            slug = disco.start(request, cards=only, slug=meta.slug, finalize=False,
                               surface="cli-discover", perimeter=perimeter)
        except (RequivoError, KeyboardInterrupt):
            # `RequivoError`, not `EngineError`: `ProviderOutputError` is a sibling, not a subclass.
            _say_nothing_drafted(meta.slug)
            raise
        out = disco.sessions.load_model(slug)
        render_turn(out, perimeter)
        _say_saved(slug)
        if out.questions:
            print(f'\n→ Answer and refine: requivo answer {slug} "<your answers>"')
        return

    # Invariant 13's gate before the loop pays (#133):
    # `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.
    try:
        meta, grounding, only, routing = disco.claim_and_ground(request, cards=only, slug=slug_hint,
                                                                 perimeter=perimeter)
    except AmbiguousPerimeterError as e:
        # Someone is at a prompt: ask, don't guess (#601). A chosen perimeter re-runs explicit.
        chosen = _prompt_perimeter_choice(e)
        if chosen is None:
            raise
        meta, grounding, only, routing = disco.claim_and_ground(request, cards=only, slug=slug_hint,
                                                                 perimeter=chosen)
    slug = meta.slug
    perimeter = meta.perimeter
    render_context_judgment(grounding, routing)
    try:
        drafted = converse(disco, request, only=only, perimeter=perimeter)
    except DraftingFailed as e:
        _rescue_drafted(disco, request, e, cards=only, slug=slug, perimeter=perimeter)
    out = drafted.model
    if out is None:
        # Unreachable while `MAX_TURNS >= 1`; kept as the narrowing so a None cannot reach `finalize_discovery`.
        print(f"\nSaved request → {store.canonical_dir(slug)}")
        return
    if drafted.stopped:
        # A deliberate stop keeps what it paid for and buys no brief (#202): the same shape `--once`
        # lands. `test_stopping_early_keeps_the_turns_it_paid_for`.
        slug = disco.finalize_discovery(request, out, cards=only, slug=slug,
                                        brief=None, surface="cli-discover", perimeter=perimeter)
        _say_saved(slug)
        print(f'\n→ Answer and refine: requivo answer {slug} "<your answers>"')
        return

    # The write comes before the last paid call (#202), so a failed assessment is one retryable call.
    # `test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry`.
    slug = disco.finalize_discovery(request, out, cards=only, slug=slug,
                                    brief=None, surface="cli-discover", perimeter=perimeter)
    _say_saved(slug)
    # A perimeter with no "brief" generator ends here (#609).
    # `test_a_finished_go_to_market_discovery_ends_with_the_saved_session_not_a_traceback`.
    if "brief" not in get_perimeter(perimeter).artifact_types:
        out = disco.sessions.load_model(slug)
        render_turn(out, perimeter)
        return
    print("\nGenerating the decision brief…")
    try:
        gen = disco.generate(slug, "brief", surface="cli-discover")
    except (RequivoError, KeyboardInterrupt) as e:
        # `KeyboardInterrupt` belongs here (#320): `test_an_interrupt_during_the_brief_reports_the_saved_session`.
        print(f"\nThe decision brief did not complete: {_why(e)}", file=sys.stderr)
        print(f"Your discovery is saved and nothing was lost — retry just this step with:\n"
              f"  requivo brief {slug}", file=sys.stderr)
        # Re-raised, not wrapped: `app()` decides the exit code (#206).
        raise
    # `gen.model`, not `out`: the assessment's reasoning is absorbed into the model by now.
    render_brief(gen.model, gen.artifact)


def _cmd_answer(a, client) -> None:
    # DiscoveryService folds the answers in through the validated apply path.
    disco = DiscoveryService(client=client)
    svc = disco.sessions
    # `accept_path=False`: this verb writes a revision back, never opens a file (#402).
    slug = svc.resolve_slug(a.session, accept_path=False)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    result = disco.answer(slug, a.answers, surface="cli-answer")
    perimeter = resolve_perimeter(svc.meta(slug).perimeter)
    out = svc.load_model(slug)
    render_turn(out, perimeter)
    if result.stale_artifacts:
        pairs = [(t, ARTIFACT_FILENAMES[t]) for t in result.stale_artifacts]
        render_stale(pairs, [slot_label(sid, perimeter) for sid in result.changed_slots])
    # Every invalidated collection is counted, or an exclusion-only change reports nothing here (invariant 1).
    # test_an_exclusion_only_invalidation_is_still_announced_on_the_apply_path.
    parts = [(result.invalidated_decisions, "decision(s)"),
             (result.invalidated_challenges, "premise(s)"),
             (result.invalidated_exclusions, "exclusion(s)"),
             (result.invalidated_thresholds, "threshold(s)")]
    n_reasoning = sum(len(items) for items, _ in parts)
    reasoning_type = get_perimeter(perimeter).primary_artifact  # #609 -- one source, not a third local copy
    if n_reasoning and reasoning_type:
        breakdown = ", ".join(f"{len(items)} {noun}" for items, noun in parts if items)
        print(f"\n⚠  This change unseats {n_reasoning} piece(s) of the {_LABEL[reasoning_type]}'s reasoning ({breakdown}) — regenerate with `requivo {reasoning_type} {slug}`.")
    print(f"\nSaved session → {store.canonical_dir(slug)}")
    if out.questions:
        print(f'\n→ Keep going: requivo answer {slug} "<your answers>"')
    else:
        print(f"\n✅ Discovery converged — run `requivo {reasoning_type} {slug}` for the {_LABEL[reasoning_type]}." if reasoning_type else "\n✅ Discovery converged.")


def _is_existing_session(svc: SessionService, ref: str) -> bool:
    """Return False for invalid slugs; let storage failures stop routing (#589)."""
    try:
        return svc.exists(ref)
    except InvalidSlugError:
        return False


def _prompt_for_request() -> str:
    """`run` with nothing to resume: ask for a request the same three shapes `discover` accepts."""
    print(f"No session to resume yet. What would you like to build? ({_REQUEST_SHAPES})")
    try:
        return input("> ")
    except EOFError:
        return ""


def _run_target(svc: SessionService) -> tuple[str, bool]:
    """`run` with no argument: the workspace's default session (#541), or a prompt for a request
    (#540). `resume` is True only when `value` came from the resolver, so a same-named file cannot
    hijack it: `test_run_with_no_argument_is_not_hijacked_by_a_same_named_file`."""
    try:
        resolution = svc.resolve_default_session()
    except SessionNotFoundError:
        return _prompt_for_request(), False
    if resolution.candidates:
        _print_session_candidates(resolution)
    return resolution.default, True


def _refuse_resume_only_flags(a) -> None:
    """`--once`/`--context` describe a new discovery; refused on a resume rather than ignored.
    `test_run_refuses_once_and_context_when_resuming`."""
    if a.once or a.context:
        raise RequivoError(
            "requivo run <slug>: --once and --context apply to a new discovery, not to resuming an "
            "existing session, which reuses the session's own context cards. Drop them, or discover "
            "a fresh session with `requivo discover`/`requivo run <request>`.")


def _resume_run(disco: DiscoveryService, slug: str) -> None:
    """`run <slug>` on a discovered session (#540): `answer` inside a loop, never a second discovery."""
    svc = disco.sessions
    perimeter = resolve_perimeter(svc.meta(slug).perimeter)
    out = svc.load_model(slug)
    for _turn in range(1, MAX_TURNS + 1):
        render_turn_state(out, perimeter)
        if not out.questions:
            primary = get_perimeter(perimeter).primary_artifact  # #609 -- was hardcoded "brief"
            print(f"\n✅ Discovery converged — run `requivo {primary} {slug}` for the {_LABEL[primary]}."
                 if primary else "\n✅ Discovery converged.")
            return
        answers = _prompt_answers(out.questions, perimeter)
        if answers is None:
            print(f"\nSaved session → {store.canonical_dir(slug)}")
            return
        result = disco.answer(slug, answers, surface="cli-run")
        if result.stale_artifacts:
            pairs = [(t, ARTIFACT_FILENAMES[t]) for t in result.stale_artifacts]
            render_stale(pairs, [slot_label(sid, perimeter) for sid in result.changed_slots])
        out = svc.load_model(slug)
    else:
        print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")
    print(f"\nSaved session → {store.canonical_dir(slug)}")


def _cmd_run(a, client) -> None:
    """`run [request | path | slug] [--context CARDS] [--once]`: one verb over `discover` and
    `answer` (#538, #540). No argument resumes the default session or asks for a request."""
    svc = SessionService()
    ref = a.request
    if ref is None:
        # `resume=True` came off the resolver, so `ref` is never re-checked against a same-named file.
        # `test_run_with_no_argument_is_not_hijacked_by_a_same_named_file`.
        ref, resume = _run_target(svc)
        if resume:
            _refuse_resume_only_flags(a)
            _resume_run(DiscoveryService(client=client or new_client(), sessions=svc), ref)
            return
    elif ref != "-" and not is_file_argument(ref) and _is_existing_session(svc, ref):
        slug = svc.resolve_slug(ref, accept_path=False)
        if not svc.exists(slug):
            raise svc.no_session(slug)
        _refuse_resume_only_flags(a)
        _resume_run(DiscoveryService(client=client or new_client(), sessions=svc), slug)
        return
    a.request = ref
    _cmd_discover(a, client)


def _resolve_ref(ref: str) -> tuple[EngineOutput, str]:
    """Resolve a model.json path or a session slug to (model, slug). The refusal widens its noun
    and nothing else (#243)."""
    p = Path(ref)
    if p.is_file():
        return load_model(p), p.parent.name
    svc = SessionService()
    if svc.exists(ref):
        slug = svc.resolve_slug(ref)
        try:
            return svc.load_model(slug), slug
        except SessionNotFoundError:
            # The session exists but was never discovered: the narrower case, under the same code (#250).
            raise SessionNotFoundError(
                f"session '{slug}' has no model yet — only the request was captured. Run "
                f"`requivo discover` on the same request to analyse it (or, in Claude Code, "
                f"/requivo:discover).",
                details={"slug": slug},
            ) from None
    raise svc.no_session(ref, what="model file or session", details={"ref": ref})


def _status_payload(ref: str) -> tuple[EngineOutput, dict]:
    """(model, machine status): the shared `model_status` projection, plus revision, perimeter,
    context and artifact freshness when the reference is a canonical session."""
    out, slug = _resolve_ref(ref)
    svc = SessionService()
    perimeter = DEFAULT_PERIMETER
    if svc.exists(slug):
        meta = svc.meta(slug)
        perimeter = resolve_perimeter(meta.perimeter)
    payload: dict = {"slug": slug, **model_status(out, perimeter)}
    if svc.exists(slug):
        payload["revision"] = meta.current_revision
        payload["context_cards"] = meta.context_cards
        payload["perimeter"] = perimeter
        # Freshness is the explicit stale flag only — revision is provenance, not an invalidation rule.
        payload["artifacts"] = {
            t: {"revision": st.revision, "filename": st.filename, "stale": st.stale}
            for t, st in meta.artifact_status.items()
        }
    return out, payload


def _cmd_status(a, client) -> None:
    want_json = getattr(a, "json", False)
    # `quiet=want_json`: no candidate listing beside a `--json` payload (#246).
    ref = _resolve_optional_session(SessionService(), a.session, quiet=want_json)
    out, payload = _status_payload(ref)
    if want_json:
        # `--json` gets no pointer (#246); `print_json` carries the `ensure_ascii` contract (#301).
        print_json(payload)
        return
    render_turn(out, payload.get("perimeter") or DEFAULT_PERIMETER)
    # The grounding after the model (#492): evidence about the readout, not a preamble to it.
    render_grounding(payload.get("context_cards"))
    # Cumulative cost from the provenance on provider-backed revisions (#292); silent when there is none.
    slug = payload.get("slug")
    if slug:
        svc = SessionService()
        if svc.exists(slug):
            render_session_cost(svc.meta(slug).revisions)
    render_next_command(payload)


DEMO_SLUG = "event-checkin-reconciliation"
# The slot step ④ changes; the prose above it describes this slot in words, so the two move together.
DEMO_CHANGED_SLOT = "constraints"
# A literal, not package metadata: a closing pointer that can raise is worse than one that can go stale.
EXAMPLES_URL = "https://github.com/jbkkz/requivo/tree/main/examples"


def _fenced_text(markdown: str) -> str:
    """The terminal output inside a saved assessment's ```text … ``` block, or the whole text."""
    m = re.search(r"```text\s*\n(.*?)```", markdown, re.DOTALL)
    return m.group(1).rstrip() if m else markdown.strip()


def _cmd_demo(a, client) -> None:
    """A no-API-key walkthrough of a real run, replayed from the saved event-check-in example."""
    # The frozen payload ships in the package; the visitor is pointed at the browsable copy under examples/.
    demo = DEMO
    request = (demo / "request.md").read_text(encoding="utf-8").strip()
    out = load_model(demo / "model.json")
    assessment = _fenced_text((demo / "solution-assessment.md").read_text(encoding="utf-8"))

    bar = "═" * 72
    print(bar)
    print("  REQUIVO — DEMO   (no API key needed)")
    print("  A real run, replayed from saved output: this is what the engine made")
    print("  of the messy request below — nothing is called.")
    print(bar)

    print("\n\n① THE REQUEST  — a rambling, multi-feature client email\n")
    print(textwrap.indent(request, "  "))

    print("\n\n② WHAT THE ENGINE MADE OF IT  — computed live from the saved model, no API\n")
    render_turn(out)

    print("\n\n③ THE DECISION BRIEF  — a judgment, not a recap (the differentiator)\n")
    print(assessment)

    # The step the engine exists for: `propagate` walks the recorded graph, offline (#223).
    # `test_the_demo_shows_the_computed_blast_radius_of_a_changed_answer`.
    print("\n\n④ CHANGE ONE ANSWER  — and this is what it costs\n")
    print("  Say the six-week deadline moves. Nothing is re-analysed and nothing is asked of a")
    print("  model: Requivo reads the dependency graph the discovery recorded and reports what")
    print("  now rests on shaky ground. Computed, not generated — the same change gives the same")
    print("  answer every time.")
    render_impact(propagate(out, [DEMO_CHANGED_SLOT]))

    print("\n\n" + bar)
    print("  ⑤ EVERYTHING ELSE IS A VIEW OF THE SAME MODEL")
    print("     Regenerated from this one model.json, no re-discovery:")
    for name in ("epic.md", "acceptance-criteria.md"):
        if (demo / name).exists():
            print(f"       • {name}")
    # A URL, because a wheel install has no `examples/` (#225):
    # `test_the_demo_points_a_wheel_install_at_something_it_can_reach`.
    print(f"     Readable in the repository, or beside this payload in the package:\n"
          f"       {EXAMPLES_URL}/{DEMO_SLUG}")
    # A closing step a reader can take without a key (#223).
    print("\n  Keep going, still no API key:")
    print("    requivo web")
    print("        the browser interface, where a changed answer renders that block live")
    print(f"    requivo impact examples/{DEMO_SLUG}/model.json <slot>")
    print("        step ④ for any slot you name, from a clone of the repo")
    # The one command here that needs a key says so (#225).
    print('\n  With a key:   requivo discover "<your own request>"')
    print("                needs the [anthropic] extra and ANTHROPIC_API_KEY — `requivo doctor`")
    print("                checks both before you spend anything")
    print(bar)


def _cmd_impact(a, client) -> None:
    """Offline query over the dependency DAG: a slot's blast radius, or every slot's downstream."""
    svc = SessionService()
    ref = _resolve_optional_session(svc, a.session)
    out, slug = _resolve_ref(ref)
    # Thinner-evidence review (#493) is a walk over frozen revisions, so only a session has one; a
    # loose file never borrows a same-named session's:
    # `test_a_loose_model_file_never_borrows_the_review_of_a_session_sharing_its_directory_name`.
    evidence = None if Path(ref).is_file() else svc.thinner_evidence(slug)
    # The session's perimeter, or software for a bare model.json (#608).
    perimeter = (resolve_perimeter(svc.meta(slug).perimeter) if svc.exists_meta(slug)
                else DEFAULT_PERIMETER)
    if not a.slots:
        render_dependency_map(out, perimeter)
        render_evidence(evidence)
        return
    resolved, unmatched = resolve_slots(a.slots, perimeter)
    if unmatched:
        print(f"Unknown slot(s): {', '.join(unmatched)} — use a slot id or a label word "
              f"(e.g. 'permissions', 'workflow', 'reporting').")
    if resolved:
        render_impact(propagate(out, resolved, perimeter))
        render_evidence(evidence)
    if unmatched:
        # A wrong probe exits 1, not 0 and not `EXIT_DEGRADED`: the input was invalid (#250).
        raise SystemExit(1)


# Provider-backed generators: resolve the session, hand off to `DiscoveryService` (reasoning, lock,
# provenance, write), choose the terminal view, say where the file went. One `_cmd_generate` over
# three per-type tables (#556); the seven `add_parser` calls stay as the public verbs.


def _generator_verb(type_: str) -> Callable[[argparse.Namespace, object], None]:
    """Bind `_cmd_generate` to one type as a real closure named `_cmd_<type>` (`args.func.__name__`
    is asserted by `test_pc_parser_binds_every_subcommand`; pyright refuses `partial.__name__`)."""
    def verb(a: argparse.Namespace, client) -> None:
        _cmd_generate(a, client, type_)
    verb.__name__ = f"_cmd_{type_}"
    return verb


# type → extra keyword arguments for `disco.generate(...)`; absent means none.
_GENERATE_KWARGS: dict[str, Callable[[argparse.Namespace], dict]] = {
    # Two calls, one snapshot, two files against one revision (#519, invariant 6); `on_stories`
    # prints them before the second call is paid for. `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`.
    "estimate": lambda a: {"on_stories": render_stories},
    "release": lambda a: {"version": a.version},
}


def _render_brief(slug: str, result) -> None:
    render_brief(result.model, result.artifact)


def _render_gtm_plan(slug: str, result) -> None:  # plain-document pattern, like every non-`brief` generator below
    print(display_document(gtm_plan_markdown(result.model, result.artifact)))


def _render_prd(slug: str, result) -> None:
    # `display_document`, not `display_text` (#449): print time only, the saved string is untouched.
    # `test_the_same_document_renders_identically_through_generation_and_read_back`.
    print(display_document(prd_markdown(result.artifact)))


def _render_stories(slug: str, result) -> None:
    render_stories(result.artifact)


def _render_estimate(slug: str, result) -> None:
    est = result.artifact
    render_estimate(est.draft, est.soft, est.confidence)
    _wrote_file(slug, est.stories_status, "user stories")


def _render_criteria(slug: str, result) -> None:
    print(display_document(criteria_markdown(result.artifact)))  # #449, see `_render_prd`


def _render_epic(slug: str, result) -> None:
    print(display_document(epic_markdown(result.artifact)))  # #449, see `_render_prd`


def _render_release(slug: str, result) -> None:
    print(display_document(release_markdown(result.artifact)))  # #449, see `_render_prd`


# type → the terminal rendering of a fresh result, before `_wrote` prints where the file went.
_RENDER: dict[str, Callable[[str, object], None]] = {
    "brief": _render_brief, "gtm_plan": _render_gtm_plan, "prd": _render_prd, "stories": _render_stories, "estimate": _render_estimate, "criteria": _render_criteria, "epic": _render_epic, "release": _render_release,
}

# type → the label `_wrote` prints; the type, verb and filename stay `brief` (#166).
_LABEL: dict[str, str] = {
    "brief": "decision brief", "gtm_plan": "go-to-market plan", "prd": "PRD", "stories": "user stories", "estimate": "estimate", "criteria": "acceptance criteria", "epic": "epic", "release": "release notes",
}


def _post_epic(a: argparse.Namespace, slug: str, result) -> None:
    # `write_artifact_file`, not `repo.save_artifact`: untracked views of the saved epic, stamped with
    # the same revision (invariant 12). `test_pc_epic_export_stamps_the_same_revision_the_paired_epic_md_was_saved_against`.
    epic = result.artifact
    if a.export_json:
        print(f"Wrote neutral epic export → "
              f"{store.write_artifact_file(slug, 'epic.json', epic_export_json(epic, slug, result.status.revision))}")
    if a.github:
        print(f"Wrote GitHub issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.github.json', to_github_json(epic, slug, result.status.revision))}")
    if a.gitlab:
        print(f"Wrote GitLab issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.gitlab.json', to_gitlab_json(epic, slug, result.status.revision))}")


# type → extra work after `_wrote`. Only `epic` has any (its three optional tracker-plan exports).
_POST: dict[str, Callable[[argparse.Namespace, str, object], None]] = {"epic": _post_epic}


def _cmd_generate(a: argparse.Namespace, client, type_: str) -> None:
    """The one body behind all seven generator verbs (#556)."""
    slug, disco = _generator_service(a, client)
    kwargs = _GENERATE_KWARGS.get(type_, lambda a: {})(a)
    result = disco.generate(slug, type_, surface=f"cli-{type_}", **kwargs)
    _RENDER[type_](slug, result)
    _wrote(slug, result, _LABEL[type_])
    _POST.get(type_, lambda a, slug, result: None)(a, slug, result)


# `docs` (#544): one verb over the seven generators, never a second generation path.
_DOC_GENERATORS = {name: _generator_verb(name) for name in _LABEL}

_DOC_SELECTION_RE = re.compile(r"[,\s]+")


def _doc_generation_order(selected: list[str]) -> list[str]:
    """Canonical order; `estimate` absorbs `stories`, so picking both writes stories once.
    `test_docs_stories_and_estimate_together_write_stories_once` (#544)."""
    chosen = set(selected)
    if "estimate" in chosen and "stories" in chosen:
        chosen.discard("stories")
    return [t for t in DOC_TYPES if t in chosen]


def _resolve_doc_types(tokens: list[str], types: tuple[str, ...] = DOC_TYPES) -> list[str]:
    """`docs <slug> <type...>`: names only, refused before any call rather than filtered
    (invariant 3). Pinned by `test_resolve_doc_types_refuses_an_unknown_type_before_any_call`."""
    unknown = [t for t in tokens if t not in types]
    if unknown:
        raise RequivoError(
            f"neither a document type nor a session in this workspace: "
            f"{', '.join(display_token(t) for t in unknown)} -- choose a type from "
            f"{', '.join(types)}, or a slug from `requivo session list`.")
    return tokens


def _prompt_doc_selection(types: tuple[str, ...] = DOC_TYPES) -> list[str] | None:
    """The menu's prompt: numbers, names, `all`, or nothing to cancel. An unknown token is refused
    (invariant 3): `test_prompt_doc_selection_refuses_an_unknown_token_before_any_call`."""
    print("\nPick one or more (numbers or names, 'all', or Enter to cancel):")
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return None
    if not raw:
        print("Cancelled.")
        return None
    if raw.lower() == "all":
        return list(types)
    selected: list[str] = []
    unknown: list[str] = []
    for tok in (t for t in _DOC_SELECTION_RE.split(raw) if t):
        if tok.isdigit() and 1 <= int(tok) <= len(types):
            selected.append(types[int(tok) - 1])
        elif tok.lower() in types:
            selected.append(tok.lower())
        else:
            unknown.append(tok)
    if unknown:
        raise RequivoError(
            f"unknown selection: {', '.join(display_token(t) for t in unknown)} -- use a number "
            f"1-{len(types)}, a document name, or 'all'.")
    return selected


def _cmd_docs(a, client) -> None:
    """`docs [slug] [type...] [--all]` (#544): no type prints the menu; the first positional is a
    slug only when it names a session, the disambiguation `run` uses."""
    svc = SessionService()
    tokens = list(a.args)
    if tokens and _is_existing_session(svc, tokens[0]):
        slug = svc.resolve_slug(tokens[0], accept_path=False)
        type_tokens = tokens[1:]
    else:
        slug = _resolve_optional_session(svc, None)
        type_tokens = tokens
    if not svc.exists(slug):
        raise svc.no_session(slug)
    meta = svc.meta(slug)
    if meta.current_revision < 1:
        print(f"Session '{display_token(slug)}' has no model yet -- run `requivo run {slug}` to "
              "start the conversation before generating a document.")
        return
    owned_types = tuple(t for t in DOC_TYPES if t in get_perimeter(resolve_perimeter(meta.perimeter)).artifact_types)
    # Validated before the `--all` branch too, or a typo generates everything against the default
    # session (invariant 3). `test_docs_all_refuses_a_token_that_names_neither_a_type_nor_a_session`.
    if type_tokens:
        type_tokens = _resolve_doc_types(type_tokens, owned_types)
    if a.all:
        if type_tokens:
            # `--all` with explicit types is ambiguous, so refused: `test_docs_all_combined_with_an_explicit_type_is_refused`.
            raise RequivoError(
                f"--all takes no types ({', '.join(display_token(t) for t in type_tokens)} given) "
                "-- drop the type names, or drop --all and name only the ones you want.")
        selected = list(owned_types)
    elif type_tokens:
        selected = type_tokens
    else:
        render_docs_menu(docs_menu_rows(meta.artifact_status, owned_types))
        selected = _prompt_doc_selection(owned_types)
        if selected is None:
            return
    ns = argparse.Namespace(session=slug, export_json=False, github=False, gitlab=False, version="")
    for doc_type in _doc_generation_order(selected):
        _DOC_GENERATORS[doc_type](ns, client)


def _cmd_web(a, client) -> None:
    """Launch the local single-user web interface (the `[web]` extra). Uvicorn is imported here,
    never at module import, and the app is a factory so nothing binds a port before this runs."""
    host, port = a.host, a.port
    _announce_bind(host, verb="web",
                   exposure="Requivo Web has NO authentication and must not be exposed on an "
                            "untrusted network.")
    try:
        import uvicorn

        from requivo.web.app import create_app
        from requivo.web.logging_setup import configure_web_logging
    except ImportError as e:
        # `EngineError` (`provider_unavailable`) is a published payload:
        # `test_the_missing_web_extra_keeps_its_published_error_code`.
        raise EngineError(_missing_extra_message("web", e)) from e
    # The process is ours from here, so logging is configured here and never at import (#291);
    # under `--reload` uvicorn's worker re-imports the app and stays on `lastResort`.
    # `test_the_web_verb_configures_the_logger_before_it_serves`.
    configure_web_logging()
    url = f"http://{host}:{port}"
    print(f"\nRequivo Web → {url}")
    print("  Sessions stay local under .requivo/sessions/. An Anthropic key (server env) is needed only")
    print("  for provider actions (discovery, generation); consulting existing sessions needs none.\n")
    if not a.no_open:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if a.reload:
        uvicorn.run("requivo.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


def _cmd_api_serve(a, client) -> None:
    """Serve the local HTTP API (the `[api]` extra): `requivo web`'s shape, plus the bind
    discipline of `decision: the-http-api-facade`: `create_api(bind_host=host)` refuses a bind
    beyond loopback with no `REQUIVO_API_TOKEN`, before uvicorn is handed anything."""
    host, port = a.host, a.port
    try:
        import uvicorn

        from requivo.api.app import create_api
        from requivo.web.logging_setup import API_LOGGER, configure_surface_logging
    except ImportError as e:
        # Same decision as `_cmd_web`'s arm: `test_the_missing_api_extra_keeps_its_published_error_code`.
        raise EngineError(_missing_extra_message("api", e)) from e
    # Built before the warning, the logger and the banner, so a refusal to bind prints alone.
    # `test_the_serve_verb_refuses_a_non_loopback_bind_with_no_token_before_binding`.
    app = create_api(bind_host=host)
    _announce_bind(host, verb="api serve",
                   exposure="the Requivo API is protected only by the REQUIVO_API_TOKEN bearer "
                            "token and must not be exposed on an untrusted network.")
    # Same placement as `configure_web_logging()` in `_cmd_web` (#291).
    # `test_the_api_serve_verb_configures_the_logger_before_it_serves`.
    configure_surface_logging(API_LOGGER)
    url = f"http://{host}:{port}"
    print(f"\nRequivo API → {url}   (docs: {url}/docs)")
    print("  Sessions stay local under .requivo/sessions/. An Anthropic key (server env) is needed only")
    print("  for provider actions (discovery, generation); reading existing sessions needs none.")
    print("  EXPERIMENTAL: paths and shapes may still change -- see docs/decisions/0004.\n")
    uvicorn.run(app, host=host, port=port)


# The closing paragraph of `requivo --help` (#244): the first command to run, and what `(API)` means.
# `requivo run`, not `discover` (#546): `decision: three-journey-verbs`.
EPILOG = (
    "Try it first, with no key and no network:\n"
    "  requivo demo\n"
    "\n"
    "Then start real work:\n"
    "  requivo run \"We need a leave approval system\"   (API)\n"
    "\n"
    "Verbs marked (API) call the Anthropic API and spend money on your own key; every other verb\n"
    "is offline and free. Set ANTHROPIC_API_KEY, or put it in a .env file in the directory you run\n"
    "from. `requivo doctor` reports whether this install can make a call, and which model it uses.\n"
)

# The three `--help` tiers (#546), presentational only: registration order stays the axis
# `test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order` reads. Every verb
# appears in exactly one tuple, or `_JourneyHelpFormatter` refuses to render (invariant 3).
# `test_every_registered_verb_appears_in_exactly_one_help_group`.
_HELP_GROUP_START = ("demo", "run", "docs", "status", "web")
_HELP_GROUP_SCRIPTS = (
    "discover", "answer", "brief", "gtm_plan", "prd", "stories", "estimate", "criteria", "epic", "release", "impact",
)
_HELP_GROUP_PLUMBING = ("doctor", "schema", "context", "session", "model", "artifact", "api")


class _JourneyHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Renders the top-level command list in the three `_HELP_GROUP_*` tiers (#546). Only the root
    parser uses it: `add_parser` does not inherit `formatter_class`, so `requivo <verb> --help` is
    untouched. `test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter`."""

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return self._format_grouped_commands(action)
        return super()._format_action(action)

    def _format_grouped_commands(self, action) -> str:
        by_name = {choice.dest: choice for choice in action._choices_actions}
        named = set(_HELP_GROUP_START) | set(_HELP_GROUP_SCRIPTS) | set(_HELP_GROUP_PLUMBING)
        if named != set(by_name):
            # Live twin of `test_every_registered_verb_appears_in_exactly_one_help_group`, so a real
            # `--help` cannot silently narrow itself.
            raise AssertionError(
                "a verb is registered but not in exactly one --help group (#546): "
                f"missing={sorted(named - set(by_name))} extra={sorted(set(by_name) - named)}")

        header = self._format_action_invocation(action)
        parts = [f"{' ' * self._current_indent}{header}\n"]
        self._indent()
        parts.append(self._format_full_group("Start here:", _HELP_GROUP_START, by_name))
        parts.append("\n")
        parts.append(self._format_compact_group(
            "For scripts and integrations (docs/integrations.md):", _HELP_GROUP_SCRIPTS, by_name))
        parts.append("\n")
        parts.append(self._format_compact_group("Plumbing:", _HELP_GROUP_PLUMBING, by_name))
        self._dedent()
        return self._join_parts(parts)

    def _format_full_group(self, title, names, by_name) -> str:
        """One row per verb with its help text, via `_format_action` so alignment matches the page."""
        lines = [f"{' ' * self._current_indent}{title}\n"]
        self._indent()
        lines.extend(self._format_action(by_name[name]) for name in names)
        self._dedent()
        return self._join_parts(lines)

    def _format_compact_group(self, title, names, by_name) -> str:
        """Names only, comma-joined; the `(API)` marker survives the collapse.
        `test_every_paid_verb_in_a_compact_group_still_shows_the_marker` (#546)."""
        labeled = [f"{name} (API)" if "(API)" in (by_name[name].help or "") else name
                   for name in names]
        body = f"{' ' * (self._current_indent + 2)}{', '.join(labeled)}\n"
        return f"{' ' * self._current_indent}{title}\n{body}"


def _build_parser(formatter_class: type[argparse.HelpFormatter] = _JourneyHelpFormatter,
                   ) -> argparse.ArgumentParser:
    # `formatter_class` is a parameter so `test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter`
    # can build a plain-formatter twin in the same interpreter (#546).
    p = argparse.ArgumentParser(
        prog="requivo",
        description="Requivo — find what could change the solution before you commit to the scope.",
        epilog=EPILOG,
        # Extends `RawDescriptionHelpFormatter`, so the epilog still renders raw (#546).
        formatter_class=formatter_class,
    )
    # Read from `requivo.__version__`, never a literal (#247):
    # `test_the_version_flag_declares_nothing_that_test_version_sites_cannot_see`.
    p.add_argument("--version", action="version", version=f"requivo {__version__}",
                   help="print the Requivo version and exit")
    p.add_argument("--workspace", metavar="DIR", help=_WORKSPACE_HELP)
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    # Registration order is the journey axis, not render order (#546); `model_cmd` is defined here
    # because the journey verbs need it first. `test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order`.

    # Only `status` and `impact` open a path they are handed; the rest resolve a slug and their help
    # must not claim otherwise (#402).
    _SESSION_HELP_WITH_PATH = "a session slug, or a path to a saved model.json"
    _SESSION_HELP_SLUG_ONLY = "a session slug"

    def model_cmd(name: str, help_: str, func, extra=None, *, accepts_path: bool = False,
                  session_required: bool = True):
        sp = sub.add_parser(name, help=help_)
        # `session`, not `model` (#248): `test_every_session_reference_positional_is_spelled_session`.
        session_help = _SESSION_HELP_WITH_PATH if accepts_path else _SESSION_HELP_SLUG_ONLY
        # Optional only on the two verbs #541 names, never on a plumbing verb.
        if session_required:
            sp.add_argument("session", help=session_help)
        else:
            sp.add_argument("session", nargs="?", default=None,
                            help=session_help + " (omit to use the workspace's default session)")
        if extra:
            extra(sp)
        sp.set_defaults(func=func)

    demo = sub.add_parser("demo", help="replay a real run from saved output — no API key needed")
    demo.set_defaults(func=_cmd_demo)

    r = sub.add_parser(
        "run", help="start or resume the conversation: no argument resumes, a request/path "
                    "discovers, a slug refines (API)")
    r.add_argument("request", nargs="?", default=None,
                   help="the client request, a path to a file containing it, '-' to read one "
                        "from stdin, an existing session's slug to resume, or omit to resume the "
                        "workspace's default session")
    r.add_argument("--once", action="store_true",
                   help="single pass when starting a new discovery, no interactive loop "
                        "(refused when resuming an existing session)")
    r.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                   help="comma-separated context cards for a new discovery (refused when "
                        "resuming, which reuses the session's own cards). Alias: --cards.")
    r.set_defaults(func=_cmd_run)

    d = sub.add_parser("discover",
                       help="analyse a request (a string, a file path or '-') and start a session (API)")
    # `-` named in the help, not only implemented (#360).
    d.add_argument("request",
                   help="the client request, a path to a file containing it, or '-' to read it "
                        "from stdin")
    d.add_argument("--once", action="store_true", help="single pass (status + questions), no interactive loop")
    # `--cards` is an alias of `--context` on one action (#85): two arguments would let the last one win.
    d.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                   help="comma-separated context cards to load instead of all "
                        "(e.g. b2b-platform,financial-reporting); sharpens discovery by dropping "
                        "irrelevant cards. Applies to this discovery only. Alias: --cards.")
    d.add_argument("--perimeter", default=None, metavar="ID",
                   help="which installed perimeter this session runs under, frozen at creation. "
                        "Omit it to let the router (#601) judge the request's shape and pick one, "
                        "or ask when more than one plausibly fits.")
    d.set_defaults(func=_cmd_discover)

    model_cmd("answer", "fold the client's answers in and report what moved (API)",
              _cmd_answer, lambda sp: sp.add_argument("answers", help="the client's answers, as free text"))
    model_cmd("status", "show the understanding, open questions and readiness", _cmd_status,
              lambda sp: sp.add_argument("--json", action="store_true", help="emit a machine status snapshot"),
              accepts_path=True, session_required=False)
    model_cmd("impact", "show what a change to given topics would reach; no topics = full map",
              _cmd_impact, lambda sp: sp.add_argument("slots", nargs="*",
              help="slot ids or label words (e.g. permissions workflow); omit for the full map"),
              accepts_path=True, session_required=False)
    model_cmd("brief", "generate the decision brief — what to review before estimating (API)",
              _generator_verb("brief"))
    model_cmd("gtm_plan", "generate go-to-market's one artifact (API)", _generator_verb("gtm_plan"))
    model_cmd("prd", "generate the PRD (API)", _generator_verb("prd"))
    model_cmd("stories", "derive user stories (API)", _generator_verb("stories"))
    model_cmd("estimate", "derive stories and estimate them, in day ranges (API)",
              _generator_verb("estimate"))
    model_cmd("criteria", "generate Given/When/Then acceptance criteria (API)",
              _generator_verb("criteria"))

    def epic_flags(sp):
        # Three export flags of one kind. `--export-json` was `--json` until #83, where `app()`'s
        # generic `getattr(args, "json")` switched failure reporting for it alone. Do NOT add a stdout `--json`.
        sp.add_argument("--export-json", action="store_true",
                        help="also write the neutral epic.json export")
        sp.add_argument("--github", action="store_true", help="also write a GitHub issue-creation plan")
        sp.add_argument("--gitlab", action="store_true", help="also write a GitLab issue-creation plan")

    model_cmd("epic", "generate the delivery epic, plus optional tracker plans (API)",
              _generator_verb("epic"), epic_flags)
    model_cmd("release", "generate client-facing release notes (API)", _generator_verb("release"),
              lambda sp: sp.add_argument("version", nargs="?", default="", help="optional version label to stamp"))

    docs = sub.add_parser(
        "docs", help="menu of the seven documents the model can produce, or generate the ones "
                     "you name (API)")
    # One `nargs="*"` positional (#544): `_cmd_docs` disambiguates the way `run` does.
    docs.add_argument("args", nargs="*", metavar="[slug] [type ...]",
                      help="an optional session slug, then document types to generate; omit the "
                           "types to see the menu (omit the slug too for the workspace's default "
                           "session)")
    docs.add_argument("--all", action="store_true", help="generate every document, skipping the menu")
    docs.set_defaults(func=_cmd_docs)

    # The deterministic surface, registered after the journey verbs (#244); `register` composes
    # its four halves at import, so a module that stops registering is an ImportError.
    register_deterministic(sub)

    # `web` is a surface, not a step: beside the plumbing.
    web = sub.add_parser("web", help="launch the local single-user web interface (needs the [web] extra)")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, localhost only)")
    web.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    # SUPPRESS so an absent `web --workspace` does not overwrite a global one; reads `_WORKSPACE_HELP`
    # like every copy (#249): `test_every_workspace_copy_carries_the_same_help_text`.
    web.add_argument("--workspace", metavar="DIR", default=argparse.SUPPRESS, help=_WORKSPACE_HELP)
    web.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    web.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    web.set_defaults(func=_cmd_web)

    # Beside `web`, a surface; a group so `serve` is not the last verb it grows (#425).
    api = sub.add_parser("api", help="the local HTTP API (needs the [api] extra)")
    api_sub = api.add_subparsers(dest="api_command", required=True, metavar="<command>")
    serve = api_sub.add_parser(
        "serve", help="serve the local HTTP API -- experimental; needs the [api] extra")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: 127.0.0.1, localhost only; anything else "
                            "requires REQUIVO_API_TOKEN)")
    serve.add_argument("--port", type=int, default=8767, help="port (default: 8767)")
    serve.add_argument("--workspace", metavar="DIR", default=argparse.SUPPRESS, help=_WORKSPACE_HELP)
    serve.set_defaults(func=_cmd_api_serve)

    # Last, after every verb group has registered: a global flag is global wherever it is written.
    _accept_workspace_after_the_command(p)
    return p


# One string bound to every copy of the flag (#249).
_WORKSPACE_HELP = ("workspace root for sessions (default: cwd). Sessions live in "
                   "<workspace>/.requivo/sessions/. Accepted before or after the command.")


def _accept_workspace_after_the_command(parser: argparse.ArgumentParser) -> None:
    """Re-declare `--workspace` on every subparser, at every depth, so its position stops mattering.

    `default=argparse.SUPPRESS` on every copy is the whole of the correctness: a subparser's namespace
    is copied onto the parent's, so a `None` default would clobber a global `--workspace DIR`.
    `test_workspace_parses_identically_before_and_after_the_command`. Walked through
    `_actions`/`_SubParsersAction` (argparse enumerates nothing publicly), as `test_cli_flag_names.py` has since #72."""
    seen: set[int] = set()

    def walk(p: argparse.ArgumentParser) -> None:
        for action in p._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            # `.choices` maps every alias to one parser; adding the option twice is an argparse conflict.
            for sp in action.choices.values():
                if id(sp) in seen:
                    continue
                seen.add(id(sp))
                if not any("--workspace" in a.option_strings for a in sp._actions):
                    sp.add_argument("--workspace", metavar="DIR", default=argparse.SUPPRESS,
                                    help=_WORKSPACE_HELP)
                walk(sp)

    walk(parser)


def app(argv: list[str] | None = None, client=None) -> None:
    """Entry point for the `requivo` command (and `python -m requivo`)."""
    # Before anything prints (#29), and never at import: importing `requivo` must not reconfigure streams.
    configure_streams()
    # `.env` per run, never at import (#419, `test_importing_the_cli_leaves_the_environment_alone`).
    load_dotenv()
    args = _build_parser().parse_args(argv)
    # A global --workspace redirects where sessions are read/written, for the duration of this run.
    if getattr(args, "workspace", None):
        os.environ["REQUIVO_WORKSPACE"] = args.workspace
    want_json = getattr(args, "json", False)
    # The run's API footprint, printed after the command; offline verbs leave the ledger empty.
    with track_usage() as ledger:
        try:
            args.func(args, client)
        except RequivoError as e:
            # Every clean failure surfaces without a traceback; `--json` gets the structured envelope.
            _render_usage_safely(ledger)
            if want_json:
                print_json(e.to_dict())
            else:
                safe_write(sys.stderr, f"\n{e}\n")
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            # Ctrl-C ends here for every command: no traceback, the spend so far, exit 130 (#206).
            # `test_a_top_level_interrupt_on_an_existing_session_exits_130_with_no_traceback`.
            _render_usage_safely(ledger)
            safe_write(sys.stderr, "\nInterrupted.\n")
            raise SystemExit(EXIT_INTERRUPTED) from None
        except UnicodeEncodeError as e:
            # A `UnicodeEncodeError` from a `print` means the work landed: say so, and whether it was billed.
            # `test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback`,
            # `test_the_render_failure_message_does_say_so_when_a_call_was_billed`. Narrow on purpose.
            _render_usage_safely(ledger)
            paid = bool(getattr(ledger, "calls", None))
            safe_write(sys.stderr, _RENDER_FAILED_HEAD.format(error=e)
                       + (_RENDER_FAILED_PAID if paid else _RENDER_FAILED_UNPAID)
                       + _RENDER_FAILED_TAIL)
            raise SystemExit(EXIT_RENDER_FAILED) from None
    # Outside the `with`, so outside the arm above: the safe wrapper keeps #29 out of the success path.
    _render_usage_safely(ledger)
