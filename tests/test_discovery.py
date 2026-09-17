"""`DiscoveryService`, the one provider-backed orchestration: its logging seams (#435), the provider and
storage seams (#424), the first-discovery guard (#209), and the input ceiling (#255)."""
from __future__ import annotations

import io
import json
import logging
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, StubProvider, full_model, out, slot

from conftest import FakeProvider, RacingClient
from requivo.core import persistence as store
from requivo.core.contracts import MAX_INPUT_CHARS, Brief
from requivo.core.errors import InputTooLargeError, InvalidSlugError, RevisionConflictError, SessionLockedError
from requivo.core.persistence import ensure_store_dir
from requivo.providers.errors import EngineError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService, _discovery_guard_path, fcntl
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

REQUEST = "a leave approval system"
_BRIEF = Brief(complexity="low", solution="S", decisions=[], challenges=[], opportunities=[])


def _seeded(sessions: SessionService, **slots):
    """A session at revision 1, straight through the service."""
    meta = sessions.create_session(REQUEST)
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high"), **slots}).model_dump_json())
    return meta.slug


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


def test_session_created_is_logged(_attached):
    handler = _attached("requivo.services.sessions")
    SessionService().create_session(REQUEST)
    assert any("session created" in m for m in handler.messages()), handler.messages()


def test_model_applied_is_logged_with_its_revision(_attached):
    handler = _attached("requivo.services.sessions")
    _seeded(SessionService())
    assert any("model applied" in m and "revision=1" in m for m in handler.messages()), handler.messages()


def test_a_write_conflict_is_logged_as_refused(_attached):
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    meta = sessions.create_session(REQUEST)
    with pytest.raises(RevisionConflictError):
        sessions.update_model(meta.slug, out({}).model_dump_json(), expected_revision=99)
    assert any("conflict" in m for m in handler.messages(logging.WARNING)), handler.messages()


def test_a_rescope_that_mints_a_revision_is_logged_too(_attached):
    """`rescope()` is `sessions.py`'s second `save_revision(...)` call site (#168)."""
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    slug = _seeded(sessions)
    handler.records.clear()
    sessions.rescope(slug, None)              # a no-op re-scope: create_session already recorded None
    sessions.rescope(slug, ["b2b-platform"])  # a genuine change, so a revision is minted
    assert any("revision" in m and "2" in m for m in handler.messages()), handler.messages()


def test_artifact_saved_is_logged_with_its_stale_verdict(_attached):
    handler = _attached("requivo.services.artifacts")
    sessions = SessionService()
    slug = _seeded(sessions)
    ArtifactService(repo=sessions.repo).save(slug, "prd", "# PRD", source_revision=1)
    assert any("artifact saved" in m and "stale=False" in m for m in handler.messages()), handler.messages()


def test_a_successful_provider_call_logs_started_and_finished(_attached):
    handler = _attached("requivo.services.discovery")
    DiscoveryService(provider=StubProvider(), sessions=SessionService()).start(REQUEST)
    messages = handler.messages()
    assert any("provider call started" in m and "operation=analyze" in m for m in messages), messages
    assert any("provider call finished" in m and "operation=analyze" in m for m in messages), messages


def test_a_failed_provider_call_logs_a_warning_and_still_raises(_attached):
    handler = _attached("requivo.services.discovery")
    disco = DiscoveryService(provider=StubProvider(analyze_error=EngineError("boom")), sessions=SessionService())
    with pytest.raises(EngineError):
        disco.start(REQUEST)
    assert any("provider call failed" in m and "operation=analyze" in m
               for m in handler.messages(logging.WARNING)), handler.messages()


def _trigger_conflict_refused(sessions: SessionService, meta) -> None:
    """The one seam most likely to leak: `_plan`'s WARNING-level "model apply refused" line."""
    with pytest.raises(RevisionConflictError):
        sessions.update_model(meta.slug, out({"problem": slot(1, "empty", "low")}).model_dump_json(),
                              expected_revision=99)


