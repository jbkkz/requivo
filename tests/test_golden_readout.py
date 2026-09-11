"""The golden harness's readout: what each lens says, what the run's verdict is, and what happens
when the console cannot encode it.

Two defects, both found by the #137 lane and filed rather than folded into that diff.

**#162 — a lens that never ran, reported as a lens that ran and found nothing.** `diff_one`
short-circuited the *whole function* on a flat slot consensus: it printed "no change above the noise
floor" and returned before the assessment lens ran. So a `brief.md` edit that moved the complexity
verdict or the challenges without moving a single slot reported as no change -- on the one capture a
maintainer had paid double for, since `--brief` doubles that request's calls. `golden_diff`'s own
docstring names the rule this breaks: a false all-clear is the one failure mode a regression lens
must not have. This is that failure one lens over.

**#164 — invariant 16, in the scripts that measure the product.** Neither harness script routed its
output through `streams.py`, so a glyph the console cannot encode raised `UnicodeEncodeError` at the
`print` -- after the capture had been paid for and written to disk.

Nothing here touches the network or the committed baselines. `GOLDEN` is redirected into a tmp
directory and `_head_version` is stubbed, so every capture below is a fixture; `golden_run`'s client
and capture loop are stubbed out entirely.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_diff  # noqa: E402
import golden_lib  # noqa: E402
import golden_run  # noqa: E402
from golden_lib import Turn  # noqa: E402

from requivo.core.analysis import slot_label  # noqa: E402
from requivo.core.contracts import (  # noqa: E402
    Brief,
    Challenge,
    Confidence,
    EngineOutput,
    Impact,
    Level,
    Question,
    Slot,
    Summary,
)

K = 3  # runs per captured baseline, matching the harness default


# ── builders ─────────────────────────────────────────────────────────────────────────────────────

def _model(impact: Impact = Impact.medium, completeness: int = 80) -> dict:
    """One captured run. `completeness` is what varies between two otherwise identical baselines:
    `movements()` grades impact and confidence only, so moving it makes the two files differ in bytes
    -- which is what stops `diff_one` reporting `stale` -- while leaving the slot consensus flat.
    That is the exact situation #162 is about."""
    slot = Slot(value="v", completeness=completeness, confidence=Confidence.explicit,
                impact=impact, evidence="e")
    return EngineOutput(model={"problem": slot}, questions=[],
                        summary=Summary()).model_dump(mode="json")


def _brief(contested: list[str], complexity: Level = Level.high) -> dict:
    """One captured assessment, contesting the named slots. `contests` is populated rather than left
    to the headline fallback so the theme labels are exact on both sides of a diff."""
    challenges = [Challenge(headline=f"about {slot_id}", premise="p", alternative="a",
                            consequence="c", recommendation="r", contests=[slot_id])
                  for slot_id in contested]
    return Brief(challenges=challenges, complexity=complexity).model_dump(mode="json")


def _capture(*, impact: Impact = Impact.medium, completeness: int = 80,
             briefs: list[dict] | None = None, model: str | None = None) -> str:
    """A `.runs.json` envelope with K identical runs, and optionally K assessments.

    `model` defaults to **absent**, which is what every baseline written before #515 looks like --
    so the lens tests above keep exercising the third state incidentally, the way a reader's own
    checkout does today."""
    body: dict = {"request": "r", "runs": [_model(impact, completeness) for _ in range(K)]}
    if model is not None:
        body["model"] = model
    if briefs is not None:
        body["briefs"] = briefs
    return json.dumps(body, indent=2)


def _briefs(contested: list[str], complexity: Level = Level.high, *, runs: int = K) -> list[dict]:
    return [_brief(contested, complexity) for _ in range(runs)]


_CURRENT_FRESHNESS = {"state": "current", "captured_at": "2026-01-01T00:00:00+00:00"}


@pytest.fixture
def diff(tmp_path, monkeypatch):
    """`diff_one` over two forged baselines. Returns its verdict and everything it printed.

    `baseline_commits_since` shells out to real git, which has nothing to do with the lens logic
    every other test in this module exercises -- stubbed to `current` by default so those tests stay
    hermetic and deterministic (an unstubbed call would answer against *this* checkout's own git
    history, which none of them are testing). The freshness tests below pass their own dict to reach
    the other two states."""
    def run(old_text: str | None, new_text: str, freshness: dict | None = None) -> tuple[str, list[str]]:
        monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(new_text, encoding="utf-8")
        monkeypatch.setattr(golden_diff, "_head_version", lambda _rel: old_text)
        monkeypatch.setattr(golden_diff, "baseline_commits_since",
                            lambda _rel: freshness or _CURRENT_FRESHNESS)
        buf = io.StringIO()
        with redirect_stdout(buf):
            verdict = golden_diff.diff_one("forged")
        return verdict, buf.getvalue().splitlines()
    return run


