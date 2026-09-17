"""The interactive `discover` loop, driven end to end over a stub provider (#77)."""
from __future__ import annotations

import builtins
import inspect
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, _run_app, full_slots, slot

from requivo.cli import MAX_TURNS, QUESTIONS_PER_CHECKPOINT, app, converse
from requivo.core.contracts import MAX_QUESTIONS, Brief, EngineOutput, Question, Slot, Summary
from requivo.providers.base import ReasoningProvider
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

ARROW = "→"  # the separator converse() puts between a question and its answer


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """An isolated temp workspace for every test here, never the real repo."""


def _model(*, objective: str, questions: list[Question] | None = None) -> EngineOutput:
    """A model complete enough to be a real turn's output."""
    return EngineOutput(
        model={"problem": Slot(completeness=80, confidence="explicit", impact="high", value="v")},
        summary=Summary(objective=objective),
        questions=questions or [],
    )


def _question() -> Question:
    return Question(q="Who approves?", slot="permissions", why="approval routing drives the workflow")


class StubProvider:
    """Records every call and replays a scripted list of turns."""

    name = "stub"

    def __init__(self, *turns: EngineOutput, brief: Brief | None = None):
        self.turns = list(turns)
        self.brief = brief or Brief(complexity="low", solution="a solution")
        self.analyze_calls: list[dict] = []
        self.generate_calls: list[dict] = []

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.analyze_calls.append({
            "request": request, "current_model": current_model, "answers": answers,
            "only": only, "reuse_system": reuse_system,
        })
        if not self.turns:
            raise AssertionError("the loop asked for more turns than the stub was scripted with")
        return self.turns.pop(0)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls.append(
            {"artifact_type": artifact_type, "model": model, "only": only, **kwargs})
        return self.brief

    def model_name(self) -> str:
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None) -> dict:
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:0"}


def _service(provider: StubProvider) -> DiscoveryService:
    return DiscoveryService(provider)


