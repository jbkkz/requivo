"""The interactive `discover` loop, driven end to end over a stub provider.

`converse()` is the CLI's TTY loop and, until #77, was also a second orchestration of discovery: it
called `run()` and then `advise()` itself and used `DiscoveryService` only for the final write. The
seam guard in `tests/test_boundaries.py` pins that those imports are gone. This file pins the other
half -- that the loop still does the same work through the service, because "the import list is
clean" is a claim about the file, not about the behaviour.

The provider is a stub implementing `ReasoningProvider`, injected into `DiscoveryService`, so nothing
here needs the Anthropic SDK, a key, or the network. `input()` is patched, since the whole point of
this path is that a human is on the other end of it.
"""
from __future__ import annotations

import builtins
import inspect
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _run_app, full_slots, slot

from requivo.cli import MAX_TURNS, app, converse
from requivo.core import persistence as store
from requivo.core.contracts import Brief, EngineOutput, Question, Slot, Summary
from requivo.core.errors import ProviderOutputError
from requivo.providers.base import ReasoningProvider
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

ARROW = "→"  # the separator converse() puts between a question and its answer


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """An isolated temp workspace for every test here, never the real repo.

    The `converse()` tests write nothing, so for them this is protection rather than a requirement.
    The `app()`-driven ones at the foot of the file really do claim sessions on disk.
    """
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


def _model(*, objective: str, questions: list[Question] | None = None) -> EngineOutput:
    """A model complete enough to be a real turn's output. The loop reads `questions` and hands the
    whole object back on the next turn, so the slot is there to make it a plausible `EngineOutput`
    rather than a shell that would pass an identity check and nothing else."""
    return EngineOutput(
        model={"problem": Slot(completeness=80, confidence="explicit", impact="high", value="v")},
        summary=Summary(objective=objective),
        questions=questions or [],
    )


def _question() -> Question:
    return Question(q="Who approves?", slot="permissions", why="approval routing drives the workflow")


class StubProvider:
    """Records every call and replays a scripted list of turns.

    Deliberately not a Mock: the assertions below are about *what the loop handed the seam* -- the
    request, the carried model, the answers, the cards -- and a recorded call list says that in one
    place. `name`/`model_name`/`provenance` are present because the protocol declares them.
    """

    name = "stub"

    def __init__(self, *turns: EngineOutput, brief: Brief | None = None):
        self.turns = list(turns)
        self.brief = brief or Brief(complexity="low", solution="a solution")
        self.analyze_calls: list[dict] = []
        self.generate_calls: list[dict] = []

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
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

    def provenance(self, op, *, only=None) -> dict:
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:0"}


def _service(provider: StubProvider) -> DiscoveryService:
    return DiscoveryService(provider)


def _converse(disco, request, answers=(), only=None):
    """Run the loop with `answers` fed to `input()` in order, capturing stdout."""
    supplied = iter(answers)
    buf = io.StringIO()
    real_input = builtins.input
    builtins.input = lambda _prompt="": next(supplied)
    try:
        with redirect_stdout(buf):
            out = converse(disco, request, only=only)
    finally:
        builtins.input = real_input
    return out, buf.getvalue()


def test_the_stub_satisfies_the_provider_protocol():
    """The control for every test in this file. A stub that had drifted from `ReasoningProvider`
    would let the loop pass against a seam the real provider does not offer -- the failure this
    fixture exists to catch, wearing a green tick.

    `isinstance` alone does not catch it, and that limit is `docs/providers.md`'s own: a
    `@runtime_checkable` protocol checks that a member is **present**, never that it has the right
    signature. The parameters are therefore compared as well, because the drift this file is exposed
    to is exactly a signature one -- `analyze` grew `reuse_system` in this very change, and a stub
    that had not grown it would have made every assertion below true about a seam that does not
    exist.

    The `Drifted` class is the must-fire control for the control: it passes `isinstance` and fails
    the signature comparison, so this test goes red if the second half is ever deleted as redundant.
    """
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
    """#77's behavioural half. Each turn goes through `DiscoveryService.draft_turn`, which hands the
    provider the request, the model so far and the answers just given.

    The first turn carries no model and no answers -- it is a first discovery, reasoning from the
    request alone. Every later turn carries the previous turn's model, which is what makes the CLI's
    loop the same turn operation `requivo answer` and the Web form use rather than a second one."""
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
    """A drafting loop sends one system prompt several times, so the breakpoint is genuinely read
    back and is worth its write. It used to be `converse()` passing `reuse_system=True` to `run()`
    directly; it has to survive the move to the seam or the interactive path silently pays full
    price on every turn after the first (#9, #58).

    MUST-FIRE control in the same fixture: a single-call operation must still say the opposite, or
    "declared it everywhere" would pass this too."""
    provider = StubProvider(_model(objective="done"), _model(objective="done"))
    disco = _service(provider)

    _converse(disco, "a leave approval system")
    assert provider.analyze_calls[0]["reuse_system"] is True, "the drafting loop lost its breakpoint"

    disco._need_provider().analyze("a leave approval system")  # any one-shot operation
    assert provider.analyze_calls[1]["reuse_system"] is False, (
        "a single-call operation pays for a cache nothing reads"
    )


