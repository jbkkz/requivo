#!/usr/bin/env python
"""Capture the golden baseline — K runs per request (the regression reference).

Runs discovery K times (``GOLDEN_K``, default 3) per request in ``fixtures/golden/requests.md`` and
writes ``fixtures/golden/<slug>.runs.json``: K runs let ``golden_diff`` tell an asset's effect from
sampling noise, which cannot be pinned on this model family, only measured. The workflow, the lenses
and the cost are ``docs/evaluations.md``.

A bare invocation captures single-pass requests and skips interactive ones, naming each (#276,
`test_a_bare_invocation_skips_every_interactive_request`). ``--brief`` also captures the assessment
(`test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal`). A request with
``answer.<slot>:`` lines is driven through `DiscoveryService.draft_turn` for ``GOLDEN_TURNS`` turns
(#137). The call ceiling is derived and printed before the first call (#290,
`test_the_announced_call_count_moves_with_the_request_set`). Needs ANTHROPIC_API_KEY.

Usage:
    python scripts/golden_run.py              # every single-pass request; interactive ones skipped
    python scripts/golden_run.py <slug>...    # only the named one(s), interactive included
    python scripts/golden_run.py --all        # every request, interactive ones included
    python scripts/golden_run.py <slug> --brief   # also capture the assessment
    GOLDEN_K=5 python scripts/golden_run.py   # override runs-per-request
    GOLDEN_TURNS=8 python scripts/golden_run.py <slug>   # override turns-per-run"""

from __future__ import annotations

import sys
import time

from anthropic import Anthropic, APIConnectionError
from dotenv import load_dotenv

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import (  # noqa: E402
    GOLDEN,
    MEASURABLE_DEPTH,
    REPO,
    REQUESTS,
    TURNS,
    AnswerSheet,
    K,
    Turn,
    answers_for_turn,
    brief_consensus,
    configure_output,
    dump_partial,
    dump_runs,
    dump_turn_runs,
    is_interactive,
    parse_requests,
    runs_envelope,
    stability,
    turn_envelope,
    turn_lens,
)

sys.path.insert(0, str(REPO / "src"))
from requivo.core.perimeters import DEFAULT_PERIMETER, SOFTWARE  # noqa: E402
from requivo.providers.anthropic import advise, run  # noqa: E402
from requivo.providers.anthropic.client import (
    STREAM_TRANSPORT_ERRORS,  # noqa: E402
    current_model_name,  # noqa: E402
)
from requivo.providers.anthropic.provider import AnthropicProvider  # noqa: E402
from requivo.providers.errors import EngineError  # noqa: E402
from requivo.services.discovery import DiscoveryService  # noqa: E402

load_dotenv()

RETRY_PAUSE = 20.0  # seconds before the one harness-level retry; tests set it to 0


def is_transport_failure(exc: BaseException) -> bool:
    """An `EngineError` the connection caused: the one failure worth a second paid attempt (#557).

        The cause, not the message: `_complete` raises every provider failure as `EngineError ... from e`,
        so a credential, rate-limit or malformed-output failure carries a different cause and is not retried
        (`test_an_auth_failure_is_not_retried_and_a_malformed_reply_neither`).
    """
    return isinstance(exc, EngineError) and isinstance(exc.__cause__, (APIConnectionError, *STREAM_TRANSPORT_ERRORS))


def with_retry(call, *args, **kwargs):
    """``call(*args, **kwargs)``, once more after `RETRY_PAUSE` if a transport failure ended it (#557).

        One extra attempt on top of the SDK's own, so a call costs at most twice; `main` announces that.
    """
    try:
        return call(*args, **kwargs)
    except EngineError as exc:
        if not is_transport_failure(exc):
            raise
        print(f"    ! transport failure ({exc.__cause__.__class__.__name__}); retrying once "
              f"in {RETRY_PAUSE:g}s", file=sys.stderr)
        time.sleep(RETRY_PAUSE)
        return call(*args, **kwargs)


def keep_partial(slug: str, envelope: str, where: dict, exc: Exception) -> None:
    """Persist the completed runs beside the baseline with the marker, say so, and let the failure propagate."""
    path = dump_partial(slug, envelope, {**where, "reason": str(exc).splitlines()[0]})
    print(f"  ! {slug}: kept the completed runs in {path.name} (partial — "
          f"not a baseline; golden_diff reports it as not re-captured)", file=sys.stderr)


def capture_model() -> str:
    """The model id this invocation captures on, resolved **once** and threaded to every call (#515, #434).

        Pinned by `test_main_resolves_the_model_once_and_threads_it_to_every_capture` and
        `test_the_interactive_capture_records_the_model_it_reasoned_on`.
    """
    return current_model_name()