def _converse(disco, request, answers=(), only=None, prompts=None):
    """Run the loop with `answers` fed to `input()` in order, capturing stdout (#592)."""
    supplied = iter(answers)
    buf = io.StringIO()
    real_input = builtins.input

    def _input(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        return next(supplied)

    builtins.input = _input
    try:
        with redirect_stdout(buf):
            out = converse(disco, request, only=only)
    finally:
        builtins.input = real_input
    return out, buf.getvalue()


def test_the_stub_satisfies_the_provider_protocol():
    """The control for every test in this file."""
    assert isinstance(StubProvider(), ReasoningProvider)
    for name in ("analyze", "generate"):
        declared = set(inspect.signature(getattr(ReasoningProvider, name)).parameters)
        offered = set(inspect.signature(getattr(StubProvider, name)).parameters)
        assert declared <= offered, (
            f"StubProvider.{name} is missing {sorted(declared - offered)} -- the fixture has drifted "
            f"from the protocol, so the tests below assert against a seam the real provider is not"
        )

    class Drifted:
        """Present on every member, wrong on every signature."""

        name = "drifted"

        def analyze(self, request): ...
        def generate(self, artifact_type, model): ...
        def model_name(self): ...
        def provenance(self, op): ...

    assert isinstance(Drifted(), ReasoningProvider), (
        "this control assumes isinstance passes on a signature-drifted stub -- that is the whole "
        "limit the parameter comparison above exists to cover"
    )
    assert not set(inspect.signature(ReasoningProvider.analyze).parameters) <= set(
        inspect.signature(Drifted.analyze).parameters
    ), "the parameter comparison cannot fire, so it is not a check"


def test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript():
    """#77's behavioural half. Each turn goes through `DiscoveryService.draft_turn`."""
    first = _model(objective="one", questions=[_question()])
    second = _model(objective="two")
    provider = StubProvider(first, second)

    out = _converse(_service(provider), "a leave approval system", ["the line manager"])[0].model

    assert out is second
    assert len(provider.analyze_calls) == 2
    opening, refinement = provider.analyze_calls
    assert opening["current_model"] is None and opening["answers"] is None
    assert opening["request"] == "a leave approval system"
    assert refinement["current_model"] is first, "the refinement turn did not carry the prior model"
    assert refinement["answers"] == f"[slot: permissions] Q: Who approves? {ARROW} A: the line manager"
    assert refinement["request"] == "a leave approval system", "the request is context on every turn"


def test_the_loop_declares_its_repeated_prompt_at_the_seam():
    """A drafting loop sends one system prompt several times, so the breakpoint is genuinely read back and is
    worth its write."""
    provider = StubProvider(_model(objective="done"), _model(objective="done"))
    disco = _service(provider)

    _converse(disco, "a leave approval system")
    assert provider.analyze_calls[0]["reuse_system"] is True, "the drafting loop lost its breakpoint"

    disco._need_provider().analyze("a leave approval system")  # any one-shot operation
    assert provider.analyze_calls[1]["reuse_system"] is False, (
        "a single-call operation pays for a cache nothing reads"
    )


def test_the_context_cards_are_held_constant_across_every_turn():
    """A card selection is what the impact estimates are read against."""
    provider = StubProvider(
        _model(objective="one", questions=[_question()]),
        _model(objective="two"),
    )
    _converse(_service(provider), "a request", ["a manager"], only=["financial-reporting"])
    assert [c["only"] for c in provider.analyze_calls] == [["financial-reporting"]] * 2


@pytest.mark.parametrize("answers, expected", [
    (["q"], "Stopped."),   # the user quits at the first question
    ([""], "No answer provided"),   # every question skipped -- nothing to feed back
])
def test_stopping_early_stops_reasoning_and_says_so(answers, expected):
    """A stop is a stop: the loop makes no further call and flags itself as stopped (#202)."""
    provider = StubProvider(_model(objective="one", questions=[_question()]))
    drafted, printed = _converse(_service(provider), "a request", answers)
    assert drafted.stopped is True
    assert drafted.model is not None, "the turn the user paid for was discarded"
    assert len(provider.analyze_calls) == 1
    assert expected in printed


def test_the_turn_limit_still_bounds_the_loop():
    """`MAX_TURNS` is the only thing between a model that keeps asking questions and an unbounded spend."""
    asking = [_model(objective=f"turn {i}", questions=[_question()]) for i in range(MAX_TURNS)]
    provider = StubProvider(*asking)
    drafted, printed = _converse(_service(provider), "a request", ["an answer"] * MAX_TURNS)
    assert len(provider.analyze_calls) == MAX_TURNS
    assert drafted.model is asking[-1]
    assert drafted.stopped is False, "the turn limit is not the user stopping — the brief still runs"
    assert f"{MAX_TURNS}-turn limit" in printed


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_an_interrupt_at_the_prompt_stops_rather_than_traces_back(interrupt):
    """Ctrl-C and EOF at a question are how a real user leaves this loop, so they end it cleanly."""
    provider = StubProvider(_model(objective="one", questions=[_question()]))

    def raiser(_prompt=""):
        raise interrupt

    real_input, builtins.input = builtins.input, raiser
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            drafted = converse(_service(provider), "a request")
    finally:
        builtins.input = real_input
    assert drafted.stopped is True, f"{interrupt.__name__} did not stop the loop"
    assert drafted.model is not None, "the turn the user paid for was discarded"
    assert "Stopped." in buf.getvalue()

# ── one question at a time (#592) ──────────────────────────────────────────────────────────────────


def _questions(n: int) -> list[Question]:
    return [Question(q=f"Question {i}?", slot="permissions", why="w") for i in range(n)]


def test_the_interactive_loop_asks_one_question_per_prompt():
    """A turn printed every question it produced and *then* walked the same list at the prompt (#592)."""
    provider = StubProvider(_model(objective="one", questions=_questions(3)),
                            _model(objective="two"))
    prompts: list[str] = []
    _, printed = _converse(_service(provider), "a request", ["a", "b", "c"], prompts=prompts)

    assert "PRIORITY QUESTIONS" not in printed
    assert "UNDERSTANDING" in printed, "the checkpoint went missing with the question block"
    assert len(prompts) == 3, "the loop did not ask each question at its own prompt"
    for i, prompt in enumerate(prompts):
        assert f"Question {i}?" in prompt
        assert sum(f"Question {j}?" in prompt for j in range(3)) == 1, (
            f"prompt {i} carried more than one question: {prompt!r}")


def test_only_the_checkpoint_window_is_asked_and_the_remainder_is_dropped():
    """`MAX_QUESTIONS` is what a turn may return; `QUESTIONS_PER_CHECKPOINT` is what the terminal asks before
    compiling."""
    surplus = _questions(MAX_QUESTIONS)
    provider = StubProvider(_model(objective="one", questions=surplus), _model(objective="two"))
    prompts: list[str] = []
    _converse(_service(provider), "a request", ["a"] * MAX_QUESTIONS, prompts=prompts)

    assert len(prompts) == QUESTIONS_PER_CHECKPOINT
    dropped = surplus[QUESTIONS_PER_CHECKPOINT:]
    assert dropped, "the fixture did not actually overflow the window"
    for q in dropped:
        assert not any(q.q in p for p in prompts), f"{q.q!r} was asked past the window"


def test_a_full_window_still_reaches_the_provider_as_one_turn():
    """The compile half. This pins forward rather than reproducing a defect."""
    provider = StubProvider(_model(objective="one", questions=_questions(QUESTIONS_PER_CHECKPOINT)),
                            _model(objective="two"))
    _converse(_service(provider), "a request", ["a", "b", "c", "d"])

    assert len(provider.analyze_calls) == 2, "a window is one turn, not one turn per question"
    folded = provider.analyze_calls[1]["answers"]
    assert folded.count(ARROW) == QUESTIONS_PER_CHECKPOINT
    assert len(folded.splitlines()) == QUESTIONS_PER_CHECKPOINT


def test_stopping_mid_window_keeps_the_turn_it_paid_for():
    """#202's guarantee, at the new boundary: quitting on the second of four questions still hands the caller
    the turn already drafted, and buys no further one."""
    provider = StubProvider(_model(objective="one", questions=_questions(QUESTIONS_PER_CHECKPOINT)))
    drafted, printed = _converse(_service(provider), "a request", ["an answer", "q"])

    assert drafted.stopped is True
    assert drafted.model is not None, "the turn the user paid for was discarded"
    assert len(provider.analyze_calls) == 1
    assert "Stopped." in printed


def test_the_checkpoint_window_fits_inside_the_contract_cap():
    """A window above the cap would silently mean "ask them all" and put the wall back."""
    assert 0 < QUESTIONS_PER_CHECKPOINT <= MAX_QUESTIONS


# ── the entry-point gate, driven through `app()` (#133) ────────────────────────────────────────────
# Everything above injects a stub provider into the service and drives `converse()` directly.

_REQUEST = "a leave approval system, discovered twice"
_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_ASKING_REPLY = json.dumps({
    "model": full_slots(problem=slot(80, "explicit", "high")),
    "questions": [{"q": "Who approves?", "slot": "permissions", "why": "approval routing drives it"}],
    "summary": {"objective": "A leave approval system"},
})


def _at_a_terminal(monkeypatch) -> None:
    """`_cmd_discover` picks its branch on `--once` *or* the absence of a TTY."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_both_discover_entry_points_refuse_a_refined_session_before_paying(monkeypatch, capsys, argv_tail):
    """Invariant 13's revision-zero gate is taken before the first billed call on *both* paths (#133)."""
    _at_a_terminal(monkeypatch)
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))  # → revision 1

    # Scripted with a turn *and* an assessment, so an ungated run gets all the way to the old refusal point and the count below reports how much it spent rather than dying on an exhausted stub.
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY, _BRIEF_REPLY)
    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, *argv_tail], client=fake)

    assert exit_.value.code == 1
    assert "already carries a model" in capsys.readouterr().err
    assert fake.calls == [], (
        f"{len(fake.calls)} provider call(s) were billed before the refusal — the gate is downstream "
        f"of the reasoning on this path"
    )