def test_the_context_cards_are_held_constant_across_every_turn():
    """A card selection is what the impact estimates are read against, so a turn that quietly widened
    to the full set would reason a different session from the one before it."""
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
    """A stop is a stop: the loop makes no further call and flags itself as stopped, so
    `_cmd_discover` knows not to buy a decision brief the user did not ask for.

    The call count is the assertion that matters, because "it stopped" would also be true of a loop
    that kept reasoning and threw the result away -- and that one costs money.

    What it returns changed in #202: the model the user paid for comes back rather than `None`, so
    stopping keeps the turn instead of discarding it. `test_stopping_early_keeps_the_turns_it_paid_for`
    pins that half; this one pins that stopping still *stops*.

    This used to also say "and no session is claimed", which stopped being true in #133: the
    revision-zero gate claims the slug *before* the loop."""
    provider = StubProvider(_model(objective="one", questions=[_question()]))
    drafted, printed = _converse(_service(provider), "a request", answers)
    assert drafted.stopped is True
    assert drafted.model is not None, "the turn the user paid for was discarded"
    assert len(provider.analyze_calls) == 1
    assert expected in printed


def test_the_turn_limit_still_bounds_the_loop():
    """`MAX_TURNS` is the only thing between a model that keeps asking questions and an unbounded
    spend. Every scripted turn carries a question, so nothing but the limit can end this."""
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

# ── the entry-point gate, driven through `app()` (#133) ────────────────────────────────────────────
# Everything above injects a stub provider into the service and drives `converse()` directly. The
# three below drive the real `requivo discover` over a `FakeClient`, because what they pin is the
# *position* of a precondition relative to the first billed call — and a position is only visible
# from the whole verb.

_REQUEST = "a leave approval system, discovered twice"
_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_ASKING_REPLY = json.dumps({
    "model": full_slots(problem=slot(80, "explicit", "high")),
    "questions": [{"q": "Who approves?", "slot": "permissions", "why": "approval routing drives it"}],
    "summary": {"objective": "A leave approval system"},
})


