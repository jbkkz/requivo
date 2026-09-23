"""The interactive `discover` loop over a stub provider (#77), its entry points (#133, #202, #206) and `run`,
one verb over discover's loop and answer's apply path (#540, #541)."""
from __future__ import annotations

import builtins
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from _fakes import (
    _ENGINE_REPLY,
    _JUDGMENT_REPLY,
    _ROUTING_REPLY,
    FakeClient,
    StubProvider,
    forge_meta,
    full_model,
    printed,
    run_cli,
    run_cli_fails,
    seed_session,
    slot,
)

from requivo.cli import MAX_TURNS, QUESTIONS_PER_CHECKPOINT, converse
from requivo.core.contracts import MAX_QUESTIONS, Brief, EngineOutput, Question, Slot, Summary, schema_slot_ids
from requivo.core.errors import ProviderOutputError
from requivo.core.perimeters import GO_TO_MARKET
from requivo.providers.base import ReasoningProvider
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

ARROW = "→"  # the separator converse() puts between a question and its answer
_REQUEST = "a leave approval system, discovered twice"
_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_ASKING_REPLY = json.dumps({**full_model(problem=slot(80, "explicit", "high")),
                            "questions": [{"q": "Who approves?", "slot": "permissions", "why": "approval routing drives it"}]})
