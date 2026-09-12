"""#272: the workspace root becomes constructor state, not ambient process environment.

The acceptance criterion the issue names verbatim: two `FileSessionRepository` instances against
two distinct tmp roots, in one process, with no `os.environ` mutation, no `monkeypatch.setenv`, no
`chdir` -- creating and listing independently. That is
`test_two_repositories_against_two_roots_are_independent_in_one_process` below; everything else in
this file is the mechanism underneath it and the three sites the issue's own 2026-09-01 comment
named as a scope amendment (the discovery guard, the reserved-slug probe inside it, and
`SessionService.no_session`'s error text).

Deliberately offline and deliberately not using the `workspace` fixture the rest of the suite shares
(`monkeypatch.setenv("REQUIVO_WORKSPACE", ...)`) except where a test is *specifically* about the
ambient default's own behaviour -- using it everywhere would defeat the point of a file whose whole
subject is "two roots addressed without env mutation".
"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest
from _fakes import out, slot

from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import Store
from requivo.paths import workspace_root
from requivo.services.discovery import DiscoveryService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService

# ── the acceptance test, verbatim against the issue's own wording ──────────────────────────────


def test_two_repositories_against_two_roots_are_independent_in_one_process(tmp_path_factory):
    """The issue's own acceptance criterion: two `FileSessionRepository` instances against two
    distinct tmp roots in one process, creating a session in each and listing them independently --
    with no `os.environ` mutation, no `monkeypatch`, no `chdir`."""
    before_env = dict(os.environ)
    root_a = tmp_path_factory.mktemp("workspace-a")
    root_b = tmp_path_factory.mktemp("workspace-b")

    repo_a = FileSessionRepository(root=root_a)
    repo_b = FileSessionRepository(root=root_b)

    repo_a.create("leave-approval", "A request filed against workspace A.")
    repo_b.create("leave-approval", "A different request, filed against workspace B.")

    assert repo_a.list_slugs() == ["leave-approval"]
    assert repo_b.list_slugs() == ["leave-approval"]
    assert repo_a.request_text("leave-approval") == "A request filed against workspace A."
    assert repo_b.request_text("leave-approval") == "A different request, filed against workspace B."

    # Independent on disk, not merely independent in what each repository reports.
    assert (root_a / ".requivo" / "sessions" / "leave-approval" / "session.json").exists()
    assert (root_b / ".requivo" / "sessions" / "leave-approval" / "session.json").exists()

    # The must-not-fire half: this test changed nothing about the process it ran in.
    assert os.environ == before_env, "the environment must not have been touched at all"


def test_a_third_slug_created_in_one_repository_does_not_appear_in_the_other(tmp_path_factory):
    """The must-fire complement to the test above: two repositories over two roots are not simply
    reporting the same store twice by coincidence -- a session created through one is genuinely
    invisible to the other."""
    root_a = tmp_path_factory.mktemp("workspace-a")
    root_b = tmp_path_factory.mktemp("workspace-b")
    repo_a = FileSessionRepository(root=root_a)
    repo_b = FileSessionRepository(root=root_b)

    repo_a.create("only-in-a", "Only workspace A should ever see this.")

    assert repo_a.list_slugs() == ["only-in-a"]
    assert repo_b.list_slugs() == []
    assert repo_b.exists("only-in-a") is False


# ── the re-entrancy fix `Store`'s own docstring names ───────────────────────────────────────────


def test_two_roots_sharing_a_slug_do_not_share_a_lock(tmp_path_factory):
    """Root identity, not `id(self)`, has to decide re-entrancy (see `Store`'s own docstring). Two
    `Store` instances over two different roots that happen to use the *same* slug name are two
    different sessions and must never be treated as one already-held lock -- keying re-entrancy by
    slug alone (the pre-#272 shape, safe only because exactly one ambient root ever existed at once)
    would make the second `session_lock` below believe it already holds a lock it never opened,
    which is a silent loss of mutual exclusion for whichever root lost the race."""
    root_a = tmp_path_factory.mktemp("workspace-a")
    root_b = tmp_path_factory.mktemp("workspace-b")
    store_a = Store(root_a)
    store_b = Store(root_b)
    store_a.create_session("shared-slug", "req A")
    store_b.create_session("shared-slug", "req B")

    with store_a.session_lock("shared-slug"):
        # If this silently believed it already held store_b's lock (a bug the shared-slug keying
        # would produce), it would return without ever opening store_b's own lock file -- so the
        # positive assertion below is the one that actually catches it: the lock file must exist.
        with store_b.session_lock("shared-slug"):
            assert (root_a / ".requivo" / "locks" / "shared-slug.lock").exists()
            assert (root_b / ".requivo" / "locks" / "shared-slug.lock").exists()


def test_reentrant_acquisition_across_fresh_ambient_stores_is_still_recognised(tmp_path, monkeypatch):
    """The other half of the same fix. The *ambient* module-level wrapper (`persistence.session_lock`,
    what `cli.py` and every un-rooted caller use) builds a **fresh** `Store` instance on every call --
    so a nested `with session_lock(slug): with session_lock(slug):` reaches two different `Store`
    objects, both addressing the same ambient root. Keying re-entrancy by `id(self)` instead of by
    root would make the inner call believe it is a stranger's lock and either deadlock retrying the
    OS lock, or hang out the full 30s timeout -- this must return immediately, nested, with no
    deadlock and no wait."""
    from requivo.core import persistence as store

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    store.create_session("amb-slug", "an ambient-workspace request")

    finished = threading.Event()

    def _nest():
        with store.session_lock("amb-slug"):
            with store.session_lock("amb-slug"):
                finished.set()

    t = threading.Thread(target=_nest, daemon=True)
    t.start()
    t.join(timeout=5)
    assert finished.is_set(), "nested ambient session_lock calls deadlocked or timed out"


def test_an_explicit_repository_root_is_immune_to_a_later_workspace_env_mutation(
        tmp_path_factory, monkeypatch):
    """The other half of `FileSessionRepository`'s own docstring: `root=None` tracks a later
    `REQUIVO_WORKSPACE` mutation (see the sibling test below); an *explicit* root must not."""
    fixed_root = tmp_path_factory.mktemp("fixed")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    repo = FileSessionRepository(root=fixed_root)
    repo.create("s", "req")
    assert repo.list_slugs() == ["s"]

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(elsewhere))
    # The same instance, after the environment moved out from under it -- must still see `fixed_root`.
    assert repo.list_slugs() == ["s"]
    assert repo.request_text("s") == "req"
    assert not (elsewhere / ".requivo").exists()


def test_the_ambient_default_repository_still_tracks_a_mid_process_workspace_mutation(
        tmp_path_factory, monkeypatch):
    """The CLI's own contract, pinned directly rather than only through a CLI-level test: `--workspace`
    mutates `REQUIVO_WORKSPACE` mid-process (`cli.py`), after a `SessionService`/`FileSessionRepository`
    may already exist. `root=None` (the default) must keep reading it fresh on every call -- not cache
    it at construction -- or that mutation would silently stop taking effect."""
    first = tmp_path_factory.mktemp("first")
    second = tmp_path_factory.mktemp("second")

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(first))
    repo = FileSessionRepository()          # constructed while REQUIVO_WORKSPACE names `first`
    repo.create("only-first", "req")
    assert repo.list_slugs() == ["only-first"]

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(second))
    # The SAME instance -- not a new one -- must now address `second`.
    assert repo.list_slugs() == []
    repo.create("only-second", "req")
    assert repo.list_slugs() == ["only-second"]


# ── the scope amendment: the three ambient reads outside core/persistence ──────────────────────


def _stub_provider():
    class _Stub:
        name = "stub"

        def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
            return out({"problem": slot(80, "explicit", "high")})

        def generate(self, *a, **k):  # pragma: no cover - unused here
            raise NotImplementedError

        def model_name(self):
            return "stub-model"

        def provenance(self, op, *, only=None):
            return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}

    return _Stub()


def test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace(
        tmp_path_factory, monkeypatch):
    """#272's scope amendment: `_discovery_guard`/`_discovery_guard_path` (services/discovery.py) used
    to read `lock_root()`/`session_root()` ambiently regardless of which repository the calling
    `DiscoveryService` was built over. Routed through `DiscoveryService._store_for_repo()` now, a
    discovery guard for an explicitly-rooted session must land under *that* root -- and must not
    touch the ambient one at all, which the negative half below is what actually proves it."""
    explicit_root = tmp_path_factory.mktemp("explicit")
    ambient_elsewhere = tmp_path_factory.mktemp("ambient-elsewhere")
    monkeypatch.chdir(ambient_elsewhere)
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)

    repo = FileSessionRepository(root=explicit_root)
    sessions = SessionService(repo)
    disco = DiscoveryService(provider=_stub_provider(), sessions=sessions)
    slug = sessions.create_session("a leave approval system").slug

    disco.run_discovery(slug, surface="test")

    assert (explicit_root / ".requivo" / "locks" / f"{slug}.discovering").exists(), (
        "the discovery guard did not land under the repository's own explicit root"
    )
    assert not (ambient_elsewhere / ".requivo").exists(), (
        "the discovery guard reached the ambient default instead of the repository's explicit root"
    )
    assert sessions.repo.read_meta(slug).current_revision == 1


def test_a_repository_with_no_store_falls_back_to_the_ambient_workspace(tmp_path, monkeypatch):
    """The other arm of `DiscoveryService._store_for_repo`, found unpinned in review of #483's first
    pass: `SessionRepository` carries no `store()` in its protocol, so a backing without one -- the
    Postgres shape -- must get the ambient default, which is exactly what every caller had
    unconditionally before #272. The positive arm (an explicitly rooted `FileSessionRepository`) is
    `test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace` above; this
    one proves the fallback resolves to the workspace and not, say, to a raise."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))

    class _NoStoreRepo:  # duck-typed: the only attribute `_store_for_repo` reads is `.store`
        pass

    disco = DiscoveryService.__new__(DiscoveryService)
    disco.sessions = SimpleNamespace(repo=_NoStoreRepo())

    store = disco._store_for_repo()
    assert isinstance(store, Store)
    assert store.root == workspace_root()
    assert store.lock_root() == tmp_path / ".requivo" / "locks"


def test_no_session_names_the_root_of_an_explicitly_rooted_repository(tmp_path_factory, monkeypatch):
    """#272's cosmetic fourth: `SessionService.no_session`'s error text used to be built by
    `store.no_session_message`, which always named the *ambient* session root -- even for a service
    holding an explicitly-rooted repository. It now reads the message off the service's own store."""
    explicit_root = tmp_path_factory.mktemp("explicit")
    ambient_elsewhere = tmp_path_factory.mktemp("ambient-elsewhere")
    monkeypatch.chdir(ambient_elsewhere)
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)

    svc = SessionService(FileSessionRepository(root=explicit_root))
    err = svc.no_session("missing-slug")

    assert isinstance(err, SessionNotFoundError)
    assert str(explicit_root) in str(err), (
        f"the refusal must name the repository's own root, got: {err}"
    )
    assert str(ambient_elsewhere) not in str(err), (
        "the refusal named the ambient workspace instead of the repository's explicit root"
    )


def test_snapshot_names_the_root_of_an_explicitly_rooted_repository_not_the_ambient_one(
        tmp_path_factory, monkeypatch):
    """#457: `snapshot()` raised through the module-level ambient `store.no_session_message(slug)`
    instead of `self.no_session(slug)` -- the instance method #272 added one function above it in
    this same file, for exactly this reason: so the refusal names the store the service actually
    addresses. One method along, in the same file and the same commit, `snapshot()` still named the
    ambient root -- the identical bug #272 closed for `no_session` itself, unfixed one call site
    over. Shaped as the sibling test above it (`test_no_session_names_the_root_of_an_explicitly_rooted_repository`),
    against `snapshot()` instead of `no_session()`."""
    explicit_root = tmp_path_factory.mktemp("explicit")
    ambient_elsewhere = tmp_path_factory.mktemp("ambient-elsewhere")
    monkeypatch.chdir(ambient_elsewhere)
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)

    svc = SessionService(FileSessionRepository(root=explicit_root))
    with pytest.raises(SessionNotFoundError) as ei:
        svc.snapshot("missing-slug")

    assert str(explicit_root) in str(ei.value), (
        f"the refusal must name the repository's own root, got: {ei.value}"
    )
    assert str(ambient_elsewhere) not in str(ei.value), (
        "the refusal named the ambient workspace instead of the repository's explicit root"
    )


