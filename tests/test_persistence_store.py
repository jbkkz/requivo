"""The store root's own bookkeeping: the privacy `.gitignore` (#211, #320) and what a
half-finished `save_revision` leaves behind (#261). Two different questions sharing one file because
both are about the store keeping its promises about *itself* rather than about a session's content
Split out of `test_persistence_guards_ii.py` by #550's original split, and split again out of
that file entirely by #555.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core import persistence as store
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.integrity import check_session
from requivo.core.persistence import store as store_module
from requivo.services.sessions import SessionService

# --- The store's privacy .gitignore (#211, #320) --------------------------------------


def test_the_privacy_gitignore_is_written_once_and_never_restored(workspace):
    """`.requivo/` lands in the caller's workspace, which defaults to cwd -- for the Claude Code plugin
that is the user's project repository by construction -- and `create_session` writes the client's
request there verbatim. A routine `git add .` published it, silently, against the local-first
confidentiality this product states as its wedge. This repository's own `.gitignore` covers
`.requivo/`, which is why the maintainer was the one person who could not experience it."""
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()

    svc = SessionService()
    svc.create_session("A leave approval system", slug="first")

    assert marker.exists(), "creating the first session did not write the privacy .gitignore"
    assert marker.read_text(encoding="utf-8").splitlines()[-1] == "*", (
        "the ignore pattern must be the self-ignoring `*`, so nothing has to be added to the user's "
        "own .gitignore -- a file Requivo has no business editing"
    )

    # Deleted on purpose: the team wants these sessions committed. Nothing may bring it back.
    marker.unlink()
    svc.create_session("A room booking tool", slug="second")
    svc.update_model("second", _full_model())
    assert not marker.exists(), "a later session operation restored an ignore file the user deleted"

    # Edited on purpose: same branch, and the edit survives byte for byte.
    marker.write_text("sessions/secret-*\n", encoding="utf-8")
    svc.create_session("A third thing", slug="third")
    assert marker.read_text(encoding="utf-8") == "sessions/secret-*\n"


def test_no_store_directory_is_created_outside_ensure_store_dir():
    """The guard behind #211, because fixing every call site leaves the next one. A bare
`mkdir(parents=True)` (or an `os.makedirs`) on a store path re-opens #211 for whichever verb
reaches a fresh workspace first, and it does so silently — the session write succeeds, and only
the absent ignore file says anything. See #320."""
    import ast

    src = Path(__file__).resolve().parent.parent / "src" / "requivo"
    exempt = {
        # Creates the staging tree for a session in flight. Its parent is `session_root()`, which the
        # line above it has already ensured, so this cannot be the call that creates the store root.
        ("core/persistence/store.py", "create_session"),
        # `ensure_store_dir` is the one place allowed to do it — that is the whole rule.
        ("core/persistence/store.py", "ensure_store_dir"),
    }
    seen: set[tuple[str, str]] = set()
    offenders: list[str] = []
    scanned = 0
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else "")
                if name == "mkdir" and any(k.arg == "parents" for k in node.keywords):
                    pass
                elif name == "makedirs":
                    pass
                else:
                    continue
                if (rel, fn.name) in exempt:
                    seen.add((rel, fn.name))
                    continue
                offenders.append(f"{rel}:{node.lineno} in {fn.name}()")

    assert scanned > 20, (
        f"the scan found only {scanned} modules under {src} — a guard that cannot see the package "
        f"reports no offenders for the wrong reason"
    )
    assert not offenders, (
        "these create a directory tree without going through `ensure_store_dir`, so on a fresh "
        "workspace they can bring `.requivo/` into existence with no privacy .gitignore (#211):\n  "
        + "\n  ".join(offenders)
    )
    assert seen == exempt, (
        "an exemption above no longer names a real call site, so it is unchecked prose: "
        f"{sorted(exempt - seen)}"
    )


