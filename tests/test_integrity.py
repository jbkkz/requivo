"""Integrity tests: the guarantees the session store makes about concurrency, provenance and bounds.

Every test here reproduces a way the store could lie or crash rather than a feature it offers — two
writers racing, an artifact recorded fresh when it isn't, a future field destroyed by an older
reader, a slug the filesystem refuses. They are grouped in one file because they share a subject:
*the session on disk is trustworthy*. All offline — no API, no provider.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from requivo.core import persistence as store
from requivo.core.context import check_selection
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.errors import InvalidSlugError, RequivoError, RevisionConflictError
from requivo.core.integrity import check_session, inspect_session, newest_readable_revision, readable_revision
from requivo.core.persistence import _atomic_write
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    # A complete model owes an objective as much as it owes its slots (see `completeness_gap`),
    # so the shared fixture carries one.
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── concurrency ───────────────────────────────────────────────────────────────


def test_racing_applies_conflict_cleanly_instead_of_crashing(workspace):
    """Two writers starting from the same revision: one lands, the other is told it lost.

    `expected_revision` was checked and then acted on, with the writes in between unguarded, so both
    writers passed the check. What surfaced was not a `RevisionConflictError` but a `FileNotFoundError`
    from `Path.replace` — the two had also collided on one shared temp filename, and the second
    renamed a scratch file the first had already moved away. A lost update is bad; a lost update
    reported as a missing file is worse, because it reads as a bug in Requivo rather than a conflict
    the caller must resolve.

    Invariant 9: a precondition is held across the writes it authorises. `save_revision` checks
    `expected_revision` and then writes more than once; without `session_lock` around both, two
    writers pass the same check and the second overwrites the first — the check reads as protection
    while providing none. Every compound mutation runs under `repo.lock(slug)`, taken by the service
    so the whole sequence is one unit; the lock is re-entrant per thread
    (`test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice`) and OS-held, so
    a crash releases it. Any new multi-step write goes inside it, and any scratch file gets a unique
    name (`test_concurrent_atomic_writes_do_not_collide_on_a_temp_file`). The sequel — a lock living
    *inside* the directory a rename moves — is `test_a_forced_import_serialises_against_a_concurrent_writer`
    (moved here from CLAUDE.md by #286)."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())  # revision 1 — the shared base

    # A dozen writers rather than two: the unguarded window was narrow, and a regression test that
    # only sometimes reopens it is not a regression test. Against the pre-0.9.4 store this reliably
    # produced both symptoms at once — several FileNotFoundError crashes, and *two* writers passing
    # the same `expected_revision=1` precondition.
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
    """Creation is idempotent by design — the same request reuses its session — so concurrent callers
    creating the same discovery is ordinary, not exotic. It was decided by a `has_meta` check followed
    by a create, and a dozen callers all passed the check: each then wrote its own `session.json` over
    the last, so the session's id, provider and context cards were whichever writer finished last, and
    a reader in between could see a session directory with no metadata in it at all. The claim is now
    the rename that moves a fully-assembled session into place, so exactly one caller creates it and
    the rest are handed the one that exists."""
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
    """`_atomic_write` is called from every write path; its scratch file must be private to the call.

    With a fixed `.model.json.tmp`, concurrent writers interleaved write-then-rename and the loser hit
    `FileNotFoundError`. The content afterwards must be one writer's payload in full — never a mix."""
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


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    """Create a symlink, or skip loudly naming what went untested.

    Windows refuses `CreateSymbolicLink` unless the process holds `SeCreateSymbolicLinkPrivilege` or
    the machine is in Developer Mode, and whether a CI runner has either is not something this suite
    can assume. Letting that surface as a failure would be the harness reporting an environment limit
    as a product verdict — the exact trap #3 names — and silently passing instead would claim a
    coverage that does not exist. So it skips, and says which assertion did not run.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(
            f"this platform refuses to create a symlink ({type(e).__name__}: {e}). UNTESTED HERE: "
            f"that a symlink escaping the session root is refused. The containment check itself still "
            f"runs on every platform; only the symlink half of it is unreachable.")


def _blind_to_dangling_links(monkeypatch) -> None:
    """Give the store's own resolution the semantics CPython 3.9 has on Windows, on any platform.

    `_WindowsFlavour.resolve` (Lib/pathlib.py, 3.9) asks `nt._getfinalpathname`, which has to *open*
    the path, so it fails on a symlink whose target does not exist. The non-strict branch then splits
    the unresolvable tail off, resolves the longest prefix that does exist, and re-joins the rest
    verbatim — which is what the substitute below does. A dangling symlink out of the session root
    therefore reports itself as sitting inside it, and a containment check that trusts the answer
    waves it through. 3.10 routes pathlib through `realpath`, which reads the reparse point itself,
    so the hole is that one combination and no other.

    The point of simulating it is that `py3.9, windows-latest` is one leg of thirteen, and is not a
    required check. What it was the only thing guarding is a containment guarantee, so the mechanism
    is pinned here and every leg runs it.

    **It patches `store._resolve` — the resolution the product performs — and not `Path.resolve`.**
    Patching `Path.resolve` is what this did first, and it was a guard that could not fire: the store
    stopped calling `Path.resolve` in the same commit, so the substitute never reached the code under
    test and four tests passed on POSIX for a reason unrelated to the one their names give. Two
    reviewers found it independently. The rule that cost: a simulation has to be installed where the
    code under test looks, and *the assertion passed* is not evidence that it was."""
    real_resolve = store._resolve

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

    monkeypatch.setattr(store, "_resolve", resolve)


# ── what the first Windows leg found (#3) ─────────────────────────────────────
# Both of these are product defects the platform matrix exposed on its first run. Neither could fail
# on Linux or macOS, and both are written to fail on every platform now that the mechanism is known.


def test_a_session_path_is_not_resolved_before_it_exists(tmp_path, monkeypatch):
    """`_child_of` must reach no resolution at all for a child that is not there.

    It used to compare `d.resolve()` with `root.resolve()`: two independent resolutions, of paths
    where one is derived from the other, each reflecting the filesystem at the instant it ran. Create
    a directory between them and they disagree — so `canonical_dir("s")` raised `InvalidSlugError`,
    which means *you gave me a bad slug*, about the slug `s`, because another thread was creating a
    session at that moment. Four of twelve concurrent creators died that way on the Windows leg.

    Pinned as "performs no resolution" rather than by reproducing the race, because a timing test that
    only sometimes reopens the window is not a regression test. No resolution, no disagreement.

    What is counted is `store._resolve`, the store's own resolution, and not `Path.resolve` — which is
    what this counted until the store stopped calling it. A counter on a function the code under test
    never reaches records zero for a module that resolves on every call, so the assertion would have
    stayed green while saying nothing at all.

    Two siblings had the identical shape and were found by sweeping the class rather than the
    instance: `artifact_path` (`test_an_artifact_path_is_not_resolved_before_it_exists`) and
    `integrity.py`'s artifact containment check, where a spurious disagreement reported
    `unsafe_artifact_filename` about a perfectly bare name — the verb that answers *is this session
    intact* accusing the user. All three are now **one** function, `core/persistence.py`'s
    `is_contained`, because each had to be corrected separately for this and then again for its
    sequel (`test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it`). It resolves
    only a path that is actually there: `validate_slug`/`validate_filename` already make a separator
    or a dot segment unrepresentable, so the sole escape is a symlink at the target, and an absent
    path is not one — `exists() or is_symlink()`, never `exists()` alone, because `exists()` follows
    the link and a dangling symlink out of the root is precisely the case to catch
    (`test_a_symlink_out_of_the_session_root_is_still_refused`). Moved here from CLAUDE.md by #286."""
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
    """The must-fire half, and the reason the check exists at all. Skipping the resolution when the
    child is absent is only safe because an absent path cannot be a symlink — so a symlink that *is*
    there must still be caught, including a dangling one, which `exists()` alone reports as absent."""
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    _symlink_or_skip(root / "live", outside, target_is_directory=True)
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "live")

    # Dangling: `exists()` follows the link and reports False, so an `exists()`-only guard would wave
    # this through and then write through it the moment the target appeared.
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
    """The same must-fire assertion as above, with the resolver blinded the way CPython 3.9 blinds it
    on Windows — the shape that turned the guard off on that one leg while every other leg stayed
    green, because a dangling link resolved to its own location instead of to its target.

    A containment decision must not rest on the platform being able to follow the link. This is the
    test that says so on Linux and macOS too, so the next regression is caught by the leg that runs on
    every push rather than by the one that runs on one platform and one Python.

    The mechanism, so the next fix aims at the right function: `Path.resolve()` on Windows under
    CPython 3.9 asks `nt._getfinalpathname`, which has to open the path and therefore fails on a
    link whose target is missing, after which the non-strict branch re-joins the unresolvable tail
    to the parent it could resolve — so a dangling symlink resolves to its own location and reads as
    contained. Two things hold it now: `_resolve` is `os.path.realpath`, never `Path.resolve()`
    (realpath reads the reparse point itself, and its `strict=` keyword is 3.10+ and must not be
    reached for), and `is_contained` refuses a symlink whose resolution comes back equal to its own
    location, because a symlink never legitimately resolves to where it sits — that equality is the
    resolver saying *I could not look*, and refusing there is the third state that takes the
    guarantee off the platform entirely. `_blind_to_dangling_links` patches `store._resolve` and
    **not** `Path.resolve`, which is where it was first installed: a simulation aimed at a function
    the code no longer calls passes for a reason unrelated to its name (moved here from CLAUDE.md by
    #286)."""
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
    """The must-not-fire control for the test above: a guard that refuses everything passes every
    must-fire assertion, so the simulation needs cases it must *not* refuse. The live symlink is the
    one that matters — a blinded resolver follows a link whose target exists perfectly well, so
    refusing it would mean the guard had stopped reading the resolver's answer and started refusing
    links on sight."""
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    (root / "target").mkdir()
    _blind_to_dangling_links(monkeypatch)
    assert store._child_of(root, "s") == root / "s"
    assert store._child_of(root, "absent") == root / "absent"

    # Last, because creating a symlink is what skips on a platform that refuses them, and the two
    # assertions above hold everywhere.
    _symlink_or_skip(root / "live", root / "target", target_is_directory=True)
    assert store._child_of(root, "live") == root / "live"


