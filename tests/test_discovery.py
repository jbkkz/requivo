"""`DiscoveryService`, the one provider-backed orchestration: its logging seams (#435), the provider and
storage seams (#424), the first-discovery guard (#209), and the input ceiling (#255)."""
from __future__ import annotations

import io
import json
import logging
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, StubProvider, full_model, out, seeded, slot

from conftest import FakeProvider, RacingClient
from requivo.core import persistence as store
from requivo.core.contracts import MAX_INPUT_CHARS
from requivo.core.errors import InputTooLargeError, InvalidSlugError, RevisionConflictError, SessionLockedError
from requivo.core.persistence import ensure_store_dir
from requivo.providers.errors import EngineError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService, _discovery_guard_path, fcntl
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

REQUEST = "a leave approval system"


def _disco(provider=None, sessions=None):
    sessions = sessions or SessionService()
    return sessions, DiscoveryService(provider=provider or StubProvider(), sessions=sessions)


def _conflict(sessions: SessionService, slug: str) -> None:
    """`_plan`'s WARNING-level "model apply refused" line, the seam most likely to leak."""
    with pytest.raises(RevisionConflictError):
        sessions.update_model(slug, out({"problem": slot(1, "empty", "low")}).model_dump_json(), expected_revision=99)


# ── the logging seams (#435) ────────────────────────────────────────────────────


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=logging.DEBUG) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= level]


@pytest.fixture
def _attached():
    """Attach a collecting handler to a named logger for one test, restoring it afterwards."""
    restores = []

    def _attach(name: str) -> _CollectingHandler:
        logger = logging.getLogger(name)
        restores.append((logger, list(logger.handlers), logger.level))
        handler = _CollectingHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return handler

    yield _attach
    for logger, handlers, level in restores:
        logger.handlers, logger.level = handlers, level


def _artifact_saved(sessions):
    ArtifactService(repo=sessions.repo).save(seeded(sessions, REQUEST), "prd", "# PRD", source_revision=1)


def _failed_call(sessions):
    with pytest.raises(EngineError):
        DiscoveryService(provider=StubProvider(analyze_error=EngineError("boom")), sessions=sessions).start(REQUEST)


@pytest.mark.parametrize("logger,act,needles,level", [
    ("sessions", lambda s: s.create_session(REQUEST), ["session created"], logging.DEBUG),
    ("sessions", lambda s: seeded(s, REQUEST), ["model applied", "revision=1"], logging.DEBUG),
    ("sessions", lambda s: _conflict(s, s.create_session(REQUEST).slug), ["conflict"], logging.WARNING),
    ("artifacts", _artifact_saved, ["artifact saved", "stale=False"], logging.DEBUG),
    ("discovery", _failed_call, ["provider call failed", "operation=analyze"], logging.WARNING),
], ids=["created", "applied", "conflict", "artifact saved", "provider failed"])
def test_each_service_seam_logs_its_line(_attached, logger, act, needles, level):
    handler = _attached(f"requivo.services.{logger}")
    act(SessionService())
    assert any(all(n in m for n in needles) for m in handler.messages(level)), handler.messages()


def test_a_rescope_that_mints_a_revision_is_logged_too(_attached):
    """`rescope()` is `sessions.py`'s second `save_revision(...)` call site (#168)."""
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    slug = seeded(sessions, REQUEST)
    handler.records.clear()
    sessions.rescope(slug, None)              # a no-op re-scope: create_session already recorded None
    sessions.rescope(slug, ["b2b-platform"])  # a genuine change, so a revision is minted
    assert any("revision" in m and "2" in m for m in handler.messages()), handler.messages()


def test_a_successful_provider_call_logs_started_and_finished(_attached):
    handler = _attached("requivo.services.discovery")
    _disco()[1].start(REQUEST)
    for phase in ("started", "finished"):
        assert any(f"provider call {phase}" in m and "operation=analyze" in m for m in handler.messages()), handler.messages()


@pytest.mark.parametrize("strip_null_handler", [False, True], ids=["default", "null handler stripped (must-fire)"])
def test_default_run_leaves_the_conflict_refused_warning_off_every_stream(strip_null_handler):
    """Must-fire control: with `requivo/__init__.py`'s NullHandler stripped, `logging.lastResort` leaks the WARNING."""
    root, requivo_logger = logging.getLogger(), logging.getLogger("requivo")
    saved = (list(root.handlers), root.level, list(requivo_logger.handlers), requivo_logger.level, requivo_logger.propagate)
    root.handlers = []
    if strip_null_handler:
        requivo_logger.handlers = []
    try:
        sessions = SessionService()
        slug = sessions.create_session(REQUEST).slug
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            _conflict(sessions, slug)
    finally:
        root.handlers, root.level, requivo_logger.handlers, requivo_logger.level, requivo_logger.propagate = saved
    if strip_null_handler:
        assert "conflict" in buf_err.getvalue(), f"the default case is not exercising the mechanism: {buf_err.getvalue()!r}"
    else:
        assert buf_out.getvalue() == "" and buf_err.getvalue() == "", (buf_out.getvalue(), buf_err.getvalue())