def _at_a_terminal(monkeypatch) -> None:
    """`_cmd_discover` picks its branch on `--once` *or* the absence of a TTY, and under pytest stdin
    is never one. Patched for both legs of the tests below, so the flag is the only difference between
    them — which is what "the two entry points refuse identically" has to mean."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_both_discover_entry_points_refuse_a_refined_session_before_paying(monkeypatch, capsys, argv_tail):
    """Invariant 13's revision-zero gate is taken before the first billed call on *both* paths (#133).

    `--once` claimed the session inside `start()` and refused for free. The interactive branch reached
    the provider through `converse()` and met the gate only inside `finalize_discovery`, after the
    reasoning: two calls in this reproduction, up to nine in the field — eight turns plus the
    assessment — every one of them paid for and then thrown away by a refusal that was correct and in
    the wrong place. CLAUDE.md's own text for that invariant says a rule enforced by an interface is
    not enforced; this was the same shape one surface along.

    **The assertion is the call count, not the refusal.** The refusal already happened before the fix,
    so a test asserting only `revision_conflict` was green on the defect. The `--once` leg is the
    control: it passed before this change and must keep passing, or the two paths have merely swapped
    which one is wrong.

    Two claims, two tests: this one pins the gate's *position*, and
    `test_stopping_early_keeps_the_turns_it_paid_for` pins what a stop keeps. Citing the second for
    the first was #320 — it passes against the un-fixed code, so CLAUDE.md's sentence had a
    reference that resolved and guarded nothing, which `test_narrative_references.py` cannot see
    because it checks that a name exists and not that it fires.
    """
    _at_a_terminal(monkeypatch)
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ENGINE_REPLY))  # → revision 1

    # Scripted with a turn *and* an assessment, so an ungated run gets all the way to the old refusal
    # point and the count below reports how much it spent rather than dying on an exhausted stub.
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, *argv_tail], client=fake)

    assert exit_.value.code == 1
    assert "already carries a model" in capsys.readouterr().err
    assert fake.calls == [], (
        f"{len(fake.calls)} provider call(s) were billed before the refusal — the gate is downstream "
        f"of the reasoning on this path"
    )


@pytest.mark.parametrize("argv_tail, calls", [(["--once"], 1), ([], 2)], ids=["once", "interactive"])
def test_a_first_discovery_still_reaches_the_provider_on_both_paths(monkeypatch, argv_tail, calls):
    """The must-fire half of the test above. `fake.calls == []` is also true of a verb that never ran,
    a stub that was never reached and a harness that broke — so a gate refusing *everything* would
    pass that test and fail this one. The interactive path pays for two calls (the turn, then the
    assessment); `--once` pays for one, since it does not finalize."""
    _at_a_terminal(monkeypatch)
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    _run_app(["discover", _REQUEST, *argv_tail], client=fake)
    assert len(fake.calls) == calls
    # Two revisions on the interactive path, and the second one is #202's fix showing through. The
    # converged model is applied first (revision 1), *then* `generate(slug, "brief")` absorbs the
    # assessment's reasoning as a revision of its own (revision 2) — the same two steps every other
    # surface takes to produce a brief. It used to be one apply, because the assessment was reasoned
    # before anything was written, which is exactly what made a failure there cost all eight turns.
    # Nothing documents a finished discovery as revision 1; the number is provenance, not a contract.
    assert [m.current_revision for m in SessionService().list_sessions()] == [1 if argv_tail else 2]


def test_stopping_early_keeps_the_turns_it_paid_for(monkeypatch, capsys):
    """Stopping is not a reason to lose what you already bought (#202).

    `q` at the first question used to discard the drafted turn and leave the session at revision 0 —
    the same loss as a failed turn wearing a friendlier word, since the model is what this loop
    carries. One `q` at turn 5 threw away four billed calls and every answer typed into them.

    What it now lands is exactly what `--once` lands: revision 1, questions still open, and
    `requivo answer` named. The two entry points leave the same shape of session rather than two,
    which is the real argument for the change — not kindness, consistency.

    Re-running `discover` on that request is then refused by invariant 13's gate, and that is the
    accepted cost: the refusal already names both ways on (refine with `answer`, or another slug), so
    it is a signpost rather than a dead end.

    The claimed session is still *printed*, for the reason the old version of this test gave: a
    session directory appearing with no line accounting for it is a directory nobody deletes.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")
    fake = FakeClient(_ASKING_REPLY)

    printed = _run_app(["discover", _REQUEST], client=fake)

    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], (
        "the stop discarded the turn the user had already paid for"
    )
    assert len(fake.calls) == 1, (
        "the loop kept reasoning after the user stopped, or bought a decision brief nobody asked for"
    )
    assert "Stopped." in printed
    assert sessions[0].slug in printed, "the claimed session is on disk and nothing said so"
    assert "requivo answer" in printed, "kept the work and did not say how to continue it"