@pytest.mark.parametrize("argv_tail, calls", [(["--once"], 3), ([], 4)], ids=["once", "interactive"])
def test_a_first_discovery_still_reaches_the_provider_on_both_paths(monkeypatch, argv_tail, calls):
    """The must-fire half of the test above. `fake.calls == []` is also true of a verb that never ran, a stub
    that was never reached and a harness that broke (#601)."""
    _at_a_terminal(monkeypatch)
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY, _BRIEF_REPLY)
    _run_app(["discover", _REQUEST, *argv_tail], client=fake)
    assert len(fake.calls) == calls
    # Two revisions on the interactive path, and the second one is #202's fix showing through.
    assert [m.current_revision for m in SessionService().list_sessions()] == [1 if argv_tail else 2]


def test_stopping_early_keeps_the_turns_it_paid_for(monkeypatch, capsys):
    """Stopping is not a reason to lose what you already bought (#202)."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY)

    printed = _run_app(["discover", _REQUEST], client=fake)

    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], (
        "the stop discarded the turn the user had already paid for"
    )
    assert len(fake.calls) == 3, (
        "the loop kept reasoning after the user stopped, or bought a decision brief nobody asked for"
    )
    assert "Stopped." in printed
    assert sessions[0].slug in printed, "the claimed session is on disk and nothing said so"
    assert "requivo answer" in printed, "kept the work and did not say how to continue it"


def test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does():
    """The golden harness's multi-turn capture drives `draft_turn` off a scripted answer sheet rather than a
    TTY."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from golden_lib import AnswerSheet, answers_for_turn

    question = _question()
    provider = StubProvider(_model(objective="one", questions=[question]),
                            _model(objective="two"))
    _converse(_service(provider), "a request", ["a scripted reply"])
    from_the_loop = provider.analyze_calls[1]["answers"]

    from_the_harness, answered = answers_for_turn(
        [question], AnswerSheet({question.slot: ["a scripted reply"]}))
    assert from_the_harness == from_the_loop
    assert answered == [question.slot]