_DISCOVER = (_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
_ASKING = (_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY)
_UNAVAILABLE = EngineError("API unavailable")


def _raise(exc):
    return lambda *a, **kw: (_ for _ in ()).throw(exc)


def _model(*, objective: str, questions: list[Question] | None = None) -> EngineOutput:
    return EngineOutput(model={"problem": Slot(completeness=80, confidence="explicit", impact="high", value="v")},
                        summary=Summary(objective=objective), questions=questions or [])


def _questions(n: int) -> list[Question]:
    return [Question(q=f"Question {i}?" if n > 1 else "Who approves?", slot="permissions", why="w") for i in range(n)]


def _asking(*objectives: str, n: int = 1) -> list[EngineOutput]:
    """One asking turn per objective, each carrying `n` questions."""
    return [_model(objective=o, questions=_questions(n)) for o in objectives]


def _provider(*turns: EngineOutput) -> StubProvider:
    return StubProvider(*turns, artifacts={"brief": Brief(complexity="low", solution="a solution")})


def _converse(provider, request="a request", answers=(), only=None, prompts=None):
    """Run the loop with `answers` fed to `input()` in order (an exception is raised there), capturing stdout (#592)."""
    supplied, drafted = iter(answers), []

    def _input(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        answer = next(supplied)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    with patch.object(builtins, "input", _input):
        text = printed(lambda: drafted.append(converse(DiscoveryService(provider), request, only=only)))
    return drafted[0], text


def _at_a_terminal(monkeypatch, answer: str | None = None) -> None:
    """`_cmd_discover` picks its branch on `--once` *or* the absence of a TTY; `answer` is what every prompt gets."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    if answer is not None:
        monkeypatch.setattr(builtins, "input", lambda _prompt="": answer)


def _fail_draft_turn_on(monkeypatch, nth: int, exc: BaseException) -> None:
    """Let the real `draft_turn` run, then raise `exc` on the `nth` call."""
    real, calls = DiscoveryService.draft_turn, {"n": 0}

    def stub(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == nth:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(DiscoveryService, "draft_turn", stub)


def _sessions():
    return SessionService().list_sessions()


def _revisions() -> list[int]:
    return [m.current_revision for m in _sessions()]


# ── the loop over `converse()` ───────────────────────────────────────────────────


def test_the_stub_satisfies_the_provider_protocol():
    """The control for every test in this file: the stub offers every parameter the protocol declares."""
    assert isinstance(_provider(), ReasoningProvider)
    for name in ("analyze", "generate"):
        declared = set(inspect.signature(getattr(ReasoningProvider, name)).parameters)
        offered = set(inspect.signature(getattr(StubProvider, name)).parameters)
        assert declared <= offered, f"StubProvider.{name} is missing {sorted(declared - offered)}"

    class Drifted:
        name = "drifted"

        def analyze(self, request): ...
        def generate(self, artifact_type, model): ...
        def model_name(self): ...
        def provenance(self, op): ...

    assert isinstance(Drifted(), ReasoningProvider), "isinstance passes on a drifted stub, which is why the parameter check exists"
    assert not set(inspect.signature(ReasoningProvider.analyze).parameters) <= set(inspect.signature(Drifted.analyze).parameters)


def test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript():
    """#77's behavioural half: each turn goes through `DiscoveryService.draft_turn` carrying the prior model and the cards."""
    first, second = *_asking("one"), _model(objective="two")
    provider = _provider(first, second)
    out = _converse(provider, "a leave approval system", ["the line manager"], only=["financial-reporting"])[0].model
    assert out is second and provider.analyze_calls == 2
    opening, refinement = provider.analyze_kwargs
    assert opening["current_model"] is None and opening["answers"] is None and opening["request"] == "a leave approval system"
    assert refinement["current_model"] is first, "the refinement turn did not carry the prior model"
    assert refinement["answers"] == f"[slot: permissions] Q: Who approves? {ARROW} A: the line manager"
    assert refinement["request"] == "a leave approval system", "the request is context on every turn"
    assert [c["only"] for c in provider.analyze_kwargs] == [["financial-reporting"]] * 2


def test_the_loop_declares_its_repeated_prompt_at_the_seam():
    """A drafting loop's breakpoint is read back; a one-shot operation pays for a cache nothing reads (#258)."""
    provider = _provider(_model(objective="done"), _model(objective="done"))
    _converse(provider, "a leave approval system")
    assert provider.analyze_kwargs[0]["reuse_system"] is True, "the drafting loop lost its breakpoint"
    DiscoveryService(provider)._need_provider().analyze("a leave approval system")
    assert provider.analyze_kwargs[1]["reuse_system"] is False


@pytest.mark.parametrize("asked, answers, expected", [
    (1, ["q"], "Stopped."),                                   # the user quits at the first question
    (1, [""], "No answer provided"),                          # every question skipped: nothing to feed back
    (QUESTIONS_PER_CHECKPOINT, ["an answer", "q"], "Stopped."),  # quitting mid-window keeps the drafted turn
    (1, [KeyboardInterrupt()], "Stopped."),
    (1, [EOFError()], "Stopped."),
], ids=["quit", "skip", "quit-mid-window", "ctrl-c", "eof"])
def test_stopping_early_stops_reasoning_and_says_so(asked, answers, expected):
    """A stop, or an interrupt at the prompt, is a stop: no further call, the paid turn kept, and the loop says so (#202)."""
    provider = _provider(*_asking("one", n=asked))
    drafted, text = _converse(provider, "a request", answers)
    assert drafted.stopped is True and drafted.model is not None and provider.analyze_calls == 1
    assert expected in text


def test_the_turn_limit_still_bounds_the_loop():
    """`MAX_TURNS` is the only thing between a model that keeps asking and an unbounded spend."""
    asking = _asking(*(f"turn {i}" for i in range(MAX_TURNS)))
    provider = _provider(*asking)
    drafted, text = _converse(provider, "a request", ["an answer"] * MAX_TURNS)
    assert provider.analyze_calls == MAX_TURNS and drafted.model is asking[-1]
    assert drafted.stopped is False, "the turn limit is not the user stopping — the brief still runs"
    assert f"{MAX_TURNS}-turn limit" in text


def test_the_interactive_loop_asks_one_question_per_prompt():
    """One question per prompt, the checkpoint between batches (#592)."""
    provider = _provider(*_asking("one", n=3), _model(objective="two"))
    prompts: list[str] = []
    _, text = _converse(provider, "a request", ["a", "b", "c"], prompts=prompts)
    assert "PRIORITY QUESTIONS" not in text and "UNDERSTANDING" in text
    assert len(prompts) == 3, "the loop did not ask each question at its own prompt"
    for i, prompt in enumerate(prompts):
        assert f"Question {i}?" in prompt and sum(f"Question {j}?" in prompt for j in range(3)) == 1


def test_the_checkpoint_window_fits_inside_the_contract_cap():
    """Only `QUESTIONS_PER_CHECKPOINT` of a turn's `MAX_QUESTIONS` are asked, then compiled as one turn (#592)."""
    assert 0 < QUESTIONS_PER_CHECKPOINT <= MAX_QUESTIONS
    first, second = *_asking("one", n=MAX_QUESTIONS), _model(objective="two")
    provider = _provider(first, second)
    prompts: list[str] = []
    _converse(provider, "a request", ["a"] * MAX_QUESTIONS, prompts=prompts)
    assert len(prompts) == QUESTIONS_PER_CHECKPOINT
    dropped = first.questions[QUESTIONS_PER_CHECKPOINT:]
    assert dropped, "the fixture did not actually overflow the window"
    for q in dropped:
        assert not any(q.q in p for p in prompts), f"{q.q!r} was asked past the window"
    assert provider.analyze_calls == 2, "a window is one turn, not one turn per question"
    folded = provider.analyze_kwargs[1]["answers"]
    assert folded.count(ARROW) == QUESTIONS_PER_CHECKPOINT and len(folded.splitlines()) == QUESTIONS_PER_CHECKPOINT


def test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does():
    """The golden harness drives `draft_turn` off an answer sheet rather than a TTY, in the loop's own words."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from golden_lib import AnswerSheet, answers_for_turn

    first = _asking("one")[0]
    provider = _provider(first, _model(objective="two"))
    _converse(provider, "a request", ["a scripted reply"])
    from_the_harness, answered = answers_for_turn(first.questions, AnswerSheet({"permissions": ["a scripted reply"]}))
    assert from_the_harness == provider.analyze_kwargs[1]["answers"] and answered == ["permissions"]


# ── the entry-point gate, through `app()` (#133) ─────────────────────────────────


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_both_discover_entry_points_refuse_a_refined_session_before_paying(monkeypatch, argv_tail):
    """Invariant 13's revision-zero gate is taken before the first billed call on *both* paths (#133)."""
    _at_a_terminal(monkeypatch)
    run_cli(["discover", _REQUEST, "--once"], client=FakeClient(*_DISCOVER))  # -> revision 1
    fake = FakeClient(*_DISCOVER, _BRIEF_REPLY)   # scripted through to the old refusal point
    code, err = run_cli_fails(["discover", _REQUEST, *argv_tail], client=fake)
    assert code == 1 and "already carries a model" in err
    assert fake.calls == [], f"{len(fake.calls)} provider call(s) were billed before the refusal"


@pytest.mark.parametrize("argv_tail, calls", [(["--once"], 3), ([], 4)], ids=["once", "interactive"])
def test_a_first_discovery_still_reaches_the_provider_on_both_paths(monkeypatch, argv_tail, calls):
    """The must-fire half: `fake.calls == []` is also true of a verb that never ran (#601)."""
    _at_a_terminal(monkeypatch)
    fake = FakeClient(*_DISCOVER, _BRIEF_REPLY)
    run_cli(["discover", _REQUEST, *argv_tail], client=fake)
    assert len(fake.calls) == calls
    assert _revisions() == [1 if argv_tail else 2]   # the second revision is #202's fix showing through


def test_stopping_early_keeps_the_turns_it_paid_for(monkeypatch):
    """Stopping is not a reason to lose what you already bought (#202)."""
    _at_a_terminal(monkeypatch, "q")
    fake = FakeClient(*_ASKING)
    text = run_cli(["discover", _REQUEST], client=fake)
    assert _revisions() == [1], "the stop discarded the turn the user had already paid for"
    assert len(fake.calls) == 3, "the loop kept reasoning after the stop, or bought a brief nobody asked for"
    assert "Stopped." in text and _sessions()[0].slug in text and "requivo answer" in text


def test_a_finished_go_to_market_discovery_ends_with_the_saved_session_not_a_traceback(monkeypatch):
    """A perimeter with no "brief" generator (go-to-market, #609) must not have one generated; its verbs refuse cleanly."""
    _at_a_terminal(monkeypatch)
    _, required = schema_slot_ids(GO_TO_MARKET)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high", "value": "x", "evidence": "y"} for sid in required}
    reply = json.dumps({"model": model, "questions": [], "summary": {"objective": "grow the funnel"}})
    out = run_cli(["discover", _REQUEST, "--perimeter", GO_TO_MARKET], client=FakeClient(_JUDGMENT_REPLY, reply))  # explicit perimeter: no routing call
    assert _revisions() == [1], "the discovery was not saved"
    assert "brief" not in out.lower() and "Capacity" in out and "business_rules" not in out
    code, err = run_cli_fails(["brief", _sessions()[0].slug])
    assert code == 1 and "Traceback" not in err and "go-to-market" in err and "brief" in err


# ── a failure after the first paid turn must not cost the turns before it (#202, #206, #320) ──


def test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry(monkeypatch):
    """The single most expensive failure a real user could hit (#202)."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(DiscoveryService, "generate", _raise(_UNAVAILABLE))
    code, err = run_cli_fails(["discover", _REQUEST], client=FakeClient(*_DISCOVER))
    assert code == 1 and _revisions() == [1], "the drafted model was not persisted before the assessment call"
    assert f"requivo brief {_sessions()[0].slug}" in err, f"the failure did not name the command that finishes the run: {err!r}"


@pytest.mark.parametrize("nth, revision", [(2, 1), (1, 0)], ids=["mid-loop", "first-turn"])
def test_a_failed_draft_turn_persists_the_turns_that_succeeded(monkeypatch, nth, revision):
    """A transient failure mid-loop keeps turn 1 and names `answer`; a first-turn failure leaves revision 0 and names no verb."""
    _at_a_terminal(monkeypatch, "the line manager approves")
    _fail_draft_turn_on(monkeypatch, nth, _UNAVAILABLE)
    code, err = run_cli_fails(["discover", _REQUEST], client=FakeClient(*_ASKING))
    assert code == 1 and _revisions() == [revision], "a drafted, paid-for turn was dropped"
    assert _sessions()[0].slug in err and ("requivo answer" in err) is bool(revision), "named a verb with no model to refine"


@pytest.mark.parametrize("path", ["once", "interactive"], ids=["#206-once-path", "#206-mid-turn"])
def test_a_provider_output_failure_mid_turn_also_names_the_claimed_session(monkeypatch, path):
    """`ProviderOutputError` is a `RequivoError` sibling of `EngineError`, and both paths name the claimed session (#206)."""
    if path == "once":
        monkeypatch.setattr(DiscoveryService, "start", _raise(ProviderOutputError("bad json")))
        argv, client = ["discover", _REQUEST, "--once"], FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY)
    else:
        _at_a_terminal(monkeypatch, "the line manager approves")
        _fail_draft_turn_on(monkeypatch, 2, ProviderOutputError("bad json"))
        argv, client = ["discover", _REQUEST], FakeClient(*_ASKING)
    code, err = run_cli_fails(argv, client=client)
    assert code == 1 and len(_sessions()) == 1 and _sessions()[0].slug in err
    if path == "once":
        assert "re-run `requivo discover`" in err
    else:
        assert _revisions() == [1] and "requivo answer" in err


@pytest.mark.parametrize("where", ["mid-turn", "during-the-brief", "in-the-rescue-save"])
def test_an_interrupt_during_the_brief_reports_the_saved_session(monkeypatch, where):
    """A Ctrl-C inside a provider call, the brief, or the rescue's own save exits 130 and keeps the paid turn (#206, #320)."""
    _at_a_terminal(monkeypatch, "the line manager approves")
    if where == "mid-turn":
        _fail_draft_turn_on(monkeypatch, 2, KeyboardInterrupt())
    elif where == "during-the-brief":
        monkeypatch.setattr(DiscoveryService, "generate", _raise(KeyboardInterrupt()))
    else:
        _fail_draft_turn_on(monkeypatch, 2, _UNAVAILABLE)
        monkeypatch.setattr(DiscoveryService, "finalize_discovery", _raise(KeyboardInterrupt()))
    client = FakeClient(*(_DISCOVER if where == "during-the-brief" else _ASKING))
    code, err = run_cli_fails(["discover", _REQUEST], client=client)
    assert code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    assert "interrupted" in err.lower(), f"a KeyboardInterrupt stringifies to '', so the message trailed off: {err!r}"
    if where == "in-the-rescue-save":
        assert "could NOT be saved" in err
    else:
        assert _revisions() == [1], "the interrupt discarded a paid turn"
        assert (f"requivo brief {_sessions()[0].slug}" if where == "during-the-brief" else _sessions()[0].slug) in err


def test_a_rescue_that_cannot_save_says_so_and_still_names_the_original_failure(monkeypatch):
    """#320: the rescue's own save was unguarded, in the code path whose entire job is keeping the work."""
    _at_a_terminal(monkeypatch, "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable (529)."))
    monkeypatch.setattr(DiscoveryService, "finalize_discovery", _raise(EngineError("the disk is full")))
    code, err = run_cli_fails(["discover", _REQUEST], client=FakeClient(*_ASKING))
    assert code == 1
    assert "could NOT be saved" in err and "the disk is full" in err, "the save's own failure was swallowed"
    assert "529" in err, f"the original provider failure was masked by the save's: {err!r}"


@pytest.mark.parametrize("claimed", [True, False], ids=["after-the-claim", "before-the-claim"])
def test_an_interrupt_in_the_once_path_names_the_claimed_session_and_the_retry(monkeypatch, claimed):
    """`--once` claims a session then pays once: an interrupt names it and the retry, or names nothing before the claim (#206)."""
    target = (DiscoveryService, "start") if claimed else ("requivo.cli.is_file_argument",)
    monkeypatch.setattr(*target, _raise(KeyboardInterrupt()))
    code, err = run_cli_fails(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY))
    assert code == 130 and "Traceback" not in err and "Interrupted." in err
    if claimed:
        assert _revisions() == [0] and _sessions()[0].slug in err and "re-run `requivo discover`" in err
    else:
        assert _sessions() == [] and "Saved" not in err, f"named a session that does not exist: {err!r}"


