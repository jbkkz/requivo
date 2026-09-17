"""Concurrency guarantees the session store makes (invariant 9), the containment checks #3's first Windows leg
found (`is_contained`, POSIX-resolution races and dangling symlinks), and invariant 18's bounded retry on a
transient `PermissionError`."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from conftest import blind_to_dangling_links as _blind_to_dangling_links
from conftest import full_model as _full_model
from conftest import healthy_session as _healthy
from conftest import slot as _slot
from conftest import symlink_or_skip as _symlink_or_skip
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError, RequivoError, RevisionConflictError
from requivo.core.persistence import _atomic_write
from requivo.services.sessions import SessionService

# ── concurrency (invariant 9) ────────────────────────────────────────────────


def test_racing_applies_conflict_cleanly_instead_of_crashing(workspace):
    """Two writers starting from the same revision: one lands, the other is told it lost (#286)."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())  # revision 1 — the shared base

    # A dozen writers rather than two: the unguarded window was narrow.
    n = 12
    start = threading.Barrier(n)
    outcomes: list[str] = []
    guard = threading.Lock()

    def apply(value: str) -> None:
        start.wait()
        try:
            svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", value)}),
                             expected_revision=1)
            outcome = "applied"
        except RevisionConflictError:
            outcome = "conflict"
        except BaseException as e:  # noqa: BLE001 - the point is to catch anything else
            outcome = f"crash:{type(e).__name__}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=apply, args=(str(i),)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count("applied") == 1                    # exactly one writer may win
    assert outcomes.count("conflict") == n - 1               # the rest are told so, and told why
    assert [o for o in outcomes if o.startswith("crash:")] == []
    # Exactly one revision was created, and the session is internally consistent afterwards.
    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert len(meta.revisions) == 2
    assert (store.canonical_dir("s") / "revisions" / "0002-model.json").exists()


def test_racing_creations_of_one_session_all_agree_on_it(workspace):
    """Creation is idempotent by design — the same request reuses its session — so concurrent callers creating
    the same discovery is ordinary, not exotic."""
    svc = SessionService()
    n = 12
    start = threading.Barrier(n)
    got: list[object] = []
    guard = threading.Lock()

    def create(i: int) -> None:
        start.wait()
        try:
            meta = svc.create_session("Same request.", slug="s", provider=f"p{i}")
            outcome: object = meta.session_id
        except BaseException as e:  # noqa: BLE001 - a race must not surface as a crash
            outcome = f"crash:{type(e).__name__}"
        with guard:
            got.append(outcome)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(set(got)) == 1                                # one session, seen identically by all
    assert not str(got[0]).startswith("crash:")
    assert store.read_meta("s").session_id == got[0]
    assert store.list_session_slugs() == ["s"]               # no staging directory left behind


def test_concurrent_atomic_writes_do_not_collide_on_a_temp_file(workspace):
    """`_atomic_write` is called from every write path; its scratch file must be private to the call."""
    d = workspace / "scratch"
    d.mkdir()
    target = d / "model.json"
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            store._atomic_write(target, f"payload-{n}\n" * 200)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    lines = set(target.read_text(encoding="utf-8").splitlines())
    assert len(lines) == 1                                    # one writer's payload, not a blend
    assert not list(d.glob(".*tmp"))                          # no scratch left behind


# ── what the first Windows leg found (#3) ─────────────────────────────────────


def test_a_session_path_is_not_resolved_before_it_exists(tmp_path, monkeypatch):
    """`_child_of` must reach no resolution at all for a child that is not there (#286)."""
    root = tmp_path / "sessions"
    root.mkdir()
    resolved: list = []
    real_resolve = store._resolve

    def counting_resolve(path):
        resolved.append(str(path))
        return real_resolve(path)

    monkeypatch.setattr(store, "_resolve", counting_resolve)
    assert store._child_of(root, "s") == root / "s"
    assert resolved == [], (
        "_child_of resolved paths for a child that does not exist; every such resolution is a "
        f"verdict that depends on what the filesystem happened to look like: {resolved}")


def test_a_symlink_out_of_the_session_root_is_still_refused(tmp_path):
    """The must-fire half, and the reason the check exists at all."""
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    _symlink_or_skip(root / "live", outside, target_is_directory=True)
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "live")

    # Dangling: `exists()` follows the link and reports False, so an `exists()`-only guard would wave this through and then write through it the moment the target appeared.
    _symlink_or_skip(root / "dangling", tmp_path / "not-yet", target_is_directory=True)
    assert not (root / "dangling").exists() and (root / "dangling").is_symlink()
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "dangling")


def test_an_ordinary_existing_session_directory_is_still_accepted(tmp_path):
    """The must-not-fire half: a guard that refuses correct input is deleted by the next person."""
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    assert store._child_of(root, "s") == root / "s"


def test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it(tmp_path, monkeypatch):
    """The same must-fire assertion as above, with the resolver blinded the way CPython 3.9 blinds it on
    Windows."""
    root = tmp_path / "sessions"
    root.mkdir()
    _symlink_or_skip(root / "dangling", tmp_path / "not-yet", target_is_directory=True)
    _blind_to_dangling_links(monkeypatch)
    assert store._resolve(root / "dangling") == store._resolve(root) / "dangling", (
        "the simulation is not reproducing the defect: the blinded resolver is supposed to report the "
        "dangling link as living inside the root")
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "dangling")


