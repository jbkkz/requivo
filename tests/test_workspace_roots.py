"""#272: the workspace root is constructor state on `FileSessionRepository`/`Store`, not ambient process environment."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from _fakes import StubProvider

from requivo.core import persistence as store
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import Store
from requivo.core.persistence import store as store_module
from requivo.paths import workspace_root
from requivo.services.discovery import DiscoveryService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


@pytest.fixture
def two_roots(tmp_path_factory) -> tuple[Path, Path]:
    return tmp_path_factory.mktemp("workspace-a"), tmp_path_factory.mktemp("workspace-b")


@pytest.fixture
def rooted(tmp_path_factory, monkeypatch) -> tuple[Path, Path]:
    """An explicit root, with cwd elsewhere and no `REQUIVO_WORKSPACE`, so the ambient default is somewhere else."""
    explicit, elsewhere = tmp_path_factory.mktemp("explicit"), tmp_path_factory.mktemp("ambient-elsewhere")
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)
    return explicit, elsewhere


def test_two_repositories_against_two_roots_are_independent_in_one_process(two_roots):
    """#272's acceptance criterion, with the must-fire half: a slug in one root does not appear in the other."""
    before_env = dict(os.environ)
    root_a, root_b = two_roots
    repo_a, repo_b = FileSessionRepository(root=root_a), FileSessionRepository(root=root_b)
    repo_a.create("leave-approval", "A request filed against workspace A.")
    repo_b.create("leave-approval", "A different request, filed against workspace B.")
    repo_a.create("only-in-a", "Only workspace A should ever see this.")
    assert (repo_a.list_slugs(), repo_b.list_slugs()) == (["leave-approval", "only-in-a"], ["leave-approval"])
    assert repo_a.request_text("leave-approval") == "A request filed against workspace A."
    assert repo_b.request_text("leave-approval") == "A different request, filed against workspace B."
    assert repo_b.exists("only-in-a") is False
    for root in (root_a, root_b):
        assert (root / ".requivo" / "sessions" / "leave-approval" / "session.json").exists()
    assert os.environ == before_env, "the environment must not have been touched at all"


def test_two_roots_sharing_a_slug_do_not_share_a_lock(two_roots):
    """Root identity, not `id(self)`, decides re-entrancy: the inner take must open store_b's own lock file."""
    root_a, root_b = two_roots
    store_a, store_b = Store(root_a), Store(root_b)
    store_a.create_session("shared-slug", "req A")
    store_b.create_session("shared-slug", "req B")
    with store_a.session_lock("shared-slug"), store_b.session_lock("shared-slug"):
        assert (root_a / ".requivo" / "locks" / "shared-slug.lock").exists()
        assert (root_b / ".requivo" / "locks" / "shared-slug.lock").exists()


def test_reentrant_acquisition_across_fresh_ambient_stores_is_still_recognised(workspace):
    """The ambient wrapper `persistence.session_lock` builds a fresh `Store` per call; nesting must not deadlock."""
    store.create_session("amb-slug", "an ambient-workspace request")
    finished = threading.Event()

    def _nest():
        with store.session_lock("amb-slug"), store.session_lock("amb-slug"):
            finished.set()

    t = threading.Thread(target=_nest, daemon=True)
    t.start()
    t.join(timeout=5)
    assert finished.is_set(), "nested ambient session_lock calls deadlocked or timed out"


def test_an_explicit_root_is_immune_to_an_env_mutation_the_ambient_default_tracks(two_roots, monkeypatch):
    """The two halves of `FileSessionRepository`'s contract, on the same instances, across one env mutation."""
    first, second = two_roots
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(first))
    fixed, ambient = FileSessionRepository(root=first), FileSessionRepository()
    fixed.create("s", "req")
    ambient.create("only-first", "req")
    assert fixed.list_slugs() == ambient.list_slugs() == ["only-first", "s"]

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(second))
    assert fixed.list_slugs() == ["only-first", "s"] and fixed.request_text("s") == "req"
    assert ambient.list_slugs() == []
    ambient.create("only-second", "req")
    assert ambient.list_slugs() == ["only-second"]
    assert not (second / ".requivo" / "sessions" / "s").exists()


