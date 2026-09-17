"""The golden harness end to end, offline: capture, run selection, the readout, the baselines (#137, #275, #276)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from _fakes import printed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_diff  # noqa: E402
import golden_lib  # noqa: E402
import golden_run  # noqa: E402
from golden_lib import GOLDEN, REQUESTS, Turn, captured_model, load_turns, parse_requests  # noqa: E402
from golden_run import planned_calls, select_runs  # noqa: E402

from requivo.core.analysis import slot_label  # noqa: E402
from requivo.core.contracts import Brief, Challenge, EngineOutput, Impact, Level, Question, Slot, Summary  # noqa: E402
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402
from requivo.services.discovery import DiscoveryService  # noqa: E402

K = 3  # runs per captured baseline, matching the harness default


def _line(lines: list[str], *needles: str) -> str | None:
    """The first line carrying every needle."""
    return next((ln for ln in lines if all(n in ln for n in needles)), None)


# ── the interactive capture loop, driven over a scripted provider (#137, #163) ────────────────────

def _asking(*slots: str) -> EngineOutput:
    """A turn's reply that asks about each named slot."""
    return EngineOutput(model={"problem": Slot(value="v", completeness=60, confidence="inferred", impact="high")},
                        questions=[Question(q=f"about {s}?", slot=s, why="it drives the shape") for s in slots],
                        summary=Summary(objective="an objective"))


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """`capture_interactive` over scripted replies: the `draft_turn` calls, the loaded file, the output (#163)."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)

    def run(replies, answers, *, k=1, turns=5, perimeter=None):
        calls: list[dict] = []

        def fake_draft_turn(self, request, *, current_model=None, answers=None, cards=None, perimeter="__UNSET__"):
            # No real default: a fallback here would hide `capture_interactive` dropping the keyword (#621).
            calls.append({"request": request, "current_model": current_model, "answers": answers, "cards": cards,
                          "perimeter": perimeter})
            turn = 0 if current_model is None else run.turn + 1
            run.turn = turn
            return replies[min(turn, len(replies) - 1)]

        monkeypatch.setattr(DiscoveryService, "draft_turn", fake_draft_turn)
        monkeypatch.setattr(golden_run, "K", k)
        monkeypatch.setattr(golden_run, "TURNS", turns)
        req = {"slug": "scripted", "form": "f", "card": "c", "request": "a request", "answers": answers,
               **({"perimeter": perimeter} if perimeter is not None else {})}
        # A stand-in client object rather than `None` (#515).
        output = printed(golden_run.capture_interactive, client=object(), req=req, model="claude-sonnet-5")
        return calls, load_turns((tmp_path / "scripted.runs.json").read_text(encoding="utf-8")), output

    run.turn = 0
    return run


@pytest.mark.parametrize("perimeter, expected", [("go-to-market", "go-to-market"), (None, DEFAULT_PERIMETER)],
                         ids=["the-requests-own", "no-perimeter-key-reads-software"])
def test_the_capture_threads_the_requests_own_perimeter_to_draft_turn(capture, perimeter, expected):
    """#621: the parsed block's `perimeter`, or the software default when the block has no key at all."""
    assert capture([_asking()], {}, perimeter=perimeter)[0][0]["perimeter"] == expected


def test_the_interactive_capture_records_the_model_it_reasoned_on(capture, tmp_path):
    """#515: the envelope records the conditions a capture ran under, not only its input."""
    capture([_asking()], {})
    assert captured_model((tmp_path / "scripted.runs.json").read_text(encoding="utf-8")) == "claude-sonnet-5"


def test_the_capture_reasons_through_the_interactive_seam_and_not_a_message_list(capture):
    """`draft_turn` is the production interactive path (#77), so it is the one the measurement must use."""
    calls, _, _ = capture([_asking("problem"), _asking("actors"), _asking()], {"problem": ["p"], "actors": ["a"]})
    assert calls[0]["current_model"] is None and calls[0]["answers"] is None
    assert all(c["request"] == "a request" for c in calls) and calls[1]["current_model"] is not None
    assert "[slot: problem]" in calls[1]["answers"] and "[slot: actors]" in calls[2]["answers"]