def capture_interactive(client: Anthropic, req: dict, model: str) -> None:
    """K interactive conversations, each answered off this request's answer sheet.

        Through `DiscoveryService.draft_turn`, the production interactive path — the measurement's whole
        validity (#137, `test_the_capture_reasons_through_the_interactive_seam_and_not_a_message_list`).
        Mirrors `converse()`'s turn loop; no session, revision or write.
    """
    # The resolved id, not a per-call resolution (test_the_interactive_capture_records_the_model_it_reasoned_on).
    disco = DiscoveryService(provider=AnthropicProvider(client, model=model))
    # A pre-#621 request dict has no `perimeter` key; it reads as the default, as in `parse_requests`.
    perimeter = req.get("perimeter", DEFAULT_PERIMETER)
    runs: list[list[Turn]] = []
    for i in range(K):
        sheet = AnswerSheet(req["answers"])
        turns: list[Turn] = []
        out, answers = None, None
        for index in range(1, TURNS + 1):
            try:
                out = with_retry(disco.draft_turn, req["request"], current_model=out, answers=answers,
                                 cards=None, perimeter=perimeter)
            except Exception as exc:
                # The in-flight run keeps the turns it finished (#557).
                keep_partial(req["slug"], turn_envelope(req["request"], req["answers"],
                                                        runs + ([turns] if turns else []), model=model,
                                                        perimeter=perimeter),
                             {"run": i + 1, "turn": index}, exc)
                raise
            print(f"    run {i + 1}/{K}  turn {index}/{TURNS}", end="\r", flush=True)
            # `answered` records what was sent onward; an answer the engine never saw would fake coverage
            # for the re-ask count.
            done = index == TURNS or not out.questions
            block, answered = (None, []) if done else answers_for_turn(out.questions, sheet)
            turns.append(Turn(index=index, answered=answered, model=out))
            if block is None:
                break
            answers = block
        runs.append(turns)
    dump_turn_runs(req["slug"], req["request"], req["answers"], runs, model=model,
                   perimeter=perimeter)

    lens = turn_lens(runs, req["answers"], perimeter=perimeter)
    depth = "/".join(str(d) for d in lens["depths"])
    verdict = "deep enough" if lens["deep_enough"] else f"SHALLOW — under {MEASURABLE_DEPTH} turns"
    print(f"  ✓ {req['slug']:<20} interactive · turns {depth} across {lens['n']} runs · {verdict}")
    st = stability([run[-1].model for run in runs], perimeter=perimeter)
    print(f"    final model         {st['unanimous']['impact']}/{st['total_slots']} slots unanimous "
          f"on impact · {st['unanimous']['state']}/{st['total_slots']} on confidence")
    for key, caption in (("reasked", "re-asked after the client answered"),
                         ("lost", "answered early, not confirmed at the end"),
                         ("regressed", "completeness fell back")):
        hits = lens[key]
        detail = ", ".join(f"{lab} ({c}/{lens['n']})" for lab, c in sorted(hits.items())) or "—"
        print(f"    {caption:<38} {detail}")
    if not lens["deep_enough"] and lens.get("unreached_layers"):
        # #163: name the sheet layers no run reached, right where the calls were spent.
        detail = ", ".join(f"{lab} ({c})" for lab, c in sorted(lens["unreached_layers"].items()))
        print(f"    {'sheet layers never reached':<38} {detail}")