def test_a_dangling_symlink_inside_the_root_is_accepted_where_the_platform_can_follow_it(tmp_path):
    """No blinding: the direction is what the refusal is about, not the dangling.

    Where the resolver can follow the link, this is answered on where it points, and a link into the
    root is inside the root. Where the resolver *cannot* follow it — 3.9 on Windows — the answer is
    refuse, because *we cannot tell* and the alternative is the defect this pair of tests exists for.
    Stating both halves here is the point: the conservative answer is confined to the platform that
    forces it, rather than becoming the rule everywhere."""
    root = tmp_path / "sessions"
    root.mkdir()
    _symlink_or_skip(root / "inside", root / "not-yet", target_is_directory=True)
    assert not (root / "inside").exists() and (root / "inside").is_symlink()
    assert store._child_of(root, "inside") == root / "inside"


def test_an_artifact_path_is_not_resolved_before_it_exists(workspace, monkeypatch):
    """`artifact_path` is `_child_of`'s sibling and had the identical two-resolution shape. It gets
    the same pin, because the previous commit claimed to have fixed it and tested only `_child_of` --
    a claim of "both are fixed" backed by one test is the shape this repo keeps finding.

    `canonical_dir` runs inside `artifact_path` and legitimately resolves an existing session
    directory, so the count is not zero. The property is that the *artifact* half adds nothing to it
    for a file that is not there — measured as a difference against `canonical_dir` alone rather than
    asserted as an absolute, which would just be a test of `canonical_dir`.

    Counts `store._resolve` rather than `Path.resolve`, for the reason its sibling above gives."""
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
    """The must-fire half for `artifact_path`, so the relaxation above cannot have turned the
    traversal guard off. These names are refused by `validate_filename` before any path is built,
    which is why the containment check can safely skip an absent target."""
    _healthy()
    # Windows-shaped escapes as well as POSIX-shaped ones. `_FILENAME_RE` refuses both for the same
    # reason on every platform, so this is not a platform branch — it is that the list was POSIX-only
    # while the leg most likely to be handed a backslash is the one that could not have said so.
    for name in ("../../../ESCAPED.md", "/etc/passwd", "..", ".hidden", "a/b.md",
                 r"..\..\ESCAPED.md", r"c:\windows\system32\drivers\etc\hosts", r"a\b.md"):
        with pytest.raises(RequivoError) as ei:
            store.artifact_path("s", name)
        assert ei.value.code == "invalid_filename", name