def test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt(workspace,
                                                                                 monkeypatch):
    """#320. The guarantee could be switched off permanently by one transient error. `ensure_store_dir`
used to read `not root.exists()` before creating anything. So when `mkdir` succeeded and the
marker write then failed — a full disk, an EACCES, a Windows scanner holding a handle, which
invariant 18 already documents as real for a structurally identical operation — the call failed
loudly but left `.requivo/` present and unignored."""
    real_open = builtins.open

    def refuse_the_marker(path, mode="r", *a, **kw):
        if str(path).endswith(".gitignore") and "x" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", refuse_the_marker)
    with pytest.raises(RequivoError) as ei:
        SessionService().create_session("A confidential client request.", slug="one")
    assert ei.value.code != "", "the failure must be structured, not a bare OSError"
    assert not (workspace / ".requivo").exists(), (
        "the store root outlived the failed marker write, so every later call takes the "
        "'already exists' branch and the privacy guarantee is off for good"
    )

    monkeypatch.setattr(builtins, "open", real_open)
    SessionService().create_session("A confidential client request.", slug="one")
    assert (workspace / ".requivo" / ".gitignore").exists(), (
        "the retry after a transient failure did not write the marker"
    )


def test_the_store_root_is_created_without_probing_whether_it_exists(workspace, monkeypatch):
    """The other half of #320, and the reason `exists()` had to go rather than be wrapped.
`Path.exists()` re-raises `EACCES` instead of swallowing it — invariant 15's #80, one function
along — and `PermissionError` is not a `RequivoError`, so `cli.app()` let it out as a traceback:
the very first command run in such a workspace crashed instead of refusing."""
    called: list[str] = []
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, *a, **kw: (called.append(str(self)),
                                                               real_exists(self, *a, **kw))[1])
    SessionService().create_session("Something.", slug="probe")
    assert not any(c.endswith(".requivo") for c in called), (
        f"the store root is still decided by an exists() probe, which can raise EACCES: {called}"
    )

    # And an OSError from the store is a structured refusal, not a traceback.
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **kw: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied")))
    with pytest.raises(RequivoError):
        store.ensure_store_dir(workspace / ".requivo" / "sessions")


# ── #261: what a half-finished save_revision leaves behind ──────────────────────


class _InjectedCrash(BaseException):
    """A process death, modelled. `BaseException` on purpose: an `Exception` could be swallowed by a
    handler on the way out, and then the test would be measuring the handler rather than the store."""


@contextmanager
def _crashing_after(after: int):
    """Let `after` writes through, then refuse the rest: ENOSPC, a SIGKILL, a pulled plug.

    Yields the record of attempted writes, so a test can assert the tear *happened*: an injection
    that fired before any write at all leaves a perfectly coherent session, and every consistency
    assertion below would then pass for that reason instead of the intended one. The count is checked
    here rather than left to each caller, because that assertion is the whole difference between
    these tests and three that pass on an untorn session.

    The patch is installed through a *scoped* `MonkeyPatch` rather than through the test fixture.
    `monkeypatch.undo()` on the shared one rewinds every patch that fixture holds, including the
    REQUIVO_WORKSPACE set by `workspace` — which pointed the assertions at the real store on the
    developer machine and failed with `no_session_json`, a plausible-looking verdict about a session
    that was never there."""
    real = store_module._atomic_write
    record: dict = {"attempted": []}

    def crashing(path, content):
        record["attempted"].append(path.name)
        if len(record["attempted"]) > after:
            raise _InjectedCrash(f"simulated death after write {after}")
        return real(path, content)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_module, "_atomic_write", crashing)
        yield record

    assert len(record["attempted"]) == after + 1, (
        f"the injection did not fire where the test aims it: writes attempted "
        f"{record['attempted']}, expected {after} to land and one to be refused")


def _tear_revision_two(*, after: int) -> dict:
    """A session at revision 1 holding 'first', interrupted `after` writes into revision 2."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"problem": _slot(10, "explicit", "low", "first")}))

    model = store.load_session_model("s")
    model.model["problem"].value = "second"

    with _crashing_after(after) as record:
        with pytest.raises(_InjectedCrash):
            store.save_revision("s", model)
    return record


def _tear_first_apply(slug: str, *, after: int) -> dict:
    """The other shape, and a different arm of `check_session`: a session at revision **0**,
    interrupted `after` writes into its very first revision. `migrate_legacy` takes this path too.

    Takes its slug, because `create_session` is an atomic claim (invariant 11) and a second tear in
    one test would otherwise lose the rename to the first."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)

    with _crashing_after(after) as record:
        with pytest.raises(_InjectedCrash):
            svc.update_model(slug, _full_model(**{"problem": _slot(10, "explicit", "low", "one")}))
    return record


