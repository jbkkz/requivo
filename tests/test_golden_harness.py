"""The golden harness end to end, offline: capture, run selection, the readout, the baselines (#137, #275, #276)."""
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
from golden_lib import GOLDEN, REQUESTS, Turn, captured_model, load_turns, parse_requests  # noqa: E402
from golden_run import planned_calls, select_runs  # noqa: E402

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
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402
from requivo.services.discovery import DiscoveryService  # noqa: E402

K = 3  # runs per captured baseline, matching the harness default


def _line(lines: list[str], needle: str) -> str | None:
    return next((ln for ln in lines if needle in ln), None)


def _printed(fn, *args) -> list[str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue().splitlines()


# ── the interactive capture loop, driven over a scripted provider (#137, #163) ────────────────────

def _asking(*slots: str) -> EngineOutput:
    """A turn's reply that asks about each named slot."""
    return EngineOutput(
        model={"problem": Slot(value="v", completeness=60, confidence="inferred", impact="high")},
        questions=[Question(q=f"about {s}?", slot=s, why="it drives the shape") for s in slots],
        summary=Summary(objective="an objective"))


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """`capture_interactive` over scripted replies: the `draft_turn` calls, the loaded file, the output (#163)."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)

    def run(replies: list[EngineOutput], answers: dict[str, list[str]], *, k: int = 1,
            turns: int = 5, perimeter: str | None = None):
        calls: list[dict] = []
        position = {"turn": 0}

        def fake_draft_turn(self, request, *, current_model=None, answers=None, cards=None,
                            perimeter="__UNSET__"):
            # No real default: a fallback here would hide `capture_interactive` dropping the keyword (#621).
            if current_model is None:
                position["turn"] = 0
            calls.append({"request": request, "current_model": current_model,
                          "answers": answers, "cards": cards, "perimeter": perimeter})
            reply = replies[min(position["turn"], len(replies) - 1)]
            position["turn"] += 1
            return reply

        monkeypatch.setattr(DiscoveryService, "draft_turn", fake_draft_turn)
        monkeypatch.setattr(golden_run, "K", k)
        monkeypatch.setattr(golden_run, "TURNS", turns)
        req = {"slug": "scripted", "form": "f", "card": "c", "request": "a request", "answers": answers}
        if perimeter is not None:
            req["perimeter"] = perimeter
        # A StringIO never encodes, and a stand-in client object rather than `None` (#515).
        buf = io.StringIO()
        with redirect_stdout(buf):
            golden_run.capture_interactive(client=object(), req=req, model="claude-sonnet-5")
        captured = load_turns((tmp_path / "scripted.runs.json").read_text(encoding="utf-8"))
        return calls, captured, buf.getvalue()

    return run


@pytest.mark.parametrize("perimeter, expected", [("go-to-market", "go-to-market"), (None, DEFAULT_PERIMETER)],
                         ids=["the-requests-own", "no-perimeter-key-reads-software"])
def test_the_capture_threads_the_requests_own_perimeter_to_draft_turn(capture, perimeter, expected):
    """#621: the parsed block's `perimeter`, or the software default when the block has no key at all."""
    calls, _, _ = capture([_asking()], {}, perimeter=perimeter)
    assert calls[0]["perimeter"] == expected


def test_the_interactive_capture_records_the_model_it_reasoned_on(capture, tmp_path):
    """#515: the envelope records the conditions a capture ran under, not only its input."""
    capture([_asking()], {})
    assert captured_model((tmp_path / "scripted.runs.json").read_text(encoding="utf-8")) == "claude-sonnet-5"


def test_the_capture_reasons_through_the_interactive_seam_and_not_a_message_list(capture):
    """`draft_turn` is the production interactive path (#77), so it is the one the measurement must use."""
    calls, _, _ = capture([_asking("problem"), _asking("actors"), _asking()],
                          {"problem": ["p"], "actors": ["a"]})
    assert calls[0]["current_model"] is None and calls[0]["answers"] is None
    assert all(c["request"] == "a request" for c in calls)
    assert calls[1]["current_model"] is not None
    assert "[slot: problem]" in calls[1]["answers"] and "[slot: actors]" in calls[2]["answers"]


@pytest.mark.parametrize("replies, answers", [
    pytest.param([_asking("problem"), _asking("risks")], {"problem": ["p"]}, id="the-sheet-has-nothing-left"),
    pytest.param([_asking("problem"), _asking()], {"problem": ["p"], "actors": ["a"]}, id="the-engine-stops-asking"),
])
def test_a_run_stops_when_the_sheet_or_the_engine_has_nothing_left_to_say(capture, replies, answers):
    """The same two events that end `converse()` end a capture."""
    calls, captured, _ = capture(replies, answers, turns=5)
    assert len(calls) == 2, "the capture kept paying after there was nothing left to say"
    assert [t.index for t in captured[0]] == [1, 2]


def test_the_final_turn_records_no_answer_it_never_sent(capture):
    """`answered` is what the conversation covered; the re-ask count is measured against exactly that."""
    _, captured, _ = capture([_asking("problem"), _asking("actors"), _asking("risks")],
                             {"problem": ["p"], "actors": ["a"], "risks": ["r"]}, turns=3)
    assert [t.answered for t in captured[0]] == [["problem"], ["actors"], []]


def test_every_run_starts_the_sheet_over(capture):
    """K runs are K independent conversations, each on a fresh sheet."""
    calls, captured, _ = capture([_asking("problem"), _asking("problem"), _asking()],
                                 {"problem": ["first", "second"]}, k=2)
    assert all("first" in a for a in (calls[1]["answers"], calls[4]["answers"]))
    assert len(captured) == 2


@pytest.mark.parametrize("replies, answers, must_report", [
    pytest.param([_asking("problem"), _asking()], {"problem": ["first", "second", "third"]}, True,
                 id="#163-shallow-must-fire"),
    pytest.param([_asking("problem")] * 5, {"problem": [f"l{i}" for i in range(1, 11)]}, False,
                 id="#163-deep-must-not-fire"),
])
def test_capture_reports_unreached_sheet_layers_only_when_shallow(capture, replies, answers, must_report):
    """#163: a run that converges early names the sheet layers it never reached, where the calls were spent."""
    _, _, output = capture(replies, answers)
    assert ("sheet layers never reached" in output) is must_report, output
    if must_report:
        assert "Real problem (2)" in output, output


# ── which requests a `golden_run.py` invocation captures, and what it announces (#276, #290, #515) ─

def _req(slug: str, answers: dict | None = None) -> dict:
    return {"slug": slug, "request": f"request for {slug}", "answers": answers or {}}


_SINGLE = _req("single-pass-a")
_INTERACTIVE = _req("interactive-a", answers={"problem": ["layer one"]})


@pytest.fixture
def golden_main(tmp_path, monkeypatch):
    """`golden_run.main([])` with the client and the capture loop stubbed out: the rc and what `capture` saw."""
    def run(requests: list[dict], **patches):
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
        monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
        monkeypatch.setattr(golden_run, "REQUESTS", tmp_path / "requests.md")
        (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
        monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
        monkeypatch.setattr(golden_run, "parse_requests", lambda _path: list(requests))
        monkeypatch.setattr(golden_run, "capture",
                            lambda _client, req, _with_brief, *, model: seen.append((req["slug"], model)))
        for name, value in patches.items():
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
    got_selected, got_skipped = select_runs(runs, wanted=wanted, capture_all=capture_all)
    assert (got_selected, got_skipped) == (selected, skipped)


def test_main_prints_which_interactive_requests_it_skipped_and_how_to_capture_them(golden_main, capsys):
    rc, seen = golden_main([_SINGLE, _INTERACTIVE])
    assert rc == 0 and [slug for slug, _ in seen] == ["single-pass-a"]
    err = capsys.readouterr().err
    assert "golden_run.py interactive-a" in err, "the skip line should name the exact command to capture the skipped request alone"


def test_main_resolves_the_model_once_and_threads_it_to_every_capture(golden_main):
    """#515: `capture_model()` reads the environment once per invocation."""
    resolutions: list[str] = []

    def fake_capture_model() -> str:
        resolutions.append("called")
        return f"model-{len(resolutions)}"

    rc, seen = golden_main([_req("single-pass-a"), _req("single-pass-b")], capture_model=fake_capture_model)
    assert rc == 0 and resolutions == ["called"]
    assert [model for _, model in seen] == ["model-1", "model-1"]


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


def _report(requests_by_slug: dict[str, dict], baselines_by_slug: dict[str, dict],
            declared_drift: dict[str, str]) -> dict:
    """Drifted baselines, declared exceptions that now agree, and baselines no request names."""
    drifted, stale_exceptions = [], []
    for slug, req in requests_by_slug.items():
        baseline = baselines_by_slug.get(slug)
        if baseline is None:
            continue  # legitimate: nobody has paid for this capture yet
        fields = [f for f in ("request", "answers") if baseline.get(f, {}) != req.get(f, {})]
        if not fields and slug in declared_drift:
            stale_exceptions.append(slug)
        elif fields and slug not in declared_drift:
            drifted.append(slug)
    return {"drifted": sorted(drifted), "stale_exceptions": sorted(stale_exceptions),
            "orphans": sorted(set(baselines_by_slug) - set(requests_by_slug))}


_X = {"slug": "x", "request": "same", "answers": {"a": ["1"]}}
_CLEAN = {"drifted": [], "stale_exceptions": [], "orphans": []}


@pytest.mark.parametrize("requests, baselines, declared, expected", [
    pytest.param({"x": {**_X, "request": "NEW"}}, {"x": {"request": "OLD", "answers": {"a": ["1"]}}}, {},
                 {"drifted": ["x"]}, id="must-fire-edited-request-wording"),
    pytest.param({"x": {**_X, "answers": {"a": ["1", "2"]}}}, {"x": {"request": "same", "answers": {"a": ["1"]}}}, {},
                 {"drifted": ["x"]}, id="must-fire-#194-added-answer-layer"),
    pytest.param({"x": _X}, {"x": {"request": "same", "answers": {"a": ["1"]}}}, {}, {}, id="a-matching-baseline-is-clean"),
    pytest.param({"x": _X}, {}, {}, {}, id="#275-no-baseline-yet-is-not-drift"),
    pytest.param({"x": {**_X, "request": "NEW"}}, {"x": {"request": "OLD", "answers": {"a": ["1"]}}}, {"x": "#999"},
                 {}, id="a-declared-exception-suppresses-a-known-drift"),
    pytest.param({"x": _X}, {"x": {"request": "same", "answers": {"a": ["1"]}}}, {"x": "#999"},
                 {"stale_exceptions": ["x"]}, id="must-fire-a-stale-exception-that-now-agrees"),
    pytest.param({}, {"gone": {"request": "r", "answers": {}}}, {}, {"orphans": ["gone"]}, id="must-fire-orphaned-baseline"),
])
def test_the_baseline_report_names_drift_stale_exceptions_and_orphans(requests, baselines, declared, expected):
    """#194/#275: the report's three verdicts over injected data, each beside its positive control."""
    assert _report(requests, baselines, declared) == {**_CLEAN, **expected}


def test_every_committed_baseline_agrees_with_requests_md_or_is_a_declared_exception():
    """#275, over the real fixtures; the scan is asserted non-empty first, so an empty one cannot pass."""
    requests = {r["slug"]: r for r in parse_requests(REQUESTS)}
    baselines = {p.name.removesuffix(".runs.json"): json.loads(p.read_text(encoding="utf-8"))
                 for p in GOLDEN.glob("*.runs.json")}
    assert len(requests) >= 5 and len(baselines) >= 5, f"the scan is not seeing {REQUESTS} / {GOLDEN}"
    for slug in _DECLARED_DRIFT:
        assert slug in requests and slug in baselines, f"_DECLARED_DRIFT names {slug!r}, which has no request or baseline to except"
    report = _report(requests, baselines, _DECLARED_DRIFT)
    assert not report["drifted"], (
        "committed baseline(s) disagree with fixtures/golden/requests.md and are not a declared exception -- "
        "re-capture with `python scripts/golden_run.py <slug>` or add a `_DECLARED_DRIFT` entry naming the "
        f"issue that owns the re-capture: {report['drifted']}")
    assert not report["stale_exceptions"], f"these `_DECLARED_DRIFT` entries now agree with requests.md; remove them: {report['stale_exceptions']}"
    assert not report["orphans"], f"these baselines have no request in requests.md any more; delete or restore: {report['orphans']}"


# ── the readout: what each lens says and what the verdict is (#162, #163, #405, #515, #621) ────────

def _run_model(impact: Impact = Impact.medium, completeness: int = 80, *,
               slot_id: str = "problem", perimeter: str = DEFAULT_PERIMETER) -> dict:
    """One captured run; `completeness` is what varies between two otherwise identical baselines (#162)."""
    slot = Slot(value="v", completeness=completeness, confidence=Confidence.explicit, impact=impact, evidence="e")
    return EngineOutput.model_validate({"model": {slot_id: slot}, "questions": [], "summary": Summary()},
                                       context={"perimeter": perimeter}).model_dump(mode="json")


def _briefs(contested: list[str], complexity: Level = Level.high, *, runs: int = K) -> list[dict]:
    """`runs` captured assessments, each contesting the named slots."""
    challenges = [Challenge(headline=f"about {s}", premise="p", alternative="a", consequence="c",
                            recommendation="r", contests=[s]) for s in contested]
    return [Brief(challenges=challenges, complexity=complexity).model_dump(mode="json") for _ in range(runs)]


def _capture(*, impact: Impact = Impact.medium, completeness: int = 80, briefs: list[dict] | None = None,
             model: str | None = None, perimeter: str | None = None, slot_id: str = "problem") -> str:
    """A `.runs.json` envelope with K identical runs, and optionally K assessments (#515)."""
    body: dict = {"request": "r", "runs": [_run_model(impact, completeness, slot_id=slot_id,
                                                      perimeter=perimeter or DEFAULT_PERIMETER) for _ in range(K)]}
    body.update({k: v for k, v in (("model", model), ("perimeter", perimeter), ("briefs", briefs)) if v is not None})
    return json.dumps(body, indent=2)


_CURRENT = {"state": "current", "captured_at": "2026-08-01T00:00:00+00:00"}


@pytest.fixture
def diff(tmp_path, monkeypatch):
    """`diff_one` over two forged baselines: its verdict and everything it printed."""
    def run(old_text: str | None, new_text: str, freshness: dict | None = None) -> tuple[str, list[str]]:
        monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(new_text, encoding="utf-8")
        monkeypatch.setattr(golden_diff, "_head_version", lambda _rel: old_text)
        monkeypatch.setattr(golden_diff, "baseline_commits_since", lambda _rel: freshness or _CURRENT)
        verdict: list[str] = []
        lines = _printed(lambda: verdict.append(golden_diff.diff_one("forged")))
        return verdict[0], lines
    return run


@pytest.mark.parametrize("old, new, present, absent", [
    pytest.param("claude-sonnet-5", "claude-sonnet-5", ["claude-sonnet-5", "candidate agree"], [], id="same-model"),
    pytest.param("claude-sonnet-5", "claude-opus-4-8", ["claude-sonnet-5", "claude-opus-4-8"], ["candidate agree"],
                 id="#405-a-model-swap-is-named"),
    pytest.param(None, "claude-sonnet-5", ["unknown", "claude-sonnet-5"], ["candidate agree"],
                 id="no-model-key-is-unknown-not-agreement"),
])
def test_a_baseline_with_no_model_key_does_not_read_as_agreement(diff, old, new, present, absent):
    """#515: the capture model in three states; an unknown baseline still states the half that is known."""
    _, lines = diff(_capture(model=old), _capture(completeness=70, model=new))
    line = _line(lines, "model") or _line(lines, "captured on")
    assert line is not None, f"no capture-model line in the readout: {lines}"
    assert all(p in line for p in present) and not any(a in line for a in absent), line


def test_a_same_perimeter_comparison_actually_compares(diff):
    """#621, must-fire (Codex, #622): two genuine go-to-market captures are diffed slot by slot."""
    verdict, lines = diff(_capture(perimeter="go-to-market", slot_id="icp", impact=Impact.medium),
                          _capture(perimeter="go-to-market", slot_id="icp", impact=Impact.high))
    line = _line(lines, "captured under perimeter")
    assert line is not None and "go-to-market" in line and "agree" in line, lines
    assert verdict == "moved", lines
    move_line = _line(lines, slot_label("icp", "go-to-market"))
    assert move_line is not None and "medium" in move_line and "high" in move_line, lines


def test_a_perimeter_mismatch_refuses_before_the_candidate_is_ever_parsed(diff):
    """#621, must-fire (Codex, #622's exact finding): the refusal moves no verdict and prints no slot line."""
    verdict, lines = diff(_capture(perimeter="software", slot_id="problem"),
                          _capture(perimeter="go-to-market", slot_id="icp", completeness=70))
    assert verdict == "perimeter_mismatch", lines
    refusal = _line(lines, "cannot compare")
    assert refusal is not None and "software" in refusal and "go-to-market" in refusal, lines
    assert all(_line(lines, s) is None for s in ("no change above the noise floor", "strong", "weak")), lines


def test_a_first_go_to_market_capture_is_read_back_and_reported_honestly(diff):
    """#621, must-fire: no baseline yet, and a working-tree capture with a real `icp` slot."""
    verdict, lines = diff(None, _capture(perimeter="go-to-market", slot_id="icp"))
    assert verdict == "moved", lines
    assert _line(lines, "NEW") is not None and _line(lines, "noise floor") is not None, lines


def test_a_baseline_with_no_perimeter_key_is_comparable_with_an_explicit_software_capture(diff):
    """A baseline written before #621 carries no `perimeter` key and reads as `software`."""
    verdict, lines = diff(_capture(), _capture(completeness=70, perimeter="software"))
    line = _line(lines, "captured under perimeter")
    assert line is not None and "software" in line and "agree" in line, lines
    assert verdict != "perimeter_mismatch", lines


def test_the_assessment_lens_runs_when_the_slot_consensus_held_still(diff):
    """#162: slots flat, challenges moved -- the run reports the challenge and does not call itself flat."""
    verdict, lines = diff(_capture(completeness=80, briefs=_briefs(["problem", "workflow"])),
                          _capture(completeness=70, briefs=_briefs(["workflow"])))
    lost = _line(lines, "no longer raised")
    assert lost is not None and slot_label("problem") in lost, lines
    assert verdict == "moved", lines
    assert _line(lines, "no change above the noise floor") is not None, lines  # the slot section, and only that


def test_a_captured_assessment_that_held_still_says_so_rather_than_going_quiet(diff):
    """The positive control for the lens above and the one below."""
    briefs = _briefs(["problem"])
    verdict, lines = diff(_capture(completeness=80, briefs=briefs), _capture(completeness=70, briefs=briefs))
    assert _line(lines, "verdict and challenges unchanged") is not None, lines
    assert _line(lines, "did not look") is None and verdict == "flat", lines


def test_an_assessment_nobody_captured_is_named_as_a_lens_that_did_not_look(diff):
    """The third state: `--brief` is an opt-in flag, and a lens that did not look moves no verdict."""
    verdict, lines = diff(_capture(completeness=80), _capture(completeness=70))
    not_run = _line(lines, "did not look")
    assert not_run is not None and "--brief" in not_run, lines
    assert _line(lines, "verdict and challenges unchanged") is None and verdict == "flat", lines


def test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal(diff):
    """HEAD has an assessment and this capture does not: louder than never-captured, and still no signal."""
    verdict, lines = diff(_capture(completeness=80, briefs=_briefs(["problem"])), _capture(completeness=70))
    dropped = _line(lines, "nothing to compare")
    assert dropped is not None and dropped.lstrip().startswith("assessment !"), lines
    assert verdict == "flat" and _line(lines, "did not look") is None, lines


def test_a_first_capture_prints_the_assessment_it_has_nothing_to_compare_against(diff):
    """No baseline in HEAD at all: the consensus readout is the finding, and a fresh capture is worth reading."""
    verdict, lines = diff(None, _capture(briefs=_briefs(["problem"], Level.medium)))
    first = _line(lines, "first capture")
    assert first is not None and "medium" in first, lines
    assert verdict == "moved", lines


def test_the_verdict_is_the_union_of_the_lenses_that_ran(diff):
    """The strongest signal any lens produced wins: strong slots + clean assessment is strong; flat + weak is weak."""
    briefs = _briefs(["problem"])
    verdict, lines = diff(_capture(impact=Impact.low, briefs=briefs), _capture(impact=Impact.high, briefs=briefs))
    assert verdict == "moved" and _line(lines, "verdict and challenges unchanged") is not None, lines
    split = _briefs([], Level.medium, runs=2) + _briefs([], Level.high, runs=1)
    verdict, lines = diff(_capture(completeness=80, briefs=_briefs([], Level.high)),
                          _capture(completeness=70, briefs=split))
    assert verdict == "weak" and _line(lines, "assessment weak complexity") is not None, lines


# #163: `_show_turns` prints `unreached_layers` only for a run that stopped short of `MEASURABLE_DEPTH`.

def _iturn(index: int, answered: list[str], *, asks: tuple = ()) -> Turn:
    questions = [Question(q=f"tell me about {s}", slot=s, why="drives the shape") for s in asks]
    return Turn(index=index, answered=list(answered), model=EngineOutput(model={}, questions=questions, summary=Summary()))


def _unused_line(run: list[Turn], layers: dict) -> str | None:
    return _line(_printed(golden_diff._show_turns, None, [run], layers), "sheet layers never reached")


def test_a_shallow_capture_reports_which_sheet_layers_went_unused():
    """#163: a run that converged at turn 2 with two of three `business_rules` layers still on the sheet says so."""
    run = [_iturn(1, ["business_rules"], asks=("business_rules",)), _iturn(2, [])]
    unused = _unused_line(run, {"business_rules": ["l1", "l2", "l3"]})
    assert unused is not None and "Business rules" in unused and "2" in unused, unused


@pytest.mark.parametrize("run, layers", [
    pytest.param([_iturn(i, ["business_rules"], asks=("business_rules",)) for i in range(1, 6)],
                 {"business_rules": [f"l{i}" for i in range(1, 11)]}, id="a-deep-run-with-layers-left-over"),
    pytest.param([_iturn(1, ["business_rules"], asks=("business_rules",)), _iturn(2, [])],
                 {"business_rules": ["l1"]}, id="a-shallow-run-with-the-sheet-spent"),
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
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
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
def golden_run_run(golden_main):
    return lambda: golden_main([_req("forged")])[0]


@pytest.mark.parametrize("script, runner", [("golden_diff", "golden_diff_run"), ("golden_run", "golden_run_run")])
def test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams(script, runner, request,
                                                                                    ascii_console, monkeypatch):
    """must fire (#164): without this the silence assertion below would pass on a harness that printed nothing."""
    ascii_console()
    monkeypatch.setattr(sys.modules[script], "configure_output", lambda: None)
    with pytest.raises(UnicodeEncodeError):
        request.getfixturevalue(runner)()


@pytest.mark.parametrize("runner", ["golden_diff_run", "golden_run_run"])
def test_a_harness_script_survives_a_console_that_cannot_encode_its_output(runner, request, ascii_console):
    """must not fire, and the escape is the evidence it ran rather than fell silent."""
    raw = ascii_console()
    assert request.getfixturevalue(runner)() == 0
    sys.stdout.flush()
    out = raw.getvalue()
    assert b"\\u" in out or b"\\x" in out, out
    assert sys.stdout.errors == "backslashreplace", sys.stdout.errors


# #405/#410: baseline freshness is named before any lens output.

def test_a_stale_baseline_is_named_before_any_lens_output(diff):
    """A baseline that predates a watched-path commit says so, ahead of the slot section."""
    stale = {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
             "commits": [{"sha": "abc123def", "date": "2026-08-15", "subject": "edit engine.md"},
                         {"sha": "def456abc", "date": "2026-08-20", "subject": "add a context card"}]}
    _, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=stale)
    warned = _line(lines, "baseline captured")
    assert warned is not None and "2026-08-01" in warned and "2 commit(s)" in warned, lines
    assert _line(lines, "abc123def") is not None and _line(lines, "edit engine.md") is not None, lines
    assert lines.index(warned) < lines.index(_line(lines, "no change above the noise floor")), lines


def test_a_current_baseline_says_so_without_alarm(diff):
    """must not fire, the positive control: no warning glyph and no commit count."""
    _, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=_CURRENT)
    said = _line(lines, "baseline current")
    assert said is not None and "2026-08-01" in said, lines
    assert _line(lines, "⚠") is None and _line(lines, "commit(s)") is None, lines


def test_an_unrecoverable_freshness_check_is_reported_as_unknown_not_current(diff):
    """must fire -- the third state: a shallow clone or a git failure reads as *could not tell*, never as clean."""
    unknown = {"state": "unknown", "reason": "shallow clone -- commit history is truncated"}
    _, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=unknown)
    said = _line(lines, "could not tell")
    assert said is not None and "shallow clone" in said, lines
    assert _line(lines, "baseline current") is None, lines


def test_a_hostile_freshness_reason_cannot_forge_a_line(diff):
    """must fire -- #461: `reason` carries text from outside the process, and a raw CR must not split it."""
    hostile = {"state": "unknown", "reason": "git log failed: fatal: bad object\rFORGED continuation"}
    _, lines = diff(_capture(completeness=80), _capture(completeness=70), freshness=hostile)
    said = _line(lines, "could not tell")
    assert said is not None and "\\r" in said and "FORGED continuation" in said, lines
    assert not any("FORGED continuation" in ln and "could not tell" not in ln for ln in lines), lines