# ── the provider seam (#424) and invariant 6 ────────────────────────────────────

# Every member `ReasoningProvider` declares and nothing more: no `name`.
_NamelessProvider = type("_NamelessProvider", (), {m: (lambda self, *a, **k: None)
                                                    for m in ("analyze", "generate", "model_name", "provenance")})


def test_discovery_runs_on_a_provider_that_is_not_anthropic():
    slug = DiscoveryService(FakeProvider()).start("A leave approval system.", slug="fake-prov")
    meta = SessionService().meta(slug)
    # Nothing hard-codes "anthropic": the session and its revision are stamped by the provider itself.
    assert meta.provider == "fake" and meta.model_name == "fake-model-1"
    assert [(r.provider, r.model_name) for r in meta.revisions] == [("fake", "fake-model-1")]


def test_the_provider_protocol_declares_every_member_the_orchestration_reads():
    """`provider.name` is read on the first discovery, so it is part of the contract."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.providers.base import ReasoningProvider

    assert isinstance(FakeProvider(), ReasoningProvider)
    assert isinstance(AnthropicProvider.__new__(AnthropicProvider), ReasoningProvider)  # no API key needed
    assert not isinstance(_NamelessProvider(), ReasoningProvider)
    with pytest.raises(AttributeError, match="name"):
        DiscoveryService(_NamelessProvider()).start("A leave approval system.", slug="nameless")


def test_a_revision_records_the_prompt_it_was_reasoned_against():
    """Invariant 6: provenance is real or absent (#286); it follows the card selection (#13)."""
    from requivo.providers.anthropic import prompt_version

    slug = DiscoveryService(client=RacingClient(json.dumps(full_model()), lambda: None)).start(
        "A leave approval system.", slug="prov")
    rec = SessionService().meta(slug).revisions[-1]
    assert rec.provider == "anthropic" and rec.model_name
    assert rec.prompt_version and rec.prompt_version.startswith("sha256:")
    assert prompt_version("analyze") != prompt_version("analyze", only=["b2b-platform"])


# ── a generation or a turn holds the revision it read (invariant 2) ─────────────

_BRIEF_REPLY = json.dumps({"complexity": "medium", "problem": "P", "solution": "S", "risks": [], "next_steps": []})


@pytest.mark.parametrize("op,reply,moved", [
    (lambda d: d.generate("s", "brief"), _BRIEF_REPLY, "business_rules"),
    (lambda d: d.answer("s", "here are my answers"), json.dumps(full_model()), "risks"),
], ids=["generate", "answer"])
def test_a_generation_or_a_turn_holds_the_revision_it_read(op, reply, moved):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", full_model())          # revision 1 -- what the provider call will read

    def concurrent():
        svc.update_model("s", full_model(**{moved: slot(90, "explicit", "high", "landed mid-flight")}))

    with pytest.raises(RevisionConflictError):
        op(DiscoveryService(client=RacingClient(reply, concurrent)))
    # The slot that landed mid-flight is still there: the losing apply did not write over it.
    assert svc.load_model("s").model[moved].value == "landed mid-flight"


def test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it():
    """The full journey the tri-state exists for: discovery -> assessment -> an ordinary answer (invariant 10)."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", {**full_model(), "decisions": [{"decision": "Managers approve in-app", "derived_from": ["permissions"]}]})
    art.save("s", "prd", "# PRD\n", source_revision=1)
    reply = full_model(workflow=slot(90, "explicit", "high", "request -> approve"))
    DiscoveryService(client=RacingClient(json.dumps(reply), lambda: None)).answer("s", "in-app")
    after = svc.load_model("s")
    assert [d.decision for d in after.decisions] == ["Managers approve in-app"]
    assert after.model["workflow"].value == "request -> approve"   # the facts did move
    assert art.list("s")["prd"]["stale"] is True                  # ...and that alone marks the PRD stale


# ── the first-discovery guard (#209) ────────────────────────────────────────────

_POSIX_ONLY = pytest.mark.skipif(fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows equivalent "
                                 "here, and the msvcrt branch takes the same non-blocking path. REASONED, NOT OBSERVED on Windows -- see #209.")


@contextmanager
def _guard_held(slug: str):
    """Someone else holds the first-discovery guard for `slug`."""
    guard_path = _discovery_guard_path(slug, store.Store(store.workspace_root()))
    ensure_store_dir(guard_path.parent)
    fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@_POSIX_ONLY
@pytest.mark.parametrize("entry", ["run_discovery", "start"])
def test_a_concurrent_first_discovery_is_refused_before_any_provider_call(entry):
    """Two concurrent first-discovery requests must not both pay, through either door (#209)."""
    sessions, disco = _disco()
    if entry == "start":
        slug = disco.claim_session(REQUEST, cards=None, slug=None).slug
        assert slug.startswith(sessions.slug_hint(REQUEST))
    else:
        slug = sessions.create_session(REQUEST).slug
    with _guard_held(slug), pytest.raises(SessionLockedError) as exc_info:
        disco.start(REQUEST, slug=slug) if entry == "start" else disco.run_discovery(slug, surface="test")
    assert exc_info.value.code == "session_locked" and slug in str(exc_info.value)
    assert disco._provider.calls == 0 and sessions.repo.read_meta(slug).current_revision == 0


def test_run_discovery_still_succeeds_once_the_guard_is_free():
    """Must-fire control: a guard that refused everything would also pass the test above."""
    sessions, disco = _disco()
    slug = sessions.create_session(REQUEST).slug
    disco.run_discovery(slug, surface="test")
    assert disco._provider.calls == 1 and sessions.repo.read_meta(slug).current_revision == 1


def test_a_late_caller_with_a_stale_outer_check_still_pays_nothing(monkeypatch):
    """The revision is re-checked after the guard is won, not only against a snapshot that can go stale (#209)."""
    from requivo.services.sessions import SessionSnapshot

    sessions, disco = _disco()
    slug = sessions.create_session(REQUEST).slug
    real_snapshot = sessions.snapshot
    disco.run_discovery(slug, surface="test")
    stale = SessionSnapshot(slug=slug, revision=0, model=None, request=REQUEST, context_cards=None)
    seen = {"n": 0}

    def fake_snapshot(s):
        seen["n"] += 1
        return stale if seen["n"] == 1 else real_snapshot(s)  # 1st (outer) call is stale, rest real

    monkeypatch.setattr(sessions, "snapshot", fake_snapshot)
    late = StubProvider()
    with pytest.raises(RevisionConflictError):
        DiscoveryService(provider=late, sessions=sessions).run_discovery(slug, surface="test")
    assert late.calls == 0 and sessions.repo.read_meta(slug).current_revision == 1


def test_a_late_caller_of_start_with_a_stale_outer_check_still_pays_nothing(monkeypatch):
    """The same race, one entry point over."""
    sessions, disco = _disco()
    slug = sessions.slug_hint(REQUEST)
    disco.start(REQUEST, slug=slug, surface="test")
    real_create_session = sessions.create_session
    monkeypatch.setattr(sessions, "create_session",
                        lambda *a, **k: real_create_session(*a, **k).model_copy(update={"current_revision": 0}))
    late = StubProvider()
    with pytest.raises(RevisionConflictError):
        DiscoveryService(provider=late, sessions=sessions).start(REQUEST, slug=slug, surface="test")
    assert late.calls == 0 and sessions.repo.read_meta(slug).current_revision == 1


@pytest.mark.skipif(fcntl is None, reason="a directory literally named 'con' cannot exist on Windows, so a session at a "
                    "reserved slug is a state only a platform that never enforced the restriction can reach. REASONED, NOT OBSERVED (#372).")
def test_a_reserved_slug_the_sweep_one_commit_later_missed_reaches_the_discovery_guard():
    """#390: a two-commit join no single diff showed."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "provider": None, "model_name": None, "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")
    # The siblings #372 swept already reach it; the join is visible in one fixture.
    assert store.session_exists("con") is True and store.canonical_dir("con") == d
    assert store.lock_path("con").name == "con.lock"
    assert _discovery_guard_path("con", store.Store(store.workspace_root())) == store.lock_root() / "con.discovering"
    sessions, disco = _disco()
    disco.run_discovery("con", surface="test")
    assert disco._provider.calls == 1 and sessions.repo.read_meta("con").current_revision == 1
    with pytest.raises(InvalidSlugError):  # must-not-fire control, in the same fixture (#221)
        _discovery_guard_path("nul", store.Store(store.workspace_root()))


# ── the input ceiling lives in the service (#255, invariant 3) ──────────────────


@pytest.mark.parametrize("entry", ["start", "create_only", "draft_turn"])
def test_an_oversized_request_is_refused_before_any_provider_call(entry):
    """Invariant 3, first half: refuse, don't truncate (#286); exactly the ceiling still reaches the provider."""
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    with pytest.raises(InputTooLargeError):
        getattr(disco, entry)("x" * (MAX_INPUT_CHARS + 1))
    assert fake.calls == [] and not SessionService().list_sessions()
    getattr(disco, entry)("x" * MAX_INPUT_CHARS)  # must-fire control, same fixture
    assert len(fake.calls) == (0 if entry == "create_only" else 1)
    assert len(SessionService().list_sessions()) == (0 if entry == "draft_turn" else 1)


@pytest.mark.parametrize("entry", ["answer", "draft_turn"])
def test_oversized_answers_are_refused_before_the_paid_turn(entry):
    """`draft_turn` is the interactive loop's own un-persisted entry point."""
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    if entry == "answer":
        slug = disco.start(REQUEST)
        turn = lambda answers: disco.answer(slug, answers)  # noqa: E731
    else:
        model = disco.draft_turn(REQUEST)
        turn = lambda answers: disco.draft_turn(REQUEST, current_model=model, answers=answers)  # noqa: E731
    with pytest.raises(InputTooLargeError):
        turn("y" * (MAX_INPUT_CHARS + 1))
    assert len(fake.calls) == 1                       # refused before the second call was billed
    turn("y" * MAX_INPUT_CHARS)                       # must-fire control, same fixture
    assert len(fake.calls) == 2
