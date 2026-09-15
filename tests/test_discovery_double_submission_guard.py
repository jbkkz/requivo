"""#209: guard paid first-discovery calls against cross-tab / refresh double-submission, server-side.

Driven directly against `DiscoveryService`, not the Web -- the guard lives in the service layer
(invariant 14), so a caller reaching past every surface still gets it. The contending holder opens its
own file descriptor on the guard's own lock file rather than racing a second thread -- `flock` is
scoped to the *open file description*, not the thread or the process, so a second `os.open` in this
same test process contends for real without needing real concurrency to prove it (the same technique
`test_persistence_lock.py::test_a_contended_lock_raises_within_the_deadline_instead_of_hanging` uses
for `session_lock` itself).
"""

from __future__ import annotations

import json
import os

import pytest
from _fakes import out, slot

from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError, RevisionConflictError, SessionLockedError
from requivo.core.persistence import ensure_store_dir
from requivo.services.discovery import DiscoveryService, _discovery_guard_path, fcntl
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _CountingProvider:
    """A stub `ReasoningProvider` that just counts how many times it was actually called."""

    name = "stub"

    def __init__(self):
        self.calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.calls += 1
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, *a, **k):  # pragma: no cover - unused by run_discovery
        raise NotImplementedError

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


@pytest.mark.skipif(fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows equivalent "
                    "here, and the msvcrt branch takes the same non-blocking path. "
                    "REASONED, NOT OBSERVED on Windows -- see #209.")
def test_a_concurrent_first_discovery_is_refused_before_any_provider_call():
    """Two concurrent first-discovery requests must not both pay. Unlike `session_lock` (released
    before a provider call, and re-entrant), this guard is a non-blocking, non-reentrant `flock`
    scoped to the *open file description*, so a crashed holder releases it on process death. The
    loser gets `SessionLockedError` (`session_locked`, 503) before spending anything --
    `assert provider.calls == 0` below is the fact, not a hope. See #209."""
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug

    guard_path = _discovery_guard_path(slug, store.Store(store.workspace_root()))
    ensure_store_dir(guard_path.parent)
    holder_fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        provider = _CountingProvider()
        disco = DiscoveryService(provider=provider, sessions=sessions)
        with pytest.raises(SessionLockedError) as exc_info:
            disco.run_discovery(slug, surface="test")
        assert exc_info.value.code == "session_locked"
        assert slug in str(exc_info.value)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # The loser made zero provider calls, and nothing was written -- the whole point of #209.
    assert provider.calls == 0
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 0


def test_run_discovery_still_succeeds_once_the_guard_is_free():
    """Must-fire control: without it, a guard that refused *everything* would also pass the test
    above, telling us nothing about whether an uncontended caller can still proceed."""
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)

    disco.run_discovery(slug, surface="test")

    assert provider.calls == 1
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 1


def test_a_late_caller_with_a_stale_outer_check_still_pays_nothing(monkeypatch):
    """Found in review: the guard alone is not the whole guarantee if the revision is checked only
    *before* it, against a snapshot that can go stale before the guard is actually won -- it must be
    re-read *inside* the guard, right before the provider is called. Reproduced by monkeypatching
    `snapshot()` so the late caller's outer (pre-guard) read is frozen at revision 0 while its inner
    (post-guard) read is the real, current one."""
    from requivo.services.sessions import SessionSnapshot

    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    real_snapshot = sessions.snapshot

    winner = _CountingProvider()
    DiscoveryService(provider=winner, sessions=sessions).run_discovery(slug, surface="test")
    assert winner.calls == 1
    assert sessions.repo.read_meta(slug).current_revision == 1

    stale = SessionSnapshot(slug=slug, revision=0, model=None,
                            request="a leave approval system", context_cards=None)
    seen = {"n": 0}

    def fake_snapshot(s):
        seen["n"] += 1
        return stale if seen["n"] == 1 else real_snapshot(s)  # 1st (outer) call is stale, rest real

    monkeypatch.setattr(sessions, "snapshot", fake_snapshot)
    late = _CountingProvider()
    late_disco = DiscoveryService(provider=late, sessions=sessions)

    with pytest.raises(RevisionConflictError):
        late_disco.run_discovery(slug, surface="test")

    assert late.calls == 0  # the whole point: the late caller never reached the provider
    assert sessions.repo.read_meta(slug).current_revision == 1  # unchanged


