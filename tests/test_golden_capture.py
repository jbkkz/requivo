"""The golden harness's interactive capture loop, driven offline (#137)."""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_lib  # noqa: E402
import golden_run  # noqa: E402
from golden_lib import captured_model, load_turns  # noqa: E402

from requivo.core.contracts import EngineOutput, Question, Slot, Summary  # noqa: E402
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402
from requivo.services.discovery import DiscoveryService  # noqa: E402


def _model(*slots: str) -> EngineOutput:
    """A turn's reply that asks about each named slot."""
    return EngineOutput(
        model={"problem": Slot(value="v", completeness=60, confidence="inferred", impact="high")},
        questions=[Question(q=f"about {s}?", slot=s, why="it drives the shape") for s in slots],
        summary=Summary(objective="an objective"),
    )


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """Run `capture_interactive` over a scripted provider, and hand back the calls, the file, and everything
    the run printed (#163)."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)

    def run(replies: list[EngineOutput], answers: dict[str, list[str]], *, k: int = 1,
            turns: int = 5, perimeter: str | None = None):
        calls: list[dict] = []
        scripted = list(replies)
        position = {"turn": 0}

        def fake_draft_turn(self, request, *, current_model=None, answers=None, cards=None,
                            perimeter="__UNSET__"):
            # No real default: a stub that quietly fell back to `DEFAULT_PERIMETER` on its own would make `test_a_request_with_no_perimeter_key_still_threads_the_software_default` pass even if `capture_interactive` stopped passing the keyword at all (#621).
            if current_model is None:
                position["turn"] = 0
            calls.append({"request": request, "current_model": current_model,
                          "answers": answers, "cards": cards, "perimeter": perimeter})
            reply = scripted[min(position["turn"], len(scripted) - 1)]
            position["turn"] += 1
            return reply

        monkeypatch.setattr(DiscoveryService, "draft_turn", fake_draft_turn)
        monkeypatch.setattr(golden_run, "K", k)
        monkeypatch.setattr(golden_run, "TURNS", turns)
        req = {"slug": "scripted", "form": "f", "card": "c", "request": "a request",
               "answers": answers}
        if perimeter is not None:
            req["perimeter"] = perimeter
        # `redirect_stdout` to a StringIO, which is the pattern `tests/test_cli_interactive.py`'s `_converse` already uses and for the same reason: `capture_interactive` ends by printing `✓` and `—`, and a StringIO never encodes.
        buf = io.StringIO()
        with redirect_stdout(buf):
            # A stand-in client object rather than `None` (#515).
            golden_run.capture_interactive(client=object(), req=req, model="claude-sonnet-5")
        captured = load_turns((tmp_path / "scripted.runs.json").read_text(encoding="utf-8"))
        return calls, captured, buf.getvalue()

    return run


def test_the_capture_threads_the_requests_own_perimeter_to_draft_turn(capture):
    """#621: the request's own `perimeter` -- read off the parsed `requests.md` block."""
    calls, _, _ = capture([_model()], {}, perimeter="go-to-market")
    assert calls[0]["perimeter"] == "go-to-market"


def test_a_request_with_no_perimeter_key_still_threads_the_software_default(capture):
    """must not fire -- every other test in this module builds a request dict with no `perimeter` key at all,
    the shape every fixture here had before #621."""
    calls, _, _ = capture([_model()], {})
    assert calls[0]["perimeter"] == DEFAULT_PERIMETER


def test_the_interactive_capture_records_the_model_it_reasoned_on(capture, tmp_path):
    """#515: the envelope records a capture's *input* and, until this, nothing about the conditions it ran
    under."""
    capture([_model()], {})
    text = (tmp_path / "scripted.runs.json").read_text(encoding="utf-8")
    assert captured_model(text) == "claude-sonnet-5"


def test_the_capture_reasons_through_the_interactive_seam_and_not_a_message_list(capture):
    """The whole validity of the measurement. `draft_turn` is the production interactive path and the shape
    #77 changed."""
    calls, _, _ = capture([_model("problem"), _model("actors"), _model()],
                          {"problem": ["p"], "actors": ["a"]})
    assert calls[0]["current_model"] is None and calls[0]["answers"] is None
    assert all(c["request"] == "a request" for c in calls)
    assert calls[1]["current_model"] is not None
    assert "[slot: problem]" in calls[1]["answers"]
    assert "[slot: actors]" in calls[2]["answers"]


def test_a_run_stops_when_the_sheet_has_nothing_left_to_say(capture):
    """The fixture client running out of answers is the same event as a user pressing Enter on every question,
    and `converse()` stops there."""
    calls, captured, _ = capture([_model("problem"), _model("risks")], {"problem": ["p"]}, turns=5)
    assert len(calls) == 2, "the capture kept paying after the client had nothing left to say"
    assert [t.index for t in captured[0]] == [1, 2]


def test_a_run_stops_when_the_engine_stops_asking(capture):
    calls, captured, _ = capture([_model("problem"), _model()], {"problem": ["p"], "actors": ["a"]})
    assert len(calls) == 2
    assert [t.index for t in captured[0]] == [1, 2]


def test_the_final_turn_records_no_answer_it_never_sent(capture):
    """`answered` is what the *conversation* covered, and the re-ask count is measured against exactly that
    set."""
    _, captured, _ = capture([_model("problem"), _model("actors"), _model("risks")],
                             {"problem": ["p"], "actors": ["a"], "risks": ["r"]}, turns=3)
    turns = captured[0]
    assert [t.answered for t in turns] == [["problem"], ["actors"], []]


def test_every_run_starts_the_sheet_over(capture):
    """K runs are K independent conversations. A sheet shared across them would leave run 2 with the layers
    run 1 had not used."""
    calls, captured, _ = capture([_model("problem"), _model("problem"), _model()],
                                 {"problem": ["first", "second"]}, k=2)
    per_run_first_answer = [calls[1]["answers"], calls[4]["answers"]]
    assert all("first" in a for a in per_run_first_answer)
    assert len(captured) == 2


# -- #163: the sheet layers a SHALLOW live capture never got to ---------------------------------
#
# `capture_interactive` prints its own SHALLOW verdict and the unreached-sheet-layers line right where the API calls were just spent -- the point a maintainer actually reads, as opposed to the offline `golden_diff.py` pass this module otherwise mirrors.

@pytest.mark.parametrize(
    "replies, answers, must_report",
    [
        pytest.param([_model("problem"), _model()], {"problem": ["first", "second", "third"]}, True,
                     id="#163-shallow-must-fire"),
        pytest.param([_model("problem")] * 5, {"problem": [f"l{i}" for i in range(1, 11)]}, False,
                     id="#163-deep-must-not-fire"),
    ],
)
def test_capture_reports_unreached_sheet_layers_only_when_shallow(capture, replies, answers,
                                                                   must_report):
    """#163: a run that converges early must name the sheet layers it never reached."""
    _, _, output = capture(replies, answers)
    assert ("sheet layers never reached" in output) is must_report, output
    if must_report:
        assert "Real problem (2)" in output, output
