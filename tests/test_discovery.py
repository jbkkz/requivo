"""Named stdlib loggers at the service seams (#435): `requivo.services.sessions`, `.artifacts` and
`.discovery` each emit a handful of INFO/DEBUG/WARNING records at real seams."""

from __future__ import annotations

import io
import json
import logging
from contextlib import redirect_stderr, redirect_stdout

import pytest
from _fakes import FakeClient, out, slot

from requivo.core.contracts import Brief
from requivo.core.errors import RevisionConflictError
from requivo.providers.errors import EngineError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """conftest's `workspace` fixture, applied automatically to every test in this module."""


class _StubProvider:
    """A minimal `ReasoningProvider`."""

    name = "stub"

    def __init__(self, *, analyze_error: Exception | None = None,
                generate_error: Exception | None = None):
        self._analyze_error = analyze_error
        self._generate_error = generate_error
        self.analyze_calls = 0
        self.generate_calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.analyze_calls += 1
        if self._analyze_error is not None:
            raise self._analyze_error
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls += 1
        if self._generate_error is not None:
            raise self._generate_error
        assert artifact_type == "brief"
        return Brief(complexity="low", solution="S", decisions=[], challenges=[], opportunities=[])

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def _attached(request):
    """Attach a collecting handler to a named logger for one test, restoring whatever handlers/level it had
    whether the test passes or not."""
    restores = []

    def _attach(name: str, level: int = logging.DEBUG) -> _CollectingHandler:
        logger = logging.getLogger(name)
        before = (list(logger.handlers), logger.level)
        restores.append((logger, before))
        handler = _CollectingHandler()
        logger.addHandler(handler)
        logger.setLevel(level)
        return handler

    yield _attach
    for logger, (handlers, level) in restores:
        logger.handlers, logger.level = handlers, level


def _messages(handler: _CollectingHandler) -> list[str]:
    return [r.getMessage() for r in handler.records]


# ── the documented seams: attach a handler, trigger the seam, see the record ────


def test_session_created_is_logged(_attached):
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    sessions.create_session("a leave approval system")
    messages = _messages(handler)
    assert any("session created" in m for m in messages), messages


def test_model_applied_is_logged_with_its_revision(_attached):
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    meta = sessions.create_session("a leave approval system")
    sessions.update_model(
        meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json(),
        expected_revision=0)
    messages = _messages(handler)
    assert any("model applied" in m and "revision=1" in m for m in messages), messages


def test_a_write_conflict_is_logged_as_refused(_attached):
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    meta = sessions.create_session("a leave approval system")
    with pytest.raises(RevisionConflictError):
        sessions.update_model(
            meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json(),
            expected_revision=99)
    warnings = [r for r in handler.records if r.levelno == logging.WARNING]
    assert any("conflict" in r.getMessage() for r in warnings), _messages(handler)


def test_a_rescope_that_mints_a_revision_is_logged_too(_attached):
    """`rescope()` is `sessions.py`'s *second* `save_revision(...)` call site (#168's own revision-
    per-selection-switch, distinct from `_plan`'s)."""
    handler = _attached("requivo.services.sessions")
    sessions = SessionService()
    meta = sessions.create_session("a leave approval system")
    sessions.update_model(
        meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json(),
        expected_revision=0)
    handler.records.clear()
    sessions.rescope(meta.slug, None)  # a no-op re-scope: create_session already recorded None
    # force a genuine change so a revision is actually minted
    sessions.rescope(meta.slug, ["b2b-platform"])
    messages = _messages(handler)
    assert any("revision" in m and "2" in m for m in messages), messages


def test_artifact_saved_is_logged_with_its_stale_verdict(_attached):
    handler = _attached("requivo.services.artifacts")
    sessions = SessionService()
    meta = sessions.create_session("a leave approval system")
    sessions.update_model(
        meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json(),
        expected_revision=0)
    artifacts = ArtifactService(repo=sessions.repo)
    artifacts.save(meta.slug, "prd", "# PRD", source_revision=1)
    messages = _messages(handler)
    assert any("artifact saved" in m and "stale=False" in m for m in messages), messages


def test_a_successful_provider_call_logs_started_and_finished(_attached):
    handler = _attached("requivo.services.discovery")
    provider = _StubProvider()
    disco = DiscoveryService(provider=provider, sessions=SessionService())
    disco.start("a leave approval system")
    messages = _messages(handler)
    assert any("provider call started" in m and "operation=analyze" in m for m in messages), messages
    assert any("provider call finished" in m and "operation=analyze" in m for m in messages), messages