@contextmanager
def _bare_root_logger(strip_requivo_null_handler: bool = False):
    """The root logger with no handlers and, for the must-fire control, `requivo`'s own NullHandler stripped."""
    root, requivo_logger = logging.getLogger(), logging.getLogger("requivo")
    root_before = (list(root.handlers), root.level)
    requivo_before = (list(requivo_logger.handlers), requivo_logger.level, requivo_logger.propagate)
    root.handlers = []
    if strip_requivo_null_handler:
        requivo_logger.handlers = []
    try:
        yield
    finally:
        root.handlers, root.level = root_before
        requivo_logger.handlers, requivo_logger.level, requivo_logger.propagate = requivo_before


def test_default_run_leaves_the_conflict_refused_warning_off_every_stream():
    with _bare_root_logger():
        sessions = SessionService()
        meta = sessions.create_session(REQUEST)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            _trigger_conflict_refused(sessions, meta)
        assert buf_out.getvalue() == "" and buf_err.getvalue() == "", (buf_out.getvalue(), buf_err.getvalue())


def test_removing_the_null_handler_reproduces_the_leak_the_test_above_guards_against():
    """Must-fire control: with `requivo/__init__.py`'s NullHandler stripped, `logging.lastResort` leaks the WARNING."""
    with _bare_root_logger(strip_requivo_null_handler=True):
        sessions = SessionService()
        meta = sessions.create_session(REQUEST)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            _trigger_conflict_refused(sessions, meta)
        assert "conflict" in buf_err.getvalue(), f"the test above is not exercising the mechanism: {buf_err.getvalue()!r}"


# ── the provider seam (#424) and invariant 6 ────────────────────────────────────


class _NamelessProvider:
    """Implements every member `ReasoningProvider` *declares* — and nothing more."""

    def analyze(self, request, *, current_model=None, answers=None, only=None, perimeter=None):
        raise AssertionError("a provider missing `name` must fail before it is asked to reason")

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not reached")

    def model_name(self):
        return "nameless-1"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": "nameless", "model_name": self.model_name(), "prompt_version": "sha256:x"}


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


def test_generation_that_races_a_concurrent_apply_does_not_lose_it():
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", full_model())          # revision 1 — what the generator will read

    def concurrent_answer():
        svc.update_model("s", full_model(business_rules=slot(90, "explicit", "high", "HR signs off")))

    brief_reply = json.dumps({"complexity": "medium", "problem": "P", "solution": "S", "risks": [], "next_steps": []})
    with pytest.raises(RevisionConflictError):
        DiscoveryService(client=RacingClient(brief_reply, concurrent_answer)).generate("s", "brief")
    # The rule that landed mid-flight is still there — the assessment's apply did not write over it.
    assert svc.load_model("s").model["business_rules"].value == "HR signs off"


def test_an_answers_turn_holds_the_revision_it_read():
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", full_model())

    def concurrent_apply():
        svc.update_model("s", full_model(risks=slot(70, "explicit", "high", "rollout risk")))

    with pytest.raises(RevisionConflictError):
        DiscoveryService(client=RacingClient(json.dumps(full_model()), concurrent_apply)).answer("s", "here are my answers")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it():
    """The full journey the tri-state exists for: discovery → assessment → an ordinary answer (invariant 10)."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", {**full_model(), "decisions": [
        {"decision": "Managers approve in-app", "derived_from": ["permissions"]}]})
    art.save("s", "prd", "# PRD\n", source_revision=1)

    reply = full_model(workflow=slot(90, "explicit", "high", "request → approve"))
    DiscoveryService(client=RacingClient(json.dumps(reply), lambda: None)).answer("s", "in-app")

    after = svc.load_model("s")
    assert [d.decision for d in after.decisions] == ["Managers approve in-app"]
    assert after.model["workflow"].value == "request → approve"   # the facts did move
    assert art.list("s")["prd"]["stale"] is True                  # …and that alone marks the PRD stale


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
def test_a_concurrent_first_discovery_is_refused_before_any_provider_call():
    """Two concurrent first-discovery requests must not both pay (#209)."""
    sessions = SessionService()
    slug = sessions.create_session(REQUEST).slug
    provider = StubProvider()
    with _guard_held(slug), pytest.raises(SessionLockedError) as exc_info:
        DiscoveryService(provider=provider, sessions=sessions).run_discovery(slug, surface="test")
    assert exc_info.value.code == "session_locked" and slug in str(exc_info.value)
    assert provider.calls == 0 and sessions.repo.read_meta(slug).current_revision == 0


@_POSIX_ONLY
def test_start_is_guarded_the_same_way_as_run_discovery():
    """`start()` is the other first-discovery door #209 names."""
    sessions = SessionService()
    provider = StubProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)
    meta = disco.claim_session(REQUEST, cards=None, slug=None)
    assert meta.slug.startswith(sessions.slug_hint(REQUEST))
    with _guard_held(meta.slug), pytest.raises(SessionLockedError):
        disco.start(REQUEST, slug=meta.slug)
    assert provider.calls == 0 and sessions.repo.read_meta(meta.slug).current_revision == 0


