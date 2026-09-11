"""The golden harness's interactive capture loop, driven offline.

`capture_interactive` is the only part of the harness that spends money, and it is the part whose
bugs are least visible: a loop that stopped one turn early, or recorded an answer the engine never
saw, still writes a well-formed baseline that every lens downstream reads without complaint. The
numbers would just be about a conversation that did not happen (#137).

Nothing here touches the network. `DiscoveryService.draft_turn` is replaced with a scripted stand-in
and the capture directory is redirected, so this is a pure test of the loop's own decisions: what it
sends, when it stops, and what it writes down about each turn.
"""
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
from requivo.services.discovery import DiscoveryService  # noqa: E402


def _model(*slots: str) -> EngineOutput:
    """A turn's reply that asks about each named slot. One filled slot keeps it a plausible model
    rather than a shell."""
    return EngineOutput(
        model={"problem": Slot(value="v", completeness=60, confidence="inferred", impact="high")},
        questions=[Question(q=f"about {s}?", slot=s, why="it drives the shape") for s in slots],
        summary=Summary(objective="an objective"),
    )


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """Run `capture_interactive` over a scripted provider, and hand back the calls, the file, and
    everything the run printed.

    `GOLDEN` is redirected before the run: the harness writes its baseline into the repository's own
    fixtures directory, and a test that forgot this would quietly overwrite a committed baseline with
    stub data.

    The printed output is returned rather than discarded because `capture_interactive` is the one
    path where the #163 diagnosis has to reach a human: it prints its own SHALLOW verdict and
    unreached-sheet-layers line right where the API calls were just spent, and the offline
    `golden_diff.py` pass this module otherwise mirrors cannot stand in for that print (#163).
    """
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)

    def run(replies: list[EngineOutput], answers: dict[str, list[str]], *, k: int = 1,
            turns: int = 5):
        calls: list[dict] = []
        scripted = list(replies)
        position = {"turn": 0}

        def fake_draft_turn(self, request, *, current_model=None, answers=None, cards=None):
            # `current_model is None` is how the loop says "this is turn 1", so the script replays
            # from the top for each of the K runs. A stand-in that indexed on the total call count
            # would hand run 2 the tail of run 1's script and end it immediately — the runs would
            # differ for a reason that came from the fixture rather than from the engine.
            if current_model is None:
                position["turn"] = 0
            calls.append({"request": request, "current_model": current_model,
                          "answers": answers, "cards": cards})
            reply = scripted[min(position["turn"], len(scripted) - 1)]
            position["turn"] += 1
            return reply

        monkeypatch.setattr(DiscoveryService, "draft_turn", fake_draft_turn)
        monkeypatch.setattr(golden_run, "K", k)
        monkeypatch.setattr(golden_run, "TURNS", turns)
        req = {"slug": "scripted", "form": "f", "card": "c", "request": "a request",
               "answers": answers}
        # `redirect_stdout` to a StringIO, which is the pattern `tests/test_cli_interactive.py`'s
        # `_converse` already uses and for the same reason: `capture_interactive` ends by printing
        # `✓` and `—`, and a StringIO never encodes. Without it these tests write those glyphs to the
        # real `sys.stdout`, and pass on Windows only because pytest's own capture happens to open
        # its buffer as UTF-8 — measured both ways: green under `pytest -q`, five
        # `UnicodeEncodeError`s under `pytest -q -s` with a cp1252 stdout. A test that is safe only
        # while a capture setting holds is a platform claim resting on something nobody stated, and
        # the maintainer who reaches for `-s` to debug one of these is the person it breaks for.
        buf = io.StringIO()
        with redirect_stdout(buf):
            # A stand-in client object rather than `None`: `capture_interactive` now constructs
            # `AnthropicProvider(client, model=...)` so the id it records is the id the calls were
            # given (#515), and `AnthropicProvider(None)` would fall through to `new_client()` and
            # want a credential. Nothing is called on it -- `draft_turn` is stubbed above.
            golden_run.capture_interactive(client=object(), req=req, model="claude-sonnet-5")
        captured = load_turns((tmp_path / "scripted.runs.json").read_text(encoding="utf-8"))
        return calls, captured, buf.getvalue()

    return run


def test_the_interactive_capture_records_the_model_it_reasoned_on(capture, tmp_path):
    """#515: the envelope records a capture's *input* and, until this, nothing about the conditions
    it ran under. The id is fixed on the provider the loop reasons through -- `AnthropicProvider`
    with an explicit `model` does no environment read at all on that path (#434) -- so what lands in
    the file is what reasoned, not a second read of `REQUIVO_MODEL` at write time."""
    capture([_model()], {})
    text = (tmp_path / "scripted.runs.json").read_text(encoding="utf-8")
    assert captured_model(text) == "claude-sonnet-5"