def test_a_late_caller_of_start_with_a_stale_outer_check_still_pays_nothing(monkeypatch):
    """The same race `test_a_late_caller_with_a_stale_outer_check_still_pays_nothing` pins for
    `run_discovery`, one entry point over. `start()`'s outer check reads the revision off the meta
    `claim_session` returns rather than a fresh snapshot, so a late caller has to be caught by the
    *inner* re-read inside the guard (`self.sessions.repo.read_meta(...)`), not by the outer check,
    which by construction cannot see the winner's write."""
    from requivo.core.persistence import SessionMeta

    sessions = SessionService()
    request = "a leave approval system"
    slug = sessions.slug_hint(request)

    winner = _CountingProvider()
    DiscoveryService(provider=winner, sessions=sessions).start(request, slug=slug, surface="test")
    assert winner.calls == 1
    assert sessions.repo.read_meta(slug).current_revision == 1

    real_create_session = sessions.create_session

    def stale_create_session(*a, **k) -> SessionMeta:
        meta = real_create_session(*a, **k)
        return meta.model_copy(update={"current_revision": 0})

    monkeypatch.setattr(sessions, "create_session", stale_create_session)
    late = _CountingProvider()
    late_disco = DiscoveryService(provider=late, sessions=sessions)

    with pytest.raises(RevisionConflictError):
        late_disco.start(request, slug=slug, surface="test")

    assert late.calls == 0  # the whole point: the late caller never reached the provider
    assert sessions.repo.read_meta(slug).current_revision == 1  # unchanged


@pytest.mark.skipif(fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows equivalent "
                    "here, and the msvcrt branch takes the same non-blocking path. "
                    "REASONED, NOT OBSERVED on Windows -- see #209.")
def test_start_is_guarded_the_same_way_as_run_discovery():
    """`start()` (the direct-request entry point `POST /sessions` uses when it discovers straight
    away) is the other first-discovery door #209 names -- guarded on the *derived* slug, since a
    caller of `start()` may not have named one."""
    sessions = SessionService()
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)
    slug = sessions.slug_hint("a leave approval system")
    # `claim_session` derives the same slug `start()` will use for this request -- reproduced here
    # only to find the guard file `start()` itself will contend on.
    meta = disco.claim_session("a leave approval system", cards=None, slug=None)
    assert meta.slug == slug or meta.slug.startswith(slug)

    guard_path = _discovery_guard_path(meta.slug, store.Store(store.workspace_root()))
    ensure_store_dir(guard_path.parent)
    holder_fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SessionLockedError):
            disco.start("a leave approval system", slug=meta.slug)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    assert provider.calls == 0
    assert sessions.repo.read_meta(meta.slug).current_revision == 0


@pytest.mark.skipif(fcntl is None, reason="the fixture needs a directory literally named 'con' "
                    "already on disk, which Windows refuses to create at the OS level independent "
                    "of anything Requivo does -- so a session at a reserved slug is a state only a "
                    "platform that never enforced the restriction can reach. REASONED, NOT "
                    "OBSERVED: the same platform limit the sibling #372 fixtures carry.")
def test_a_reserved_slug_the_sweep_one_commit_later_missed_reaches_the_discovery_guard():
    """#390: a two-commit join no single diff showed. `e03aa47` added `_discovery_guard_path`
    calling `validate_slug` unconditionally; `3fa1423` swept the other call sites onto #372's
    conditional pair and missed this one. Cost: a session on disk under a Windows reserved name
    (pre-#221) is readable and lockable, and only `run_discovery`'s guard still refused it with
    `InvalidSlugError`. Driven through `run_discovery`, asserting the provider was actually reached."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    # The siblings #372 swept already reach it -- quoted here so the join is visible in one fixture
    # rather than inferred from another file: these three passing while the fourth refused is
    # precisely the state this test was written against.
    assert store.session_exists("con") is True
    assert store.canonical_dir("con") == d
    assert store.lock_path("con").name == "con.lock"

    assert (_discovery_guard_path("con", store.Store(store.workspace_root()))
            == store.lock_root() / "con.discovering")

    sessions = SessionService()
    provider = _CountingProvider()
    DiscoveryService(provider=provider, sessions=sessions).run_discovery("con", surface="test")
    assert provider.calls == 1
    assert sessions.repo.read_meta("con").current_revision == 1

    # Must-not-fire control, in the same fixture: a reserved name nothing occupies is still refused,
    # so this cannot pass by dropping #221's creation refusal instead of narrowing it.
    with pytest.raises(InvalidSlugError):
        _discovery_guard_path("nul", store.Store(store.workspace_root()))
