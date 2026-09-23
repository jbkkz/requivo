"""Suite-wide guarantees — what every test gets without asking, and deliberately nothing else (#419)."""
import os
from pathlib import Path

import pytest
from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL
from _fakes import StubProvider, _FakeResponse, full_model, slot  # noqa: F401  (re-exported for the suites)

from requivo.core import persistence as _persistence_store
from requivo.core.contracts import EngineOutput
from requivo.services.artifacts import ArtifactService as _ArtifactService
from requivo.services.sessions import SessionService as _SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A temp `.requivo/` workspace, isolated from the caller's real one and from the legacy `out/` root --
    shared by every persistence/sessions/integrity test that touches disk (#555)."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── the stub providers the sessions-service and discovery-seam suites share (#555) ──────────


class RacingClient:
    """A provider whose reply arrives only after someone else has already moved the session."""

    def __init__(self, reply: str, on_call):
        self._reply, self._on_call = reply, on_call
        self.messages = self

    def create(self, **kwargs):
        self._on_call()          # the concurrent write lands while "reasoning" is in flight
        return _FakeResponse(self._reply)


class FakeProvider(StubProvider):
    """A `ReasoningProvider` with no vendor behind it -- the stand-in for a second implementation."""

    name = "fake"

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False, perimeter=None):
        self.analyze_calls += 1
        return EngineOutput.model_validate({**full_model(), "summary": {"objective": "A leave system"}})


class CountingProvider(FakeProvider):
    """A provider that records whether it was asked to reason -- the point of a pre-flight check."""


# ── a session at a given revision with a saved artifact, and the symlink-containment probes ──
# shared by the persistence and integrity suites (#555) -- declared twice, once per file, before.


def healthy_session(slug: str = "s"):
    """A session at revision 2 with a `prd` saved against it (#555)."""
    svc = _SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, full_model())
    svc.update_model(slug, full_model(**{"workflow": slot(80, "explicit", "high", "moved")}))
    # Two revisions were applied above, so 2 is the revision this PRD was generated from (#6).
    _ArtifactService().save(slug, "prd", "# PRD\n", source_revision=2)
    return svc


def symlink_or_skip(link, target, *, target_is_directory: bool = False) -> None:
    """Create a symlink, or skip loudly naming what went untested (#3)."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(
            f"this platform refuses to create a symlink ({type(e).__name__}: {e}). UNTESTED HERE: "
            f"that a symlink escaping the session root is refused. The containment check itself "
            f"still runs on every platform; only the symlink half of it is unreachable.")


def blind_to_dangling_links(monkeypatch) -> None:
    """Give the store's own resolution the semantics CPython 3.9 has on Windows, on any platform (#3)."""
    real_resolve = _persistence_store._resolve

    def resolve(path):
        s = Path(path)
        tail: list[str] = []
        while True:
            if s.exists():                       # stands in for `_getfinalpathname` succeeding
                return Path(real_resolve(s), *reversed(tail))
            if s.parent == s:                    # nothing resolved: 3.9 hands the path straight back
                return Path(path)
            tail.append(s.name)
            s = s.parent

    monkeypatch.setattr(_persistence_store, "_resolve", resolve)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    # Layer 1: no credential variable survives into a test, whatever shell ran the suite.
    for var in _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)
    # Layer 2: `app()` reads `.env` per run (#419 moved it out of import time).
    monkeypatch.setattr("requivo.cli.load_dotenv", lambda *a, **kw: False)
    # Layer 3: a call that still escapes — an on-disk profile resolves without a single variable set.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", SINKHOLE_BASE_URL)


def _workspace_entries(root):
    try:
        root.lstat()
    except FileNotFoundError:
        return set()

    def scan_failed(error):
        raise error  # os.walk otherwise silently ignores an unreadable directory.

    entries = {"."}
    for directory, dirs, files in os.walk(root, onerror=scan_failed):
        relative = Path(directory).relative_to(root)
        entries.update((relative / name).as_posix() for name in dirs + files)
    return entries


@pytest.fixture(scope="session", autouse=True)
def _no_workspace_leaks():
    # Observe, never redirect: test_the_workspace_guard_catches_a_real_unisolated_dump pins #432.
    root = Path.cwd() / ".requivo"
    before = _workspace_entries(root)
    yield
    added = _workspace_entries(root) - before
    assert not added, (
        f"Test suite created new entries under {root}: {', '.join(sorted(added))}. "
        "Set REQUIVO_WORKSPACE to a per-test tmp_path for tests that write application data."
    )