def test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does():
    """The golden harness's multi-turn capture drives `draft_turn` off a scripted answer sheet rather
    than a TTY, and it is only a measurement of this loop for as long as it hands the seam the same
    bytes (#137). The answer block is the one thing it has to reproduce and the one thing that can
    silently drift, because a differently-shaped `Client answers:` body still reasons and still
    produces a plausible model — the capture would just be of a shape no user ever meets.

    So the comparison is behavioural, not a shared constant: this drives `converse()` with a known
    answer and asserts the harness's `answers_for_turn` builds character-for-character what the loop
    passed to `draft_turn`. Either side changing its wording goes red here.
    """
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
    """Let the real `draft_turn` run, then raise `exc` on the `nth` call. Patched at the service
    rather than in the transport because what these pin is `_cmd_discover`'s handling of a failed
    turn, not how the SDK's error becomes an `EngineError` — `tests/test_provider.py` owns that."""
    real = DiscoveryService.draft_turn
    calls = {"n": 0}

    def stub(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == nth:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(DiscoveryService, "draft_turn", stub)


def test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry(monkeypatch, capsys):
    """The single most expensive failure a real user could hit (#202).

    The interactive loop drafts up to eight paid turns entirely in memory, then makes a ninth paid
    call for the decision brief — and that ninth call used to come *before* the one write. An
    `EngineError` on it discarded all eight turns, every answer the user had typed, and left the
    session at revision 0 while printing a transport message that named neither the session nor a way
    back. Retrying meant restarting the conversation at full price.

    The write now comes first, so the failure costs one call instead of nine and the remedy is a verb
    the user can actually run. The assertion that matters is the *revision*: a test checking only the
    error message would have been green on the defect.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(EngineError("API unavailable")))
    fake = FakeClient(_ENGINE_REPLY)

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


def test_a_failed_draft_turn_persists_the_turns_that_succeeded(monkeypatch, capsys):
    """The same loss, one call earlier: a transient failure *mid-loop* rather than on the assessment.

    Turn 3 raising took turns 1 and 2 with it. The model is what this loop carries, so the last turn
    that succeeded already holds every answer given so far — keeping it costs nothing and turns a
    restart-from-scratch into a `requivo answer`, which works from any revision >= 1.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable"))
    fake = FakeClient(_ASKING_REPLY)

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
    """The one case with genuinely nothing to save, and it must not invent a revision out of it.

    `_rescue_drafted` persists the last model that succeeded; on turn 1 there is none. The session
    stays where `claim_session` left it and the remedy is `discover` again, not `answer` — pointing
    at `answer` here would name a verb that has no model to refine.
    """
    _at_a_terminal(monkeypatch)
    _fail_draft_turn_on(monkeypatch, 1, EngineError("API unavailable"))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ASKING_REPLY))

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [0]
    err = capsys.readouterr().err
    assert sessions[0].slug in err
    assert "requivo answer" not in err, "named a verb with no model to refine"


def test_an_interrupt_inside_a_draft_turn_is_not_a_traceback(monkeypatch, capsys):
    """`converse`'s existing catch wraps the `input()` loop, so a Ctrl-C landing *inside* the provider
    call — the several-second window where it is most likely to land — went past it. It is not a
    `RequivoError` either, so `app()` let it out as a raw traceback with the claimed session unnamed
    and the drafted turn lost.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, KeyboardInterrupt())

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ASKING_REPLY))

    assert exit_.value.code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "the interrupt discarded a paid turn"
    err = capsys.readouterr().err
    assert "Interrupted." in err and sessions[0].slug in err


def test_an_interrupt_during_the_brief_reports_the_saved_session(monkeypatch, capsys):
    """#320. #202's changelog promised that a Ctrl-C inside a provider call is no longer a traceback,
    and delivered it only for `draft_turn`.

    The decision-brief call was wrapped in `except RequivoError`, which cannot catch a
    `KeyboardInterrupt` — so the one remaining multi-second call in the verb, the very call #202
    moved *because* it is the expensive one to land on, was still a raw traceback. The claim was in
    the changelog and the guard was not in the code.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ENGINE_REPLY))

    assert exit_.value.code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "the interrupt discarded the drafted turn"
    err = capsys.readouterr().err
    assert f"requivo brief {sessions[0].slug}" in err
    assert "interrupted" in err.lower(), (
        f"a KeyboardInterrupt stringifies to '', so the message trailed off into nothing: {err!r}"
    )