def test_no_session_still_names_the_ambient_root_for_the_default_repository(monkeypatch, tmp_path):
    """The must-not-regress control for the fix above: the ordinary, un-rooted case (every existing
    caller) must still name the ambient workspace exactly as before."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    svc = SessionService()
    err = svc.no_session("missing-slug")
    assert str(tmp_path) in str(err)


@pytest.mark.parametrize("root_kw", [{}, {"root": None}])
def test_default_repository_construction_is_unchanged(root_kw, tmp_path, monkeypatch):
    """`FileSessionRepository()` and `FileSessionRepository(root=None)` are the same ambient default
    -- `default_repository()` (the CLI's own constructor) passes neither, so this is the shape every
    existing caller in the codebase actually uses."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    repo = FileSessionRepository(**root_kw)
    repo.create("s", "req")
    assert repo.list_slugs() == ["s"]


# ── found in review: output_root must stay ambient, on every Store alike ───────────────────────


def test_an_explicit_stores_legacy_root_still_honours_the_ambient_output_dir_override(
        tmp_path_factory, monkeypatch):
    """A reviewer's finding, fixed before this shipped past this branch: `Store.output_root()`
    briefly read `self.root / "out"`, silently substituting the workspace root for cwd -- so
    `requivo --workspace <dir>` with no `REQUIVO_OUTPUT_DIR` set would look for the legacy `out/`
    layout under `<dir>` instead of under cwd, exactly where `paths.output_root()`'s own docstring
    says it has always lived (`REQUIVO_OUTPUT_DIR`/cwd, deliberately independent of
    `REQUIVO_WORKSPACE`). `session migrate` would then fail every legacy session it found, because
    its own scan (`paths.output_root()`, ambient, correct) and its migration
    (`Store.legacy_dir`/`Store.output_root()`, workspace-root-derived, wrong) disagreed about where
    the legacy directory was.

    An explicit `Store`'s `output_root()` must equal the ambient `paths.output_root()` regardless of
    its own `root` -- both when `REQUIVO_OUTPUT_DIR` is set (checked here) and, by the same
    mechanism, when it is not."""
    explicit_root = tmp_path_factory.mktemp("explicit-workspace")
    legacy_root = tmp_path_factory.mktemp("legacy-out-dir")
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(legacy_root))
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)

    explicit_store = Store(explicit_root)
    assert explicit_store.output_root() == legacy_root
    assert explicit_store.output_root() != explicit_root / "out"

    # And the ambient default (root=None, what session migrate's scan and its actual migration both
    # ultimately read) must agree with the same override too.
    from requivo.core import persistence as store
    assert store.output_root() == legacy_root