def test_an_artifact_symlink_is_refused_where_the_platform_cannot_resolve_it(workspace, tmp_path,
                                                                             monkeypatch):
    """`artifact_path` gets the blinded resolver too, because it and `_child_of` share the decision
    and a fix applied to one of them is this branch's own recurring defect."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")
    _blind_to_dangling_links(monkeypatch)
    with pytest.raises(RequivoError) as ei:
        store.artifact_path("s", "prd.md")
    assert ei.value.code == "invalid_filename"


def test_atomic_write_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """On Windows `rename` is `MoveFileEx`, which fails with `PermissionError(13, 'Access is denied')`
    whenever anything holds a handle to the destination — an antivirus scanner or the Search Indexer,
    neither of which this process can serialise against. `model.json` is the durable product, so
    losing a completed write to a scanner is not an acceptable outcome. Eight concurrent writers hit
    exactly this on the Windows leg."""
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
    """Bounded, and the bound is the point: a genuinely unwritable destination — a read-only file, a
    real permissions problem — must still fail loudly and quickly. Turning a permanent error into a
    slow permanent error helps nobody, and a retry that never gives up is how a crash becomes a hang."""
    target = tmp_path / "model.json"
    target.write_text("old", encoding="utf-8")
    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    with pytest.raises(PermissionError):
        store._atomic_write(target, "new")
    # The attempt count is the part that makes this a test of the *retry* rather than of `replace`:
    # without it the assertions below hold identically whether the retry exists or not, so the test
    # would pass against the code it was written to pin. Found by review, and it is the whole reason
    # the bound is worth asserting -- an unbounded retry turns a crash into a hang.
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


# ── forward compatibility of the session file ─────────────────────────────────


def test_a_field_from_a_future_requivo_survives_a_round_trip(workspace):
    """`docs/compatibility.md` promises that adding a field is a compatible change. Under
    `extra="ignore"` an older reader honoured that only until it wrote the file back, at which point
    the unknown field was silently dropped — so the first mutation by an older Requivo destroyed it."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["future_field"] = {"added_by": "requivo 1.4", "keep": True}
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())        # a real mutation: reads, then rewrites session.json
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["future_field"] == {"added_by": "requivo 1.4", "keep": True}
    assert after["current_revision"] == 1       # and the known fields still moved