@pytest.mark.parametrize("root_kw", [{}, {"root": None}])
def test_default_repository_construction_is_unchanged(root_kw, workspace):
    repo = FileSessionRepository(**root_kw)
    repo.create("s", "req")
    assert repo.list_slugs() == ["s"]


# ── the scope amendment: the ambient reads outside core/persistence ──────────


def test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace(rooted):
    explicit_root, ambient_elsewhere = rooted
    sessions = SessionService(FileSessionRepository(root=explicit_root))
    disco = DiscoveryService(provider=StubProvider(), sessions=sessions)
    slug = sessions.create_session("a leave approval system").slug
    disco.run_discovery(slug, surface="test")
    assert (explicit_root / ".requivo" / "locks" / f"{slug}.discovering").exists()
    assert not (ambient_elsewhere / ".requivo").exists()
    assert sessions.repo.read_meta(slug).current_revision == 1


def test_a_repository_with_no_store_falls_back_to_the_ambient_workspace(workspace):
    """The other arm of `DiscoveryService._store_for_repo` (#483)."""
    disco = DiscoveryService.__new__(DiscoveryService)
    disco.sessions = SimpleNamespace(repo=SimpleNamespace())    # duck-typed: nothing but `.store` is read
    got = disco._store_for_repo()
    assert isinstance(got, Store) and got.root == workspace_root()
    assert got.lock_root() == workspace / ".requivo" / "locks"


def test_no_session_names_the_root_of_an_explicitly_rooted_repository(rooted, tmp_path, monkeypatch):
    """#272's cosmetic fourth: the refusal names the repository's own root, and still the ambient one by default."""
    explicit_root, ambient_elsewhere = rooted
    err = SessionService(FileSessionRepository(root=explicit_root)).no_session("missing-slug")
    assert isinstance(err, SessionNotFoundError)
    assert str(explicit_root) in str(err) and str(ambient_elsewhere) not in str(err)
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    assert str(tmp_path) in str(SessionService().no_session("missing-slug"))


def test_snapshot_names_the_root_of_an_explicitly_rooted_repository_not_the_ambient_one(rooted):
    """#457: `snapshot()` raised through the ambient `no_session_message` instead of `self.no_session`."""
    explicit_root, ambient_elsewhere = rooted
    with pytest.raises(SessionNotFoundError) as ei:
        SessionService(FileSessionRepository(root=explicit_root)).snapshot("missing-slug")
    assert str(explicit_root) in str(ei.value) and str(ambient_elsewhere) not in str(ei.value)


# ── found in review: output_root stays ambient, on every Store alike ─────────


def test_an_explicit_stores_legacy_root_still_honours_the_ambient_output_dir_override(two_roots, monkeypatch):
    """`REQUIVO_OUTPUT_DIR` wins on an explicit Store and on the ambient default alike; with no override, cwd-relative."""
    explicit_root, legacy_root = two_roots
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(legacy_root))
    assert Store(explicit_root).output_root() == legacy_root == store.output_root()
    monkeypatch.delenv("REQUIVO_OUTPUT_DIR", raising=False)
    assert Store(explicit_root).output_root() == Path.cwd() / "out" != explicit_root / "out"


def test_lock_key_resolves_the_root_once_at_construction_not_per_acquisition(tmp_path, monkeypatch):
    """`Store.__init__` calls `_resolve` through `store.py`'s own globals, so that is the binding to count (#550)."""
    calls = []
    real_resolve = store_module._resolve
    monkeypatch.setattr(store_module, "_resolve", lambda path: (calls.append(path), real_resolve(path))[1])
    s = store.Store(tmp_path)
    assert len(calls) == 1, "constructing a Store must resolve its root exactly once"
    calls.clear()
    for _ in range(5):
        assert s._lock_key("s") == s._lock_key("s")
    assert calls == [], f"_lock_key resolved the root itself: {len(calls)} call(s)"