@pytest.mark.parametrize("replies, answers", [
    pytest.param([_asking("problem"), _asking("risks")], {"problem": ["p"]}, id="the-sheet-has-nothing-left"),
    pytest.param([_asking("problem"), _asking()], {"problem": ["p"], "actors": ["a"]}, id="the-engine-stops-asking"),
])
def test_a_run_stops_when_the_sheet_or_the_engine_has_nothing_left_to_say(capture, replies, answers):
    """The same two events that end `converse()` end a capture."""
    calls, captured, _ = capture(replies, answers, turns=5)
    assert len(calls) == 2 and [t.index for t in captured[0]] == [1, 2], "the capture kept paying"


def test_the_final_turn_records_no_answer_it_never_sent(capture):
    """`answered` is what the conversation covered; the re-ask count is measured against exactly that."""
    _, captured, _ = capture([_asking("problem"), _asking("actors"), _asking("risks")],
                             {"problem": ["p"], "actors": ["a"], "risks": ["r"]}, turns=3)
    assert [t.answered for t in captured[0]] == [["problem"], ["actors"], []]


def test_every_run_starts_the_sheet_over(capture):
    """K runs are K independent conversations, each on a fresh sheet."""
    calls, captured, _ = capture([_asking("problem"), _asking("problem"), _asking()], {"problem": ["first", "second"]}, k=2)
    assert all("first" in a for a in (calls[1]["answers"], calls[4]["answers"])) and len(captured) == 2


@pytest.mark.parametrize("replies, answers, must_report", [
    pytest.param([_asking("problem"), _asking()], {"problem": ["first", "second", "third"]}, True, id="#163-shallow-must-fire"),
    pytest.param([_asking("problem")] * 5, {"problem": [f"l{i}" for i in range(1, 11)]}, False, id="#163-deep-must-not-fire"),
])
def test_capture_reports_unreached_sheet_layers_only_when_shallow(capture, replies, answers, must_report):
    """#163: a run that converges early names the sheet layers it never reached, where the calls were spent."""
    output = capture(replies, answers)[2]
    assert ("sheet layers never reached" in output) is must_report and ("Real problem (2)" in output) is must_report, output


# ── which requests a `golden_run.py` invocation captures, and what it announces (#276, #290, #515) ─

def _req(slug: str, answers: dict | None = None) -> dict:
    return {"slug": slug, "request": f"request for {slug}", "answers": answers or {}}


_SINGLE, _INTERACTIVE = _req("single-pass-a"), _req("interactive-a", answers={"problem": ["layer one"]})