def test_a_retired_key_is_dropped_rather_than_carried_forever(workspace):
    """The mirror image: `extra="allow"` must not resurrect keys a past Requivo retired. Retirement is
    explicit in `migrate_session`, so a key from the *past* is dropped and one from the *future* kept."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["prompt_versions"] = {"engine.md": "sha256:dead"}   # declared once, never written, now retired
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())
    assert "prompt_versions" not in json.loads(p.read_text(encoding="utf-8"))


# ── slug bounds ───────────────────────────────────────────────────────────────


def test_a_long_request_yields_a_slug_the_filesystem_accepts(workspace):
    """A request is arbitrary user text. One 300-character word made a 300-character directory name
    and the write failed deep inside with a bare `OSError: File name too long`."""
    long_word = "a" * 300
    slug = store.derive_slug(f"{long_word} system")
    assert len(slug) <= store.MAX_SLUG_LENGTH
    store.validate_slug(slug)                    # still a well-formed kebab-case token

    # Truncation must not merge two different requests into one session directory.
    other = store.derive_slug(f"{long_word} platform")
    assert slug != other

    svc = SessionService()
    meta = svc.create_session(f"{long_word} system")   # and the whole path is writable
    assert store.canonical_dir(meta.slug).is_dir()


def test_an_explicit_over_long_slug_is_refused_at_the_boundary(workspace):
    with pytest.raises(InvalidSlugError) as e:
        store.validate_slug("x" * (store.MAX_SLUG_LENGTH + 1))
    assert e.value.code == "invalid_slug"
    assert e.value.details["max_length"] == store.MAX_SLUG_LENGTH


# ── artifact freshness ────────────────────────────────────────────────────────


def _with_decision(model: dict, why: str) -> dict:
    model["decisions"] = [{"decision": "Draft-first", "why": why, "derived_from": ["permissions"]}]
    return model


def test_an_artifact_saved_against_an_older_revision_is_recorded_stale(workspace):
    """Saving is not the same moment as reasoning. A provider call takes minutes and the session can
    move under it; Claude Code can save a file it produced several turns ago. The source revision was
    recorded faithfully and the freshness beside it was simply assumed `False`, so a PRD reasoned from
    a superseded model sat on disk marked fresh."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                        # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new")}))  # 2

    st = art.save("s", "prd", "# PRD\n", source_revision=1)   # reasoned from 1, saved at 2
    assert st.revision == 1
    assert st.stale is True
    assert art.list("s")["prd"]["stale"] is True


def test_an_older_revision_that_missed_the_artifact_leaves_it_fresh(workspace):
    """The control. Staleness is the dependency graph, not revision drift: an artifact whose slots
    were untouched stays fresh however far the session has moved on."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    # `current_process` is in no artifact's slot set except the assessment's `*`.
    svc.update_model("s", _full_model(**{"current_process": _slot(80, "explicit", "high", "email")}))

    assert art.save("s", "prd", "# PRD\n", source_revision=1).stale is False
    assert art.save("s", "brief", "# Assessment\n", source_revision=1).stale is True


# ── the reasoning layer as a dependency ───────────────────────────────────────


def test_reasoning_that_changes_without_a_slot_moving_still_invalidates(workspace):
    """Every generator is prompted with the whole model, reasoning included, so a rewritten design
    decision can change the PRD with no slot touched. `diff_models` sees only slots, so this reported
    `changed_slots: []` and left the PRD marked fresh."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)
    assert art.list("s")["prd"]["stale"] is False

    result = svc.update_model("s", _with_decision(_full_model(), "reviewers were the bottleneck"))
    assert result.changed_slots == []                       # the facts did not move
    assert len(result.changed_decisions) == 1               # the judgment over them did
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True
    assert "changed_decisions" in result.to_dict()