# ── #202: a failure after the first paid turn must not cost the turns before it ───────────────────


def _fail_draft_turn_on(monkeypatch, nth: int, exc: BaseException) -> None:
    """Let the real `draft_turn` run, then raise `exc` on the `nth` call."""
    real = DiscoveryService.draft_turn
    calls = {"n": 0}

    def stub(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == nth:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(DiscoveryService, "draft_turn", stub)


def test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry(monkeypatch, capsys):
    """The single most expensive failure a real user could hit (#202)."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(EngineError("API unavailable")))
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=fake)

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], (
        "the drafted model was not persisted before the assessment call, so the failure discarded it"
    )
    err = capsys.readouterr().err
    assert f"requivo brief {sessions[0].slug}" in err, (
        "the failure did not name the one command that finishes the run without re-paying for "
        f"discovery; it said: {err!r}"
    )


def test_a_finished_go_to_market_discovery_ends_with_the_saved_session_not_a_traceback(monkeypatch, capsys):
    """[P2, review] A perimeter with no "brief" generator (go-to-market, #609's own scope) used to call
    `disco.generate(slug, "brief")` unconditionally once the interactive loop converged."""
    from requivo.core.contracts import schema_slot_ids
    from requivo.core.perimeters import GO_TO_MARKET

    _at_a_terminal(monkeypatch)
    _, required = schema_slot_ids(GO_TO_MARKET)
    reply = json.dumps({
        "model": {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                        "value": "x", "evidence": "y"} for sid in required},
        "questions": [], "summary": {"objective": "grow the funnel"},
    })
    # --perimeter is explicit here, so the router (#601) is never asked -- no routing reply queued.
    fake = FakeClient(_JUDGMENT_REPLY, reply)

    out = _run_app(["discover", _REQUEST, "--perimeter", GO_TO_MARKET], client=fake)

    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "the discovery was not saved"
    assert "brief" not in out.lower(), "a brief-generation attempt was made for a perimeter with none"
    # The rendering half of the same review finding.
    assert "Capacity" in out and "business_rules" not in out


def test_a_software_only_verb_on_a_go_to_market_session_refuses_cleanly(monkeypatch, capsys):
    """#609 (flagged as out of scope, then asked for)."""
    from requivo.core.contracts import schema_slot_ids
    from requivo.core.perimeters import GO_TO_MARKET
    from requivo.services.sessions import SessionService

    _at_a_terminal(monkeypatch)
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm-refusal", perimeter=GO_TO_MARKET)
    _, required = schema_slot_ids(GO_TO_MARKET)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                   "value": "x", "evidence": "y"} for sid in required}
    svc.update_model(meta.slug, json.dumps({"model": model, "questions": [],
                                            "summary": {"objective": "grow"}}), expected_revision=0)

    with pytest.raises(SystemExit) as exit_:
        app(["brief", meta.slug])

    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, f"a clean refusal must not traceback; got: {err!r}"
    assert "go-to-market" in err and "brief" in err