def test_the_capture_reasons_through_the_interactive_seam_and_not_a_message_list(capture):
    """The whole validity of the measurement. `draft_turn` is the production interactive path and the
    shape #77 changed; a loop that assembled its own message list here would capture a conversation no
    surface has held since. Turn 1 carries the request alone, and every turn after it carries the
    model so far plus the answers just given -- exactly what `converse()` sends."""
    calls, _, _ = capture([_model("problem"), _model("actors"), _model()],
                          {"problem": ["p"], "actors": ["a"]})
    assert calls[0]["current_model"] is None and calls[0]["answers"] is None
    assert all(c["request"] == "a request" for c in calls)
    assert calls[1]["current_model"] is not None
    assert "[slot: problem]" in calls[1]["answers"]
    assert "[slot: actors]" in calls[2]["answers"]


def test_a_run_stops_when_the_sheet_has_nothing_left_to_say(capture):
    """The fixture client running out of answers is the same event as a user pressing Enter on every
    question, and `converse()` stops there. Continuing would pay for turns carrying no new input and
    would let a run look deep without being it."""
    calls, captured, _ = capture([_model("problem"), _model("risks")], {"problem": ["p"]}, turns=5)
    assert len(calls) == 2, "the capture kept paying after the client had nothing left to say"
    assert [t.index for t in captured[0]] == [1, 2]


def test_a_run_stops_when_the_engine_stops_asking(capture):
    calls, captured, _ = capture([_model("problem"), _model()], {"problem": ["p"], "actors": ["a"]})
    assert len(calls) == 2
    assert [t.index for t in captured[0]] == [1, 2]


def test_the_final_turn_records_no_answer_it_never_sent(capture):
    """`answered` is what the *conversation* covered, and the re-ask count is measured against exactly
    that set. Recording the sheet's reply to the last turn -- which no call ever carried -- would put
    a slot in the covered set that the engine was never told about, and every finding downstream
    would be about a turn that did not happen."""
    _, captured, _ = capture([_model("problem"), _model("actors"), _model("risks")],
                             {"problem": ["p"], "actors": ["a"], "risks": ["r"]}, turns=3)
    turns = captured[0]
    assert [t.answered for t in turns] == [["problem"], ["actors"], []]


def test_every_run_starts_the_sheet_over(capture):
    """K runs are K independent conversations. A sheet shared across them would leave run 2 with the
    layers run 1 had not used, so the runs would not be comparable and the consensus would be over
    inputs that differed."""
    calls, captured, _ = capture([_model("problem"), _model("problem"), _model()],
                                 {"problem": ["first", "second"]}, k=2)
    per_run_first_answer = [calls[1]["answers"], calls[4]["answers"]]
    assert all("first" in a for a in per_run_first_answer)
    assert len(captured) == 2


# -- #163: the sheet layers a SHALLOW live capture never got to ---------------------------------
#
# `capture_interactive` prints its own SHALLOW verdict and the unreached-sheet-layers line right
# where the API calls were just spent -- the point a maintainer actually reads, as opposed to the
# offline `golden_diff.py` pass this module otherwise mirrors. A reviewer found that the live print
# was not wired to `unreached_layers` at all until it was added alongside `golden_diff.py`'s; these
# two cases are what stop that regressing silently, since nothing else in this file's suite reads
# `capture_interactive`'s own stdout.

def test_a_shallow_capture_prints_which_sheet_layers_it_never_reached(capture):
    """must fire. The engine stops asking about `problem` after turn 1, so the run converges at
    depth 2 with two of the sheet's three `problem` layers still unused -- the live print has to
    name that right where the 15 API calls were just spent, not only on a later `golden_diff.py`
    run against a committed baseline."""
    _, _, output = capture([_model("problem"), _model()],
                           {"problem": ["first", "second", "third"]})
    assert "sheet layers never reached" in output, output
    assert "Real problem (2)" in output, output


def test_a_deep_capture_does_not_report_leftover_sheet_layers(capture):
    """must not fire, the control for the case above. The engine keeps asking about `problem`
    every turn, so the run reaches the loop's own five-turn cap -- `deep_enough` -- with six of the
    sheet's ten `problem` layers still unused. Those leftovers are by design: the sheet is
    deliberately authored deeper than five turns so a run does not go dry before the cap, and
    reporting them here would be noise on every healthy capture."""
    replies = [_model("problem")] * 5
    _, _, output = capture(replies, {"problem": [f"l{i}" for i in range(1, 11)]}, turns=5)
    assert "sheet layers never reached" not in output, output