def test_reasoning_merely_omitted_by_a_turn_is_preserved(workspace):
    """A refinement turn answers a question; it does not re-derive the brief, so its reply routinely
    arrives with no decisions at all. That silence must leave the established reasoning standing.

    It used to erase it. `engine.md` asks only for model/questions/summary, the reply was read as a
    whole `EngineOutput` (empty lists by default), and the apply path stored it verbatim — so an
    ordinary answer turn deleted every decision, challenge and opportunity the assessment had
    produced. Worse, the deletion was silent in both directions: the diff reported no reasoning
    movement and the PRD stayed marked fresh, because the diff absorbed the populated → empty case to
    keep exactly this turn from marking everything stale. The two defects hid each other."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", _full_model())           # same slots, reasoning simply absent
    assert [d.why for d in svc.load_model("s").decisions] == ["drafts are cheap"]
    assert result.changed_decisions == []
    assert result.stale_artifacts == []
    assert art.list("s")["prd"]["stale"] is False


def test_reasoning_explicitly_replaced_is_a_change_that_invalidates(workspace):
    """The other side of the tri-state: a proposal that *states* its reasoning replaces what was
    there, and every generator is prompted with the reasoning, so the saved PRD goes stale."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    replacement = {**_full_model(),
                   "decisions": [{"decision": "Approve-first", "derived_from": ["permissions"]}]}
    result = svc.update_model("s", replacement)
    assert [d.decision for d in svc.load_model("s").decisions] == ["Approve-first"]
    assert len(result.changed_decisions) == 2               # the one dropped, the one added
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def test_reasoning_explicitly_emptied_is_a_deletion_that_invalidates(workspace):
    """`"decisions": []` is a statement, not a silence: it deletes, and what rested on the deleted
    reasoning goes stale. Distinguishing this from an omission is the whole point of the tri-state —
    before it, a real deletion was indistinguishable from a quiet turn and passed unrecorded."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", {**_full_model(), "decisions": []})
    assert svc.load_model("s").decisions == []
    assert len(result.changed_decisions) == 1               # the deletion is reported, not absorbed
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


# ── the second version contract: the slot vocabulary ──────────────────────────


def test_a_session_from_a_newer_slot_schema_is_refused_clearly(workspace):
    """`schema_version` was recorded on every session and read by nothing. A model authored against a
    newer vocabulary can hold slots this build has no definition for, and the first symptom was an
    `unknown_slot` error naming a slot the user never typed."""
    from requivo.core.errors import InvalidSessionError

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["schema_version"] = store.SCHEMA_VERSION + 1
    p.write_text(json.dumps(raw))

    with pytest.raises(InvalidSessionError) as e:
        store.read_meta("s")
    assert e.value.details["schema_version"] == store.SCHEMA_VERSION + 1
    # An older schema is ordinary backward compatibility, not an error.
    raw["schema_version"] = 0
    p.write_text(json.dumps(raw))
    assert store.read_meta("s").slug == "s"


# ── reasoning identity ────────────────────────────────────────────────────────


def test_a_repeated_reasoning_item_is_refused_rather_than_deduplicated(workspace):
    """Ids are content-derived, so two identical decisions collide on one id. The id is what a diff
    keys on and what a user cites a decision by — a collision makes one of the pair invisible to change
    detection. The engine restating itself is a defect in the reply, which the retry loop can fix;
    quietly keeping one of the two cannot be undone."""
    from requivo.core.errors import RequivoError

    model = _full_model()
    model["decisions"] = [
        {"decision": "Draft-first", "derived_from": ["permissions"]},
        {"decision": "Draft-first", "derived_from": ["workflow"]},   # same text → same id
    ]
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    with pytest.raises(RequivoError) as e:
        svc.update_model("s", model)
    assert "repeated" in str(e.value).lower()


def test_a_snapshot_cannot_report_one_revision_and_another_revisions_model(workspace):
    """Every provider-backed operation reads a revision and a model before it reasons. Read as two
    calls, a write landing between them yields revision N with the model of N+1 — the generation then
    reasons from the newer model and files the artifact against the older revision. Nothing downstream
    can detect that: the recorded number is entirely plausible, it just describes a different model
    than the one the document was written from. `SessionService.snapshot` takes both under the session
    lock, so the pair is a state that actually existed."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"workflow": _slot(50, "inferred", "medium", "first")}))

    reading, wrote = threading.Event(), threading.Event()

    def concurrent_apply() -> None:
        reading.wait(timeout=10)
        try:
            SessionService().update_model(
                "s", _full_model(**{"workflow": _slot(90, "explicit", "high", "second")}))
        finally:
            wrote.set()

    writer = threading.Thread(target=concurrent_apply)
    writer.start()

    # Widen the window between the two reads to whatever the writer needs. Under the old two-call read
    # this is exactly where revision 2 landed; under the lock the writer cannot get in, so the wait
    # times out and the snapshot completes on the state it started from.
    real_read_meta = svc.repo.read_meta

    def slow_read_meta(slug):
        meta = real_read_meta(slug)
        reading.set()
        wrote.wait(timeout=0.5)
        return meta

    svc.repo.read_meta = slow_read_meta
    snap = svc.snapshot("s")
    writer.join(timeout=10)

    assert snap.revision == 1
    assert snap.model.model["workflow"].value == "first"   # the model *of* revision 1, not a later one


# ── session integrity: does a session tell the truth about itself? ────────────


def _healthy(slug: str = "s") -> SessionService:
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, _full_model())
    svc.update_model(slug, _full_model(**{"workflow": _slot(80, "explicit", "high", "moved")}))
    # Two revisions were applied above, so 2 is the revision this PRD was generated from. Stating
    # it is now the caller's job rather than the service's guess (#6).
    ArtifactService().save(slug, "prd", "# PRD\n", source_revision=2)
    return svc


def test_a_coherent_session_reports_no_problems(workspace):
    _healthy()
    assert check_session("s") == []


def test_check_session_waits_for_a_concurrent_writer_instead_of_reporting_a_tear(workspace,
                                                                                  monkeypatch):
    """#263. `check_session` used to read session.json, the revision files and model.json with no
    lock, so it could observe `save_revision`'s compound write torn: revisions/NNNN-model.json and
    model.json already replaced, session.json about to follow. An old session.json read alongside a
    fresh model.json then reported `model_is_not_the_last_revision` -- 'the file was changed after
    it was written' -- about a perfectly healthy session: invariant 17's class ('a check that can
    answer differently for the same argument depending on when it runs is not a check') landing in
    the one verb whose job is truth-telling.

    Reproduced with `write_meta` paused mid-save, the same gap
    `test_a_forced_import_serialises_against_a_concurrent_writer` freezes to prove the analogous
    claim for `session import`."""
    svc = _healthy()
    # Patched on the `Store` class, not the module function (#272): `store.save_revision` is now a
    # thin ambient-default wrapper over `Store.save_revision`, which calls `self.write_meta(...)` --
    # a class-method lookup, not the module-level `write_meta` name -- so patching the module
    # function no longer intercepts it.
    real_write_meta = store.Store.write_meta
    at_the_gate, release = threading.Event(), threading.Event()

    def paused(self, slug, meta):
        if slug == "s" and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, slug, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)

    write_failures: list[BaseException] = []

    def _write():
        try:
            svc.update_model("s", _full_model(**{"problem": _slot(90, "explicit", "high",
                                                                    "moved again")}))
        except BaseException as e:  # noqa: BLE001 - reported, not swallowed
            write_failures.append(e)

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"

    check_results: list[list] = []
    checker_done = threading.Event()

    def _check():
        check_results.append(check_session("s"))
        checker_done.set()

    checker = threading.Thread(target=_check, daemon=True)
    checker.start()

    # Must fire: a checker racing the writer must not read past the lock while the writer still
    # holds it -- it must block instead of returning the torn state (or anything at all).
    assert not checker_done.wait(1.0), (
        "check_session read past a writer still mid-save instead of waiting for its lock")

    release.set()
    writer.join(10)
    checker.join(10)
    assert not write_failures, f"the writer failed: {write_failures}"
    assert checker_done.is_set(), "check_session never returned once the writer released the lock"
    assert check_results == [[]], (
        f"a healthy session mid-save must report clean once the writer finishes, got {check_results}")