def test_a_rescue_that_cannot_save_says_so_and_still_names_the_original_failure(monkeypatch, capsys):
    """#320. The rescue's own save was unguarded, in the code path whose entire job is keeping the
    work.

    `finalize_discovery` re-runs the revision-zero gate and then writes; either can fail. Unguarded,
    that exception propagated *before* the "turns saved" lines ran — so the user was shown whatever
    the save raised instead of the provider failure that actually stopped them, and was told nothing
    about whether their turns survived. A rescue that fails silently about its own failure is worse
    than no rescue.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable (529)."))
    monkeypatch.setattr(DiscoveryService, "finalize_discovery",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            EngineError("the disk is full")))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ASKING_REPLY))

    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert "could NOT be saved" in err, "the failed save was reported as a success"
    assert "the disk is full" in err, "the save's own failure was swallowed"
    assert "529" in err, (
        f"the original provider failure was masked by the save's, so the user cannot tell what "
        f"stopped the run: {err!r}"
    )


# ── #206: the quick (`--once`/non-tty) path had no rescue of its own ──────────────────────────────


def test_an_interrupt_in_the_once_path_names_the_claimed_session_and_the_retry(monkeypatch, capsys):
    """`--once`/non-tty `discover` claims a session and makes exactly one paid call, through
    `disco.start()` -- and until now nothing in `_cmd_discover` wrapped that call. A Ctrl-C landing
    inside it reached `app()` as a bare `KeyboardInterrupt` with the claimed session unnamed, the same
    trap `_rescue_drafted` already closed for the interactive loop (#202) and never closed here.

    `start()` itself is stubbed to raise once the session it claims is on disk, so what's under test
    is `_cmd_discover`'s own handling of the call raising -- not how the SDK's interrupt becomes one.
    """
    monkeypatch.setattr(DiscoveryService, "start",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, "--once"], client=FakeClient())

    assert exit_.value.code == 130, "a Ctrl-C should exit 130, not the generic 1 (#206)"
    sessions = SessionService().list_sessions()
    assert len(sessions) == 1, "the interrupted call left no claimed session to retry"
    assert sessions[0].current_revision == 0, "nothing was drafted, so revision 0 is the truth"
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert sessions[0].slug in err
    assert "re-run `requivo discover`" in err


def test_an_interrupt_before_a_session_is_claimed_names_no_slug(monkeypatch, capsys):
    """The trap on the other side of the fix above (#206): an abort point that has genuinely claimed
    nothing must not invent a session to name -- a message naming a slug that does not exist is worse
    than the traceback it replaces. `is_file_argument` runs before any session is claimed on every
    `discover` call, so patching it to interrupt reproduces the earliest realistic abort point."""
    monkeypatch.setattr("requivo.cli.is_file_argument",
                        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, "--once"], client=FakeClient())

    assert exit_.value.code == 130
    assert SessionService().list_sessions() == [], (
        "a session was claimed before the request was even read"
    )
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Interrupted." in err
    assert "Saved" not in err, f"named a session that does not exist: {err!r}"


def test_a_top_level_interrupt_on_an_existing_session_exits_130_with_no_traceback(monkeypatch, capsys):
    """Every command other than `discover` reaches the provider with no claim of its own to make --
    the session it operates on already existed before this run started -- so `app()`'s own top-level
    handler is the whole fix for it, with nothing discover-specific to say (#206)."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ENGINE_REPLY))
    slug = SessionService().list_sessions()[0].slug
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["brief", slug])

    assert exit_.value.code == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Interrupted." in err


# ── found in review of #206: `except (EngineError, ...)` is narrower than the RequivoError family ──


def test_a_provider_output_failure_in_the_once_path_also_names_the_claimed_session(monkeypatch, capsys):
    """`ProviderOutputError` (raised when the provider's JSON retry loop gives up) is a `RequivoError`
    sibling of `EngineError`, not a subclass of it -- so the quick path's `except (EngineError,
    KeyboardInterrupt)` let it straight through to `app()`'s generic handler with the claimed session
    unnamed, contradicting #206's own changelog entry ("if that call was interrupted or the provider
    failed"). Found in review of this diff, not in the issue as filed."""
    monkeypatch.setattr(DiscoveryService, "start",
                        lambda self, *a, **kw: (_ for _ in ()).throw(ProviderOutputError("bad json")))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, "--once"], client=FakeClient())

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert len(sessions) == 1, "the failed call left no claimed session to retry"
    err = capsys.readouterr().err
    assert sessions[0].slug in err
    assert "re-run `requivo discover`" in err