def test_an_explicit_stores_legacy_root_is_cwd_relative_with_no_override(
        tmp_path_factory, monkeypatch):
    """The must-fire complement, with no `REQUIVO_OUTPUT_DIR` set at all: the legacy root is cwd's
    `out/`, never the explicit workspace root's -- `--workspace` alone has never redirected it."""
    explicit_root = tmp_path_factory.mktemp("explicit-workspace")
    monkeypatch.delenv("REQUIVO_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("REQUIVO_WORKSPACE", raising=False)

    from pathlib import Path
    explicit_store = Store(explicit_root)
    assert explicit_store.output_root() == Path.cwd() / "out"
    assert explicit_store.output_root() != explicit_root / "out"


def test_lock_key_resolves_the_root_once_at_construction_not_per_acquisition(
        tmp_path_factory, monkeypatch):
    """Found in review: `_lock_key` used to call `_resolve(self.root)` -- a real `os.path.realpath`
    stat -- on every `session_lock` entry, including every re-entrant nested one, where the
    pre-#272 re-entrancy key (`slug` alone) cost no syscalls at all. `self.root` never changes after
    construction, so the resolve belongs in `__init__`, once. Proved by counting calls rather than by
    reading the source: `_resolve` must be called exactly once for a `Store` that is then locked,
    re-entrantly, three times over."""
    from requivo.core import persistence as store

    # `Store.__init__` (in `core/persistence/store.py`) imports `_resolve` from `lock.py` and calls
    # the bare name, which resolves in `store.py`'s own globals -- not the package-level re-export
    # (#550): patching `store._resolve` is a second binding the constructor never reads.
    from requivo.core.persistence import store as store_module

    root = tmp_path_factory.mktemp("workspace")
    calls = []
    real_resolve = store_module._resolve

    def _counting_resolve(path):
        calls.append(path)
        return real_resolve(path)

    monkeypatch.setattr(store_module, "_resolve", _counting_resolve)

    s = store.Store(root)
    assert len(calls) == 1, "constructing a Store must resolve its root exactly once"

    # `_lock_key` in isolation, not the whole of `session_lock` -- `session_exists`/`canonical_dir`/
    # `is_contained` legitimately call `_resolve` too, for containment, on their own schedule (once
    # the session directory exists, `is_contained` no longer takes its early "nothing there yet"
    # return). Conflating that with the re-entrancy key would test the wrong thing; this isolates the
    # one call this fix actually touches.
    calls.clear()
    for _ in range(5):
        assert s._lock_key("s") == s._lock_key("s")
    assert calls == [], f"_lock_key resolved the root itself: {len(calls)} call(s)"