def test_a_session_whose_history_is_gone_is_caught(workspace):
    """The reviewer's repro, and the one shape that used to pass every check: session.json announces
    revision 2, `revisions/` is empty, and nothing is malformed — model.json parses, the metadata
    parses, the slug agrees. Only the *relationships* are broken, which is precisely what validating
    each file on its own cannot see."""
    _healthy()
    for f in (store.canonical_dir("s") / "revisions").glob("*.json"):
        f.unlink()
    codes = {p.code for p in check_session("s")}
    assert codes == {"missing_revision_file"}


def test_a_model_swapped_out_from_under_its_hash_is_caught(workspace):
    """Every revision records the hash of what was written. A model.json replaced by hand still parses
    as a perfectly good model — it is simply no longer the revision the session says it is at, so the
    history and the current state describe different things."""
    _healthy()
    d = store.canonical_dir("s")
    (d / "model.json").write_text(json.dumps(_full_model(**{"problem": _slot(90, "explicit", "high", "other")})))
    codes = {p.code for p in check_session("s")}
    assert "model_is_not_the_last_revision" in codes


def test_a_hand_edited_revision_file_is_caught(workspace):
    _healthy()
    f = store.canonical_dir("s") / "revisions" / "0001-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert "revision_hash_mismatch" in {p.code for p in check_session("s")}


def test_a_recorded_artifact_with_no_file_is_caught(workspace):
    _healthy()
    (store.canonical_dir("s") / "artifacts" / "prd.md").unlink()
    assert "missing_artifact_file" in {p.code for p in check_session("s")}


def test_a_model_from_a_newer_requivo_is_not_reported_as_a_defect(workspace):
    """#14. The checker read model.json through the strict boundary contract, so an unknown key — the
    thing `docs/compatibility.md` explicitly permits without a format bump — came back as
    `invalid_model`. Now that the loader carries such a key, the diagnostic still refusing it would be
    the worse half of the two: the session opens fine and `doctor` reports a defect in it, which is a
    health verdict measured against a rule this version no longer follows."""
    _healthy()
    d = store.canonical_dir("s")
    for f in (d / "model.json", d / "revisions" / "0001-model.json"):
        payload = json.loads(f.read_text(encoding="utf-8"))
        f.write_text(json.dumps({**payload, "risk_register": []}), encoding="utf-8")
    codes = {p.code for p in check_session("s")}
    assert "invalid_model" not in codes
    assert "invalid_revision_model" not in codes

    # The positive control. The assertions above are absences, and an absence also arrives from a
    # check that stopped looking — so the same two codes must still fire on a model that is broken
    # rather than merely newer.
    (d / "model.json").write_text('{"model": {"workflow": ', encoding="utf-8")
    (d / "revisions" / "0001-model.json").write_text('{"model": {"workflow": ', encoding="utf-8")
    broken = {p.code for p in check_session("s")}
    assert "invalid_model" in broken
    assert "invalid_revision_model" in broken


def test_a_structurally_invalid_session_json_is_a_problem_not_a_traceback(workspace):
    """A session.json that is valid JSON but not valid metadata raised a bare Pydantic
    `ValidationError` through the CLI. Every failure a user can cause has to arrive as a Requivo
    problem — the checker's whole job is to describe what is wrong, not to fail at it."""
    _healthy()
    (store.canonical_dir("s") / "session.json").write_text('{"slug": "s"}')  # no session_id, no dates
    codes = {p.code for p in check_session("s")}
    assert codes == {"invalid_session_json"}


def test_a_revision_log_that_does_not_match_the_revision_count_is_caught(workspace):
    _healthy()
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["revisions"] = raw["revisions"][:1]          # claims revision 2, logs one
    p.write_text(json.dumps(raw))
    assert "revision_count_mismatch" in {p_.code for p_ in check_session("s")}


def _point_artifact_at(slug: str, filename: str) -> None:
    """Rewrite the recorded artifact's `filename` in `session.json` — what a crafted or hand-edited
    session does. `ArtifactStatus.filename` is a bare `str` with no constraint, so this round-trips."""
    p = store.canonical_dir(slug) / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["prd"]["filename"] = filename
    p.write_text(json.dumps(raw))