def _line(lines: list[str], needle: str) -> str | None:
    return next((ln for ln in lines if needle in ln), None)


# ── #515: the model a capture ran on, in three states ────────────────────────────────────────────


def _model_line(lines: list[str]) -> str:
    """The preamble's model line. Asserted to exist before anything reads it: a line that is simply
    absent must never be the way this readout renders a state, which is half of what #515 asks."""
    line = _line(lines, "model") or _line(lines, "captured on")
    assert line is not None, f"no capture-model line in the readout: {lines}"
    return line


def test_two_captures_on_the_same_model_say_so(diff):
    _, lines = diff(_capture(model="claude-sonnet-5"),
                    _capture(completeness=70, model="claude-sonnet-5"))
    line = _model_line(lines)
    assert "claude-sonnet-5" in line
    assert "agree" in line


def test_a_model_swap_is_named_rather_than_left_to_read_as_prompt_movement(diff):
    """The defect this key exists for: export `REQUIVO_MODEL=<something else>`, re-capture one
    request, and every lens below reports the swap as a prompt edit -- #405's confident readout,
    about something the reader did not change."""
    _, lines = diff(_capture(model="claude-sonnet-5"),
                    _capture(completeness=70, model="claude-opus-4-8"))
    line = _model_line(lines)
    assert "claude-sonnet-5" in line and "claude-opus-4-8" in line
    assert "agree" not in line


def test_a_baseline_with_no_model_key_does_not_read_as_agreement(diff):
    """The third state, and the whole of it. Every baseline on disk the day this landed carries no
    model key; rendering that as "same model" -- or as nothing at all -- is the collapse
    `_freshness_from_git_data` already refuses one axis along: *`unknown` must never read as
    `current`*."""
    _, lines = diff(_capture(), _capture(completeness=70, model="claude-sonnet-5"))
    line = _model_line(lines)
    assert "unknown" in line
    assert "agree" not in line or "nothing here says the two agree" in line
    assert "claude-sonnet-5" in line, (
        "the half that *is* known should still be stated -- an unknown baseline is not an unknown "
        "candidate")


# ── #162: every lens runs, and the verdict is the union of the ones that did ─────────────────────

def test_the_assessment_lens_runs_when_the_slot_consensus_held_still(diff):
    """The finding. Slots flat, challenges moved: the run must report the challenge and must not
    report itself as flat.

    Asserting the *verdict* is not enough on its own and asserting the exit code is worth nothing at
    all -- `golden_diff` exited 0 throughout the defect. What settles it is that the assessment line
    is present, because that is the lens having actually looked."""
    old = _capture(completeness=80, briefs=_briefs(["problem", "workflow"]))
    new = _capture(completeness=70, briefs=_briefs(["workflow"]))

    verdict, lines = diff(old, new)

    lost = _line(lines, "no longer raised")
    assert lost is not None, lines
    assert slot_label("problem") in lost, lost
    assert verdict == "moved", lines
    # The short-circuit still decides the *slot* section, and only that: the flat line is the honest
    # readout for a consensus that held still, and removing it would trade one silence for another.
    assert _line(lines, "no change above the noise floor") is not None, lines


def test_a_captured_assessment_that_held_still_says_so_rather_than_going_quiet(diff):
    """The positive control for the test above, and for the one below it.

    An assertion that a lens reported nothing passes when the lens never ran, so the clean case has
    to *speak*: `verdict and challenges unchanged` is what distinguishes a measurement from an
    absence, and it is a different sentence from the not-captured line."""
    briefs = _briefs(["problem"])
    verdict, lines = diff(_capture(completeness=80, briefs=briefs),
                          _capture(completeness=70, briefs=briefs))

    assert _line(lines, "verdict and challenges unchanged") is not None, lines
    assert _line(lines, "did not look") is None, lines
    assert verdict == "flat", lines


