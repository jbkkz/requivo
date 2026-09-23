#!/usr/bin/env python
"""The regression lens — changes that clear the noise floor, not run-to-run jitter.

Compares each request's working-tree K-run capture against its committed baseline in ``HEAD``. A
slot dimension *moved* only when the old baseline was unanimous on it and the new consensus shifted:
**strong** if the new runs are unanimous too, **weak** on a bare majority. With no baseline yet it
prints the noise floor. Interactive requests add the deep-turn lens (#137), ``--brief`` captures the
assessment lens; every lens runs and the verdict is their union (#162,
`test_the_verdict_is_the_union_of_the_lenses_that_ran`). Captures under different perimeters are
refused before either side is parsed (#621). The manual is ``docs/evaluations.md``.

Usage:
    python scripts/golden_diff.py              # every request
    python scripts/golden_diff.py <slug>...    # only the named one(s)
    python scripts/golden_diff.py <slug> --questions   # the questions themselves, old vs new"""

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
    captured_perimeter,
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
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402
from requivo.core.selectors import display_token  # noqa: E402


def _head_version(rel_path: str) -> str | None:
    res = subprocess.run(["git", "show", f"HEAD:{rel_path}"],
                         cwd=REPO, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def _show_freshness(rel_path: str) -> None:
    """Is the committed baseline at `rel_path` current with respect to `WATCHED_PATHS`? (#405, #410)

        Printed before any lens, in three states; `unknown` never renders as `current`
        (`test_an_unrecoverable_freshness_check_is_reported_as_unknown_not_current`).
    """
    fr = baseline_commits_since(rel_path)
    watched = ", ".join(WATCHED_PATHS)
    if fr["state"] == "unknown":
        # `reason` carries text from outside the process (git's stderr), so it is escaped too (#461).
        print(f"  ? baseline freshness: could not tell ({display_token(fr['reason'])})")
        return
    if fr["state"] == "current":
        print(f"  · baseline current as of {fr['captured_at']} (no commits since touching {watched})")
        return
    commits = fr["commits"]
    print(f"  ⚠ baseline captured {fr['captured_at']}; {len(commits)} commit(s) touching {watched} "
          f"since — any movement below may be their combined effect, not only a working-tree edit:")
    for c in commits[:5]:
        # A commit subject is contributor text: escaped, or a raw CR could overwrite the date/sha prefix
        # (invariant 14, test_a_hostile_freshness_reason_cannot_forge_a_line).
        print(f"      {display_token(c['date'])}  {display_token(c['sha'])}  "
              f"{display_token(c['subject'])}")
    if len(commits) > 5:
        print(f"      … and {len(commits) - 5} more")


def _show_model(old_text: str | None, new_text: str | None) -> None:
    """Which model each side was captured on, beside the freshness line (#515).

        Nothing else watches the model, and a swap would read as prompt movement in every lens. A baseline
        with no model key renders *unknown*, never agreement
        (`test_a_baseline_with_no_model_key_does_not_read_as_agreement`).
    """
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


def _perimeters_comparable(old_text: str, new_text: str) -> bool:
    """Print each side's perimeter and whether a comparison is possible at all (#621).

        A slot id means something different in each perimeter's schema, so a mismatch is refused — said
        on its own line, moving no verdict — where a model swap is only named.
    """
    old_p, new_p = captured_perimeter(old_text), captured_perimeter(new_text)
    if old_p == new_p:
        print(f"  · captured under perimeter {display_token(old_p)} (baseline and candidate agree)")
        return True
    print(f"  ! cannot compare — baseline captured under perimeter {display_token(old_p)}, this "
          f"capture under {display_token(new_p)}; a slot id means a different thing in each, so "
          f"nothing below would be a real comparison (commit this as a first capture for "
          f"{display_token(new_p)} instead)")
    return False


def diff_one(slug: str) -> str:
    """Print the signal for one request; returns ``moved``, ``flat``, ``stale`` or ``perimeter_mismatch``.

        ``stale`` is no capture on disk, or one byte-identical to HEAD. The perimeter check reads the raw
        envelopes *before* either side is parsed: parsing first crashed on a real go-to-market capture
        (#621, `test_a_perimeter_mismatch_refuses_before_the_candidate_is_ever_parsed`).
    """
    path = runs_path(slug)
    rel_path = f"fixtures/golden/{slug}.runs.json"
    old_text = _head_version(rel_path)

    print(f"\n{slug}")
    if old_text is not None:
        # Freshness is about the committed baseline; the NEW branch below names its absence.
        _show_freshness(rel_path)
        _show_model(old_text, path.read_text(encoding="utf-8") if path.exists() else None)

    if not path.exists():
        print("  ! no working-tree capture (run golden_run.py first)")
        return "stale"

    new_text = path.read_text(encoding="utf-8")
    new_perimeter = captured_perimeter(new_text)

    if old_text is None:
        # No baseline yet: report the noise floor, under this capture's own perimeter.
        new = load_runs(new_text, perimeter=new_perimeter)
        st = stability(new, perimeter=new_perimeter)
        print("  ⊕ NEW (no baseline in HEAD)")
        print(f"  noise floor  {st['unanimous']['impact']}/{st['total_slots']} slots unanimous on "
              f"impact, {st['unanimous']['state']}/{st['total_slots']} on confidence, across "
              f"{st['n']} runs")
        print(f"  stable themes: {', '.join(st['themes']) or '—'}")
        # On a first capture these readouts *are* the finding (#137, #162).
        _show_turns(None, load_turns(new_text, perimeter=new_perimeter), load_answers(new_text),
                   perimeter=new_perimeter)
        _show_assessment(None, load_briefs(new_text))
        return "moved"

    if new_text == old_text:
        # Byte-identical to HEAD means the capture never landed (a real re-run cannot reproduce a file);
        # "no change" here would be a false all-clear.
        print("  ! capture identical to HEAD — not re-captured (re-run golden_run.py)")
        return "stale"

    # The gate: envelope metadata only, no `EngineOutput` built on either side yet.
    if not _perimeters_comparable(old_text, new_text):
        return "perimeter_mismatch"

    # Reachable only once both sides are confirmed to agree, so `perimeter` below is unambiguous.
    old = load_runs(old_text, perimeter=new_perimeter)
    new = load_runs(new_text, perimeter=new_perimeter)
    m = movements(old, new, perimeter=new_perimeter)

    # Every lens runs, and the verdict is the union of those that ran: a flat slot consensus is not
    # evidence against the assessment lens (#162,
    # test_the_assessment_lens_runs_when_the_slot_consensus_held_still).
    signals = [
        _show_turns(load_turns(old_text, perimeter=new_perimeter),
                   load_turns(new_text, perimeter=new_perimeter), load_answers(new_text),
                   perimeter=new_perimeter),
        _show_slots(m),
        _show_assessment(load_briefs(old_text), load_briefs(new_text)),
    ]
    if "strong" in signals:
        return "moved"
    return "weak" if "weak" in signals else "flat"


def _show_slots(m: dict) -> str | None:
    """Print what moved in the slot consensus; returns `strong`, `weak` or None (never a run verdict, #162)."""
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


def _show_turns(old_turns, new_turns, layers: dict[str, list[str]] | None = None, *,
                perimeter: str = DEFAULT_PERIMETER) -> str | None:
    """Print what the interactive capture says about turn 3 and beyond; returns `strong` or None.

        States: not applicable (prints nothing), compared, first capture (the readout is the finding),
        lens lost (loud). `layers` adds a line only on a SHALLOW capture (#163,
        `test_a_shallow_capture_reports_which_sheet_layers_went_unused`).
    """
    if new_turns is None and old_turns is None:
        return None
    if new_turns is None:
        print("  interactive  ! the baseline has turns and this capture does not — the deep-turn "
              "lens is gone, which is not the same as clean")
        return "strong"

    lens = turn_lens(new_turns, layers, perimeter=perimeter)
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
        # #163: the sheet, not the engine, may be why this run stopped short.
        detail = ", ".join(f"{lab} ({c})" for lab, c in sorted(lens["unreached_layers"].items()))
        print(f"               {'sheet layers never reached':<38} {detail}")

    move = turn_movements(old_turns, new_turns, perimeter=perimeter)
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
    """Print what moved in the assessment; returns `strong`, `weak` or None.

        States: not captured (named on its own line; `--brief` is per-invocation), first capture,
        baseline only (`!`, yet nothing measured —
        `test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal`), compared.
        A lost challenge theme is strong on its own: the pushback quietly disappearing is the regression.
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
        # A gained challenge is watched, not acted on: often a rephrasing that cleared clustering.
        tier = tier or "weak"
        print(f"  assessment + challenge(s) now raised: {'; '.join(b['themes_added'])}")
    if not (b["complexity"] or b["themes_added"] or b["themes_removed"]):
        print("  assessment · verdict and challenges unchanged")
    return tier


def questions_one(slug: str) -> None:
    """Print the questions each baseline actually asked, run by run, old then new.

        Every string is provider prose read off disk, so it goes through `display_token` (invariant 14,
        #40): `test_a_forged_question_cannot_write_a_line_of_the_golden_readout` and
        `test_an_ordinary_question_is_rendered_byte_for_byte`. Not `plugin_cli_drift._log_safe`'s lossy
        squash (#139, #176): here the exact wording is what is being judged.
    """
    path = runs_path(slug)
    rel_path = f"fixtures/golden/{slug}.runs.json"
    old_text = _head_version(rel_path)
    if not path.exists() or old_text is None:
        print(f"\n{slug}\n  ! need both a working-tree capture and a HEAD baseline")
        return
    # The freshness line belongs to every reader of a baseline (#405).
    _show_freshness(rel_path)
    new_text = path.read_text(encoding="utf-8")
    _show_model(old_text, new_text)
    # Informational here: this view lists questions rather than diffing slot ids (#621).
    _perimeters_comparable(old_text, new_text)
    for title, text in (("HEAD", old_text), ("working tree", new_text)):
        print(f"\n{slug} — {title}")
        # Each side parses against its own recorded perimeter: this view never gates on agreement (#621).
        text_perimeter = captured_perimeter(text)
        turns = load_turns(text, perimeter=text_perimeter)
        if turns is not None:
            # Interactive: *when* a question was asked matters, so the turn leads and answered slots are marked.
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
        for i, m in enumerate(load_runs(text, perimeter=text_perimeter), 1):
            print(f"  run {i}")
            for q in m.questions:
                print(f"    [{display_token(q.slot)}] {display_token(q.q)}")
        for i, b in enumerate(load_briefs(text), 1):
            print(f"  run {i} — challenges")
            for c in b.challenges:
                # alternative+recommendation carry the domain grounding a prompt edit is judged on.
                print(f"    ‹{display_token(c.headline)}› {display_token(c.premise)}")
                print(f"        alt: {display_token(c.alternative)}")
                print(f"        rec: {display_token(c.recommendation)}")


def main(argv: list[str]) -> int:
    # First: a glyph must not kill this script on a console that cannot encode it (invariant 16, #164).
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
    moved, weak, stale, mismatched = (
        results.count(k) for k in ("moved", "weak", "stale", "perimeter_mismatch"))
    line = f"{moved}/{len(slugs)} request(s) moved on strong signal."
    if weak:
        line += f"  {weak} moved on weak signal only (watch, don't act)."
    if stale:
        line += f"  ⚠ {stale} not re-captured — that is not a clean bill of health."
    if mismatched:
        # A perimeter change is its own count, not `stale` (#621).
        line += (f"  ⚠ {mismatched} could not be compared — captured under a different perimeter "
                 f"than its baseline.")
    print(f"\n{'─' * 60}\n{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