def test_a_top_level_interrupt_on_an_existing_session_exits_130_with_no_traceback(monkeypatch):
    """Every command other than `discover` reaches the provider with no claim of its own to make (#206)."""
    run_cli(["discover", _REQUEST, "--once"], client=FakeClient(*_DISCOVER))
    monkeypatch.setattr(DiscoveryService, "generate", _raise(KeyboardInterrupt()))
    code, err = run_cli_fails(["brief", _sessions()[0].slug])
    assert code == 130 and "Traceback" not in err and "Interrupted." in err


# ── `run`: one verb over discover's loop and answer's apply path (#540, #541) ─────


def test_run_with_a_request_makes_the_same_call_count_as_discover():
    """#540's acceptance criterion, as call counts: `run "…"` reaches `_cmd_discover` rather than reimplementing it."""
    fake_discover, fake_run = FakeClient(*_DISCOVER), FakeClient(*_DISCOVER)
    run_cli(["discover", _REQUEST, "--once"], client=fake_discover)
    run_cli(["run", _REQUEST + ", via run", "--once"], client=fake_run)
    assert len(fake_discover.calls) == 3 and len(fake_run.calls) == 3


def test_run_on_a_refined_session_resumes_through_answer_never_rediscovers(monkeypatch):
    """#540: a slug resumes through `answer` (one call); a claimed-but-undiscovered session refuses with none."""
    run_cli(["discover", _REQUEST, "--once"], client=FakeClient(*_ASKING))
    assert _revisions() == [1]
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    fake = FakeClient(_ENGINE_REPLY)   # converges, so the resume loop stops after this one turn
    run_cli(["run", _sessions()[0].slug], client=fake)
    assert len(fake.calls) == 1 and _revisions() == [2]
    SessionService().create_session(_REQUEST, slug="claimed-only")
    fake = FakeClient()
    assert run_cli_fails(["run", "claimed-only"], client=fake)[0] == 1 and fake.calls == []