def test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session(
        workspace, tmp_path):
    """An existence oracle: `st.filename` was read out of `session.json` and joined straight into the
    artifacts directory, so `.is_file()` was called on whatever it named.

    `pathlib` makes the absolute case the sharp one — `Path(d) / "artifacts" / "/etc/passwd"` is
    `/etc/passwd`, because an absolute component *replaces* everything before it — so the join did
    not even have to escape upwards. Nothing is read, so this discloses no content; what it discloses
    is **existence**, and the test for that has to be built accordingly.

    So the assertion is not "a problem is reported" — the old code reported a problem too, sometimes.
    It is that the verdict is now **identical whether or not the probed file exists**. Under the old
    code those two runs differ, and that difference is precisely the oracle: `missing_artifact_file`
    means the outside path was absent, its absence means the outside path was there.
    """
    present = tmp_path / "outside-present.md"
    present.write_text("secret\n")
    absent = tmp_path / "outside-absent.md"
    assert present.is_file() and not absent.exists(), "fixture is blind: the two paths must differ"

    verdicts = []
    for probe in (present, absent):
        _healthy("probe")
        _point_artifact_at("probe", str(probe))
        verdicts.append({p.code for p in check_session("probe")})
        shutil.rmtree(store.canonical_dir("probe"))

    assert verdicts[0] == verdicts[1], (
        f"the verdict leaks whether {present} exists: {verdicts[0]} vs {verdicts[1]}")
    assert "unsafe_artifact_filename" in verdicts[0]
    assert "missing_artifact_file" not in verdicts[0], (
        "reporting the artifact as merely missing means the path outside the session was stat-ed")


def test_an_unknown_artifact_type_does_not_fall_through_to_the_filesystem(workspace, tmp_path):
    """The fall-through the #23 lane's auditor named: an unknown artifact *type* recorded its problem
    and then carried on to the join with the untrusted filename still in hand. Both branches did —
    the filename-mismatch one too — so neither is a guard.

    Re-aimed by #260, where the unknown *type* stopped being a problem and became a note. The claim
    this test makes is unchanged and is about the **filename**: the row is still reported, and the
    name it carries is still refused rather than probed. What moved is which list the type-level
    finding lands in, so `inspect_session` is what the first assertion now reads — asserting it
    through `check_session` would have made this test pass for a reason unrelated to its name.
    `tests/test_future_artifact_types.py` holds the rest of that promise."""
    outside = tmp_path / "outside.md"
    outside.write_text("x\n")

    _healthy("probe")
    p = store.canonical_dir("probe") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["not-an-artifact-type"] = dict(raw["artifact_status"]["prd"],
                                                          filename=str(outside))
    p.write_text(json.dumps(raw))

    assert "unknown_artifact_type" in {pr.code for pr in inspect_session("probe")}   # must-fire
    codes = {pr.code for pr in check_session("probe")}
    assert "unsafe_artifact_filename" in codes       # and its filename was refused, not followed
    assert "missing_artifact_file" not in codes


def test_a_safe_but_missing_artifact_file_is_still_reported(workspace):
    """The must-fire control for the two tests above: refusing an unsafe name must not have turned
    the ordinary missing-file check off."""
    _healthy()
    (store.canonical_dir("s") / "artifacts" / "prd.md").unlink()
    codes = {p.code for p in check_session("s")}
    assert "missing_artifact_file" in codes
    assert "unsafe_artifact_filename" not in codes


def test_an_artifact_that_is_a_symlink_out_of_the_session_is_still_refused(workspace, tmp_path):
    """The branch the #3 fix leans on. `check_session_dir` now resolves the artifact path only when
    something is there, because two independent `resolve()` calls disagree whenever the tree moves
    between them and a spurious disagreement reports `unsafe_artifact_filename` about a bare name.
    That is only safe if a symlink which *is* there still gets caught — including a dangling one,
    which `exists()` reports as absent because it follows the link."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not part of this session", encoding="utf-8")

    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", outside)
    assert "unsafe_artifact_filename" in {p.code for p in check_session("s")}

    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")   # dangling, still outside
    assert not (artifacts / "prd.md").exists() and (artifacts / "prd.md").is_symlink()
    assert "unsafe_artifact_filename" in {p.code for p in check_session("s")}


def test_an_artifact_symlink_is_reported_unsafe_where_the_platform_cannot_resolve_it(workspace,
                                                                                     tmp_path,
                                                                                     monkeypatch):
    """`session verify`'s copy of the decision, blinded the same way. Its failure mode is quieter than
    the other two and worth stating on its own: nothing raises, the row simply comes back
    `missing_artifact_file` — *there is no file there* — about a link that is very much there and
    points out of the session. A verdict that is wrong is worse here than a verdict that is missing,
    because this is the verb whose whole job is telling you whether the session is intact.

    The classification has to be reached before the existence check for that to be visible at all,
    which is why the second assertion is not decoration."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")
    _blind_to_dangling_links(monkeypatch)
    codes = {p.code for p in check_session("s")}
    assert "unsafe_artifact_filename" in codes
    assert "missing_artifact_file" not in codes