def _current_model_is_the_recorded_revision(slug: str) -> bool:
    """Does model.json hold the content session.json says the current revision holds?"""
    meta = store.read_meta(slug)
    d = store.canonical_dir(slug)
    recorded = meta.revisions[meta.current_revision - 1].model_hash
    payload = (d / "model.json").read_text(encoding="utf-8")
    return store.content_hash(payload) == recorded


def test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision(workspace):
    """The window `save_revision` writes the frozen revision file first in order to make benign. With
model.json written first, a death in this window left every read path serving content no revision
records while session.json, the provenance log and the freshness machinery all believed the
session was still at the previous revision."""
    record = _tear_revision_two(after=1)

    # The claim: every read path still serves the content session.json accounts for. The helper has
    # already asserted that a second write was attempted and refused, so this is not passing because
    # the injection fired too early and nothing was torn at all.
    assert store.read_meta("s").current_revision == 1
    assert _current_model_is_the_recorded_revision("s"), (
        "model.json holds content no revision records, and the metadata does not say so")
    assert store.load_session_model("s").model["problem"].value == "first"

    codes = {p.code for p in check_session("s")}
    assert codes == {"orphan_revision_file"}, (
        f"a crash in this window must leave at most an orphan revision file, got {sorted(codes)}")

    # The mechanism behind it, asserted separately so a failure above reads as the defect rather
    # than as a rearranged implementation: the one write that landed went to `revisions/`, which no
    # read path consults, and it carries the content of the revision that never completed.
    assert record["attempted"][0] == "0002-model.json", (
        "the first payload write is model.json again — the reorder is gone")
    orphan = store.canonical_dir("s") / "revisions" / "0002-model.json"
    assert orphan.is_file() and "second" in orphan.read_text(encoding="utf-8")


def test_a_crash_after_both_payload_writes_is_still_reported_as_inconsistent(workspace):
    """The must-fire half, and the honest limit of the reorder. Reordering shrinks the inconsistent
window; it does not close it. Once *both* payloads are on disk and session.json is not,
model.json genuinely is content the metadata does not account for, and that has to keep being
reported — the same verdict, in the same words, as before the reorder."""
    _tear_revision_two(after=2)

    assert store.read_meta("s").current_revision == 1
    assert not _current_model_is_the_recorded_revision("s")
    codes = {p.code for p in check_session("s")}
    assert codes == {"orphan_revision_file", "model_is_not_the_last_revision"}, (
        f"the window that is still inconsistent stopped saying so: {sorted(codes)}")


def test_the_next_apply_reclaims_the_orphan_and_verifies_clean(workspace):
    """A tear is survivable because the revision number was never spent. session.json still says 1,
    so the next apply mints 2 again and `_atomic_write` replaces the orphan rather than colliding
    with it — no manual repair, and `verify` is clean afterwards."""
    _tear_revision_two(after=1)
    assert {p.code for p in check_session("s")} == {"orphan_revision_file"}

    svc = SessionService()
    svc.update_model("s", _full_model(**{"problem": _slot(20, "explicit", "low", "healed")}))

    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert store.load_session_model("s").model["problem"].value == "healed"
    assert store.load_revision_model("s", 2).model["problem"].value == "healed", (
        "the orphan was left holding the content of the revision that never landed")
    assert check_session("s") == []


def test_a_crash_in_the_very_first_apply_leaves_a_session_still_at_revision_zero(workspace):
    """The same two gaps on the revision 0 → 1 path, which is a *different* arm of `check_session` and
the one `migrate_legacy` takes. Worth its own test because the codes differ: with no revision
recorded yet there is no hash to compare a current model against, so the second gap reports
`model_without_revision` rather than `model_is_not_the_last_revision`."""
    _tear_first_apply("gap-one", after=1)

    assert store.read_meta("gap-one").current_revision == 0
    assert not (store.canonical_dir("gap-one") / "model.json").exists(), (
        "model.json was written before the frozen revision file — a model at revision 0")
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("gap-one")     # "no model yet", which is the truth
    assert {p.code for p in check_session("gap-one")} == {"orphan_revision_file"}

    # And the second gap, which the reorder does not close: model.json is now on disk with the
    # metadata still at revision 0, and `check_session` has to keep saying so.
    _tear_first_apply("gap-two", after=2)
    assert store.read_meta("gap-two").current_revision == 0
    assert {p.code for p in check_session("gap-two")} == {"orphan_revision_file",
                                                          "model_without_revision"}