def test_an_assessment_nobody_captured_is_named_as_a_lens_that_did_not_look(diff):
    """The third state. `--brief` is an opt-in flag rather than a property of the request, so an
    absent assessment is *not measured* -- and with nothing said, it reads exactly like the clean
    case above. One line is what separates them."""
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70))

    not_run = _line(lines, "did not look")
    assert not_run is not None, lines
    assert "--brief" in not_run, not_run
    # must not fire: a lens that did not look reports no finding, so it cannot move the verdict.
    assert _line(lines, "verdict and challenges unchanged") is None, lines
    assert verdict == "flat", lines


def test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal(diff):
    """HEAD has an assessment and this capture does not.

    Marked `!` because committing this capture would drop a lens the baseline had — and graded as
    *nothing measured*, not as a finding. This was `strong` when the change was first written, on the
    analogy of `_show_turns`' matching state, and the analogy fails: interactivity is declared in
    `requests.md` and reproduced on every capture, while `--brief` is a per-invocation flag no
    capture remembers. Every single-pass baseline in `fixtures/golden/` carries one, so grading this
    strong turned the documented no-`--brief` workflow into six strong signals over a run where
    nothing moved. The assertion that matters is the second one."""
    verdict, lines = diff(_capture(completeness=80, briefs=_briefs(["problem"])),
                          _capture(completeness=70))

    dropped = _line(lines, "nothing to compare")
    assert dropped is not None, lines
    assert dropped.lstrip().startswith("assessment !"), dropped
    assert verdict == "flat", lines
    # must not fire: this state is louder than the never-captured one and must not be the same line.
    assert _line(lines, "did not look") is None, lines


def test_a_first_capture_prints_the_assessment_it_has_nothing_to_compare_against(diff):
    """No baseline in HEAD at all. There is nothing to diff, and the consensus readout is the finding
    -- the same shape the noise floor beside it already has."""
    verdict, lines = diff(None, _capture(briefs=_briefs(["problem"], Level.medium)))

    first = _line(lines, "first capture")
    assert first is not None, lines
    assert "medium" in first, first
    assert verdict == "moved", lines  # a fresh capture is always worth reading


def test_the_verdict_is_the_union_of_the_lenses_that_ran(diff):
    """What happens when the lenses disagree: the strongest signal any of them produced wins. They
    are independent measurements of one capture, not votes on one question, so a null result from one
    is not evidence against a finding from another."""
    # slots moved strongly, assessment clean -> still strong.
    briefs = _briefs(["problem"])
    verdict, lines = diff(_capture(impact=Impact.low, briefs=briefs),
                          _capture(impact=Impact.high, briefs=briefs))
    assert verdict == "moved", lines
    assert _line(lines, "verdict and challenges unchanged") is not None, lines

    # slots flat, assessment moved on a bare majority -> weak, not flat and not strong.
    split = _briefs([], Level.medium, runs=2) + _briefs([], Level.high, runs=1)
    verdict, lines = diff(_capture(completeness=80, briefs=_briefs([], Level.high)),
                          _capture(completeness=70, briefs=split))
    assert verdict == "weak", lines
    assert _line(lines, "assessment weak complexity") is not None, lines


# ── #163: the sheet a SHALLOW capture never got to ───────────────────────────────────────────────
#
# `_show_turns` prints `unreached_layers` from `turn_lens` only when a run stopped short of
# `MEASURABLE_DEPTH` -- a healthy capture is deliberately given a sheet deeper than five turns so it
# never runs dry before the loop's own cap, and leftover layers there are by design, not a finding.

def _q(slot: str) -> Question:
    return Question(q=f"tell me about {slot}", slot=slot, why="drives the shape")


def _iturn(index: int, answered: list[str], *, asks: tuple = ()) -> Turn:
    return Turn(index=index, answered=list(answered),
                model=EngineOutput(model={}, questions=[_q(s) for s in asks], summary=Summary()))


def test_a_shallow_capture_reports_which_sheet_layers_went_unused():
    """The #163 finding. A run that converged at turn 2 with two of three `business_rules` layers
    still on the sheet has to say so -- that is exactly the diagnosis that had to be run by hand to
    explain the 4/5/4 depths."""
    layers = {"business_rules": ["l1", "l2", "l3"]}
    run = [_iturn(1, ["business_rules"], asks=("business_rules",)), _iturn(2, [])]
    buf = io.StringIO()
    with redirect_stdout(buf):
        golden_diff._show_turns(None, [run], layers)
    unused = _line(buf.getvalue().splitlines(), "sheet layers never reached")
    assert unused is not None, buf.getvalue()
    assert "Business rules" in unused and "2" in unused, unused