def test_a_provider_output_failure_mid_turn_also_names_the_claimed_session(monkeypatch, capsys):
    """The same gap, one layer in: `converse()`'s own turn-draft catch is the identical
    `except (EngineError, KeyboardInterrupt)`, pre-dating #206 and not part of its diff, but sharing
    the file and the exact class the two counted findings above are about -- swept once the first
    instance turned up. A `ProviderOutputError` on turn 2 used to reach `app()` as a bare
    `RequivoError` with up to `MAX_TURNS - 1` paid, un-rescued turns behind it."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, ProviderOutputError("bad json"))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ASKING_REPLY))

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [1], "turn 1 was drafted, paid for, and dropped"
    err = capsys.readouterr().err
    assert sessions[0].slug in err and "requivo answer" in err


# ── #540/#541: `run` — one verb over discover's loop and answer's apply path ───────────────────────


def test_run_with_a_request_makes_the_same_call_count_as_discover():
    """#540's acceptance criterion, as call counts: `run "…"` reaches `_cmd_discover` for a
    request/path shape rather than reimplementing it, so the two pay identically."""
    fake_discover = FakeClient(_ENGINE_REPLY)
    _run_app(["discover", _REQUEST, "--once"], client=fake_discover)
    assert len(fake_discover.calls) == 1

    fake_run = FakeClient(_ENGINE_REPLY)
    _run_app(["run", _REQUEST + ", via run", "--once"], client=fake_run)
    assert len(fake_run.calls) == 1


def test_run_on_a_refined_session_resumes_through_answer_never_rediscovers(monkeypatch):
    """#540: an existing session's slug resumes it through `answer`, never a second discovery. If
    `run` mis-routed this slug back into `discover`, invariant 13's gate would refuse it for **zero**
    paid calls (the session is already past revision 0) -- so the call count below is also the proof
    the right path was taken, not only that it succeeded."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ASKING_REPLY))
    slug = SessionService().list_sessions()[0].slug
    assert SessionService().list_sessions()[0].current_revision == 1

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    fake = FakeClient(_ENGINE_REPLY)   # converges, so the resume loop stops after this one turn
    _run_app(["run", slug], client=fake)

    assert len(fake.calls) == 1, "resuming cost more or less than the one answer call"
    assert SessionService().list_sessions()[0].current_revision == 2


def test_run_on_a_session_with_no_model_refuses_before_any_paid_call():
    """#540: resuming a session that was claimed but never discovered (revision 0, no model yet)
    refuses cleanly with zero paid calls -- the shape
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying` pins one gate over."""
    SessionService().create_session(_REQUEST, slug="claimed-only")
    fake = FakeClient()

    with pytest.raises(SystemExit) as exit_:
        app(["run", "claimed-only"], client=fake)

    assert exit_.value.code == 1
    assert fake.calls == []


def test_run_with_no_argument_and_one_session_resumes_it(monkeypatch):
    """#540/#541: no argument, exactly one session -> resume it, without creating a second one."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ASKING_REPLY))
    slug = SessionService().list_sessions()[0].slug

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")   # stop immediately
    printed = _run_app(["run"], client=FakeClient())

    assert slug in printed
    assert len(SessionService().list_sessions()) == 1, "a no-argument run created a second session"


def test_run_with_no_argument_and_no_session_prompts_for_a_request():
    """#540: no argument, no session at all -> ask for a request the same way `discover` reads one.
    stdin is never a tty under pytest, so the quick path takes over and this costs one call."""
    import builtins as _builtins

    real_input = _builtins.input
    _builtins.input = lambda _prompt="": _REQUEST
    try:
        fake = FakeClient(_ENGINE_REPLY)
        _run_app(["run"], client=fake)
    finally:
        _builtins.input = real_input

    assert len(fake.calls) == 1
    sessions = SessionService().list_sessions()
    assert len(sessions) == 1
    assert sessions[0].current_revision == 1


def test_run_with_no_argument_and_several_sessions_lists_them_and_resumes_the_default(monkeypatch):
    """#541: several -> every candidate is listed, the default marked, before the loop's own
    (potentially paid) prompt ever runs -- and the default is the most recently *written* one."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ASKING_REPLY))
    older = SessionService().list_sessions()[0].slug
    _run_app(["discover", _REQUEST + ", again", "--once"], client=FakeClient(_ASKING_REPLY))
    newer = next(m.slug for m in SessionService().list_sessions() if m.slug != older)

    # A deterministic tie-break: force `newer`'s `updated_at` strictly ahead of `older`'s, rather
    # than trusting two real-time writes in the same test to land in different seconds.
    p = store.canonical_dir(newer) / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["updated_at"] = "2999-01-01T00:00:00Z"
    p.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")
    printed = _run_app(["run"], client=FakeClient())

    assert older in printed and newer in printed, "the listing must name every candidate"
    default_line = next(ln for ln in printed.splitlines() if newer in ln)
    assert "→" in default_line, "the default was not marked"
    assert len(SessionService().list_sessions()) == 2, "a no-argument run created a third session"


