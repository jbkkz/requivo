#!/usr/bin/env python
"""The regression lens — changes that clear the noise floor, not run-to-run jitter.

Compares each request's working-tree K-run baseline against its committed baseline in git ``HEAD``,
and reports a slot's impact/confidence as *moved* only when the old baseline was unanimous on that
dimension (a reliable reference) and the new consensus clearly shifted. A dimension that flickers
across the K runs is noise and stays silent — that is the whole point of capturing K runs instead of
one. Moves are split into **strong** (the new runs are unanimous too, so no single run's jitter can
explain it) and **weak** (a bare majority — at K=3 that is one run flipping). Act on strong; watch
weak in aggregate. See ``golden_lib`` for the consensus and floor logic.

With no committed baseline yet (a fresh capture), it instead prints the **noise floor** itself: how
much of each request's model is stable enough to diff on. A request with few unanimous slots will only
ever surface large changes; that's information, not a failure.

An **interactive** request (one with an answer sheet) gets a second readout on top: what the capture's
deep turns did — questions re-asked after the client answered them, confirmations the model stopped
carrying, completeness that fell back. That lens has its own third state and says *not measured*
rather than printing an empty finding set, because a single-pass baseline is silent about turn 3 in a
way that reads exactly like a clean one (#137).

A capture taken with ``golden_run.py --brief`` gets a third: the **assessment** lens, over the
complexity verdict and the challenge themes. All three run on every request, and the run's verdict is
the **union** of the ones that ran — the strongest signal any of them found. They are independent
measurements of one capture rather than votes on one question, so a lens finding nothing is never
evidence against another lens finding something, and a lens that could not look says so on its own
line instead of being folded into a silent pass (#162).

Workflow: golden_run.py (re-capture) → golden_diff.py (read the signal) → commit if intended.

Usage:
    python scripts/golden_diff.py              # every request
    python scripts/golden_diff.py <slug>...    # only the named one(s)
    python scripts/golden_diff.py <slug> --questions   # the questions themselves, old vs new
"""

from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import (  # noqa: E402
    GOLDEN,
    MEASURABLE_DEPTH,
    REPO,
    WATCHED_PATHS,
    baseline_commits_since,
    brief_consensus,
    brief_movements,
    captured_model,
    configure_output,
    load_answers,
    load_briefs,
    load_runs,
    load_turns,
    movements,
    runs_path,
    stability,
    turn_lens,
    turn_movements,
)

sys.path.insert(0, str(REPO / "src"))
from requivo.core.selectors import display_token  # noqa: E402