def capture(client: Anthropic, req: dict, with_brief: bool = False, *,
            model: str | None = None) -> None:
    model = model or capture_model()
    # A pre-#621 request dict has no `perimeter` key: software, as in `capture_interactive`.
    perimeter = req.get("perimeter", DEFAULT_PERIMETER)
    if is_interactive(req):
        if with_brief:
            # Said, not silently dropped: --brief would double a K x TURNS spend nobody asked for.
            print(f"  ! {req['slug']:<20} --brief is not captured for an interactive request "
                  f"(it would double a {K * TURNS}-call capture); the turn lens follows",
                  file=sys.stderr)
        return capture_interactive(client, req, model)

    if with_brief and perimeter != SOFTWARE:
        # `advise()` reasons software's `brief.md` with no perimeter, so the assessment lens is software-only
        # (#607: each perimeter ships its own one artifact).
        print(f"  ! {req['slug']:<20} --brief is not captured for a {perimeter!r} request "
              f"(the assessment lens is software-only)", file=sys.stderr)
        with_brief = False

    models, briefs = [], ([] if with_brief else None)
    for i in range(K):
        try:
            # reuse_system=True: engine.md's system prompt is sent K times here (#58); brief.md's too (#9).
            out = with_retry(run, client, [{"role": "user", "content": req["request"]}], reuse_system=True,
                             model=model, perimeter=perimeter)
            brief = with_retry(advise, client, out, reuse_system=True, model=model) if with_brief else None
        except Exception as exc:
            # A run counts only once its assessment (if asked for) landed too (#557).
            keep_partial(req["slug"], runs_envelope(req["request"], models, briefs, model=model,
                                                    perimeter=perimeter), {"run": i + 1}, exc)
            raise
        models.append(out)
        if with_brief:
            briefs.append(brief)
        print(f"    run {i + 1}/{K} done", end="\r", flush=True)
    dump_runs(req["slug"], req["request"], models, briefs, model=model, perimeter=perimeter)
    st = stability(models, perimeter=perimeter)
    # The noise floor up front: how much of the model was stable across the K runs.
    print(f"  ✓ {req['slug']:<20} {st['unanimous']['impact']}/{st['total_slots']} slots "
          f"unanimous on impact · {st['unanimous']['state']}/{st['total_slots']} on confidence "
          f"· stable themes: {', '.join(st['themes']) or '—'}")
    if with_brief:
        bc = brief_consensus(briefs)
        stable = sorted(bc["themes"])
        print(f"    assessment          complexity {bc['complexity'][0]} "
              f"({bc['complexity'][1]}/{bc['n']} runs) · stable challenges: "
              f"{'; '.join(stable) or '—'}")


def planned_calls(runs: list[dict], with_brief: bool) -> int:
    """The API-call ceiling for exactly these requests, derived rather than written down (#290).

        An upper bound: an interactive request costs up to a call per turn, and ``--brief`` is counted at
        2x even where `capture` refuses it. Pure and offline; pinned by
        `test_the_announced_call_count_moves_with_the_request_set`.
    """
    return sum(K * (TURNS if is_interactive(r) else (2 if with_brief else 1)) for r in runs)


def select_runs(runs: list[dict], wanted: set[str], capture_all: bool) -> tuple[list[dict], list[dict]]:
    """`(selected, skipped)`: a bare invocation skips every interactive request (#276).

        Naming a slug or passing ``--all`` includes them. Pure and offline;
        `test_a_bare_invocation_skips_every_interactive_request`.
    """
    if wanted:
        return [r for r in runs if r["slug"] in wanted], []
    if capture_all:
        return runs, []
    selected = [r for r in runs if not is_interactive(r)]
    skipped = [r for r in runs if is_interactive(r)]
    return selected, skipped


def main(argv: list[str]) -> int:
    # First: a glyph the console cannot encode must not kill a run that already paid (invariant 16, #164).
    configure_output()
    if not REQUESTS.exists():
        print(f"Missing request set: {REQUESTS}", file=sys.stderr)
        return 1
    with_brief = "--brief" in argv
    capture_all = "--all" in argv
    argv = [a for a in argv if a not in ("--brief", "--all")]
    all_runs = parse_requests(REQUESTS)
    wanted = set(argv)
    if wanted:
        for slug in sorted(wanted - {r["slug"] for r in all_runs}):
            print(f"  ! unknown slug (skipped): {slug}", file=sys.stderr)
    runs, skipped = select_runs(all_runs, wanted, capture_all)
    for r in skipped:
        slug = r["slug"]
        print(f"  ! skipped {slug:<20} interactive request (K x GOLDEN_TURNS = "
              f"{K * TURNS} calls) -- capture it on its own: "
              f"python scripts/golden_run.py {slug}", file=sys.stderr)
    if not runs:
        print("Nothing to capture.", file=sys.stderr)
        return 1

    GOLDEN.mkdir(parents=True, exist_ok=True)
    client = Anthropic()
    # Resolved once, so every envelope records the id the calls were given (#515); printed with the
    # budget because re-capturing on a different model is a decision, not an inherited environment.
    model = capture_model()
    # Computed for the set actually selected — see `planned_calls`.
    calls = planned_calls(runs, with_brief)
    print(f"Capturing {len(runs)} request(s) × {K} runs → {GOLDEN.relative_to(REPO)}/  "
          f"(up to {calls} API calls{', assessment included' if with_brief else ''}; "
          f"a transport failure retries its call once, so up to {2 * calls}) "
          f"on {model}")
    for req in runs:
        try:
            capture(client, req, with_brief, model=model)
        except Exception as exc:  # one bad request should not lose the others
            print(f"  ✗ {req['slug']:<20} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
