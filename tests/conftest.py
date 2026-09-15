"""Suite-wide guarantees — what every test gets without asking, and deliberately nothing else.

`tests/_fakes.py` records why this file did not exist: an autouse *workspace* fixture at the root
would silently change what every unrelated test runs against, so workspace isolation stays local to
each file. That reasoning survives this file, because it is about fixtures that alter test
semantics — and the net below alters the run of no correct test: no test may depend on the
developer's own credential, and a test that wants one sets its own after the scrub (a per-test
`monkeypatch` layers over an autouse one).

#419 is the incident that funds it: `cli.py` loaded the repo's `.env` at import time, `client=None`
meant "build the default client", and one journey test made a real paid Anthropic call on every
machine with a resolvable credential — red locally, green in keyless CI, billed either way. The
sentence this file makes structural was prose before: "the whole suite: no API calls, no network".

Three layers, because #419 measured what one layer costs. The tests that exercise the SDK's own
discovery chain (profile/federation, in `test_provider_credentials.py` and `test_cli_doctor.py`) are untouched:
the net clears the *environment* and re-routes the *wire*, but leaves `default_credentials` real —
those tests re-set their own sources on top and keep asserting against the SDK, not a stub.

Must-fire pair in `tests/test_suite_hermeticity.py`:
`test_no_ambient_credential_reaches_a_test` (the probe) and
`test_the_net_fires_when_a_credential_is_ambient` (the probe re-run under a planted key).
"""
import os
from pathlib import Path

import pytest
from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL

from requivo.core import persistence as _persistence_store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.services.artifacts import ArtifactService as _ArtifactService
from requivo.services.sessions import SessionService as _SessionService


def slot(completeness=0, confidence="empty", impact="low", value=""):
    """A raw slot dict -- the four keys `full_model` and every direct `svc.update_model(...)` call
    in the persistence/sessions/integrity suites build a proposal out of. Distinct from
    `tests/_fakes.py`'s `slot()`, which has no `value` and feeds `EngineOutput.model_validate`
    rather than a raw proposal dict -- the two suites build different shapes on purpose (#555)."""
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def full_model(**overrides) -> dict:
    """A complete required-slot model proposal, with per-slot overrides -- the raw-dict counterpart
    of `_fakes.out()`. A complete model owes an objective as much as it owes its slots
    (`completeness_gap`), so the shared fixture carries one."""
    _, required = schema_slot_ids()
    model = {sid: slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A temp `.requivo/` workspace, isolated from the caller's real one and from the legacy `out/`
    root -- shared by every persistence/sessions/integrity test that touches disk (#555)."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── a stub ReasoningProvider that records calls, shared by the sessions-service and ─────────
# discovery-provider-seam suites (#555) -- was declared twice in test_sessions.py before the split.


class RacingClient:
    """A provider whose reply arrives only after someone else has already moved the session --
    drives the invariant-2 "a generation carries the revision it read" tests."""

    def __init__(self, reply: str, on_call):
        self._reply, self._on_call = reply, on_call
        self.messages = self

    def create(self, **kwargs):
        self._on_call()          # the concurrent write lands while "reasoning" is in flight
        return RacingReply(self._reply)


class RacingReply:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = None


class FakeProvider:
    """A `ReasoningProvider` with no vendor behind it -- the stand-in for a second implementation."""

    name = "fake"

    def analyze(self, request, *, current_model=None, answers=None, only=None, perimeter=None):
        from requivo.core.contracts import EngineOutput
        return EngineOutput.model_validate({**full_model(), "summary": {"objective": "A leave system"}})

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not needed for this test")

    def model_name(self):
        return "fake-model-1"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:fake"}


class CountingProvider(FakeProvider):
    """A provider that records whether it was asked to reason -- the point of a pre-flight check."""

    def __init__(self):
        self.calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None, perimeter=None):
        self.calls += 1
        return super().analyze(request, current_model=current_model, answers=answers, only=only)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        # Overrides `FakeProvider.generate`, which raises "not needed for this test". A guard test
        # has to be able to tell *reached the provider* from *raised somewhere else on the way*, and
        # an AssertionError from the stand-in reads like a failed assertion in the test itself.
        self.calls += 1
        raise AssertionError(f"the provider was reached with model={model!r}")


# ── a session at a given revision with a saved artifact, and the symlink-containment probes ──
# shared by the persistence and integrity suites (#555) -- declared twice, once per file, before.


def healthy_session(slug: str = "s"):
    """A session at revision 2 with a `prd` saved against it -- "a session at revision N with a
    saved artifact", the shared fixture #555 asks for. Returns the `SessionService`."""
    svc = _SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, full_model())
    svc.update_model(slug, full_model(**{"workflow": slot(80, "explicit", "high", "moved")}))
    # Two revisions were applied above, so 2 is the revision this PRD was generated from. Stating
    # it is now the caller's job rather than the service's guess (#6).
    _ArtifactService().save(slug, "prd", "# PRD\n", source_revision=2)
    return svc


def symlink_or_skip(link, target, *, target_is_directory: bool = False) -> None:
    """Create a symlink, or skip loudly naming what went untested -- Windows refuses
    `CreateSymbolicLink` without a privilege or Developer Mode, which no CI runner can be assumed to
    have (#3). Silently passing instead would claim coverage that does not exist."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(
            f"this platform refuses to create a symlink ({type(e).__name__}: {e}). UNTESTED HERE: "
            f"that a symlink escaping the session root is refused. The containment check itself "
            f"still runs on every platform; only the symlink half of it is unreachable.")


def blind_to_dangling_links(monkeypatch) -> None:
    """Give the store's own resolution the semantics CPython 3.9 has on Windows, on any platform
    (#3): a dangling symlink's unresolvable tail is split off and the resolved prefix re-joined to
    it verbatim, so it reports itself as sitting wherever the prefix does. Patches `store._resolve`
    -- the resolution the product performs -- not `Path.resolve`, which the store no longer calls."""
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
    # Layer 2: `app()` reads `.env` per run (#419 moved it out of import time) — a no-op here, or
    # every in-process CLI test running from the repo root would put the developer's real key
    # straight back after layer 1. The contract that `app()` *does* load `.env` is covered where a
    # subprocess owns its own environment: `test_a_verb_still_reads_the_dotenv_file`.
    # `raising` stays at its default (True) on purpose: if `requivo.cli` ever stops importing
    # `load_dotenv` under that name, this layer must fail loudly here rather than silently stop
    # guarding — keyless CI would never notice the loss, and the first symptom would be a keyed
    # machine billing again (found in review of #420).
    monkeypatch.setattr("requivo.cli.load_dotenv", lambda *a, **kw: False)
    # Layer 3: a call that still escapes — an on-disk profile resolves without a single variable
    # set, and a future path may hand the SDK a key some other way — dies on an unroutable loopback
    # port in milliseconds, unpaid, instead of reaching Anthropic.
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