@pytest.fixture
def golden_main(tmp_path, monkeypatch):
    """`golden_run.main([])` with the client and the capture loop stubbed out: the rc and what `capture` saw."""
    def run(requests: list[dict], **patches):
        seen: list[tuple[str, str]] = []
        (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
        stubs = dict(GOLDEN=tmp_path, REPO=tmp_path.parent, REQUESTS=tmp_path / "requests.md",
                     Anthropic=lambda *a, **k: object(), parse_requests=lambda _path: list(requests),
                     capture=lambda _client, req, _with_brief, *, model: seen.append((req["slug"], model)), **patches)
        for name, value in stubs.items():
            monkeypatch.setattr(golden_run, name, value)
        return golden_run.main([]), seen
    return run


@pytest.mark.parametrize("runs, wanted, capture_all, selected, skipped", [
    pytest.param([_SINGLE, _INTERACTIVE], set(), False, [_SINGLE], [_INTERACTIVE], id="bare-run-skips-interactive"),
    pytest.param([_SINGLE], set(), False, [_SINGLE], [], id="bare-run-with-nothing-interactive-loses-nothing"),
    pytest.param([_SINGLE, _INTERACTIVE], {"interactive-a"}, False, [_INTERACTIVE], [], id="a-named-slug-is-captured"),
    pytest.param([_SINGLE, _INTERACTIVE], set(), True, [_SINGLE, _INTERACTIVE], [], id="--all-captures-everything"),
    pytest.param([_SINGLE, _INTERACTIVE], {"single-pass-a"}, True, [_SINGLE], [], id="a-named-slug-ignores---all"),
])
def test_a_bare_invocation_skips_every_interactive_request(runs, wanted, capture_all, selected, skipped):
    """#276: a bare run skips interactive requests; naming a slug or `--all` is the opt-in, and a slug list is exhaustive."""
    assert select_runs(runs, wanted=wanted, capture_all=capture_all) == (selected, skipped)


def test_main_prints_which_interactive_requests_it_skipped_and_how_to_capture_them(golden_main, capsys):
    rc, seen = golden_main([_SINGLE, _INTERACTIVE])
    assert rc == 0 and [slug for slug, _ in seen] == ["single-pass-a"]
    assert "golden_run.py interactive-a" in capsys.readouterr().err, "the skip line names the command to capture it alone"


def test_main_resolves_the_model_once_and_threads_it_to_every_capture(golden_main):
    """#515: `capture_model()` reads the environment once per invocation."""
    resolutions: list[str] = []
    rc, seen = golden_main([_req("single-pass-a"), _req("single-pass-b")],
                           capture_model=lambda: resolutions.append("called") or f"model-{len(resolutions)}")
    assert rc == 0 and resolutions == ["called"] and [m for _, m in seen] == ["model-1", "model-1"]


def test_the_announced_call_count_moves_with_the_request_set():
    """#290: the ceiling is derived from the set, never written down as a total."""
    six = [_req(f"single-{i}") for i in range(6)]
    assert planned_calls(six, with_brief=False) == 6 * golden_run.K
    assert planned_calls(six + [_req("single-6")], with_brief=False) == 7 * golden_run.K


def test_planned_calls_prices_interactive_turns_and_the_brief_arm_separately():
    """An interactive request costs K × (1 + TURNS); `--brief` doubles a single pass and leaves it alone."""
    assert planned_calls([_SINGLE, _INTERACTIVE], with_brief=False) == golden_run.K * (1 + golden_run.TURNS)
    assert planned_calls([_SINGLE], with_brief=True) == 2 * golden_run.K
    assert planned_calls([_INTERACTIVE], with_brief=True) == planned_calls([_INTERACTIVE], with_brief=False)


def test_main_announces_the_count_it_computed_for_the_set_it_actually_selected(golden_main, capsys):
    assert golden_main([_SINGLE, _INTERACTIVE])[0] == 0
    out = capsys.readouterr().out
    assert f"up to {planned_calls([_SINGLE], with_brief=False)} API calls" in out
    assert f"up to {planned_calls([_SINGLE, _INTERACTIVE], with_brief=False)} API calls" not in out


# ── a committed baseline still measures what requests.md describes, or says why not (#194, #275) ──

_DECLARED_DRIFT: dict[str, str] = {}   # slug -> the issue that owns the re-capture


def _report(requests: dict[str, dict], baselines: dict[str, dict], declared: dict[str, str]) -> dict:
    """Drifted baselines, declared exceptions that now agree, and baselines no request names."""
    drifted, stale = [], []
    for slug, req in requests.items():
        if slug not in baselines:
            continue  # legitimate: nobody has paid for this capture yet
        moved = any(baselines[slug].get(f, {}) != req.get(f, {}) for f in ("request", "answers"))
        (drifted if moved and slug not in declared else stale if not moved and slug in declared else []).append(slug)
    return {"drifted": sorted(drifted), "stale_exceptions": sorted(stale), "orphans": sorted(set(baselines) - set(requests))}


_X = {"slug": "x", "request": "same", "answers": {"a": ["1"]}}
_B = {"x": {"request": "same", "answers": {"a": ["1"]}}}
_B_OLD = {"x": {"request": "OLD", "answers": {"a": ["1"]}}}
_CLEAN = {"drifted": [], "stale_exceptions": [], "orphans": []}


@pytest.mark.parametrize("requests, baselines, declared, expected", [
    pytest.param({"x": {**_X, "request": "NEW"}}, _B_OLD, {}, {"drifted": ["x"]}, id="must-fire-edited-request-wording"),
    pytest.param({"x": {**_X, "answers": {"a": ["1", "2"]}}}, _B, {}, {"drifted": ["x"]}, id="must-fire-#194-added-answer-layer"),
    pytest.param({"x": _X}, _B, {}, {}, id="a-matching-baseline-is-clean"),
    pytest.param({"x": _X}, {}, {}, {}, id="#275-no-baseline-yet-is-not-drift"),
    pytest.param({"x": {**_X, "request": "NEW"}}, _B_OLD, {"x": "#999"}, {}, id="a-declared-exception-suppresses-a-known-drift"),
    pytest.param({"x": _X}, _B, {"x": "#999"}, {"stale_exceptions": ["x"]}, id="must-fire-a-stale-exception-that-now-agrees"),
    pytest.param({}, {"gone": {"request": "r", "answers": {}}}, {}, {"orphans": ["gone"]}, id="must-fire-orphaned-baseline"),
])
def test_the_baseline_report_names_drift_stale_exceptions_and_orphans(requests, baselines, declared, expected):
    """#194/#275: the report's three verdicts over injected data, each beside its positive control."""
    assert _report(requests, baselines, declared) == {**_CLEAN, **expected}


def test_every_committed_baseline_agrees_with_requests_md_or_is_a_declared_exception():
    """#275, over the real fixtures; the scan is asserted non-empty first, so an empty one cannot pass."""
    requests = {r["slug"]: r for r in parse_requests(REQUESTS)}
    baselines = {p.name.removesuffix(".runs.json"): json.loads(p.read_text(encoding="utf-8")) for p in GOLDEN.glob("*.runs.json")}
    assert len(requests) >= 5 and len(baselines) >= 5, f"the scan is not seeing {REQUESTS} / {GOLDEN}"
    assert all(s in requests and s in baselines for s in _DECLARED_DRIFT), "_DECLARED_DRIFT names a slug with no request or baseline"
    report = _report(requests, baselines, _DECLARED_DRIFT)
    assert not report["drifted"], f"re-capture (`golden_run.py <slug>`) or declare in `_DECLARED_DRIFT`: {report['drifted']}"
    assert not report["stale_exceptions"], f"these `_DECLARED_DRIFT` entries now agree; remove them: {report['stale_exceptions']}"
    assert not report["orphans"], f"these baselines have no request any more; delete or restore: {report['orphans']}"


# ── the readout: what each lens says and what the verdict is (#162, #163, #405, #515, #621) ────────

def _briefs(contested: list[str], complexity: Level = Level.high, *, runs: int = K) -> list[dict]:
    """`runs` captured assessments, each contesting the named slots."""
    challenges = [Challenge(headline=f"about {s}", premise="p", alternative="a", consequence="c", recommendation="r",
                            contests=[s]) for s in contested]
    return [Brief(challenges=challenges, complexity=complexity).model_dump(mode="json") for _ in range(runs)]


def _capture(*, impact: Impact = Impact.medium, completeness: int = 80, briefs: list[dict] | None = None,
             model: str | None = None, perimeter: str | None = None, slot_id: str = "problem") -> str:
    """A `.runs.json` envelope with K identical runs, and optionally K assessments (#515)."""
    slot = Slot(value="v", completeness=completeness, confidence="explicit", impact=impact, evidence="e")
    run = EngineOutput.model_validate({"model": {slot_id: slot}, "questions": [], "summary": Summary()},
                                      context={"perimeter": perimeter or DEFAULT_PERIMETER}).model_dump(mode="json")
    body = {"request": "r", "runs": [run] * K, "model": model, "perimeter": perimeter, "briefs": briefs}
    return json.dumps({k: v for k, v in body.items() if v is not None}, indent=2)


_CURRENT = {"state": "current", "captured_at": "2026-08-01T00:00:00+00:00"}
_80, _70 = _capture(completeness=80), _capture(completeness=70)


@pytest.fixture
def diff(tmp_path, monkeypatch):
    """`diff_one` over two forged baselines: its verdict and everything it printed."""
    def run(old_text: str | None, new_text: str, freshness: dict | None = None) -> tuple[str, list[str]]:
        monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(new_text, encoding="utf-8")
        monkeypatch.setattr(golden_diff, "_head_version", lambda _rel: old_text)
        monkeypatch.setattr(golden_diff, "baseline_commits_since", lambda _rel: freshness or _CURRENT)
        verdict: list[str] = []
        lines = printed(lambda: verdict.append(golden_diff.diff_one("forged"))).splitlines()
        return verdict[0], lines
    return run


@pytest.mark.parametrize("old, new, present, absent", [
    pytest.param("claude-sonnet-5", "claude-sonnet-5", ["claude-sonnet-5", "candidate agree"], [], id="same-model"),
    pytest.param("claude-sonnet-5", "claude-opus-4-8", ["claude-sonnet-5", "claude-opus-4-8"], ["candidate agree"], id="#405-a-model-swap-is-named"),
    pytest.param(None, "claude-sonnet-5", ["unknown", "claude-sonnet-5"], ["candidate agree"], id="no-model-key-is-unknown-not-agreement"),
])
def test_a_baseline_with_no_model_key_does_not_read_as_agreement(diff, old, new, present, absent):
    """#515: the capture model in three states; an unknown baseline still states the half that is known."""
    _, lines = diff(_capture(model=old), _capture(completeness=70, model=new))
    line = _line(lines, "model") or _line(lines, "captured on")
    assert line is not None and all(p in line for p in present) and not any(a in line for a in absent), lines


def test_a_same_perimeter_comparison_actually_compares(diff):
    """#621, must-fire (Codex, #622): two genuine go-to-market captures are diffed slot by slot."""
    verdict, lines = diff(_capture(perimeter="go-to-market", slot_id="icp"), _capture(perimeter="go-to-market", slot_id="icp", impact=Impact.high))
    assert verdict == "moved" and _line(lines, "captured under perimeter", "go-to-market", "agree"), lines
    assert _line(lines, slot_label("icp", "go-to-market"), "medium", "high"), lines


def test_a_perimeter_mismatch_refuses_before_the_candidate_is_ever_parsed(diff):
    """#621, must-fire (Codex, #622's exact finding): the refusal moves no verdict and prints no slot line."""
    verdict, lines = diff(_capture(perimeter="software"), _capture(perimeter="go-to-market", slot_id="icp", completeness=70))
    assert verdict == "perimeter_mismatch" and _line(lines, "cannot compare", "software", "go-to-market"), lines
    assert all(_line(lines, s) is None for s in ("no change above the noise floor", "strong", "weak")), lines


def test_a_first_go_to_market_capture_is_read_back_and_reported_honestly(diff):
    """#621, must-fire: no baseline yet, and a working-tree capture with a real `icp` slot."""
    verdict, lines = diff(None, _capture(perimeter="go-to-market", slot_id="icp"))
    assert verdict == "moved" and _line(lines, "NEW") and _line(lines, "noise floor"), lines


def test_a_baseline_with_no_perimeter_key_is_comparable_with_an_explicit_software_capture(diff):
    """A baseline written before #621 carries no `perimeter` key and reads as `software`."""
    verdict, lines = diff(_80, _capture(completeness=70, perimeter="software"))
    assert verdict != "perimeter_mismatch" and _line(lines, "captured under perimeter", "software", "agree"), lines


def test_the_assessment_lens_runs_when_the_slot_consensus_held_still(diff):
    """#162: slots flat, challenges moved -- the run reports the challenge and does not call itself flat."""
    verdict, lines = diff(_capture(briefs=_briefs(["problem", "workflow"])), _capture(completeness=70, briefs=_briefs(["workflow"])))
    assert verdict == "moved" and _line(lines, "no longer raised", slot_label("problem")), lines
    assert _line(lines, "no change above the noise floor"), lines  # the slot section, and only that


_HELD = _briefs(["problem"])


@pytest.mark.parametrize("old, new, verdict, present, absent", [
    pytest.param(_capture(briefs=_HELD), _capture(completeness=70, briefs=_HELD), "flat",
                 [("verdict and challenges unchanged",)], ["did not look"], id="held-still-says-so-rather-than-going-quiet"),
    pytest.param(_80, _70, "flat", [("did not look", "--brief")], ["verdict and challenges unchanged"], id="never-captured-is-a-lens-that-did-not-look"),
    pytest.param(None, _capture(briefs=_briefs(["problem"], Level.medium)), "moved", [("first capture", "medium")], [],
                 id="a-first-capture-prints-what-it-cannot-compare"),
])
def test_an_assessment_that_held_or_was_never_compared_is_named_as_such(diff, old, new, verdict, present, absent):
    """The lens's other states, each a positive control for the two tests around it."""
    got, lines = diff(old, new)
    assert got == verdict and all(_line(lines, *p) for p in present) and not any(_line(lines, a) for a in absent), lines


def test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal(diff):
    """HEAD has an assessment and this capture does not: louder than never-captured, and still no signal."""
    verdict, lines = diff(_capture(briefs=_HELD), _70)
    dropped = _line(lines, "nothing to compare")
    assert dropped is not None and dropped.lstrip().startswith("assessment !"), lines
    assert verdict == "flat" and _line(lines, "did not look") is None, lines


def test_the_verdict_is_the_union_of_the_lenses_that_ran(diff):
    """The strongest signal any lens produced wins: strong slots + clean assessment is strong; flat + weak is weak."""
    verdict, lines = diff(_capture(impact=Impact.low, briefs=_HELD), _capture(impact=Impact.high, briefs=_HELD))
    assert verdict == "moved" and _line(lines, "verdict and challenges unchanged"), lines
    split = _briefs([], Level.medium, runs=2) + _briefs([], Level.high, runs=1)
    verdict, lines = diff(_capture(briefs=_briefs([], Level.high)), _capture(completeness=70, briefs=split))
    assert verdict == "weak" and _line(lines, "assessment weak complexity"), lines


# #163: `_show_turns` prints `unreached_layers` only for a run that stopped short of `MEASURABLE_DEPTH`.

def _iturn(index: int, answered: list[str], *, asks: tuple = ()) -> Turn:
    questions = [Question(q=f"tell me about {s}", slot=s, why="drives the shape") for s in asks]
    return Turn(index=index, answered=list(answered), model=EngineOutput(model={}, questions=questions, summary=Summary()))


def _unused_line(run: list[Turn], layers: dict) -> str | None:
    return _line(printed(golden_diff._show_turns, None, [run], layers).splitlines(), "sheet layers never reached")


_SHALLOW = [_iturn(1, ["business_rules"], asks=("business_rules",)), _iturn(2, [])]


def test_a_shallow_capture_reports_which_sheet_layers_went_unused():
    """#163: a run that converged at turn 2 with two of three `business_rules` layers still on the sheet says so."""
    unused = _unused_line(_SHALLOW, {"business_rules": ["l1", "l2", "l3"]})
    assert unused is not None and "Business rules" in unused and "2" in unused, unused


@pytest.mark.parametrize("run, layers", [
    pytest.param([_iturn(i, ["business_rules"], asks=("business_rules",)) for i in range(1, 6)],
                 {"business_rules": [f"l{i}" for i in range(1, 11)]}, id="a-deep-run-with-layers-left-over"),
    pytest.param(_SHALLOW, {"business_rules": ["l1"]}, id="a-shallow-run-with-the-sheet-spent"),
])
def test_a_deep_capture_with_layers_left_over_does_not_report_them(run, layers):
    """must not fire: leftover layers past `MEASURABLE_DEPTH` are by design, and a spent sheet is not a finding."""
    assert _unused_line(run, layers) is None


# #164: a glyph must not kill a harness script after the work has landed. `PYTHONIOENCODING=ascii` reaches a
# real strict encoder on every platform, since `streams._target_encoding` honours an operator-named codec.

@pytest.fixture
def ascii_console(monkeypatch):
    """Substitute stdout and stderr with real ASCII-strict encoders, and hand back stdout's bytes."""
    def install() -> io.BytesIO:
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        raw = io.BytesIO()
        for name in ("stdout", "stderr"):
            monkeypatch.setattr(sys, name, io.TextIOWrapper(raw if name == "stdout" else io.BytesIO(), encoding="ascii", errors="strict"))
        return raw
    return install


@pytest.fixture
def harness_run(tmp_path, monkeypatch, golden_main):
    """`golden_diff.main([])` over one forged capture with no baseline in HEAD, or `golden_run.main([])` over one request."""
    def run(script: str) -> int:
        if script == "golden_run":
            return golden_main([_req("forged")])[0]
        monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
        monkeypatch.setattr(golden_diff, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(_capture(), encoding="utf-8")
        monkeypatch.setattr(golden_diff, "_head_version", lambda _rel: None)
        return golden_diff.main([])
    return run


@pytest.mark.parametrize("script", ["golden_diff", "golden_run"])
def test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams(script, harness_run, ascii_console, monkeypatch):
    """must fire (#164): without this the silence assertion below would pass on a harness that printed nothing."""
    ascii_console()
    monkeypatch.setattr(sys.modules[script], "configure_output", lambda: None)
    with pytest.raises(UnicodeEncodeError):
        harness_run(script)


@pytest.mark.parametrize("script", ["golden_diff", "golden_run"])
def test_a_harness_script_survives_a_console_that_cannot_encode_its_output(script, harness_run, ascii_console):
    """must not fire, and the escape is the evidence it ran rather than fell silent."""
    raw = ascii_console()
    assert harness_run(script) == 0
    sys.stdout.flush()
    assert (b"\\u" in raw.getvalue() or b"\\x" in raw.getvalue()) and sys.stdout.errors == "backslashreplace", raw.getvalue()


# #405/#410: baseline freshness is named before any lens output.

def test_a_stale_baseline_is_named_before_any_lens_output(diff):
    """A baseline that predates a watched-path commit says so, ahead of the slot section."""
    stale = {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
             "commits": [{"sha": "abc123def", "date": "2026-08-15", "subject": "edit engine.md"},
                         {"sha": "def456abc", "date": "2026-08-20", "subject": "add a context card"}]}
    _, lines = diff(_80, _70, freshness=stale)
    warned = _line(lines, "baseline captured", "2026-08-01", "2 commit(s)")
    assert warned and _line(lines, "abc123def") and _line(lines, "edit engine.md"), lines
    assert lines.index(warned) < lines.index(_line(lines, "no change above the noise floor")), lines


def test_a_current_baseline_says_so_without_alarm(diff):
    """must not fire, the positive control: no warning glyph and no commit count."""
    _, lines = diff(_80, _70, freshness=_CURRENT)
    assert _line(lines, "baseline current", "2026-08-01") and not _line(lines, "⚠") and not _line(lines, "commit(s)"), lines


def test_an_unrecoverable_freshness_check_is_reported_as_unknown_not_current(diff):
    """must fire -- the third state: a shallow clone or a git failure reads as *could not tell*, never as clean."""
    _, lines = diff(_80, _70, freshness={"state": "unknown", "reason": "shallow clone -- commit history is truncated"})
    assert _line(lines, "could not tell", "shallow clone") and not _line(lines, "baseline current"), lines


def test_a_hostile_freshness_reason_cannot_forge_a_line(diff):
    """must fire -- #461: `reason` carries text from outside the process, and a raw CR must not split it."""
    _, lines = diff(_80, _70, freshness={"state": "unknown", "reason": "git log failed: fatal: bad object\rFORGED continuation"})
    assert _line(lines, "could not tell", "\\r", "FORGED continuation"), lines
    assert not any("FORGED continuation" in ln and "could not tell" not in ln for ln in lines), lines
