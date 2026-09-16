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
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter, resolve_perimeter
from requivo.core.persistence import load_model
from requivo.core.selectors import display_document, display_text, display_token
from requivo.deterministic import is_file_argument, print_json, read_source
from requivo.deterministic import register as register_deterministic
from requivo.paths import DEMO

# The only two names this surface takes from `requivo.providers`, and each is a *surface* concern
# rather than an orchestration one: `new_client` builds the SDK client that gets handed to
# DiscoveryService, and `EngineError` is an exception type the top-level handler catches — neither
# reasons about anything. `track_usage` was a third until #167 and is no longer a provider name at
# all: the ledger is provider-neutral and lives in `requivo.usage`.
#
# `run`, `advise` and `estimate` used to be here too, and that was #77: the interactive `discover`
# branch drove two provider calls of its own before letting the service do the write, so the primary
# surface held a second orchestration of discovery while CLAUDE.md, the README and
# docs/architecture.md all said it did not. `tests/test_boundaries.py` guards the list now, in both
# directions — an unexpected import fails, and so does an entry here that nothing imports.
from requivo.providers.anthropic import new_client
from requivo.providers.errors import EngineError
from requivo.render.markdown import criteria_markdown, epic_markdown, gtm_brief_markdown, prd_markdown, release_markdown
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

# How many questions an interactive loop asks before compiling the answers and buying the next turn.
# Not `MAX_QUESTIONS`, which is the contract ceiling on how many a turn may *return*: the engine
# returns up to that many, ordered by information value, and the loop asks the leading few. The
# remainder is dropped rather than carried, because a proposal replaces `questions` wholesale
# (invariant 10) and the next turn re-derives them against the updated model -- carrying one forward
# would re-ask what the model has since learned. Cost of letting this exceed the cap: the window
# silently means "ask them all" and a turn is a wall of questions again (#592). Guarded by
# `test_the_checkpoint_window_fits_inside_the_contract_cap`.
QUESTIONS_PER_CHECKPOINT = 4

# A command whose *work* succeeded and whose *report* could not be encoded. Distinct from 1 (a clean,
# expected failure) so a script can tell "nothing happened" from "it happened and you cannot see it";
# see the `UnicodeEncodeError` arm in `app()` and `streams.py` for why the distinction is the point.
EXIT_RENDER_FAILED = 3

# The conventional SIGINT code (128 + signal 2), and the one condition it is reserved for: the
# operator pressed Ctrl-C. Distinct from 1 on purpose -- 1 already means "a clean, expected failure",
# and a script gating on it should be able to tell "the provider refused this" from "somebody stopped
# the run themselves" (#206). Before this, an interrupt that escaped every local handler was an
# unhandled Python exception -- a traceback, and whichever exit code the interpreter happens to give
# one, which was never a documented promise. Added beside EXIT_RENDER_FAILED under the rule
# `test_the_degraded_code_collides_with_nothing` already enforces for 4: nothing else may claim this
# number, and that test is what stops it happening by accident.
EXIT_INTERRUPTED = 130

# Two messages, because this arm cannot see how far the handler got and must not pretend it can.
# `app()` wraps the whole handler, and several verbs print before they mutate anything -- `discover`
# echoes its context cards before the provider call, and `doctor`/`status`/`schema` never mutate at
# all. A single message asserting "whatever this changed HAS been applied" is therefore false about
# roughly half the verbs, which is the same misreporting this branch exists to remove, one layer up.
#
# What the arm *can* see is the usage ledger: a non-empty one means a provider call completed and was
# billed. That is the fact worth being precise about, so it is read rather than assumed.
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
    """What the drafting loop came back with: the model, and whether the *user* ended it.

    Two outcomes that used to be one value and must not be: a converged or turn-limited loop should
    go on to the decision brief, a user-stopped one should not, since the brief is a paid call they
    did not ask for. Returning `None` for the second collapsed them into "nothing to do" and
    discarded the turns as well. `model` is `None` only when the very first turn produced nothing to
    stop *from*. Pinned by `test_stopping_early_stops_reasoning_and_says_so`.
    """

    model: EngineOutput | None
    stopped: bool