def test_run_with_no_argument_and_one_session_resumes_it(monkeypatch):
    """#540/#541: no argument, one session -> resume it; several -> list them, the most recently written marked."""
    run_cli(["discover", _REQUEST, "--once"], client=FakeClient(*_ASKING))
    older = _sessions()[0].slug
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")   # stop immediately
    text = run_cli(["run"], client=FakeClient())
    assert older in text and len(_sessions()) == 1, "a no-argument run created a second session"
    run_cli(["discover", _REQUEST + ", again", "--once"], client=FakeClient(*_ASKING))
    newer = next(m.slug for m in _sessions() if m.slug != older)
    forge_meta(newer, {"updated_at": "2999-01-01T00:00:00Z"})
    text = run_cli(["run"], client=FakeClient())
    assert older in text and newer in text, "the listing must name every candidate"
    assert "→" in next(ln for ln in text.splitlines() if newer in ln), "the default was not marked"
    assert len(_sessions()) == 2, "a no-argument run created a third session"


def test_run_with_no_argument_and_no_session_prompts_for_a_request(monkeypatch):
    """#540: no argument, no session -> ask for a request the way `discover` reads one."""
    monkeypatch.setattr(builtins, "input", lambda _prompt="": _REQUEST)   # a pipe, not a terminal: `--once`'s path
    fake = FakeClient(*_DISCOVER)
    run_cli(["run"], client=fake)
    assert len(fake.calls) == 3, "the perimeter route, the grounding judgment, and the turn"
    assert _revisions() == [1]