def test_a_failed_draft_turn_persists_the_turns_that_succeeded(monkeypatch, capsys):
    """The same loss, one call earlier: a transient failure *mid-loop* rather than on the assessment."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable"))
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=fake)

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "turn 1 was drafted, paid for, and dropped"
    err = capsys.readouterr().err
    assert sessions[0].slug in err and "requivo answer" in err, (
        f"the abort named neither the session nor the way to continue it: {err!r}"
    )


def test_a_first_turn_that_fails_leaves_the_session_at_revision_zero(monkeypatch, capsys):
    """The one case with genuinely nothing to save, and it must not invent a revision out of it."""
    _at_a_terminal(monkeypatch)
    _fail_draft_turn_on(monkeypatch, 1, EngineError("API unavailable"))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [0]
    err = capsys.readouterr().err
    assert sessions[0].slug in err
    assert "requivo answer" not in err, "named a verb with no model to refine"


def test_an_interrupt_inside_a_draft_turn_is_not_a_traceback(monkeypatch, capsys):
    """`converse`'s existing catch wraps the `input()` loop, so a Ctrl-C landing *inside* the provider call —
    the several-second window where it is most likely to land — went past it."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, KeyboardInterrupt())

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))

    assert exit_.value.code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "the interrupt discarded a paid turn"
    err = capsys.readouterr().err
    assert "Interrupted." in err and sessions[0].slug in err


def test_an_interrupt_during_the_brief_reports_the_saved_session(monkeypatch, capsys):
    """#320. #202's changelog promised that a Ctrl-C inside a provider call is no longer a traceback."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))

    assert exit_.value.code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "the interrupt discarded the drafted turn"
    err = capsys.readouterr().err
    assert f"requivo brief {sessions[0].slug}" in err
    assert "interrupted" in err.lower(), (
        f"a KeyboardInterrupt stringifies to '', so the message trailed off into nothing: {err!r}"
    )


def test_a_rescue_that_cannot_save_says_so_and_still_names_the_original_failure(monkeypatch, capsys):
    """#320. The rescue's own save was unguarded, in the code path whose entire job is keeping the work."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable (529)."))
    monkeypatch.setattr(DiscoveryService, "finalize_discovery",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            EngineError("the disk is full")))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))

    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert "could NOT be saved" in err, "the failed save was reported as a success"
    assert "the disk is full" in err, "the save's own failure was swallowed"
    assert "529" in err, (
        f"the original provider failure was masked by the save's, so the user cannot tell what "
        f"stopped the run: {err!r}"
    )