def _head_version(rel_path: str) -> str | None:
    res = subprocess.run(["git", "show", f"HEAD:{rel_path}"],
                         cwd=REPO, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def _show_freshness(rel_path: str) -> None:
    """Is the committed baseline at `rel_path` current with respect to `WATCHED_PATHS`, or how many
    commits since it was captured touched one of them? Printed first, before any lens output — the
    worked example #405's own third acceptance criterion asks for: "baseline captured 2026-08-01; 3
    asset commits since" turns a day of measurement into one glance, because without it the reader
    *is* the control — nothing else says whether the movement a lens reports below is a working-tree
    edit's own effect or the accumulation of commits nobody has re-captured against yet (#405, #410).

    Three states, and `unknown` must never render as `current` — the same collapse `golden_diff`'s
    own module docstring already refuses for a byte-identical capture, one layer up: a git failure
    and a clean baseline must not read the same way."""
    fr = baseline_commits_since(rel_path)
    watched = ", ".join(WATCHED_PATHS)
    if fr["state"] == "unknown":
        # `reason` is the only one of this function's three printed fields that carries text from
        # outside the process (git's stderr, or `str(exc)`) rather than a fixed git format like
        # `%cI`/`%H` -- so it has the strongest claim to the same guard the commit-row loop below
        # already gives `date`/`sha`/`subject`, and #461 is that guard reaching this print site too.
        print(f"  ? baseline freshness: could not tell ({display_token(fr['reason'])})")
        return
    if fr["state"] == "current":
        print(f"  · baseline current as of {fr['captured_at']} (no commits since touching {watched})")
        return
    commits = fr["commits"]
    print(f"  ⚠ baseline captured {fr['captured_at']}; {len(commits)} commit(s) touching {watched} "
          f"since — any movement below may be their combined effect, not only a working-tree edit:")
    for c in commits[:5]:
        # A commit subject is contributor-written text, exactly the class `questions_one` already
        # treats as untrusted for this file's own stated reason (invariant 14, #40): raw `\r` in a
        # subject would return the cursor to column 0 and let it overwrite the date/sha prefix it
        # sits behind. `baseline_commits_since` no longer lets a subject like that manufacture a
        # *second* row (#456 -- the record boundary parsed there is a real `\n`, decoded from bytes
        # rather than rewritten by `subprocess.run(text=True)`'s universal-newlines translation, and
        # `str.split("\n")` rather than `str.splitlines()`). This guard is what stops that same `\r`
        # from corrupting *this* row once it can no longer manufacture a new one -- `date` and `sha`
        # go through it too, even though neither is presently attacker-reachable (`%cI`/`%H` are
        # fixed git formats, never derived from commit text): a future field added to the same
        # `--format` string inherits the guard for free rather than needing its own print site fixed
        # later.
        print(f"      {display_token(c['date'])}  {display_token(c['sha'])}  "
              f"{display_token(c['subject'])}")
    if len(commits) > 5:
        print(f"      … and {len(commits) - 5} more")


def _show_model(old_text: str | None, new_text: str | None) -> None:
    """Which model each side of this comparison was captured on — printed beside the freshness line,
    because it is the other capture-time variable and the cheaper of the two to move (#515).

    `baseline_commits_since` watches the assets a capture's prompt is built from; nothing watched the
    model, and two of its three sources are the environment. Export `REQUIVO_MODEL=<something else>`,
    re-capture one request, and every lens below reports the swap as prompt movement — the confident
    readout #405 describes, about something the reader did not change.

    **Three states, and the third is the whole of it.** A baseline written before this key existed
    carries no model, and that renders as *unknown* — never as agreement with whatever is configured
    now, and never as a missing line. It is `_show_freshness`'s own rule on a second axis: `unknown`
    must never render as `current`. Pinned by
    `test_a_baseline_with_no_model_key_does_not_read_as_agreement`."""
    baseline = captured_model(old_text) if old_text is not None else None
    candidate = captured_model(new_text) if new_text is not None else None
    if baseline is None or candidate is None:
        missing = " and ".join(
            [name for name, value in (("baseline", baseline), ("candidate", candidate))
             if value is None])
        known = [f"{name} on {display_token(value)}"
                 for name, value in (("baseline", baseline), ("candidate", candidate))
                 if value is not None]
        tail = f" ({'; '.join(known)})" if known else ""
        print(f"  ? capture model: unknown for the {missing} — captured before the model was "
              f"recorded, so nothing here says the two agree{tail}")
        return
    if baseline == candidate:
        print(f"  · captured on {display_token(baseline)} (baseline and candidate agree)")
        return
    print(f"  ⚠ baseline captured on {display_token(baseline)}, candidate on "
          f"{display_token(candidate)} — any movement below may be the model swap rather than a "
          f"prompt or context edit")


def diff_one(slug: str) -> str:
    """Print the signal for one request. Returns its status: ``moved``, ``flat``, or ``stale``
    (no capture on disk, or a capture that is byte-identical to HEAD and so never landed)."""
    path = runs_path(slug)
    rel_path = f"fixtures/golden/{slug}.runs.json"
    old_text = _head_version(rel_path)

    print(f"\n{slug}")
    if old_text is not None:
        # Freshness is a fact about the *committed* baseline — there is nothing to say about it when
        # there isn't one yet; the "⊕ NEW" branch below already names that state on its own.
        _show_freshness(rel_path)
        _show_model(old_text, path.read_text(encoding="utf-8") if path.exists() else None)

    if not path.exists():
        print("  ! no working-tree capture (run golden_run.py first)")
        return "stale"

    new = load_runs(path.read_text(encoding="utf-8"))
    new_text = path.read_text(encoding="utf-8")

    if old_text is None:
        # No baseline yet — report the noise floor so we know how trustworthy future diffs will be.
        st = stability(new)
        print("  ⊕ NEW (no baseline in HEAD)")
        print(f"  noise floor  {st['unanimous']['impact']}/{st['total_slots']} slots unanimous on "
              f"impact, {st['unanimous']['state']}/{st['total_slots']} on confidence, across "
              f"{st['n']} runs")
        print(f"  stable themes: {', '.join(st['themes']) or '—'}")
        # On a first capture these readouts *are* the finding — there is nothing to diff against,
        # and what the deep turns did is the whole reason an interactive request exists (#137). The
        # assessment gets the same treatment for the same reason (#162).
        _show_turns(None, load_turns(new_text), load_answers(new_text))
        _show_assessment(None, load_briefs(new_text))
        return "moved"

    if new_text == old_text:
        # Byte-identical to HEAD means the capture never landed — the engine is non-deterministic, so
        # a genuine re-run can't reproduce a file exactly. Reporting "no change" here would be a false
        # all-clear, which is the one failure mode a regression lens must not have.
        print("  ! capture identical to HEAD — not re-captured (re-run golden_run.py)")
        return "stale"

    old = load_runs(old_text)
    m = movements(old, new)

    # Every lens runs, and the verdict is the union of what the ones that ran found. Independence is
    # the point, not the ordering: each watches something the others cannot see, so a null result
    # from one is not evidence against a finding from another. CLAUDE.md says this directly about
    # these two — *the slot tiers are a projection; the questions and challenges are the product*.
    #
    # It used to be a short-circuit over the whole function: a flat slot consensus printed "no change
    # above the noise floor" and returned, so the assessment lens was unreachable in exactly the case
    # it exists for, and `--brief` doubles that request's calls — the maintainer who paid for the
    # capture was the one told there was nothing to see. A lens that never ran, reported as a lens
    # that ran and found nothing, which is this file's own stated failure mode one lens over (#162).
    # `test_the_assessment_lens_runs_when_the_slot_consensus_held_still` is what fails if the
    # short-circuit comes back.
    signals = [
        _show_turns(load_turns(old_text), load_turns(new_text), load_answers(new_text)),
        _show_slots(m),
        _show_assessment(load_briefs(old_text), load_briefs(new_text)),
    ]
    if "strong" in signals:
        return "moved"
    return "weak" if "weak" in signals else "flat"


def _show_slots(m: dict) -> str | None:
    """Print what moved in the slot consensus. Returns its tier: `strong`, `weak` or None.

    The flat line decides only whether *this section* is a dash. It is not a verdict on the run, and
    reading it as one is what made the assessment lens unreachable (#162)."""
    if not m["moved"] and not m["themes_added"] and not m["themes_removed"]:
        print("  · no change above the noise floor")
        return None

    def _show(entries: list[dict], tier: str) -> None:
        print(f"  {tier:<10} {len(entries)} slot(s):")
        for mv in entries:
            print(f"               {mv['slot']:<22} {mv['dim']} {mv['from']}→{mv['to']}"
                  f"   (was {mv['old_agree']}/{mv['n']}, now {mv['new_agree']}/{mv['n']})")

    if m["strong"]:
        _show(m["strong"], "strong")
    if m["weak"]:
        _show(m["weak"], "weak")
    if m["themes_added"]:
        print(f"  questions  + stable theme(s): {', '.join(m['themes_added'])}")
    if m["themes_removed"]:
        print(f"  questions  − stable theme(s): {', '.join(m['themes_removed'])}")
    return "strong" if m["strong"] else "weak"


def _show_turns(old_turns, new_turns, layers: dict[str, list[str]] | None = None) -> str | None:
    """Print what the interactive capture says about turn 3 and beyond. Returns its tier — `strong`
    for a finding every run agrees on, None for no signal or no measurement.

    Four states, and the last two are the ones this function exists for:

    - **not applicable** — neither side is interactive. A single-pass request is not silent about the
      deep turns, it has none, so this prints nothing at all rather than a reassuring dash.
    - **compared** — both sides are interactive; the unanimous sets are diffed.
    - **first capture** — the working tree has turns and HEAD does not. The lens is printed with no
      comparison, because on a first capture the readout *is* the finding.
    - **lens lost** — HEAD had turns and the working tree does not. That is a request that stopped
      being interactive, and it has to be loud: the deep-turn lens went away, which reads exactly like
      it went quiet.

    `layers` is this capture's answer sheet (#163). It only ever adds a line, and only when the
    capture is SHALLOW: a healthy run's leftover layers are by design — the sheet is deliberately
    authored deeper than `MEASURABLE_DEPTH` so it doesn't run dry before the loop's own cap — and
    reporting them there would be noise on every clean capture.
    """
    if new_turns is None and old_turns is None:
        return None
    if new_turns is None:
        print("  interactive  ! the baseline has turns and this capture does not — the deep-turn "
              "lens is gone, which is not the same as clean")
        return "strong"

    lens = turn_lens(new_turns, layers)
    depth = "/".join(str(d) for d in lens["depths"])
    print(f"  interactive  turns {depth} across {lens['n']} run(s)"
          + ("" if lens["deep_enough"]
             else f"  ⚠ under {MEASURABLE_DEPTH} — this capture did not reach the deep turns"))
    for key, caption in (("reasked", "re-asked after the client answered"),
                         ("lost", "answered early, not confirmed at the end"),
                         ("regressed", "completeness fell back")):
        detail = ", ".join(f"{lab} ({c}/{lens['n']})" for lab, c in sorted(lens[key].items()))
        print(f"               {caption:<38} {detail or '—'}")
    if not lens["deep_enough"] and lens.get("unreached_layers"):
        # The #163 diagnosis: the sheet, not the engine, may be why this run stopped short — it
        # still had a layer to give on a slot the conversation never came back to.
        detail = ", ".join(f"{lab} ({c})" for lab, c in sorted(lens["unreached_layers"].items()))
        print(f"               {'sheet layers never reached':<38} {detail}")

    move = turn_movements(old_turns, new_turns)
    if not move["measured"]:
        print(f"               no comparison: {move['reason']}")
        return None
    strong = None
    for key, caption in (("reasked", "re-asks"), ("lost", "lost confirmations"),
                         ("regressed", "completeness regressions")):
        if move[f"{key}_added"]:
            strong = "strong"
            print(f"               + {caption} in every run: {', '.join(move[f'{key}_added'])}")
        if move[f"{key}_removed"]:
            print(f"               − {caption} no longer in every run: "
                  f"{', '.join(move[f'{key}_removed'])}")
    return strong


def _show_assessment(old_briefs: list | None, new_briefs: list) -> str | None:
    """Print what moved in the assessment. Returns its tier: `strong`, `weak` or None.

    Four states, mirroring `_show_turns` and for the same reason (#162, #137). This lens used to sit
    behind the slot section's short-circuit, so a capture whose slots held still printed "no change
    above the noise floor" over an assessment nobody had looked at:

    - **not captured** — neither side has `--brief` output. Named on a line of its own rather than
      left silent, because `--brief` is an opt-in flag and not a property of the request: an absent
      assessment is *not measured*, and with nothing said it reads exactly like measured-and-clean.
      It contributes nothing to the verdict, since it did not look.
    - **first capture** — nothing to compare against, so the consensus readout *is* the finding, the
      same shape the noise floor beside it already has.
    - **baseline only** — HEAD has an assessment and this capture does not. Marked `!` rather than
      `·`, because committing this capture would drop a lens the baseline had — but it contributes
      **nothing** to the verdict, for the same reason the not-captured state does: there is nothing
      to compare, so nothing was measured.
    - **compared** — both sides have one, and `brief_movements` grades it.

    A lost challenge theme counts as strong on its own: the engine used to contest that premise in a
    majority of runs and stopped. On the deliverable, losing a challenge is the regression that
    matters most — sharper questions are worth little if the pushback quietly disappears.

    **Why `baseline only` is not strong, unlike `_show_turns`' matching state.** It was, and that was
    wrong. Interactivity is declared in `requests.md` and reproduced on every capture, so the turn
    lens cannot vanish by accident and its disappearance really is a finding. `--brief` is a manual
    per-invocation flag that no capture remembers, and **every** single-pass baseline in
    `fixtures/golden/` currently carries one — so grading this strong turns the documented workflow
    (`golden_run.py` with no `--brief`, to measure an `engine.md` or context-card change) into six
    strong signals over a run where nothing moved. That is the noise this file exists to suppress,
    manufactured by the lens meant to catch it, and it is the rule stated two bullets up: a lens that
    could not look contributes nothing to the verdict.

    Deliberately *not* tallied in the summary line the way `stale` is: the per-request line is where
    a lens's own state belongs, and a counter that fires on nearly every run is one nobody reads.
    """
    if not new_briefs:
        if old_briefs:
            print("  assessment ! the baseline has an assessment and this capture does not — nothing "
                  "to compare (re-capture with --brief, or the committed baseline loses this lens)")
            return None
        print("  assessment · not captured — this lens did not look "
              "(re-run golden_run.py with --brief to measure it)")
        return None
    if not old_briefs:
        bc = brief_consensus(new_briefs)
        print(f"  assessment first capture · complexity {bc['complexity'][0]} "
              f"({bc['complexity'][1]}/{bc['n']} runs) · stable challenges: "
              f"{'; '.join(sorted(bc['themes'])) or '—'}")
        return None

    b = brief_movements(old_briefs, new_briefs)
    tier = None
    if b["complexity"]:
        c = b["complexity"]
        tier = "strong" if c["strong"] else "weak"
        print(f"  assessment {tier} complexity {c['from']}→{c['to']}"
              f"   (was {c['old_agree']}/{c['n']}, now {c['new_agree']}/{c['n']})")
    if b["themes_removed"]:
        tier = "strong"
        print(f"  assessment − challenge(s) no longer raised: {'; '.join(b['themes_removed'])}")
    if b["themes_added"]:
        # A gained challenge is a movement worth watching rather than acting on: the engine raising
        # something new is as often a rephrasing that cleared the clustering threshold as it is a
        # real gain, so it never outranks a loss on the same capture.
        tier = tier or "weak"
        print(f"  assessment + challenge(s) now raised: {'; '.join(b['themes_added'])}")
    if not (b["complexity"] or b["themes_added"] or b["themes_removed"]):
        print("  assessment · verdict and challenges unchanged")
    return tier


def questions_one(slug: str) -> None:
    """Print the questions each baseline actually asked, run by run, old then new.

    The slot tiers above are a *projection* of the model; the questions are what the user meets. In
    practice a card or prompt change reads far more clearly here than in a per-slot impact shift, so
    this is the view to open when a diff says something moved and you want to know whether it moved
    in a good direction.

    **Every string this prints is provider-written prose read back off disk**, so all of it goes
    through `display_token` — the same treatment `session show` and `artifact list` give a persisted
    value, for the same reason (invariant 14, #40). A question carrying a newline would otherwise
    write what reads as a second, authoritative line of the readout at column 0, and a regression
    lens whose own output can be forged is answering a different question from the one asked.
    `display_token` returns a safe line byte-for-byte, so ordinary prose is unchanged, which is what
    keeps the guard from being deleted for making the view unreadable.
    `test_a_forged_question_cannot_write_a_line_of_the_golden_readout` is what fails when a print here
    stops going through it, and `test_an_ordinary_question_is_rendered_byte_for_byte` is the other
    half.

    **Deliberately not `_log_safe`, the sibling answer in `scripts/plugin_cli_drift.py` (#139,
    #176).** Same class, two sinks, and the sinks decide the remedy. That one prints into a GitHub
    Actions step, where the log is *parsed* — at column 0 for `::name::`, and at any column at all
    for the legacy `##[name]`, which is why a whitespace squash alone was not enough there — and its
    value is a directory name nobody reads for its wording, so a lossy sanitise at the point the
    value enters is exactly right. This one prints into a maintainer's terminal, where column 0 is
    *read*, and the value is the engine's prose being judged on its exact wording: collapsing
    whitespace here would silently rewrite the text the harness exists to compare, which is a worse
    failure than the one being fixed. Squashing at entry is also unavailable — these strings arrive
    inside `EngineOutput`/`Brief`, which `consensus`, `movements` and `_challenge_themes` read too, so
    a squash there would change what the lens concludes. `_cluster_headlines` is the one place the
    entry-squash *is* right, and it does it, for the reason stated at that line."""
    path = runs_path(slug)
    rel_path = f"fixtures/golden/{slug}.runs.json"
    old_text = _head_version(rel_path)
    if not path.exists() or old_text is None:
        print(f"\n{slug}\n  ! need both a working-tree capture and a HEAD baseline")
        return
    # A baseline is what this whole readout is about here too -- the freshness line is not just a
    # `diff_one` fixture, it belongs to every reader of a per-request baseline (#405).
    _show_freshness(rel_path)
    new_text = path.read_text(encoding="utf-8")
    _show_model(old_text, new_text)
    for title, text in (("HEAD", old_text), ("working tree", new_text)):
        print(f"\n{slug} — {title}")
        turns = load_turns(text)
        if turns is not None:
            # An interactive capture: the question that settles this issue is *when* something was
            # asked, not whether it was, so the turn number leads and an already-answered slot is
            # marked. Reading the final model's questions alone would show only what the last turn
            # happened to still be asking.
            for i, run in enumerate(turns, 1):
                print(f"  run {i}")
                covered: set[str] = set()
                for turn in run:
                    print(f"    turn {turn.index}")
                    for q in turn.model.questions:
                        again = "  ← already answered" if q.slot in covered else ""
                        print(f"      [{display_token(q.slot)}] {display_token(q.q)}{again}")
                    if turn.answered:
                        print(f"      answered: {', '.join(map(display_token, turn.answered))}")
                    covered.update(turn.answered)
            continue
        for i, m in enumerate(load_runs(text), 1):
            print(f"  run {i}")
            for q in m.questions:
                print(f"    [{display_token(q.slot)}] {display_token(q.q)}")
        for i, b in enumerate(load_briefs(text), 1):
            print(f"  run {i} — challenges")
            for c in b.challenges:
                # headline+premise names the contest; alternative+recommendation are what separate a
                # real architect's pushback from a bare observation — show them so a prompt edit can be
                # judged on the half of the challenge that actually carries the domain grounding.
                print(f"    ‹{display_token(c.headline)}› {display_token(c.premise)}")
                print(f"        alt: {display_token(c.alternative)}")
                print(f"        rec: {display_token(c.recommendation)}")


def main(argv: list[str]) -> int:
    # First, before anything can print: a box rule or an arrow must not be able to kill this script
    # on a console that cannot encode it (invariant 16, #164).
    configure_output()
    show_questions = "--questions" in argv
    argv = [a for a in argv if a != "--questions"]
    slugs = argv or sorted(p.name[: -len(".runs.json")]
                           for p in GOLDEN.glob("*.runs.json"))
    if not slugs:
        print("No golden baselines found. Run golden_run.py first.", file=sys.stderr)
        return 1

    if show_questions:
        for slug in slugs:
            questions_one(slug)
        return 0

    print("Golden diff — working tree vs HEAD (strong = every run agrees, before and after)")
    results = [diff_one(slug) for slug in slugs]
    moved, weak, stale = (results.count(k) for k in ("moved", "weak", "stale"))
    line = f"{moved}/{len(slugs)} request(s) moved on strong signal."
    if weak:
        line += f"  {weak} moved on weak signal only (watch, don't act)."
    if stale:
        line += f"  ⚠ {stale} not re-captured — that is not a clean bill of health."
    print(f"\n{'─' * 60}\n{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