def test_run_with_no_argument_is_not_hijacked_by_a_same_named_file(monkeypatch, tmp_path):
    """Found in review: the resolver's own slug used to be re-run through `is_file_argument`."""
    monkeypatch.chdir(tmp_path)
    seed_session("sample", "a request about sample", problem=slot(80, "explicit", "high"))
    (tmp_path / "sample").write_text("unrelated file contents", encoding="utf-8")
    fake = FakeClient()
    run_cli(["run"], client=fake)
    assert fake.calls == [], "the resolved slug was re-detected as a file and paid for a discovery"
    assert len(_sessions()) == 1, "a second session was created from the file"


@pytest.mark.parametrize("flag", [["--once"], ["--context", "b2b-platform"]], ids=["once", "context"])
def test_run_refuses_once_and_context_when_resuming(flag):
    """#540, found in review: `--once`/`--context` describe a *new* discovery and used to be silently ignored on a resume."""
    SessionService().create_session("a request about resumed", slug="resumed")
    SessionService().update_model("resumed", {**full_model(problem=slot(80, "explicit", "high")),
                                              "questions": [{"q": "Who approves?", "slot": "permissions", "why": "u"}]})
    fake = FakeClient()
    code, err = run_cli_fails(["run", "resumed", *flag], client=fake)
    assert code == 1 and "new discovery" in err
    assert fake.calls == [], "a refused resume still paid for a provider call"