def test_a_failed_provider_call_logs_a_warning_and_still_raises(_attached):
    handler = _attached("requivo.services.discovery")
    provider = _StubProvider(analyze_error=EngineError("boom"))
    disco = DiscoveryService(provider=provider, sessions=SessionService())
    with pytest.raises(EngineError):
        disco.start("a leave approval system")
    warnings = [r for r in handler.records if r.levelno == logging.WARNING]
    assert any("provider call failed" in r.getMessage() and "operation=analyze" in r.getMessage()
              for r in warnings), _messages(handler)


# ── silence by default, with a positive control on the mechanism itself ─────────
#
# pytest attaches its own capture handler to the ROOT logger for the length of every test.


def _trigger_conflict_refused(sessions: SessionService, meta) -> None:
    """The one seam most likely to leak: `_plan`'s WARNING-level "model apply refused" line."""
    with pytest.raises(RevisionConflictError):
        sessions.update_model(
            meta.slug, out({"problem": slot(1, "empty", "low")}).model_dump_json(),
            expected_revision=99)


def test_default_run_leaves_the_conflict_refused_warning_off_every_stream():
    root = logging.getLogger()
    root_before = (list(root.handlers), root.level)
    root.handlers = []
    try:
        sessions = SessionService()
        meta = sessions.create_session("a leave approval system")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            _trigger_conflict_refused(sessions, meta)
        assert buf_out.getvalue() == "", buf_out.getvalue()
        assert buf_err.getvalue() == "", buf_err.getvalue()
    finally:
        root.handlers, root.level = root_before


def test_removing_the_null_handler_reproduces_the_leak_the_test_above_guards_against():
    """Must-fire control. Strips `requivo/__init__.py`'s own `NullHandler` (and the root logger's handlers)
    and proves the *identical* seam, with nothing configured anywhere in the process."""
    root = logging.getLogger()
    requivo_logger = logging.getLogger("requivo")
    root_before = (list(root.handlers), root.level)
    requivo_before = (list(requivo_logger.handlers), requivo_logger.level, requivo_logger.propagate)
    root.handlers = []
    requivo_logger.handlers = []  # the fix under test, removed
    try:
        sessions = SessionService()
        meta = sessions.create_session("a leave approval system")
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            _trigger_conflict_refused(sessions, meta)
        assert "conflict" in buf_err.getvalue(), (
            "expected logging.lastResort to leak this WARNING to stderr with no handler anywhere "
            "in the process -- if it did not, the test above is not exercising the mechanism it "
            f"claims to. stderr was: {buf_err.getvalue()!r}")
    finally:
        root.handlers, root.level = root_before
        requivo_logger.handlers = requivo_before[0]
        requivo_logger.level = requivo_before[1]
        requivo_logger.propagate = requivo_before[2]


def test_a_judgment_naming_a_card_the_install_does_not_have_is_refused():
    """An invented card name is not inert: it would reach `resolve_cards` as a selection and refuse the very
    discovery the judgment was supposed to ground (#593)."""
    from requivo.core.context import CardSummary
    from requivo.core.errors import ProviderOutputError
    from requivo.providers.anthropic.generators import judge_context

    cards = [CardSummary(stem="b2b-platform", domain="enterprise management")]
    invented = json.dumps({"decision": "installed", "reason": "r", "cards": ["dentistry-es"]})
    client = FakeClient(invented, invented, invented)

    # `ProviderOutputError`, a `RequivoError` *sibling* of `EngineError` rather than a subclass.
    with pytest.raises(ProviderOutputError):
        judge_context(client, "a request", cards)
    assert len(client.calls) == 3, "the correction did not ride the retry loop"

    # Must fire: the same shape naming a card that *is* installed comes straight back.
    good = json.dumps({"decision": "installed", "reason": "r", "cards": ["b2b-platform"]})
    judged = judge_context(FakeClient(good), "a request", cards)
    assert judged.cards == ["b2b-platform"]


def test_the_judgment_prompt_carries_neither_the_schema_nor_the_cards():
    """Its whole economy is asking about ~9k of context for the price of a few hundred tokens (#593)."""
    from requivo.core.context import SHARED_PROMPT_HEAD, CardSummary
    from requivo.providers.anthropic.generators import judge_context

    client = FakeClient(json.dumps({"decision": "none", "reason": "r"}))
    judge_context(client, "a leave approval system", [CardSummary(stem="b2b-platform", domain="d")])

    system = client.calls[0]["system"]
    text = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert not text.startswith(SHARED_PROMPT_HEAD[:40])
    assert "# Model schema" not in text, "the judgment call is paying for the schema"
    assert "a leave approval system" in text and "b2b-platform" in text