def test_run_discovery_still_succeeds_once_the_guard_is_free():
    """Must-fire control: a guard that refused everything would also pass the tests above."""
    sessions = SessionService()
    slug = sessions.create_session(REQUEST).slug
    provider = StubProvider()
    DiscoveryService(provider=provider, sessions=sessions).run_discovery(slug, surface="test")
    assert provider.calls == 1 and sessions.repo.read_meta(slug).current_revision == 1


def test_a_late_caller_with_a_stale_outer_check_still_pays_nothing(monkeypatch):
    """The revision is re-checked after the guard is won, not only against a snapshot that can go stale (#209)."""
    from requivo.services.sessions import SessionSnapshot

    sessions = SessionService()
    slug = sessions.create_session(REQUEST).slug
    real_snapshot = sessions.snapshot
    DiscoveryService(provider=StubProvider(), sessions=sessions).run_discovery(slug, surface="test")
    assert sessions.repo.read_meta(slug).current_revision == 1

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
    from requivo.core.persistence import SessionMeta

    sessions = SessionService()
    slug = sessions.slug_hint(REQUEST)
    DiscoveryService(provider=StubProvider(), sessions=sessions).start(REQUEST, slug=slug, surface="test")
    assert sessions.repo.read_meta(slug).current_revision == 1
    real_create_session = sessions.create_session

    def stale_create_session(*a, **k) -> SessionMeta:
        return real_create_session(*a, **k).model_copy(update={"current_revision": 0})

    monkeypatch.setattr(sessions, "create_session", stale_create_session)
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
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    # The siblings #372 swept already reach it; the join is visible in one fixture.
    assert store.session_exists("con") is True and store.canonical_dir("con") == d
    assert store.lock_path("con").name == "con.lock"
    assert _discovery_guard_path("con", store.Store(store.workspace_root())) == store.lock_root() / "con.discovering"

    sessions = SessionService()
    provider = StubProvider()
    DiscoveryService(provider=provider, sessions=sessions).run_discovery("con", surface="test")
    assert provider.calls == 1 and sessions.repo.read_meta("con").current_revision == 1
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
    persisted = entry != "draft_turn"
    assert len(fake.calls) == (0 if entry == "create_only" else 1)
    assert len(SessionService().list_sessions()) == (1 if persisted else 0)


def test_oversized_answers_are_refused_before_the_paid_turn():
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    slug = disco.start(REQUEST)
    with pytest.raises(InputTooLargeError):
        disco.answer(slug, "y" * (MAX_INPUT_CHARS + 1))
    assert len(fake.calls) == 1                       # refused before the second call was billed
    disco.answer(slug, "y" * MAX_INPUT_CHARS)         # must-fire control, same fixture
    assert len(fake.calls) == 2


def test_draft_turn_refuses_oversized_answers_before_reasoning():
    """The interactive loop's own un-persisted entry point."""
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    model = disco.draft_turn(REQUEST)
    with pytest.raises(InputTooLargeError):
        disco.draft_turn(REQUEST, current_model=model, answers="y" * (MAX_INPUT_CHARS + 1))
    assert len(fake.calls) == 1
    disco.draft_turn(REQUEST, current_model=model, answers="y" * MAX_INPUT_CHARS)
    assert len(fake.calls) == 2