def test_a_deep_capture_with_layers_left_over_does_not_report_them():
    """must not fire: leftover layers on a run that reached `MEASURABLE_DEPTH` are by design -- the
    sheet is deliberately authored deeper than five turns so a run doesn't go dry before the loop's
    own cap. Reporting them here would be noise on every healthy capture."""
    layers = {"business_rules": [f"l{i}" for i in range(1, 11)]}
    run = [_iturn(i, ["business_rules"], asks=("business_rules",)) for i in range(1, 6)]
    buf = io.StringIO()
    with redirect_stdout(buf):
        golden_diff._show_turns(None, [run], layers)
    assert _line(buf.getvalue().splitlines(), "sheet layers never reached") is None, buf.getvalue()


def test_a_shallow_capture_with_nothing_left_on_the_sheet_reports_nothing():
    """must not fire, the other control: a run can converge early because the engine genuinely
    moved on, with the sheet fully spent. That is not this diagnosis and must not print as one."""
    layers = {"business_rules": ["l1"]}
    run = [_iturn(1, ["business_rules"], asks=("business_rules",)), _iturn(2, [])]
    buf = io.StringIO()
    with redirect_stdout(buf):
        golden_diff._show_turns(None, [run], layers)
    assert _line(buf.getvalue().splitlines(), "sheet layers never reached") is None, buf.getvalue()


# ── #164: a glyph must not be able to kill a script after the work has landed ────────────────────
#
# `PYTHONIOENCODING=ascii` is what reaches a real strict encoder on every platform rather than only
# on a Windows leg -- `streams._target_encoding` honours an operator-named codec, so without it
# `configure_streams` would move the stream to UTF-8 and these tests would prove nothing. It is the
# same mechanism `tests/test_encoding.py` uses for the product's own streams.

@pytest.fixture
def ascii_console(monkeypatch):
    """Substitute stdout and stderr with real ASCII-strict encoders, and hand back stdout's bytes."""
    def install() -> io.BytesIO:
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
        monkeypatch.setattr(sys, "stderr",
                            io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
        return raw
    return install


@pytest.fixture
def golden_diff_run(tmp_path, monkeypatch):
    """`golden_diff.main([])` over one forged capture with no baseline in HEAD."""
    def run() -> int:
        monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
        monkeypatch.setattr(golden_diff, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(_capture(), encoding="utf-8")
        monkeypatch.setattr(golden_diff, "_head_version", lambda _rel: None)
        return golden_diff.main([])
    return run


@pytest.fixture
def golden_run_run(tmp_path, monkeypatch):
    """`golden_run.main([])` with the client and the capture loop stubbed out -- no key, no call, no
    write. What survives is the header line, which is where the glyphs are."""
    def run() -> int:
        monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
        monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
        monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
        monkeypatch.setattr(golden_run, "parse_requests",
                            lambda _path: [{"slug": "forged", "request": "r", "answers": {}}])
        monkeypatch.setattr(golden_run, "capture", lambda *a, **k: None)
        return golden_run.main([])
    return run


@pytest.mark.parametrize("script, runner", [("golden_diff", "golden_diff_run"),
                                            ("golden_run", "golden_run_run")])
def test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams(
        script, runner, request, ascii_console, monkeypatch):
    """must fire. Without this the two silence assertions below would pass on a harness that printed
    nothing at all, or on a script that had quietly stopped emitting the glyph rather than surviving
    it -- which is the sweep #164 explicitly refuses."""
    ascii_console()
    monkeypatch.setattr(sys.modules[script], "configure_output", lambda: None)
    with pytest.raises(UnicodeEncodeError):
        request.getfixturevalue(runner)()


@pytest.mark.parametrize("runner", ["golden_diff_run", "golden_run_run"])
def test_a_harness_script_survives_a_console_that_cannot_encode_its_output(
        runner, request, ascii_console):
    """must not fire, and the escape is the evidence it ran rather than fell silent.

    The handler is asserted directly rather than by hunting for a `?` in the bytes. `backslashreplace`
    over `replace` is the decision that matters — a reader cannot tell a substituted character from
    one that was never there — but scanning the output for `?` would couple this test to the claim
    that no line of the harness ever legitimately prints a question mark, which is true today and is
    not something this test is entitled to assume."""
    raw = ascii_console()
    assert request.getfixturevalue(runner)() == 0
    sys.stdout.flush()
    out = raw.getvalue()
    assert b"\\u" in out or b"\\x" in out, out
    assert sys.stdout.errors == "backslashreplace", sys.stdout.errors

# -- #405/#410: baseline freshness is named before any lens output ------------------------------
#
# `diff_one` reports whether the committed baseline in HEAD predates a commit that changes what a
# capture measures (`WATCHED_PATHS`) -- printed first, so a reader sees it before reading a single
# slot or assessment movement below. Three states, and the third (`unknown`) must not collapse into
# the clean one: `_freshness_from_git_data` is unit-tested directly in `tests/test_golden_lib.py`;
# these three exercise `diff_one`'s own reporting of what `baseline_commits_since` hands back.

def test_a_stale_baseline_is_named_before_any_lens_output(diff):
    """The finding. A baseline that predates a watched-path commit has to say so, by name, ahead of
    the slot/assessment sections -- CLAUDE.md's own worked example for this: "baseline captured
    2026-08-01; 3 asset commits since"."""
    stale = {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
             "commits": [{"sha": "abc123def", "date": "2026-08-15", "subject": "edit engine.md"},
                        {"sha": "def456abc", "date": "2026-08-20", "subject": "add a context card"}]}
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=stale)

    warned = _line(lines, "baseline captured")
    assert warned is not None, lines
    assert "2026-08-01" in warned and "2 commit(s)" in warned, warned
    assert _line(lines, "abc123def") is not None, lines
    assert _line(lines, "edit engine.md") is not None, lines
    # it leads the readout: printed before the slot section runs at all.
    noise = _line(lines, "no change above the noise floor")
    assert lines.index(warned) < lines.index(noise), lines


def test_a_current_baseline_says_so_without_alarm(diff):
    """must not fire, the positive control: a baseline with nothing watched changed since it was
    captured reports plainly, with no warning glyph and no commit count -- the same shape
    `golden_diff`'s own "verdict and challenges unchanged" line has for the assessment lens."""
    current = {"state": "current", "captured_at": "2026-08-01T00:00:00+00:00"}
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=current)

    said = _line(lines, "baseline current")
    assert said is not None, lines
    assert "2026-08-01" in said, said
    assert _line(lines, "⚠") is None, lines
    assert _line(lines, "commit(s)") is None, lines