class DraftingFailed(Exception):
    """A provider failure (or a Ctrl-C) *during* a draft turn, carrying the work that survived it.

    `converse` drafts up to eight paid turns in memory and writes none of them — deliberate, because
    what is drafted becomes real through the one validated apply path. The cost of that design is that
    an exception anywhere in the loop used to discard every prior turn **and** every answer the user
    typed, leaving the session at revision 0 and printing a transport message that named neither the
    session nor a way back. One transient 529 on turn 8 threw away seven paid calls and ten minutes of
    typing (#202).

    So the loop stops raising the transport error directly. It raises this, which carries the last
    model that succeeded, and `_cmd_discover` persists that model before letting the failure surface.
    `last` is None only when the very first turn failed, where there is genuinely nothing to save.
    Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`.
    """

    def __init__(self, cause: BaseException, last: EngineOutput | None, turn: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.last = last
        self.turn = turn


def converse(disco: DiscoveryService, request: str, only: list[str] | None = None,
            perimeter: str = "software") -> Drafted:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns a `Drafted`: the model, and whether the *user* ended the loop. Never `None` and never a
    bare model — a caller has to read `.stopped`, since a `Drafted` is truthy either way and the old
    `if not converse(...)` idiom would be silently wrong. Finalization (brief, save) is handled by
    the caller so the interactive and --from paths share it. `only` restricts the context cards for
    every turn — held constant across the loop so the cached system prefix survives. Pinned by
    `test_stopping_early_keeps_the_turns_it_paid_for`.

    This is the CLI's job and all of it: prompting, rendering, and deciding when to stop. The
    reasoning is `DiscoveryService.draft_turn`, so the loop holds no provider client of its own and a
    second interactive surface reuses the same operation instead of copying this function. Pinned by
    `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns`.

    **The model is the state that is carried, not a transcript.** Each turn hands back the model so
    far plus the answers just given — the same shape `requivo answer` and the Web form already use,
    so there is one turn operation across every surface rather than a conversational one here and a
    stateless one everywhere else. Turns 1 and 2 send exactly what the old in-CLI loop sent; from
    turn 3 the earlier rounds of question-and-answer are no longer re-sent, because the evidence they
    produced is in the model being carried. Pinned by
    `test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript`."""
    out = None
    answers = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        try:
            out = disco.draft_turn(request, current_model=out, answers=answers, cards=only,
                                   perimeter=perimeter)
        except (RequivoError, KeyboardInterrupt) as e:
            # `RequivoError`, not `EngineError`: `ProviderOutputError` (the JSON retry loop giving
            # up) is a `RequivoError` sibling of `EngineError`, not a subclass of it, and used to
            # reach `app()` as a bare, un-rescued failure with every turn already drafted lost.
            # `out` still holds the last turn that succeeded, because the failed assignment did not
            # land, and handing it to the caller is the difference between a transient failure
            # costing one turn and it costing all of them. Pinned by
            # `test_a_provider_output_failure_mid_turn_also_names_the_claimed_session`.
            raise DraftingFailed(e, out, turn) from e
        # The checkpoint, and the questions are *not* printed here: `_prompt_answers` asks them one
        # at a time below. A turn boundary and a checkpoint are the same event, so the cadence the
        # user sees is `QUESTIONS_PER_CHECKPOINT` questions and then this (#592).
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
    """Prompt for one turn's answers at the terminal, folding them into the
    `[slot: ...] Q: ... → A: ...` shape both `converse()` and `run`'s resume loop send back to the
    provider. `None` means the user stopped -- quit, Ctrl-C/EOF, or answered nothing -- and this
    already printed why. Extracted from `converse()` (#540) so a second loop over an existing
    session does not reimplement the terminal side of a turn.

    Only the leading `QUESTIONS_PER_CHECKPOINT` are asked, one per prompt; the rest of the turn's
    questions are dropped rather than carried, for the reason stated at that constant (#592)."""
    asked = questions[:QUESTIONS_PER_CHECKPOINT]
    print("\nEnter skips a question · 'q' stops.")
    replies = []
    try:
        for i, q in enumerate(asked, 1):
            # `q.q` is LLM-authored prose over an untrusted client request (SECURITY.md), and this
            # is now the **only** place the interactive path neutralizes it: `render_turn` used to
            # escape the same field one call earlier, and the loops render `render_turn_state`
            # instead since #592, which prints no question at all. What used to be the second
            # interpretation site invariant 14 warns about is the first and last one.
            # `display_text` escapes embedded control characters per character rather than dropping
            # them, so a multi-line forged question becomes one long readable line with a visible
            # `\n` instead of writing a second line at column 0 that `input()`'s prompt cannot own.
            # Reproduced through this loop, not through a renderer, by
            # `test_a_forged_question_cannot_write_a_line_at_column_zero_of_the_input_prompt`
            # (#330); the readability half is `test_an_ordinary_question_still_reads_at_the_input_prompt`.
            safe_q = display_text(q.q)
            # One question per prompt, and the slot label with it -- `render_turn`'s block used to
            # carry that line and the interactive loops no longer print the block (#592).
            ans = input(
                f"\n  [{i}/{len(asked)}] {safe_q}\n"
                f"        ({slot_label(q.slot, perimeter)})\n"
                f"      > "
            ).strip()
            if ans.lower() == "q":
                print("Stopped.")
                return None
            if ans:
                # Same field folded back into the transcript sent to the provider -- an embedded
                # newline would break the `[slot: ...] Q: ... → A: ...` structure the next turn
                # reads. Pinned by
                # `test_a_forged_question_cannot_break_the_answer_folded_back_to_the_provider`.
                replies.append(f"[slot: {q.slot}] Q: {safe_q} → A: {ans}")
    except (EOFError, KeyboardInterrupt):
        print("\nStopped.")
        return None

    if not replies:
        print("No answer provided — stopping.")
        return None

    return "\n".join(replies)


# ── Subcommand CLI (`requivo`) ────────────────────────────────────────────────
# The modern surface. A thin layer over the same core: each handler parses, calls
# the services, renders, writes — no business logic here.
# `app()` takes an optional client so tests can inject a stub; only verbs that hit
# the API build one, so `requivo status` runs fully offline.


def _why(e: BaseException) -> str:
    """What to print for a failure that may be a structured error or a bare interrupt. A
    `KeyboardInterrupt` stringifies to the empty string, so it needs a word of its own rather than a
    sentence that trails off into nothing (#320)."""
    return "interrupted" if isinstance(e, KeyboardInterrupt) else str(e)


def _say_saved(slug: str) -> None:
    """Where the session landed. `canonical_dir` direct, and justified (#76): the path *is* the
    answer, and `SessionRepository` exposes none because a non-file backing has none. Same at the
    three other display sites in this file and in `deterministic/sessions/`."""
    print(f"\nSaved session → {store.canonical_dir(slug)}")


def _say_nothing_drafted(slug: str) -> None:
    """The one case with genuinely nothing to salvage: a session was claimed and the paid call that
    would have drafted its first turn never returned. Shared by `_rescue_drafted`'s first-turn
    failure and `_cmd_discover`'s quick (`--once`/non-tty) path, which claims a session and makes its
    one paid call the same way `converse()`'s loop does but has no loop of its own to fail mid-turn
    (#206) -- both land here rather than duplicating the two lines."""
    print(f"\nSaved request → {store.canonical_dir(slug)}", file=sys.stderr)
    print("Nothing was drafted, so the session is unchanged — re-run `requivo discover` to try "
          "again.", file=sys.stderr)


def _rescue_drafted(disco, request: str, e: DraftingFailed, *, cards, slug: str,
                    perimeter: str = "software"):
    """Persist what an interrupted drafting loop had already paid for, then let the failure surface.

    Every abort path after `claim_session` must name the session, and this one must also *keep* the
    work: the turns that succeeded are in `e.last`, because the model is what the loop carries. The
    user retries with `requivo answer`, which works from any revision >= 1, instead of restarting a
    conversation they already had at full price (#202).

    Turn 1 failing is the one case with nothing to save; the session stays at revision 0 and
    `requivo discover` is still the right retry, so it says so rather than pointing at `answer`.
    Never returns. Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`."""
    if e.last is None:
        _say_nothing_drafted(slug)
    else:
        kept = e.turn - 1
        # **The save is guarded, because this is the code path whose entire job is keeping the work**
        # (#320). `finalize_discovery` re-runs the revision-zero gate and then writes; either can
        # fail, and unguarded it propagated *before* the lines below ran — so the user was shown a
        # revision conflict instead of the provider error that actually stopped them, and was told
        # nothing about whether their turns had been kept. A rescue that fails silently about its own
        # failure is worse than no rescue. Pinned by
        # `test_a_rescue_that_cannot_save_says_so_and_still_names_the_original_failure`.
        try:
            slug = disco.finalize_discovery(request, e.last, cards=cards, slug=slug,
                                            brief=None, surface="cli-discover", perimeter=perimeter)
        except (RequivoError, OSError, KeyboardInterrupt) as save_failed:
            # `KeyboardInterrupt` added here in review of #206: a second Ctrl-C landing on the
            # rescue's own save used to propagate bare and silent past this except -- no message at
            # all, in the one function this diff rewrote to promise every abort path names what was
            # kept. `_why()`, not the bare object, for the same reason as everywhere else in this
            # file: a `KeyboardInterrupt` stringifies to `''`.
            print(f"\nTurn {e.turn} failed, and the {kept} turn(s) before it could NOT be saved: "
                  f"{_why(save_failed)}", file=sys.stderr)
            print(f"The request is still captured at {store.canonical_dir(slug)}.", file=sys.stderr)
            print(f"The failure that stopped the run was: {_why(e.cause)}", file=sys.stderr)
            if isinstance(save_failed, KeyboardInterrupt):
                # Re-raised bare, like every other interrupt in this file, so `app()`'s handler
                # assigns the one exit code they all share (130) instead of this branch inventing a
                # second one for `RequivoError`/`OSError` to keep.
                raise
            raise SystemExit(1) from save_failed
        print(f"\nTurn {e.turn} failed, so the {kept} turn(s) before it were saved rather than "
              f"discarded.", file=sys.stderr)
        print(f"Saved session → {store.canonical_dir(slug)}", file=sys.stderr)
        print(f'Continue where you left off with:\n  requivo answer {slug} "<your answers>"',
              file=sys.stderr)
    # Re-raised rather than wrapped: `app()` is the one place that decides the final exit code and
    # prints the generic "Interrupted."/usage-summary tail for every command, discover included
    # (#206). This function's job stops at naming what was kept and how to continue -- a
    # `RequivoError` reaches `app()`'s own handler for that, and so, since #206, does a bare
    # `KeyboardInterrupt`, which used to have nowhere to land and was wrapped in `SystemExit(1)` here
    # instead: exit 1, the code for "a clean, expected failure", on the one condition that has its
    # own conventional code and is not that.
    raise e.cause


# The three shapes `discover`'s argument can take, said once so the two refusals below cannot
# describe different products. `-` is named here because a message that lists two of three ways in
# is how a reader concludes the third does not exist (#360).
_REQUEST_SHAPES = ("a sentence describing what to build, a path to a file containing one, or '-' to "
                   "read one from stdin.")


def _cmd_discover(a, client) -> None:
    # `getattr`, not `a.perimeter` directly: `run`'s subparser reuses this function (`a.request =
    # ref; _cmd_discover(a, client)`) and does not itself define `--perimeter` (#608) -- a namespace
    # without the attribute means "no explicit choice", the same default the flag itself carries.
    perimeter = getattr(a, "perimeter", None) or "software"
    if not a.request or not a.request.strip():
        print(f"discover needs a request: {_REQUEST_SHAPES}", file=sys.stderr)
        raise SystemExit(2)
    client = client or new_client()
    # `a.request != "-"` first, so this agrees with where `read_source` below actually reads from.
    # `is_file_argument("-")` is True when a file literally named `-` exists in the working
    # directory -- and `read_source` reads stdin for `-` regardless -- so computing the two
    # independently would derive the slug hint from a file whose content was never used. Narrow, and
    # introduced by this very diff: before it, `-` was never stdin and the two agreed by
    # construction. Found in review of #360. Pinned by
    # `test_a_dash_is_stdin_even_when_a_file_of_that_name_exists`.
    is_file = a.request != "-" and is_file_argument(a.request)
    # `read_source`, the shared reader, and not `read_user_text` alone: the argument has *three*
    # shapes, not two, and the third is `-` (#360). `is_file_argument("-")` is False -- correctly,
    # `-` is not a file -- so reading the file case here and letting everything else fall through as
    # literal text meant `requivo discover -` discovered on the two characters `-`, silently and at
    # full price, while `session init -`, `model apply <slug> -` and `artifact save --file -` all
    # read stdin. Pinned by `test_discover_reads_the_request_from_stdin_when_the_argument_is_a_dash`
    # and, for the half a pipe-sniffing fix would break,
    # `test_a_one_character_request_that_is_not_a_dash_is_still_literal_text`.
    request = read_source(a.request)
    if not request.strip():
        # The blank-argument refusal above cannot see this one: `-` and a path are both perfectly
        # good arguments whose *contents* turn out to be empty, and discovering a product from
        # nothing is the same non-answer either way. Same code, same sentence, one noun different --
        # a separate exit code here would be a code per condition rather than per shape of answer.
        source = "stdin" if a.request == "-" else "that file"
        print(f"discover needs a request and {source} is empty: {_REQUEST_SHAPES}", file=sys.stderr)
        raise SystemExit(2)

    # One resolver, in Core, shared with the deterministic verbs and the Web: an unknown card is a hard
    # error. This used to warn and carry on with `only = None`, which does not mean "the cards you
    # named minus the typo" — it means *every* card. A misspelling widened the context instead of
    # narrowing it, and the run looked like it had honoured the selection.
    only = resolve_cards(a.context.split(",")) if a.context else None
    if only:
        print(f"Context cards: {', '.join(only)}")
    else:
        # #257: the default is *every* installed card, and CLAUDE.md's own "Known limit" note
        # already names that as the most expensive and most diluted path -- adding a card dilutes
        # its neighbours, and every card adds prompt weight on every call. Nothing said which cards
        # that was before this turn spent money reasoning over them, so name them here, before the
        # paid call -- disclosure only: `only` stays `None`, so nothing about which cards get loaded
        # changes (see docs/context-cards.md for the measured per-card cost).
        all_cards = available_cards()
        if all_cards:
            avg_bytes = average_card_byte_size()
            weight = f"~{avg_bytes:,} bytes each" if avg_bytes else "measurable weight"
            print(f"Context cards: all {len(all_cards)} ({', '.join(all_cards)}) — no --context "
                  f"given. Each card adds {weight} to every prompt and dilutes the others; narrow "
                  "with --context/--cards if this request is about one product area "
                  "(see docs/context-cards.md).")

    # Discovery orchestration (run the provider → apply through the validated path) lives in the shared
    # DiscoveryService, so the Web drives the exact same pipeline — the CLI only owns the interactive
    # TTY loop and the rendering.
    disco = DiscoveryService(client=client)
    # A filename is a *suggestion* for the slug, not a slug: "Leave Approval v2.md" has a space and a
    # capital, and a slug names a directory under the session store, so it is validated strictly.
    # Passing the raw stem through turned a perfectly ordinary input file into an invalid_slug error.
    slug_hint = SessionService.slug_hint(Path(a.request).stem) if is_file else None
    quick = a.once or not sys.stdin.isatty()
    if quick:
        # Claimed here, ahead of `start()`'s own internal claim, purely so the `except` below has a
        # slug to name. `claim_session` is idempotent and makes no provider call (its own docstring
        # says so), so claiming it a second time inside `start()` right after costs nothing. `start()`
        # then makes exactly one paid call -- and until #206 an abort inside it had no handler of its
        # own: the session was already claimed and on disk, and the traceback that reached the
        # operator never said so.
        # Claim, judge, and re-claim if the verdict narrows -- one service call, because the
        # re-claim deletes a session and a destructive step does not get two implementations (#593).
        # `only` is rebound: the narrowed selection is what the turn must reason with, or the
        # session would record cards it never read.
        meta, grounding, only = disco.claim_and_ground(request, cards=only, slug=slug_hint,
                                                        perimeter=perimeter)
        render_context_judgment(grounding)
        try:
            slug = disco.start(request, cards=only, slug=meta.slug, finalize=False,
                               surface="cli-discover", perimeter=perimeter)
        except (RequivoError, KeyboardInterrupt):
            # `RequivoError`, not `EngineError`: the identical gap as `converse()`'s own catch above,
            # found in review of this diff -- `ProviderOutputError` is a `RequivoError` sibling of
            # `EngineError`, not a subclass, so it slipped straight past this except and reached
            # `app()`'s generic handler with the claimed session unnamed.
            _say_nothing_drafted(meta.slug)
            raise
        out = disco.sessions.load_model(slug)
        render_turn(out, perimeter)
        _say_saved(slug)
        if out.questions:
            print(f'\n→ Answer and refine: requivo answer {slug} "<your answers>"')
        return

    # Invariant 13's gate, here rather than only inside `finalize_discovery`: refusing after the
    # loop meant paying for up to nine provider calls first (#133). Pinned by
    # `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.
    meta, grounding, only = disco.claim_and_ground(request, cards=only, slug=slug_hint,
                                                    perimeter=perimeter)
    slug = meta.slug
    render_context_judgment(grounding)
    try:
        drafted = converse(disco, request, only=only, perimeter=perimeter)
    except DraftingFailed as e:
        _rescue_drafted(disco, request, e, cards=only, slug=slug, perimeter=perimeter)
    out = drafted.model
    if out is None:
        # Unreachable while `MAX_TURNS >= 1`, because the loop drafts before it ever prompts: a stop
        # always has a turn to stop *from*, and a turn that failed leaves through `DraftingFailed`
        # instead. It is here as the narrowing rather than as a user path — so that lowering the
        # bound, or adding an earlier exit, cannot hand `finalize_discovery` a None. Claiming first
        # means the request is captured at revision 0 either way, so it still says where it went.
        print(f"\nSaved request → {store.canonical_dir(slug)}")
        return
    if drafted.stopped:
        # **A deliberate stop keeps what it paid for, and does not buy a brief nobody asked for**
        # (#202). Stopping used to discard every drafted turn and leave revision 0, which is the same
        # loss as a failed turn wearing a friendlier word: the model is what the loop carries, so one
        # `q` at turn 5 threw away four billed calls and every answer typed into them.
        #
        # What this lands is exactly what `--once` lands — revision 1, questions still open, and
        # `requivo answer` named — so the two entry points now leave the same shape of session rather
        # than two. Re-running `discover` on the request is then refused by invariant 13's gate, and
        # that refusal already names both ways on: refine with `answer`, or use another slug.
        # Pinned by `test_stopping_early_keeps_the_turns_it_paid_for`.
        slug = disco.finalize_discovery(request, out, cards=only, slug=slug,
                                        brief=None, surface="cli-discover", perimeter=perimeter)
        _say_saved(slug)
        print(f'\n→ Answer and refine: requivo answer {slug} "<your answers>"')
        return

    # **The write comes before the last paid call, and that ordering is the fix** (#202). The
    # assessment used to be reasoned first and passed into `finalize_discovery`, so an `EngineError`
    # on that ninth call discarded all eight drafted turns along with it. Persisting first costs
    # nothing — `generate(slug, "brief")` reads the session back, absorbs the assessment's reasoning
    # as a revision of its own and saves the document, which is the same path every other surface
    # takes — and it turns the worst failure in the product into one retryable call. Pinned by
    # `test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry`.
    slug = disco.finalize_discovery(request, out, cards=only, slug=slug,
                                    brief=None, surface="cli-discover", perimeter=perimeter)
    _say_saved(slug)
    # A perimeter with no "brief" generator (go-to-market, #609's own scope) has nothing to finish
    # with: generate() would raise past the discovery already saved above. Pinned by
    # `test_a_finished_go_to_market_discovery_ends_with_the_saved_session_not_a_traceback`.
    if "brief" not in get_perimeter(perimeter).artifact_types:
        out = disco.sessions.load_model(slug)
        render_turn(out, perimeter)
        return
    print("\nGenerating the decision brief…")
    try:
        gen = disco.generate(slug, "brief", surface="cli-discover")
    except (RequivoError, KeyboardInterrupt) as e:
        # **`KeyboardInterrupt` belongs here and was missing** (#320). `except RequivoError` cannot
        # catch it, and this is the one remaining multi-second provider call in the verb — the very
        # call #202 moved *because* it is the expensive one to land on. So the fix that made an
        # interrupt survivable inside `converse` left it a raw traceback on the call most likely to
        # be interrupted, while the changelog said otherwise. Pinned by
        # `test_an_interrupt_during_the_brief_reports_the_saved_session`.
        print(f"\nThe decision brief did not complete: {_why(e)}", file=sys.stderr)
        print(f"Your discovery is saved and nothing was lost — retry just this step with:\n"
              f"  requivo brief {slug}", file=sys.stderr)
        # Re-raised, not wrapped: `app()` decides the final exit code -- 1 for the `RequivoError`,
        # 130 for the bare interrupt (#206), both with the usage summary it prints for every command.
        raise
    # `gen.model`, not `out`: the assessment's reasoning has been absorbed into the model as a
    # revision by now, so this renders what was actually saved rather than the pre-absorption copy.
    render_brief(gen.model, gen.artifact)


def _cmd_answer(a, client) -> None:
    # Same shared orchestration as the Web: DiscoveryService folds the answers in and applies the
    # refined model through the validated path (diff → revision → stale-flag).
    disco = DiscoveryService(client=client)
    svc = disco.sessions
    # `accept_path=False`: this verb writes a revision back into a session and never opens a file
    # it is handed, so a model.json path was never a meaningful input (#402).
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
    # Every invalidated collection is counted here, or a change that unseats only one kind of
    # reasoning reports nothing on this path while `impact`, --json and the Web all report it — the
    # half-registered shape invariant 1 fails through. Guarded by
    # test_an_exclusion_only_invalidation_is_still_announced_on_the_apply_path.
    parts = [(result.invalidated_decisions, "decision(s)"),
             (result.invalidated_challenges, "premise(s)"),
             (result.invalidated_exclusions, "exclusion(s)"),
             (result.invalidated_thresholds, "threshold(s)")]
    n_reasoning = sum(len(items) for items, _ in parts)
    reasoning_type = next((t for t in ("brief", "gtm_brief") if t in get_perimeter(perimeter).artifact_types), None)
    if n_reasoning and reasoning_type:
        breakdown = ", ".join(f"{len(items)} {noun}" for items, noun in parts if items)
        print(f"\n⚠  This change unseats {n_reasoning} piece(s) of the {_LABEL[reasoning_type]}'s reasoning ({breakdown}) — regenerate with `requivo {reasoning_type} {slug}`.")
    print(f"\nSaved session → {store.canonical_dir(slug)}")
    if out.questions:
        print(f'\n→ Keep going: requivo answer {slug} "<your answers>"')
    else:
        print(f"\n✅ Discovery converged — run `requivo {reasoning_type} {slug}` for the {_LABEL[reasoning_type]}." if reasoning_type else "\n✅ Discovery converged.")


def _is_existing_session(svc: SessionService, ref: str) -> bool:
    """Whether `ref` already names a session -- the branch `run` needs between resuming and
    discovering (#540). A request sentence is not a valid slug shape, so the resulting
    `InvalidSlugError` fails closed to "no" rather than escaping this check."""
    try:
        return svc.exists(ref)
    except RequivoError:
        return False


def _prompt_for_request() -> str:
    """`run` with nothing to resume: ask for a request the same three shapes `discover` accepts."""
    print(f"No session to resume yet. What would you like to build? ({_REQUEST_SHAPES})")
    try:
        return input("> ")
    except EOFError:
        return ""


def _run_target(svc: SessionService) -> tuple[str, bool]:
    """`run` with no argument: resolve the workspace's default session (#541) -- listing the
    candidates when there are several -- or prompt for a request when none exists at all (#540).

    Returns `(value, resume)`: `resume` is True only when `value` came straight from the resolver,
    so `_cmd_run` resumes it directly instead of re-running it through file/session detection --
    where a file in the cwd sharing the resolved slug's name would hijack it into an unrelated
    discovery. Found in review; pinned by
    `test_run_with_no_argument_is_not_hijacked_by_a_same_named_file`."""
    try:
        resolution = svc.resolve_default_session()
    except SessionNotFoundError:
        return _prompt_for_request(), False
    if resolution.candidates:
        _print_session_candidates(resolution)
    return resolution.default, True


def _refuse_resume_only_flags(a) -> None:
    """`--once`/`--context` describe a *new* discovery; on a resume they used to be silently
    ignored, so refuse before any provider call rather than discard what the user asked for --
    found in review. Pinned by `test_run_refuses_once_and_context_when_resuming`."""
    if a.once or a.context:
        raise RequivoError(
            "requivo run <slug>: --once and --context apply to a new discovery, not to resuming an "
            "existing session, which reuses the session's own context cards. Drop them, or discover "
            "a fresh session with `requivo discover`/`requivo run <request>`.")


def _resume_run(disco: DiscoveryService, slug: str) -> None:
    """`run <slug>`'s loop on an already-discovered session (#540): each turn folds the answers in
    through `DiscoveryService.answer`, the path `requivo answer` already takes -- resuming is
    `answer` inside a loop, never a second discovery. Shares `_prompt_answers` with `converse()`
    rather than reimplementing the terminal side of a turn."""
    svc = disco.sessions
    perimeter = resolve_perimeter(svc.meta(slug).perimeter)
    out = svc.load_model(slug)
    for _turn in range(1, MAX_TURNS + 1):
        render_turn_state(out, perimeter)
        if not out.questions:
            if "brief" in get_perimeter(perimeter).artifact_types:
                print(f"\n✅ Discovery converged — run `requivo brief {slug}` for the decision brief.")
            else:
                print("\n✅ Discovery converged.")
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
    """`run [request | path | slug] [--context CARDS] [--once]` -- one verb over the conversation
    `discover` and `answer` already run (#538, #540). No argument resumes the workspace's default
    session or asks for a request; a request or a path is `discover`, unchanged; an existing
    session's slug resumes it through the *answer* path, never a second discovery."""
    svc = SessionService()
    ref = a.request
    if ref is None:
        # `resume=True` came straight off the resolver, not off `ref`'s own shape -- so it is
        # resumed directly and never re-checked against `is_file_argument`/`_is_existing_session`,
        # where a same-named file in the cwd would hijack a resolved slug into an unrelated
        # discovery. Pinned by `test_run_with_no_argument_is_not_hijacked_by_a_same_named_file`.
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
    """Resolve a reference to (model, slug). Accepts a model.json path (legacy or direct) OR a session
    slug in the canonical/legacy store — so the read verbs work both on a raw file and on a session.

    The refusal widens its noun and nothing else (#243): this is the one site that also accepts a
    path to a `model.json`, so a bare "no session" would name half of what it looked for. Everything
    after the noun is the shared message — the root searched, the listing verb, the workspace hint.
    """
    p = Path(ref)
    if p.is_file():
        return load_model(p), p.parent.name
    svc = SessionService()
    if svc.exists(ref):
        slug = svc.resolve_slug(ref)
        try:
            return svc.load_model(slug), slug
        except SessionNotFoundError:
            # `svc.exists(ref)` above already established that the session directory is real, so this
            # is not "no such session" -- it is the narrower "claimed but never discovered" case
            # `core/persistence/store.py:load_session_model` raises under the same `session_not_found` code
            # (#250). Reconstructed here with the CLI's own remedy rather than reworded at the source,
            # which is held by a concurrent lane this round; see the changelog fragment for #250.
            raise SessionNotFoundError(
                f"session '{slug}' has no model yet — only the request was captured. Run "
                f"`requivo discover` on the same request to analyse it (or, in Claude Code, "
                f"/requivo:discover).",
                details={"slug": slug},
            ) from None
    raise svc.no_session(ref, what="model file or session", details={"ref": ref})


def _status_payload(ref: str) -> tuple[EngineOutput, dict]:
    """(model, machine status). The model-derived view (readiness, understanding, questions, summary,
    gaps) comes from the shared `model_status` projection — the same one `SessionService.status` uses,
    so there is no second status implementation to drift. Revision, perimeter, context and artifact
    freshness are layered on when the reference resolves to a canonical session (a bare model.json
    has none)."""
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
    # `quiet=want_json`: the resolved `slug` already rides the JSON payload below, so a candidate
    # listing here would be the exact line beside a `--json` payload #246 already refuses (a
    # second such line, one call earlier) -- `_resolve_optional_session`'s own docstring.
    ref = _resolve_optional_session(SessionService(), a.session, quiet=want_json)
    out, payload = _status_payload(ref)
    if want_json:
        # `--json` deliberately gets no pointer (#246): a machine consumer picks its own next step,
        # and a line printed beside the payload would break every caller that pipes this into `jq`.
        # `print_json`, not a second `json.dumps(..., indent=2)` (#301): it carries the #70
        # `ensure_ascii` contract, and a call site duplicating the arguments has no way to inherit a
        # fix to it.
        print_json(payload)
        return
    render_turn(out, payload.get("perimeter") or DEFAULT_PERIMETER)
    # What the impact estimates above were scored against (#492). After the model rather than before
    # it, deliberately: the reader came here for where the session stands, and the grounding is what
    # they check that answer *against* -- it is evidence about the readout, not a preamble to it.
    render_grounding(payload.get("context_cards"))
    # A cumulative "what has this session cost so far" line, from the token/rate provenance stamped
    # onto each provider-backed revision (#292) -- silent when nothing on the session carries one, so
    # a bare model.json (no `slug` in the payload) or a session applied entirely through Claude Code
    # prints nothing rather than a misleading $0.00.
    slug = payload.get("slug")
    if slug:
        svc = SessionService()
        if svc.exists(slug):
            render_session_cost(svc.meta(slug).revisions)
    render_next_command(payload)


DEMO_SLUG = "event-checkin-reconciliation"
# The slot step ④ changes. Named here rather than inline because the prose above it describes this
# slot in words ("the six-week deadline"), and the two have to move together — `constraints` is where
# that deadline lives in the bundled model.
DEMO_CHANGED_SLOT = "constraints"
# Where the browsable copies live. A literal rather than a read of the package metadata: the demo
# prints this on a machine that may have no `importlib.metadata` entry for a `uv run` from a clone,
# and a closing pointer that can raise is worse than one that can go stale. `test_version_sites.py`
# is where a URL claim gets its guard.
EXAMPLES_URL = "https://github.com/jbkkz/requivo/tree/main/examples"


def _fenced_text(markdown: str) -> str:
    """Pull the terminal output back out of a saved assessment's ```text … ``` block, so the demo
    shows the clean assessment rather than the markdown wrapper. Falls back to the whole text."""
    m = re.search(r"```text\s*\n(.*?)```", markdown, re.DOTALL)
    return m.group(1).rstrip() if m else markdown.strip()


def _cmd_demo(a, client) -> None:
    """A no-API-key walkthrough of a real run, replayed from the saved event-check-in example.

    A visitor shouldn't need a key, a clone, and a venv before feeling what the product does. This
    renders the understanding + questions LIVE from the saved model (pure Python, proving the engine
    runs offline) and shows the assessment it produced — the differentiator — from disk. No network."""
    # Read from the frozen payload bundled in the package (so `requivo demo` works from a wheel, no clone),
    # but point the visitor at the browsable copy under examples/ at the repo root.
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

    # **The step the whole engine exists for, and the demo used to stop one beat short of it** (#223).
    # Steps ① to ③ are things a strong prompt can also do; this one is not, because it is not
    # reasoned. `propagate` walks the dependency graph the discovery recorded — the slots each
    # decision was `derived_from`, the slots each challenge `contests`, `ARTIFACT_SLOTS` — so the
    # same change yields the same list every time, which is exactly the promise a generated answer
    # cannot make. It is also free and offline, which is why it belongs in the keyless demo rather
    # than behind a key. Pinned by `test_the_demo_shows_the_computed_blast_radius_of_a_changed_answer`.
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
    # **A URL, because the README's own recommended installs are uvx and pipx** (#225). This block
    # used to prove its point with two `examples/<slug>/…` paths, which exist in a clone and nowhere
    # else — so the demo's closing evidence was two dead pointers for the majority install path. The
    # files themselves ship in the wheel; what a wheel user lacked was any way to reach them.
    # Pinned by `test_the_demo_points_a_wheel_install_at_something_it_can_reach`.
    print(f"     Readable in the repository, or beside this payload in the package:\n"
          f"       {EXAMPLES_URL}/{DEMO_SLUG}")
    # **A closing step a reader can take without a key** (#223). The demo's whole premise is that no
    # key is needed, and it used to end on the one command that requires one — so the visitor it was
    # written for had nothing to do next.
    print("\n  Keep going, still no API key:")
    print("    requivo web")
    print("        the browser interface, where a changed answer renders that block live")
    print(f"    requivo impact examples/{DEMO_SLUG}/model.json <slot>")
    print("        step ④ for any slot you name, from a clone of the repo")
    # The banner promises no key is needed; the one command here that needs one has to say so in the
    # same breath, or the demo converts a keyless reader into a failed command (#225).
    print('\n  With a key:   requivo discover "<your own request>"')
    print("                needs the [anthropic] extra and ANTHROPIC_API_KEY — `requivo doctor`")
    print("                checks both before you spend anything")
    print(bar)


def _cmd_impact(a, client) -> None:
    """Offline query over the dependency DAG — no API call. With slots, show their blast
    radius; without, map every slot's downstream."""
    svc = SessionService()
    ref = _resolve_optional_session(svc, a.session)
    out, slug = _resolve_ref(ref)
    # Decisions derived from thinner evidence than the session now holds (#493) -- a walk over the
    # frozen revisions, so only a session has it. A bare model.json is *not reviewed*, which the
    # renderer says in those words rather than as an empty section. Decided by the same predicate
    # `_resolve_ref` took the file branch on, never by `svc.exists(slug)`: the slug a file resolves
    # to is its parent directory's name, and a session of that name in the workspace is a different
    # model whose review would print as this file's. Pinned by
    # `test_a_loose_model_file_never_borrows_the_review_of_a_session_sharing_its_directory_name`.
    evidence = None if Path(ref).is_file() else svc.thinner_evidence(slug)
    # `perimeter` (#608): the session's own, or software for a bare model.json -- the same
    # fallback `_status_payload` uses, or a go-to-market model renders under the wrong vocabulary.
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
        # A wrong probe used to be indistinguishable from an empty result -- both exited 0 -- so a
        # script gating on the exit code alone could not tell "nothing downstream" from "you asked
        # about a slot that does not exist" (#250). Exit 1, not `EXIT_DEGRADED`: the *input* was
        # invalid, not the answer partial, and whatever did match is still rendered above in full.
        raise SystemExit(1)


# Provider-backed generators. Each resolves a session (slug or model.json path) and hands off to
# `DiscoveryService`, which is where reasoning, the revision lock, provenance and the artifact write
# actually happen — so a document asked for from the terminal is produced, saved and tracked exactly as
# the same document asked for from the browser or from Claude Code. The CLI's job here is to resolve
# the session, choose the terminal view, and say where the file went.
#
# One `_cmd_generate` over three per-type tables (#556), not seven near-identical bodies that had
# drifted unevenly under a shared fix before (`display_document`'s tab-preserving guard, #449). The
# seven `add_parser` calls below stay: they are the public verbs, each still with its own help text
# and flags (`epic`'s three export flags, `release`'s version positional).


def _generator_verb(type_: str) -> Callable[[argparse.Namespace, object], None]:
    """Bind `_cmd_generate` to one type, named `_cmd_<type>` so `args.func.__name__` still reads as
    the verb -- `test_pc_parser_binds_every_subcommand` asserts exactly that name. A real closure,
    not a `functools.partial`: pyright refuses `partial.__name__ = ...` outright
    (`reportAttributeAccessIssue` -- a `partial` object has no such attribute at the type level, even
    though CPython allows the assignment at runtime), and a plain function's `__name__` is an
    ordinary, type-checkable attribute."""
    def verb(a: argparse.Namespace, client) -> None:
        _cmd_generate(a, client, type_)
    verb.__name__ = f"_cmd_{type_}"
    return verb


# type → extra keyword arguments for `disco.generate(slug, type, surface=..., **here)`. Absent for
# the other five, which pass none.
_GENERATE_KWARGS: dict[str, Callable[[argparse.Namespace], dict]] = {
    # Two calls, one snapshot, two files against one revision, inside `generate()` since #519: the
    # estimate is read against the stories saved beside it (#135, invariant 6). `on_stories` prints
    # them the moment they're saved, before the second call is paid for. Pinned by
    # `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot` and
    # `test_the_estimate_verb_writes_both_files_and_still_prints_both_views`.
    "estimate": lambda a: {"on_stories": render_stories},
    "release": lambda a: {"version": a.version},
}


def _render_brief(slug: str, result) -> None:
    render_brief(result.model, result.artifact)


def _render_gtm_brief(slug: str, result) -> None:  # plain-document pattern, like every non-`brief` generator below
    print(display_document(gtm_brief_markdown(result.model, result.artifact)))


def _render_prd(slug: str, result) -> None:
    # `display_document`, not `display_text` (#449): a multi-paragraph document -- headings, lists, a
    # table -- whose newlines and tabs are its layout. Print time only; the string `_wrote` saves to
    # disk below is untouched, so the byte-identical-on-disk promise `core/integrity.py`'s hashing
    # rests on stays intact. Pinned by
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


# type → the terminal rendering for a freshly generated result, called before `_wrote` prints where
# the file went; `estimate` also writes the stories file it saved alongside itself, the same order
# the seven bodies this replaces used.
_RENDER: dict[str, Callable[[str, object], None]] = {
    "brief": _render_brief, "gtm_brief": _render_gtm_brief, "prd": _render_prd, "stories": _render_stories, "estimate": _render_estimate, "criteria": _render_criteria, "epic": _render_epic, "release": _render_release,
}

# type → the label `_wrote` prints. "Decision brief" is the caption a reader sees everywhere; the
# type, the verb and the file on disk stay `brief`/`solution-assessment.md` (#166) -- only the label
# lookup moved here.
_LABEL: dict[str, str] = {
    "brief": "decision brief", "gtm_brief": "go-to-market plan", "prd": "PRD", "stories": "user stories", "estimate": "estimate", "criteria": "acceptance criteria", "epic": "epic", "release": "release notes",
}


def _post_epic(a: argparse.Namespace, slug: str, result) -> None:
    # `write_artifact_file`, not `repo.save_artifact`: three extra, deliberately untracked *views* of
    # the one already-saved artifact -- no ArtifactService staleness row. `result.status.revision` is
    # the same stamp `epic.md` just saved against (invariant 12), so a reader can compare it to
    # `requivo status --json`'s `artifacts.epic.stale` for a freshness verdict. Pinned by
    # `test_pc_epic_export_stamps_the_same_revision_the_paired_epic_md_was_saved_against`.
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


# `docs` (#544): one verb over the seven generators above, never a second generation path -- every
# type it can produce loops through the same dispatch `requivo <type> <slug>` already calls.
_DOC_GENERATORS = {name: _generator_verb(name) for name in _LABEL}

_DOC_SELECTION_RE = re.compile(r"[,\s]+")


def _doc_generation_order(selected: list[str]) -> list[str]:
    """Canonical order; `estimate` absorbs `stories` (`generate(..., "estimate", ...)` already
    reasons and saves both, invariant 6), so picking both must not write stories twice. Pinned by
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
    """The menu's own prompt: numbers, names, `all`, or nothing to cancel. An unknown token is
    refused before any generator runs, never silently dropped (invariant 3). Pinned by
    `test_prompt_doc_selection_refuses_an_unknown_token_before_any_call`."""
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
    """`docs [slug] [type...] [--all]` (#544): no type given prints the menu and prompts a pick;
    a type (or `--all`) generates it, no prompt. The first positional is a slug only when it already
    names a session -- the same disambiguation `run` uses -- so `docs prd` with no session of that
    name resolves the workspace's default session and treats `prd` as a type."""
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
    # Validated before the `--all` branch, not only on the explicit-types path: a token that
    # matched no session above (invalid slug or typo) falls through to here as a stray type token,
    # and `--all` used to discard it unchecked -- generating every document against the *default*
    # session instead of refusing (invariant 3). Pinned by
    # `test_docs_all_refuses_a_token_that_names_neither_a_type_nor_a_session` (#544).
    if type_tokens:
        type_tokens = _resolve_doc_types(type_tokens, owned_types)
    if a.all:
        if type_tokens:
            # `--all` and explicit types together are ambiguous rather than additive -- refused
            # outright rather than guessing which one wins. Pinned by
            # `test_docs_all_combined_with_an_explicit_type_is_refused` (#544).
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
    """Launch the local, single-user web interface (the `[web]` extra). Binds to localhost by default;
    the Anthropic key is read from the server environment and is only needed for provider actions —
    consulting existing sessions needs none. Uvicorn is imported and started here, never at module
    import, and the FastAPI app is a factory so nothing binds a port until this runs."""
    host, port = a.host, a.port
    _announce_bind(host, verb="web",
                   exposure="Requivo Web has NO authentication and must not be exposed on an "
                            "untrusted network.")
    try:
        import uvicorn

        from requivo.web.app import create_app
        from requivo.web.logging_setup import configure_web_logging
    except ImportError as e:
        # `EngineError` (code `provider_unavailable`) is a decision about a published payload, not a
        # leftover -- see `_missing_extra_message`'s docstring and the `cli.py` entry in
        # `tests/test_boundaries.py`'s surface-provider allowlist for why it stays here rather than
        # moving with the message template. Pinned by
        # `test_the_missing_web_extra_keeps_its_published_error_code`.
        raise EngineError(_missing_extra_message("web", e)) from e
    # The process is ours from here, so this is where `requivo.web` gets its handler (#291) — the
    # same placement, and the same reason, as `configure_streams()` in `app()` above: importing the
    # package must not reconfigure logging for a program that merely imported it, and `create_app()`
    # is a factory a third party can mount inside their own service. `logging_setup` carries the
    # argument in full; it declines rather than competing, so a host that configured this logger
    # itself keeps what it set.
    #
    # Before the URL is printed and before uvicorn starts, so a record emitted during startup is
    # already formatted. Known limit: under `--reload`, uvicorn spawns a worker process that
    # re-imports the app, and that process has not been through here — a development flag's own
    # worker still logs through `lastResort`. Closing that would mean configuring at import, which
    # is the thing this placement exists to refuse.
    # Pinned by `test_the_web_verb_configures_the_logger_before_it_serves`.
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
    """Serve the local HTTP API (the `[api]` extra, #425 slice 4) -- `requivo web`'s shape, one
    surface along: the same bind announcement, the same lazy import with the same install hint, the
    same logging placement, and no browser to open.

    The one thing this verb has that `web` does not is the bind discipline of
    `docs/decisions/0004-the-http-api-facade.md` §5: `create_api(bind_host=host)` refuses to build
    the app for a bind beyond loopback with no `REQUIVO_API_TOKEN` set, and it does so *before*
    uvicorn is handed anything, so the refusal is a clean one-line `RequivoError` and never a bound
    port. With a token set, every `/api/v1` route except `/health` requires it as a bearer -- the
    warning below says so, because "NO authentication" would be false for this surface.
    """
    host, port = a.host, a.port
    try:
        import uvicorn

        from requivo.api.app import create_api
        from requivo.web.logging_setup import API_LOGGER, configure_surface_logging
    except ImportError as e:
        # The same decision as `_cmd_web`'s arm above, sharing its `_missing_extra_message` template.
        # This arm is for uvicorn specifically; `create_api()` raises the identical message for a
        # missing fastapi. Pinned by `test_the_missing_api_extra_keeps_its_published_error_code`.
        raise EngineError(_missing_extra_message("api", e)) from e
    # Built before the bind warning, the logger and the banner: a refusal to bind should be the only
    # thing this verb prints when it refuses. Pinned by
    # `test_the_serve_verb_refuses_a_non_loopback_bind_with_no_token_before_binding`.
    app = create_api(bind_host=host)
    _announce_bind(host, verb="api serve",
                   exposure="the Requivo API is protected only by the REQUIVO_API_TOKEN bearer "
                            "token and must not be exposed on an untrusted network.")
    # Same placement and same reason as `configure_web_logging()` in `_cmd_web`: the process is
    # ours from here. `api/usage.py` writes the operator's cost line to `requivo.api` at INFO from
    # a `finally`, which `lastResort` would drop (#291's exact defect, one surface along).
    # Pinned by `test_the_api_serve_verb_configures_the_logger_before_it_serves`.
    configure_surface_logging(API_LOGGER)
    url = f"http://{host}:{port}"
    print(f"\nRequivo API → {url}   (docs: {url}/docs)")
    print("  Sessions stay local under .requivo/sessions/. An Anthropic key (server env) is needed only")
    print("  for provider actions (discovery, generation); reading existing sessions needs none.")
    print("  EXPERIMENTAL: paths and shapes may still change -- see docs/decisions/0004.\n")
    uvicorn.run(app, host=host, port=port)


# The closing paragraph of `requivo --help` (#244). It carries the two things a flat list of
# verbs cannot: the first command to run, and what the (API) marker on nine of them means.
# A marker nobody defines is a decoration, and the old help defined nothing at all -- a reader could
# not tell from it that `brief` would bill them and `status` would not.
#
# `requivo run`, not `requivo discover` (#546/#547): the three-journey-verb decision
# (docs/decisions/0018-three-journey-verbs.md) makes `run` the second command a first-time reader
# meets, and naming `discover` here would send that reader straight back at the verb #546 moved
# under "For scripts and integrations".
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

# The two-tier `--help` grouping (#546): "Start here" (`demo`/`run`/`docs`/`status`/`web`), "For
# scripts and integrations" (the automation contract docs/integrations.md documents) and "Plumbing"
# (session/model/artifact CRUD, install diagnostics). Presentational only -- registration order below
# is untouched and stays the axis
# `test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order` and
# CLAUDE.md's tree entry for `cli.py` read; this table is what `_JourneyHelpFormatter` renders
# instead of argparse's flat subaction listing. Every verb the parser registers must appear in
# exactly one of these three tuples -- `_JourneyHelpFormatter` refuses to render, rather than
# silently narrowing `--help`, if one is missing or duplicated: a dropped verb here is invariant 3's
# shape one layer up, half a listing reading as a complete one. Pinned by
# `test_every_registered_verb_appears_in_exactly_one_help_group`.
_HELP_GROUP_START = ("demo", "run", "docs", "status", "web")
_HELP_GROUP_SCRIPTS = (
    "discover", "answer", "brief", "gtm_brief", "prd", "stories", "estimate", "criteria", "epic", "release", "impact",
)
_HELP_GROUP_PLUMBING = ("doctor", "schema", "context", "session", "model", "artifact", "api")


class _JourneyHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Renders the top-level command list in the three `_HELP_GROUP_*` tiers instead of argparse's
    flat, registration-order listing (#546).

    Only the *root* parser is built with this class (`_build_parser`, below). `add_parser` does not
    inherit `formatter_class` from the parent it is added to -- each verb's own subparser defaults to
    plain `argparse.HelpFormatter` unless told otherwise, which none of them are -- so
    `requivo <verb> --help` renders exactly as it did before this class existed. Pinned by
    `test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter`, which builds the
    parser twice in one interpreter (this class, then plain `argparse.HelpFormatter`) rather than
    against a committed fixture, since that guard is the one that actually matters here: this
    class's own rendering is easy to eyeball, `requivo <verb> --help` staying untouched is the
    property that is easy to break by accident.
    """

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return self._format_grouped_commands(action)
        return super()._format_action(action)

    def _format_grouped_commands(self, action) -> str:
        by_name = {choice.dest: choice for choice in action._choices_actions}
        named = set(_HELP_GROUP_START) | set(_HELP_GROUP_SCRIPTS) | set(_HELP_GROUP_PLUMBING)
        if named != set(by_name):
            # Not reached by a passing suite --
            # `test_every_registered_verb_appears_in_exactly_one_help_group` catches this at test
            # time. Kept as a live check too, so a `--help` a person actually asks for cannot
            # silently narrow itself if the two ever drift apart.
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
        """One row per verb, its own help text beside it -- the shape argparse renders a subaction
        list in natively, reused via `_format_action` so alignment matches the rest of the page."""
        lines = [f"{' ' * self._current_indent}{title}\n"]
        self._indent()
        lines.extend(self._format_action(by_name[name]) for name in names)
        self._dedent()
        return self._join_parts(lines)

    def _format_compact_group(self, title, names, by_name) -> str:
        """Names only, comma-joined on one line -- these two groups are the automation contract and
        the plumbing, neither the first screen a new user needs, so the per-verb help text
        `requivo <verb> --help` gives would only add noise here. The `(API)` marker survives the
        collapse: dropping it would leave the epilogue's "every unmarked verb is offline and free"
        claim false for the paid verbs in these two groups. Pinned by
        `test_every_paid_verb_in_a_compact_group_still_shows_the_marker` (#546)."""
        labeled = [f"{name} (API)" if "(API)" in (by_name[name].help or "") else name
                   for name in names]
        body = f"{' ' * (self._current_indent + 2)}{', '.join(labeled)}\n"
        return f"{' ' * self._current_indent}{title}\n{body}"


def _build_parser(formatter_class: type[argparse.HelpFormatter] = _JourneyHelpFormatter,
                   ) -> argparse.ArgumentParser:
    # `formatter_class` defaults to the grouped one every real caller gets; the parameter exists so
    # `test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter` can build a second
    # parser with plain `argparse.HelpFormatter` in the *same* interpreter and compare, rather than
    # against a committed fixture -- a fixture broke across both a Python version ("options:" vs
    # "optional arguments:") and a terminal width, neither of which this parameter depends on (#546).
    p = argparse.ArgumentParser(
        prog="requivo",
        description="Requivo — find what could change the solution before you commit to the scope.",
        epilog=EPILOG,
        # `_JourneyHelpFormatter` extends `RawDescriptionHelpFormatter`, so the epilog still renders
        # raw and the two example commands stay copy-pasteable -- it affects the description and the
        # epilog exactly as the plain class did, and additionally groups the command list (#546).
        formatter_class=formatter_class,
    )
    # Read from `requivo.__version__` rather than written here (#247). `tests/test_version_sites.py`
    # scans pyproject, the package dunder and the two plugin manifests; `cli.py` is in none of those
    # globs, so a literal here would be a fifth declaration with no guard on it -- added by the very
    # change whose subject is telling people the right version. Pinned by
    # `test_the_version_flag_declares_nothing_that_test_version_sites_cannot_see`.
    p.add_argument("--version", action="version", version=f"requivo {__version__}",
                   help="print the Requivo version and exit")
    p.add_argument("--workspace", metavar="DIR", help=_WORKSPACE_HELP)
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    # Registration order is no longer render order -- #546 moved that job to `_JourneyHelpFormatter`
    # and the `_HELP_GROUP_*` tables above. It stays the axis CLAUDE.md's tree entry for `cli.py`
    # documents: demo → discover → the refinement verbs → the generators → the plumbing → web → api,
    # the order a user meets them in even though the printed page now groups them differently.
    #
    # `model_cmd` is defined here rather than further down for the same reason: the journey verbs
    # are registered above the plumbing now, and they need it. Pinned by
    # `test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order`.

    # Two verbs (`status`, `impact`) genuinely open a path they are handed -- `_resolve_ref` reads
    # the file's own bytes directly, no session lookup involved. The rest resolve a *slug* and
    # read/write the store's own copy, so a path was never a meaningful input for them and their
    # help must not claim otherwise (#402); `_generator_service`/`_cmd_answer` pass
    # `resolve_slug(..., accept_path=False)` to refuse one outright, naming what was given.
    _SESSION_HELP_WITH_PATH = "a session slug, or a path to a saved model.json"
    _SESSION_HELP_SLUG_ONLY = "a session slug"

    def model_cmd(name: str, help_: str, func, extra=None, *, accepts_path: bool = False,
                  session_required: bool = True):
        sp = sub.add_parser(name, help=help_)
        # `session`, not `model` (#248). The two authoring eras spelled one concept two ways: every
        # verb under `deterministic/` says `session`, and this helper said `model` -- so the usage
        # error a person actually meets read "the following arguments are required: model" about a
        # session slug, beside a `model` verb group of its own. A dest is internal and a positional
        # is passed by position, so no invocation changed. Pinned by
        # `test_every_session_reference_positional_is_spelled_session` and
        # `test_the_missing_argument_error_names_a_session_not_a_model`.
        session_help = _SESSION_HELP_WITH_PATH if accepts_path else _SESSION_HELP_SLUG_ONLY
        # `nargs="?"`/`default=None` on the two verbs #541 makes optional -- never on a plumbing
        # verb (`session`/`model`/`artifact` keep it required: a script must never act on
        # "whichever session is newest"). `SessionResolution` picks the default when it is omitted.
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
    # `-` named in the help, not only implemented (#360): it is the shape a reader reaches for when
    # piping a messy client email in, and the three sibling verbs that already accept it say so.
    d.add_argument("request",
                   help="the client request, a path to a file containing it, or '-' to read it "
                        "from stdin")
    d.add_argument("--once", action="store_true", help="single pass (status + questions), no interactive loop")
    # `--cards` is a permanent alias of `--context` (#85): the same selector was spelled two ways
    # across three verbs. One action with two option strings, never two arguments — two arguments
    # would let whichever came last on the command line silently discard the other. `--context` is
    # the documented primary and owns the dest, so no handler moved.
    d.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                   help="comma-separated context cards to load instead of all "
                        "(e.g. b2b-platform,financial-reporting); sharpens discovery by dropping "
                        "irrelevant cards. Applies to this discovery only. Alias: --cards.")
    d.add_argument("--perimeter", default="software", metavar="ID",
                   help="which installed perimeter this session runs under, frozen at creation "
                        "(default: software). #601's router picks one automatically; until then, "
                        "name it explicitly.")
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
    model_cmd("gtm_brief", "generate go-to-market's one artifact (API)", _generator_verb("gtm_brief"))
    model_cmd("prd", "generate the PRD (API)", _generator_verb("prd"))
    model_cmd("stories", "derive user stories (API)", _generator_verb("stories"))
    model_cmd("estimate", "derive stories and estimate them, in day ranges (API)",
              _generator_verb("estimate"))
    model_cmd("criteria", "generate Given/When/Then acceptance criteria (API)",
              _generator_verb("criteria"))

    def epic_flags(sp):
        # Three sibling flags of one kind: each writes an export file. `--export-json` was spelled
        # `--json` until #83, where it was the odd one out twice over — on every other verb `--json`
        # means "emit the payload on stdout", and `app()` reads `getattr(args, "json", False)`
        # generically to switch failures to a structured envelope. So the flag that documented
        # itself as writing a file also, silently, changed how failures were reported, while
        # `--github` and `--gitlab` did not. With no `json` dest on this verb that getattr falls
        # through to False and all three report a failure the same way. Do NOT add a stdout
        # `--json` here: it would restore the divergence under a new name.
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
    # One `nargs="*"` positional rather than a `session`/`types` pair (#544): the two are the same
    # shape as `run`'s single positional, and `_cmd_docs` disambiguates the same way `run` does --
    # `session model_cmd()` above cannot express "optional slug, then a variable tail" at all.
    docs.add_argument("args", nargs="*", metavar="[slug] [type ...]",
                      help="an optional session slug, then document types to generate; omit the "
                           "types to see the menu (omit the slug too for the workspace's default "
                           "session)")
    docs.add_argument("--all", action="store_true", help="generate every document, skipping the menu")
    docs.set_defaults(func=_cmd_docs)

    # The deterministic surface (doctor / schema / context / session / model / artifact) — no LLM,
    # no API key. Registered here rather than first (#244) so the plumbing renders below the product.
    # Moving the call weakens nothing: `register` composes its four halves at import, so a module
    # that stops registering is still an ImportError rather than a quietly shorter `--help`.
    register_deterministic(sub)

    # Last, and not with the journey verbs: `web` is a *surface*, not a step. It launches the same
    # services behind a browser, so it belongs beside the plumbing rather than in a sequence.
    web = sub.add_parser("web", help="launch the local single-user web interface (needs the [web] extra)")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, localhost only)")
    web.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    # SUPPRESS so an absent web --workspace does not overwrite a global `requivo --workspace … web`.
    # This copy is hand-written and predates `_accept_workspace_after_the_command`, which only *adds*
    # the option where it is absent (adding it twice is an argparse conflict) -- so it is the one
    # copy free to describe the flag differently from the other thirty, and it did until #249's own
    # review caught it. It reads `_WORKSPACE_HELP` like every other copy now, and
    # `test_every_workspace_copy_carries_the_same_help_text` is what stops the next hand-written one
    # drifting the same way.
    web.add_argument("--workspace", metavar="DIR", default=argparse.SUPPRESS, help=_WORKSPACE_HELP)
    web.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    web.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    web.set_defaults(func=_cmd_web)

    # Beside `web`, for the same reason: a surface, not a step. `api` is a group so that `serve` is
    # not the last verb it ever grows (#425 slice 4); `--workspace` follows `web`'s SUPPRESS pattern
    # for the reason stated on that copy.
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


# One string, bound to every copy of the flag, so the global one and the per-verb ones cannot
# describe two different things. The clause it used to end on -- "Place before the command." -- is
# gone with the constraint it stated (#249); prose that outlives its rule is what turned a working
# CLI into `unrecognized arguments`.
_WORKSPACE_HELP = ("workspace root for sessions (default: cwd). Sessions live in "
                   "<workspace>/.requivo/sessions/. Accepted before or after the command.")


def _accept_workspace_after_the_command(parser: argparse.ArgumentParser) -> None:
    """Re-declare `--workspace` on every subparser, at every depth, so its position stops mattering.

    `--workspace` lived on the root parser alone, and argparse hands everything after the
    subcommand to the subparser -- so `requivo status <slug> --workspace DIR` died with
    `unrecognized arguments: --workspace DIR` at exit 2. That message is wrong about the one thing a
    reader needs from it: the flag is not unrecognized, it is misplaced, and the constraint saying
    so lived only in `--help` text, which the person who just hit the error is by definition not
    reading. The natural phrasing is the one that failed.

    **`default=argparse.SUPPRESS` on every copy, and that is the whole of the correctness here.**
    `_SubParsersAction` parses into a fresh namespace and copies every attribute of it onto the
    parent's, so a copy defaulting to `None` would overwrite a perfectly good
    `requivo --workspace DIR <command>` with None for every verb at once -- the fix silently
    breaking the position it was meant to preserve. `web` has carried this pattern, with that
    reasoning written beside it, since long before this function existed; all this does is stop it
    being the only verb that has it. Pinned by
    `test_an_absent_subcommand_workspace_does_not_clobber_the_global_one`.

    Walked rather than added at each registration site, for two reasons. The verb groups register
    from four modules under `deterministic/`, so a per-site edit is a list to keep in step with the
    next subcommand anybody adds -- and `session`, `model` and `artifact` nest their own subparsers,
    where the flag has to be on the *leaf* to be reachable at all. `test_cli_flag_names.py`'s
    `test_every_verb_accepts_workspace_after_its_own_name` reads the built parser rather than a
    list, for the same reason.

    argparse exposes no public way to enumerate subparsers, so `_actions`/`_SubParsersAction` are
    read directly. They are as stable as anything in that module and `tests/test_cli_flag_names.py`
    has walked them the same way since #72; a private attribute that disappears fails loudly at
    import, which is the acceptable direction.
    """
    seen: set[int] = set()

    def walk(p: argparse.ArgumentParser) -> None:
        for action in p._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            # `.choices` maps every alias to the same parser object, so a parser reached twice would
            # be given the option twice and argparse would raise on the conflict.
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
    # First, before anything can print: make stdout and stderr unable to kill this process on a
    # character they cannot encode (#29). Not at import time — importing `requivo` must not
    # reconfigure the streams of a program that merely imported it.
    configure_streams()
    # `.env` is read here, per run, not at module import — the same principle one comment up, for
    # the environment instead of the streams: importing `requivo.cli` must not mutate the process
    # that imported it. At import time it handed every pytest worker the developer's real key, and
    # the suite paid for a live call (#419, `test_importing_the_cli_leaves_the_environment_alone`).
    load_dotenv()
    args = _build_parser().parse_args(argv)
    # A global --workspace redirects where sessions are read/written, for the duration of this run.
    if getattr(args, "workspace", None):
        os.environ["REQUIVO_WORKSPACE"] = args.workspace
    want_json = getattr(args, "json", False)
    # Track the run's API footprint and print it after the command. Offline verbs make no call, so
    # the ledger stays empty and render_usage() prints nothing.
    with track_usage() as ledger:
        try:
            args.func(args, client)
        except RequivoError as e:
            # Every clean, expected failure — a core validation/session error OR a provider transport
            # error (EngineError is a RequivoError) — surfaces without a traceback. With --json the
            # caller (e.g. Claude Code) gets the structured envelope; otherwise a one-line message.
            _render_usage_safely(ledger)
            if want_json:
                print_json(e.to_dict())
            else:
                safe_write(sys.stderr, f"\n{e}\n")
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            # Every clean, expected failure surfaces without a traceback (the arm above); Ctrl-C was
            # the one interruption that did not, because it is not a `RequivoError` and used to
            # propagate straight past this function -- skipping the usage summary, and, for any
            # command with no rescue logic of its own, naming nothing at all.
            #
            # `_cmd_discover`'s own handlers (`_rescue_drafted`, the quick path's own claim above, and
            # the brief-generation catch) print what a claimed session held and how to continue,
            # *then re-raise the bare interrupt* rather than exiting themselves -- so this is where
            # every one of them, discover included, actually ends: no traceback, the spend so far, and
            # the conventional SIGINT code rather than 1, so a script can tell "the operator stopped
            # it" from "the operator got back a clean refusal". Pinned by
            # `test_a_top_level_interrupt_on_an_existing_session_exits_130_with_no_traceback`.
            _render_usage_safely(ledger)
            safe_write(sys.stderr, "\nInterrupted.\n")
            raise SystemExit(EXIT_INTERRUPTED) from None
        except UnicodeEncodeError as e:
            # The braces to `configure_streams`' belt, and the reason this arm exists at all: a
            # `UnicodeEncodeError` escaping a handler was raised by a `print`, which means the
            # handler had already finished the work it was reporting. Letting it surface as a
            # traceback tells the operator the command failed when the revision has landed and the
            # artifact has been written -- so they re-run, and pay for a second provider call on top
            # of the first. Say what actually happened instead, and whether it was billed -- pinned
            # by `test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback` for the
            # exit code, and `test_the_render_failure_message_does_say_so_when_a_call_was_billed`
            # with `test_the_render_failure_message_does_not_claim_a_call_was_billed_when_none_was`
            # for the two arms of the billed claim.
            #
            # Reached only where `configure_streams` reported `could-not` for this stream, which
            # `requivo doctor` prints. Narrow on purpose: a broad `except Exception` here would
            # swallow real failures and claim they had landed.
            _render_usage_safely(ledger)
            paid = bool(getattr(ledger, "calls", None))
            safe_write(sys.stderr, _RENDER_FAILED_HEAD.format(error=e)
                       + (_RENDER_FAILED_PAID if paid else _RENDER_FAILED_UNPAID)
                       + _RENDER_FAILED_TAIL)
            raise SystemExit(EXIT_RENDER_FAILED) from None
    # Outside the `with`, and therefore outside the arm above -- which is exactly why it needs the
    # safe wrapper. This is the wholly-successful path: the provider call is billed and the revision
    # is applied by the time it runs, so an exception here is the #29 ordering bug on the one route
    # where nothing was wrong in the first place.
    _render_usage_safely(ledger)