def test_a_context_card_that_no_longer_resolves_is_not_an_integrity_problem(workspace, tmp_path,
                                                                            monkeypatch):
    """The boundary of what this module answers, pinned as behaviour rather than left in a docstring.

    `check_session_dir` asks whether a session directory tells the truth **about itself**. A context
    card lives outside the directory — in the installed package or in `user_context_dir()` — so an
    unresolvable card is a fact about *this machine*, not about the session. Two consequences follow
    and both are load-bearing:

    - the same directory would be "broken" on one machine and coherent on another, which is not a
      property an integrity check can have; and
    - `session import` refuses an archive on these problems, so a colleague's perfectly good session
      would become unimportable merely because you do not have one of their cards.

    It is a real problem and it is reported — by `doctor` and `session verify`, as an *environment*
    finding, through `core.context.check_selection`. It is deliberately not reported here.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))

    svc = SessionService()
    slug = svc.create_session("Something.", context_cards=["lost-domain"], slug="carded").slug
    assert store.read_meta(slug).context_cards == ["lost-domain"]
    assert check_session(slug) == []                 # must-fire control: the session is coherent

    (cards / "lost-domain.md").unlink()
    assert check_session(slug) == [], "integrity must not depend on what this machine has installed"
    # …and the environment check, which is where it belongs, does see it.
    assert check_selection(store.read_meta(slug).context_cards) is not None


# ── newest_readable_revision: the repair-target search (#210) ─────────────────
#
# `session verify`'s remedy line and `session restore`'s default target both come from here. It is
# deliberately not a verdict about the session -- it never raises -- only a fact about which history
# this build can actually open, read newest first because a repair wants the most recent trustworthy
# state.


def test_newest_readable_revision_returns_the_latest_when_everything_parses(workspace):
    _healthy()  # revision 1, then 2 (see _healthy's own docstring)
    d = store.canonical_dir("s")
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 2
    # byte-for-byte, not a re-serialized model -- restoring from this payload must reproduce the
    # revision file's own content_hash (see ReadableRevision's docstring).
    assert found.payload == (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")


def test_newest_readable_revision_skips_a_broken_file_and_returns_an_older_one(workspace):
    _healthy()
    d = store.canonical_dir("s")
    (d / "revisions" / "0002-model.json").write_text("{not json", encoding="utf-8")
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 1


def test_newest_readable_revision_treats_a_missing_file_the_same_as_an_unreadable_one(workspace):
    _healthy()
    d = store.canonical_dir("s")
    (d / "revisions" / "0002-model.json").unlink()
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 1


def test_newest_readable_revision_returns_none_when_nothing_in_range_is_readable(workspace):
    """The honest third answer -- must be able to say plainly that there is nothing to restore from,
    never invent one. The must-fire control above already proves the same session's revision 2 is
    found when it is readable; corrupting every file is what earns the None here rather than a bug
    in the search."""
    _healthy()
    d = store.canonical_dir("s")
    for f in (d / "revisions").glob("*.json"):
        f.write_text("{not json", encoding="utf-8")
    assert newest_readable_revision(d, 2) is None


# ── trusted, not merely parseable: the hash check (found in review, #210) ─────
#
# A revision file edited by hand after being frozen still parses as a valid model -- the exact case
# `inspect_session_dir`'s own `revision_hash_mismatch` exists to catch. `newest_readable_revision` and
# `readable_revision` must not trust a candidate that parses but disagrees with the hash
# `session.json`'s own revision log recorded for it, or a repair tool would trust what the diagnostic
# it is paired with already refuses to.


def test_newest_readable_revision_skips_a_revision_whose_hash_no_longer_matches(workspace):
    _healthy()
    d = store.canonical_dir("s")
    meta = store.read_meta("s")
    hashes = {r.revision: r.model_hash for r in meta.revisions}

    # must-fire control: with the real hashes, revision 2 (the latest) is trusted
    assert newest_readable_revision(d, 2, expected_hashes=hashes).revision == 2

    # tamper with revision 2's file after the fact -- it still parses, but no longer matches
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    found = newest_readable_revision(d, 2, expected_hashes=hashes)
    assert found is not None and found.revision == 1, (
        "a tampered-but-parseable revision must be skipped, not trusted")


def test_newest_readable_revision_with_no_expected_hashes_trusts_anything_that_parses(workspace):
    """The permissive default, stated as behaviour: when the caller has no hash log to check against
    (or passes none), a revision that merely parses is still returned -- `expected_hashes` narrows
    trust, it does not become a requirement nothing can satisfy."""
    _healthy()
    d = store.canonical_dir("s")
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert newest_readable_revision(d, 2).revision == 2


def test_readable_revision_checks_one_specific_number(workspace):
    _healthy()
    d = store.canonical_dir("s")
    meta = store.read_meta("s")
    hashes = {r.revision: r.model_hash for r in meta.revisions}

    assert readable_revision(d, 2, expected_hashes=hashes).revision == 2
    assert readable_revision(d, 1, expected_hashes=hashes).revision == 1
    assert readable_revision(d, 3, expected_hashes=hashes) is None  # does not exist

    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert readable_revision(d, 2, expected_hashes=hashes) is None, (
        "a named revision that no longer matches its recorded hash must be refused, not silently "
        "returned")