def test_an_unrecoverable_freshness_check_is_reported_as_unknown_not_current(diff):
    """must fire -- the third state. A shallow clone or a git failure has to read as *could not
    tell*, never silently as *current*: the same collapse `golden_diff`'s own module docstring
    already refuses for a byte-identical capture, one layer up, for a commit count instead of a
    byte comparison."""
    unknown = {"state": "unknown", "reason": "shallow clone -- commit history is truncated"}
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=unknown)

    said = _line(lines, "could not tell")
    assert said is not None, lines
    assert "shallow clone" in said, said
    # must not fire: an unrecoverable check must never render as the clean state.
    assert _line(lines, "baseline current") is None, lines


def test_a_hostile_freshness_reason_cannot_forge_a_line(diff):
    """must fire -- #461. `reason` is the only one of `_show_freshness`'s three printed fields that
    carries text from outside the process (git's stderr, or `str(exc)`) rather than a fixed git
    format like `%cI`/`%H` -- #456 wrapped `date` and `sha` in `display_token` on exactly that
    argument, for the ``stale`` branch's commit rows, and left this ``unknown`` branch's `reason`
    raw. A `\r` in it moves the cursor back to column 0 and prints past the "could not tell ("
    prefix, forging what reads as an unrelated second line -- the same shape #456 already fixed one
    print site over, for a commit subject.

    The must-not-fire control lives beside it, in
    `test_an_unrecoverable_freshness_check_is_reported_as_unknown_not_current` above: an ordinary
    reason with no control character renders unchanged."""
    hostile = {"state": "unknown",
               "reason": "git log failed: fatal: bad object\rFORGED continuation"}
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=hostile)

    said = _line(lines, "could not tell")
    assert said is not None, lines
    # must fire: the raw CR must never split the reason into a line of its own, unprefixed.
    forged = next((ln for ln in lines if "FORGED continuation" in ln and "could not tell" not in ln),
                  None)
    assert forged is None, lines
    # the reason survives, escaped rather than dropped or silently truncated.
    assert "\\r" in said, said
    assert "FORGED continuation" in said, said