def test_a_blinded_resolver_still_accepts_what_is_genuinely_inside(tmp_path, monkeypatch):
    """The must-not-fire control for the test above."""
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    (root / "target").mkdir()
    _blind_to_dangling_links(monkeypatch)
    assert store._child_of(root, "s") == root / "s"
    assert store._child_of(root, "absent") == root / "absent"

    # Last, because creating a symlink is what skips on a platform that refuses them, and the two assertions above hold everywhere.
    _symlink_or_skip(root / "live", root / "target", target_is_directory=True)
    assert store._child_of(root, "live") == root / "live"


def test_a_dangling_symlink_inside_the_root_is_accepted_where_the_platform_can_follow_it(tmp_path):
    """No blinding: the direction is what the refusal is about, not the dangling."""
    root = tmp_path / "sessions"
    root.mkdir()
    _symlink_or_skip(root / "inside", root / "not-yet", target_is_directory=True)
    assert not (root / "inside").exists() and (root / "inside").is_symlink()
    assert store._child_of(root, "inside") == root / "inside"


def test_an_artifact_path_is_not_resolved_before_it_exists(workspace, monkeypatch):
    """`artifact_path` is `_child_of`'s sibling and had the identical two-resolution shape."""
    _healthy()
    resolved: list = []
    real_resolve = store._resolve

    def counting_resolve(path):
        resolved.append(str(path))
        return real_resolve(path)

    monkeypatch.setattr(store, "_resolve", counting_resolve)

    store.canonical_dir("s")                           # the baseline this test is not about
    baseline = len(resolved)
    resolved.clear()

    p = store.artifact_path("s", "epic.json")          # a valid name, no such file
    assert p.name == "epic.json"
    assert len(resolved) == baseline, (
        f"artifact_path resolved {len(resolved) - baseline} path(s) beyond canonical_dir's for a "
        f"file that does not exist; each one is a verdict that depends on what the filesystem "
        f"looked like at that instant: {resolved[baseline:]}")


def test_an_artifact_filename_that_escapes_is_still_refused(workspace):
    """The must-fire half for `artifact_path`, so the relaxation above cannot have turned the traversal guard
    off."""
    _healthy()
    # Windows-shaped escapes as well as POSIX-shaped ones.
    for name in ("../../../ESCAPED.md", "/etc/passwd", "..", ".hidden", "a/b.md",
                 r"..\..\ESCAPED.md", r"c:\windows\system32\drivers\etc\hosts", r"a\b.md"):
        with pytest.raises(RequivoError) as ei:
            store.artifact_path("s", name)
        assert ei.value.code == "invalid_filename", name


def test_an_artifact_symlink_is_refused_where_the_platform_cannot_resolve_it(workspace, tmp_path,
                                                                             monkeypatch):
    """`artifact_path` gets the blinded resolver too, because it and `_child_of` share the decision and a fix
    applied to one of them is this branch's own recurring defect."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")
    _blind_to_dangling_links(monkeypatch)
    with pytest.raises(RequivoError) as ei:
        store.artifact_path("s", "prd.md")
    assert ei.value.code == "invalid_filename"


# ── a transient PermissionError is retried, briefly and only that (invariant 18) ──


def test_atomic_write_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """On Windows `rename` is `MoveFileEx`, which fails with `PermissionError(13, 'Access is denied')`
    whenever anything holds a handle to the destination."""
    target = tmp_path / "model.json"
    target.write_text("old", encoding="utf-8")
    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    store._atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert attempts["n"] == 4, "the write did not actually go through the retry path"


def test_atomic_write_still_gives_up_on_a_permanent_permission_error(tmp_path, monkeypatch):
    """Bounded, and the bound is the point: a genuinely unwritable destination."""
    target = tmp_path / "model.json"
    target.write_text("old", encoding="utf-8")
    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    with pytest.raises(PermissionError):
        store._atomic_write(target, "new")
    # The attempt count is the part that makes this a test of the *retry* rather than of `replace`.
    assert attempts["n"] == store._REPLACE_ATTEMPTS, (
        f"expected exactly {store._REPLACE_ATTEMPTS} attempts before giving up, got {attempts['n']}")
    assert target.read_text(encoding="utf-8") == "old"      # the old content is intact
    assert not list(tmp_path.glob(".*tmp")), "scratch left behind after a failed write"


def test_a_failed_atomic_write_leaves_no_scratch_file(workspace):
    d = workspace / "scratch"
    d.mkdir()
    with pytest.raises(TypeError):
        store._atomic_write(d / "model.json", None)  # type: ignore[arg-type]
    assert list(d.iterdir()) == []


def test_atomic_write_persists_content_and_leaves_no_tmp(tmp_path):
    dest = tmp_path / "model.json"
    _atomic_write(dest, '{"ok": true}')
    assert dest.read_text(encoding="utf-8") == '{"ok": true}'
    # The temp sidecar is renamed onto the target, never left behind.
    assert not (tmp_path / ".model.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [dest]