def _converged_model() -> EngineOutput:
    """A session with no open questions -- `run`'s resume loop stops after `render_turn` alone, so a
    test using this needs no `input()` patch and no provider call."""
    return EngineOutput.model_validate(
        {"model": full_slots(problem=slot(80, "explicit", "high")), "questions": [],
         "summary": {"objective": "o"}})


def test_run_with_no_argument_is_not_hijacked_by_a_same_named_file(monkeypatch, tmp_path):
    """Found in review: the resolver's own slug used to be re-run through `is_file_argument`, so a
    file in the cwd sharing a resumed session's name silently started a paid discovery on the
    file's *contents* instead of resuming it. Zero provider calls is the proof the discovery path
    was never taken."""
    monkeypatch.chdir(tmp_path)
    SessionService().create_session("a request about sample", slug="sample")
    store.save_revision("sample", _converged_model())
    (tmp_path / "sample").write_text("unrelated file contents", encoding="utf-8")

    fake = FakeClient()
    _run_app(["run"], client=fake)

    assert fake.calls == [], "the resolved slug was re-detected as a file and paid for a discovery"
    assert len(SessionService().list_sessions()) == 1, "a second session was created from the file"


def test_run_refuses_once_and_context_when_resuming(capsys):
    """#540, found in review: `--once`/`--context` describe a *new* discovery and used to be
    silently ignored on a resume. Refused instead, before any provider call -- one test covers
    both flags, since they share the identical refusal."""
    SessionService().create_session("a request about resumed", slug="resumed")
    store.save_revision("resumed", EngineOutput.model_validate(
        {"model": full_slots(problem=slot(80, "explicit", "high")),
         "questions": [{"q": "Who approves?", "slot": "permissions", "why": "u"}],
         "summary": {"objective": "o"}}))
    fake = FakeClient()

    with pytest.raises(SystemExit) as exit_:
        app(["run", "resumed", "--once"], client=fake)
    assert exit_.value.code == 1
    assert "new discovery" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exit_:
        app(["run", "resumed", "--context", "b2b-platform"], client=fake)
    assert exit_.value.code == 1
    assert "new discovery" in capsys.readouterr().err

    assert fake.calls == [], "a refused resume still paid for a provider call"


def test_a_second_interrupt_during_the_rescues_own_save_exits_130(monkeypatch, capsys):
    """`_rescue_drafted`'s own save is guarded against `RequivoError`/`OSError` (#320) but not
    against a second `KeyboardInterrupt` landing on the save itself -- which used to propagate bare
    and silent (no message at all) before reaching `app()`'s generic handler. Found in review of
    this diff: the function this diff rewrote to promise "every abort path ... must name the
    session" did not hold for this one abort path inside it."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
    _fail_draft_turn_on(monkeypatch, 2, EngineError("API unavailable"))
    monkeypatch.setattr(DiscoveryService, "finalize_discovery",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST], client=FakeClient(_ASKING_REPLY))

    assert exit_.value.code == 130, "a second Ctrl-C should exit 130 like every other interrupt here"
    err = capsys.readouterr().err
    assert "could NOT be saved" in err
    assert "interrupted" in err.lower(), (
        f"a KeyboardInterrupt stringifies to '', so the message trailed off into nothing: {err!r}"
    )
