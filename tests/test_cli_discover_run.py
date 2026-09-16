"""Interrupt handling across `discover`'s entry points (#206) and `run` — one verb over discover's
loop and answer's apply path (#540/#541).

Split out of `test_cli_interactive.py` by #555, once that file outgrew one module; the sibling keeps
the loop itself and the #202 rescue-after-a-paid-turn guarantee, which is where
`_at_a_terminal`/`_fail_draft_turn_on` are documented in full -- duplicated here rather than imported,
per this suite's own convention of keeping test-module helpers local (`tests/_fakes.py` makes the
argument).
"""
from __future__ import annotations

import builtins
import json

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, _run_app, full_slots, slot

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.errors import ProviderOutputError
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """Every test in this module writes sessions into an isolated temp workspace, never the real
    repo -- `workspace` (conftest.py) does the pointing; autouse means no test here has to ask."""


_REQUEST = "a leave approval system, discovered twice"
_ASKING_REPLY = json.dumps({
    "model": full_slots(problem=slot(80, "explicit", "high")),
    "questions": [{"q": "Who approves?", "slot": "permissions", "why": "approval routing drives it"}],
    "summary": {"objective": "A leave approval system"},
})


def _at_a_terminal(monkeypatch) -> None:
    """`_cmd_discover` picks its branch on `--once` *or* the absence of a TTY, and under pytest stdin
    is never one. Patched for both legs of the tests below, so the flag is the only difference between
    them -- which is what "the two entry points refuse identically" has to mean."""
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def _fail_draft_turn_on(monkeypatch, nth: int, exc: BaseException) -> None:
    """Let the real `draft_turn` run, then raise `exc` on the `nth` call. Patched at the service
    rather than in the transport because what these pin is `_cmd_discover`'s handling of a failed
    turn, not how the SDK's error becomes an `EngineError` -- `tests/test_provider.py` owns that."""
    real = DiscoveryService.draft_turn
    calls = {"n": 0}

    def stub(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == nth:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(DiscoveryService, "draft_turn", stub)


def test_an_interrupt_in_the_once_path_names_the_claimed_session_and_the_retry(monkeypatch, capsys):
    """`--once`/non-tty `discover` claims a session and makes exactly one paid call, through
    `disco.start()` -- and until now nothing in `_cmd_discover` wrapped that call. A Ctrl-C landing
    inside it reached `app()` as a bare `KeyboardInterrupt` with the claimed session unnamed, the same
    trap `_rescue_drafted` already closed for the interactive loop (#202) and never closed here."""
    monkeypatch.setattr(DiscoveryService, "start",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY))

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
        app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY))

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
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    slug = SessionService().list_sessions()[0].slug
    monkeypatch.setattr(DiscoveryService, "generate",
                        lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_:
        app(["brief", slug])

    assert exit_.value.code == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Interrupted." in err


@pytest.mark.parametrize("path", ["once", "interactive"], ids=["#206-once-path", "#206-mid-turn"])
def test_a_provider_output_failure_mid_turn_also_names_the_claimed_session(monkeypatch, capsys, path):
    """`ProviderOutputError` (raised when the provider's JSON retry loop gives up) is a
    `RequivoError` sibling of `EngineError`, not a subclass of it -- so two different
    `except (EngineError, KeyboardInterrupt)` catches let it through with the claimed session
    unnamed: the quick (`--once`) path's own catch, and `converse()`'s turn-draft catch one layer in.
    Swept into one parametrized test once the first instance turned up in review of #206 (#555)."""
    if path == "once":
        monkeypatch.setattr(DiscoveryService, "start",
                            lambda self, *a, **kw: (_ for _ in ()).throw(ProviderOutputError("bad json")))
        argv = ["discover", _REQUEST, "--once"]
        client = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY)
    else:
        _at_a_terminal(monkeypatch)
        monkeypatch.setattr(builtins, "input", lambda _prompt="": "the line manager approves")
        _fail_draft_turn_on(monkeypatch, 2, ProviderOutputError("bad json"))
        argv = ["discover", _REQUEST]
        client = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(argv, client=client)

    assert exit_.value.code == 1
    sessions = SessionService().list_sessions()
    assert len(sessions) == 1, "the failed call left no claimed session to retry"
    err = capsys.readouterr().err
    assert sessions[0].slug in err
    if path == "once":
        assert "re-run `requivo discover`" in err
    else:
        assert [m.current_revision for m in sessions] == [1], "turn 1 was drafted, paid for, and dropped"
        assert "requivo answer" in err


# ── #540/#541: `run` — one verb over discover's loop and answer's apply path ───────────────────────


def test_run_with_a_request_makes_the_same_call_count_as_discover():
    """#540's acceptance criterion, as call counts: `run "…"` reaches `_cmd_discover` for a
    request/path shape rather than reimplementing it, so the two pay identically. Three calls each
    since #601 -- the perimeter router, the grounding judgment, then the turn -- and *identically
    three* is the assertion."""
    fake_discover = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    _run_app(["discover", _REQUEST, "--once"], client=fake_discover)
    assert len(fake_discover.calls) == 3

    fake_run = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    _run_app(["run", _REQUEST + ", via run", "--once"], client=fake_run)
    assert len(fake_run.calls) == len(fake_discover.calls)


def test_run_on_a_refined_session_resumes_through_answer_never_rediscovers(monkeypatch):
    """#540: an existing session's slug resumes it through `answer`, never a second discovery. If
    `run` mis-routed this slug back into `discover`, invariant 13's gate would refuse it for **zero**
    paid calls (the session is already past revision 0) -- so the call count below is also the proof
    the right path was taken, not only that it succeeded."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))
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
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))
    slug = SessionService().list_sessions()[0].slug

    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")   # stop immediately
    printed = _run_app(["run"], client=FakeClient())

    assert slug in printed
    assert len(SessionService().list_sessions()) == 1, "a no-argument run created a second session"


def test_run_with_no_argument_and_no_session_prompts_for_a_request():
    """#540: no argument, no session at all -> ask for a request the same way `discover` reads one.
    stdin is never a tty under pytest, so the quick path takes over; #601's router adds a third call
    ahead of the two #593 already cost here."""
    import builtins as _builtins

    real_input = _builtins.input
    _builtins.input = lambda _prompt="": _REQUEST
    try:
        fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
        _run_app(["run"], client=fake)
    finally:
        _builtins.input = real_input

    assert len(fake.calls) == 3, "the perimeter route, the grounding judgment, and the turn"
    sessions = SessionService().list_sessions()
    assert len(sessions) == 1
    assert sessions[0].current_revision == 1


def test_run_with_no_argument_and_several_sessions_lists_them_and_resumes_the_default(monkeypatch):
    """#541: several -> every candidate is listed, the default marked, before the loop's own
    (potentially paid) prompt ever runs -- and the default is the most recently *written* one."""
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))
    older = SessionService().list_sessions()[0].slug
    _run_app(["discover", _REQUEST + ", again", "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))
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
        app(["discover", _REQUEST], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ASKING_REPLY))

    assert exit_.value.code == 130, "a second Ctrl-C should exit 130 like every other interrupt here"
    err = capsys.readouterr().err
    assert "could NOT be saved" in err
    assert "interrupted" in err.lower(), (
        f"a KeyboardInterrupt stringifies to '', so the message trailed off into nothing: {err!r}"
    )
