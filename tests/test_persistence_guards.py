"""Guards in `core/persistence/` that lived one layer above the function that needed them.

Split into two modules by #550, at the seam its own docstring already named ("the file is worth
reading as two"): this half is `#4`'s atomic slug claim (`migrate_legacy` must lose to a live
session rather than overwrite it, invariant 11) plus the first part of the artifact-path chokepoint
-- the filename-as-write-target validation (#5), the end-of-line anchor (#40), the read side of the
chokepoint (#23), the atomic-write newline guard (#464), and session locking (#22). What `#22`'s
`session_lock` locking tests need from #23's `_session` helper is duplicated in
`test_persistence_guards_ii.py` rather than shared, since that half also needs it and a cross-module
import for one eight-line helper would be a second, thinner coupling for no shorter a file.

`test_persistence_guards_ii.py` continues with `#238` (session delete), `#36` (the two display-only
path joins), the slug/model-loader group `#72` added at the foot, and `#261` (what a half-finished
`save_revision` leaves behind).

What every group here shares is the shape of the defect: a rule stated at the callers that happened
to be careful, in a store whose threat model is the caller that is not one of them. Offline, like
the rest of the session tests: a temp workspace via REQUIVO_WORKSPACE.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

# The one control in this repo that can actually move the ambient default encoding, measured rather
# than assumed. Borrowed rather than restated: two copies of a probe like this drift, and the copy
# that drifts is the one that silently stops firing.
from test_boundaries import _force_default_encoding

from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError
from requivo.core.integrity import check_session

# `_acquire`/`_release`/`_LOCK_TIMEOUT_SECONDS` moved to `core/persistence/lock.py` by #550, and
# `Store.session_lock` (in `lock.py`'s own `_LockMixin`) reads them off *that* module's globals --
# patching the package-level re-export (`store._acquire`) is a second binding that does not reach
# the call site, so the lock-behaviour tests below patch this module directly instead.
from requivo.core.persistence import lock as store_lock

# Same reasoning, for `_atomic_write`: `Store.save_revision`/`create_session`/`write_meta` (in
# `core/persistence/store.py`) import it from `atomic.py` and call the bare name, which resolves in
# `store.py`'s own globals -- not the package-level re-export, and not `atomic.py`'s own, either.
from requivo.deterministic._shared import EXIT_DEGRADED
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate the legacy root too
    return tmp_path


def _legacy(slug: str, marker: str) -> None:
    """A legacy out/<slug>/ session whose `problem` slot is identifiable."""
    d = store.legacy_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text(json.dumps(
        _full_model(**{"problem": _slot(10, "inferred", "low", marker)})))


def _problem(slug: str, revision: int | None = None) -> str:
    out = (store.load_session_model(slug) if revision is None
           else store.load_revision_model(slug, revision))
    return out.model["problem"].value


# ── #4: migrate_legacy() must not overwrite a live session ──────────────────────


def test_migrating_onto_a_live_session_is_refused_rather_than_overwriting_it(workspace):
    """`migrate_legacy` checked only that the *legacy* model existed. Pointed at a slug a real session
    already occupies, it rewrote session.json at current_revision 0 and then wrote the legacy model
    over revisions/0001-model.json — and revisions/ is the only durable copy, so revision 1 was gone
    with no copy anywhere. The refusal belongs inside the function: the single in-repo caller guarded
    with a preceding existence check, which invariant 11 forbids precisely because it is not held
    across the write, and every other caller had no guard at all."""
    svc = SessionService()
    svc.create_session("A real request.", slug="dup")
    svc.update_model("dup", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL v1")}))
    svc.update_model("dup", _full_model(**{"problem": _slot(90, "explicit", "high", "REAL v2")}))
    _legacy("dup", "LEGACY")

    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("dup")
    assert ei.value.code == "session_exists"

    # The live session is untouched: the revision count, both revision files, and the current model.
    meta = store.read_meta("dup")
    assert meta.current_revision == 2
    assert [r.revision for r in meta.revisions] == [1, 2]
    assert _problem("dup", 1) == "REAL v1"
    assert _problem("dup", 2) == "REAL v2"
    assert _problem("dup") == "REAL v2"
    # …and it still tells the truth about itself — the overwrite left an orphan_revision_file behind.
    assert [p.code for p in check_session("dup")] == []
    # The legacy originals are preserved on the refusal, as they are on the success path.
    assert (store.legacy_dir("dup") / "model.json").exists()


def test_migrating_onto_a_slug_claimed_at_revision_zero_is_refused_too(workspace):
    """The other half of the claim. A session created but never analysed holds no revision to destroy,
    yet it is still somebody's session — its id, provider and context cards were claimed by a
    `create_session` that won the slug. A refusal keyed on `current_revision > 0` would take the slug
    out from under it, which is the bug invariant 11 already describes for two concurrent creations."""
    SessionService().create_session("A real request.", slug="fresh", provider="claude-code")
    claimed = store.read_meta("fresh").session_id
    _legacy("fresh", "LEGACY")

    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("fresh")
    assert ei.value.code == "session_exists"
    meta = store.read_meta("fresh")
    assert meta.session_id == claimed
    assert meta.current_revision == 0
    assert meta.provider == "claude-code"


def test_migrating_a_free_slug_still_works(workspace):
    """The positive control for both refusals above: without it, a `migrate_legacy` that raised
    unconditionally would satisfy every assertion in this section."""
    _legacy("free", "LEGACY")
    legacy = store.legacy_dir("free")
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")
    (legacy / "session.json").write_text(json.dumps(
        {"created_at": "2026-01-02T03:04:05Z", "provider": "anthropic", "model_name": "claude-x"}))

    meta = store.migrate_legacy("free")
    assert meta.current_revision == 1
    assert _problem("free", 1) == "LEGACY"
    assert _problem("free") == "LEGACY"
    assert meta.artifact_status["prd"].revision == 1
    assert (store.canonical_dir("free") / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"
    assert (store.canonical_dir("free") / "request.md").read_text(encoding="utf-8") == "Legacy request."
    assert [p.code for p in check_session("free")] == []
    assert (legacy / "model.json").exists()   # the originals are preserved

    # Provenance recovered from the legacy session.json, not invented.
    assert meta.created_at == "2026-01-02T03:04:05Z"
    assert (meta.provider, meta.model_name) == ("anthropic", "claude-x")
    # The session id stays derived from the slug, so a migrated session has a stable identity.
    assert meta.session_id == store.read_meta("free").session_id


def test_the_bulk_migrate_command_skips_a_slug_that_is_already_taken(workspace, capsys):
    """The sweep reports `migrated` and `skipped_already_present`, so a refusal has to degrade that one
    row rather than abort the pass — the rule invariant 15 states for a listing, applied to a loop."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    svc = SessionService()
    svc.create_session("A real request.", slug="aaa-taken")
    svc.update_model("aaa-taken", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL")}))
    _legacy("aaa-taken", "LEGACY")
    _legacy("zzz-free", "LEGACY")

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"] == ["zzz-free"]
    assert out["skipped_already_present"] == ["aaa-taken"]
    assert _problem("aaa-taken", 1) == "REAL"
    assert _problem("zzz-free", 1) == "LEGACY"


def test_the_bulk_migrate_command_degrades_a_bad_legacy_session_rather_than_aborting(workspace, capsys):
    """#262. One legacy session with an unparseable `model.json` must not abort the whole pass --
    the docstring on `_cmd_session_migrate` used to admit exactly this gap. The two healthy sessions
    sorted before and after the bad one (alphabetically, so both sides of the loop are exercised)
    still migrate, and the bad one is named with its own error rather than silently dropped, per
    invariant 15's "a listing survives its own members" applied to this loop. A must-fire control:
    without the fix this test's own `pytest.raises(SystemExit)` around the first call never returns,
    because the unhandled `ModelUnreadableError` aborts the process before any JSON is printed."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("aaa-first", "FIRST")
    d = store.legacy_dir("mmm-corrupt")
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text("{not valid json", encoding="utf-8")
    _legacy("zzz-last", "LAST")

    with pytest.raises(SystemExit) as ei:
        _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    assert ei.value.code == EXIT_DEGRADED
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["migrated"]) == ["aaa-first", "zzz-last"]
    assert out["skipped_already_present"] == []
    assert [e["slug"] for e in out["errors"]] == ["mmm-corrupt"]
    assert out["errors"][0]["error"]   # the structured message, not just the slug
    assert _problem("aaa-first", 1) == "FIRST"
    assert _problem("zzz-last", 1) == "LAST"


def test_an_interrupted_migration_is_reported_distinctly_from_already_present(workspace, capsys):
    """#262. `migrate_legacy` claims the slug via `create_session` and only afterwards, under a
    separate lock, applies the model -- a crash between the two leaves a revision-0 shell occupying
    the canonical slug with the legacy model never copied. That must not render as
    `skipped_already_present`, which means the work is done: it is a different fact, and folding the
    two together is the false receipt this issue is about. A must-not-fire control sits beside it:
    `test_the_bulk_migrate_command_skips_a_slug_that_is_already_taken` builds a *genuinely* migrated
    session (current_revision 1) onto a legacy slug and asserts it stays in `skipped_already_present`
    -- so this test only proves something if that one still passes too."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("half-done", "NEVER-COPIED")
    # The crash window `migrate_legacy` documents: the slug is claimed but the model was never
    # applied, so the canonical session sits at revision 0. Empty request text, not an arbitrary one
    # -- `_legacy` writes no request.md/request.txt, so `migrate_legacy` would have claimed this slug
    # with request="" (its own fallback), and the interrupted/unrelated discriminator compares
    # exactly this against the legacy request text. An arbitrary request here would make this fixture
    # indistinguishable from the *unrelated*-session case the sibling test below covers.
    SessionService().create_session("", slug="half-done")

    with pytest.raises(SystemExit) as ei:
        _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    assert ei.value.code == EXIT_DEGRADED
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"] == []
    assert out["skipped_already_present"] == []
    assert out["interrupted"] == ["half-done"]
    # The legacy model was never copied in -- the canonical session is still empty, current_revision 0.
    assert SessionService().repo.read_meta("half-done").current_revision == 0


def test_a_canonical_session_that_cannot_be_read_is_reported_not_crashed(workspace, capsys):
    """Found in review of #262 itself. `repo.read_meta(slug)` -- the call that decides
    `skipped_already_present` vs `interrupted` for an occupied slug -- can itself raise
    `SessionUnreadableError` when the *canonical* session's own `session.json` is corrupt, and that
    call sat outside the loop's per-slug isolation: an unreadable canonical session for an occupied
    legacy slug aborted the whole pass exactly the way an unparseable *legacy* `model.json` did before
    #262 was filed -- the identical defect, one call away from the one the issue named. Must-fire:
    every other legacy session in the sweep (sorted before and after the unreadable one) still
    migrates, and the run exits with a receipt rather than an unhandled `SessionUnreadableError`."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("aaa-first", "FIRST")
    _legacy("broken-canonical", "NEVER-READ")
    store.canonical_dir("broken-canonical").mkdir(parents=True, exist_ok=True)
    (store.canonical_dir("broken-canonical") / "session.json").write_text(
        "{not valid json", encoding="utf-8")
    _legacy("zzz-last", "LAST")

    with pytest.raises(SystemExit) as ei:
        _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    assert ei.value.code == EXIT_DEGRADED
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["migrated"]) == ["aaa-first", "zzz-last"]
    assert [e["slug"] for e in out["errors"]] == ["broken-canonical"]
    assert _problem("aaa-first", 1) == "FIRST"
    assert _problem("zzz-last", 1) == "LAST"


def test_the_bulk_migrate_command_degrades_an_unreadable_legacy_directory_rather_than_crashing(
        workspace, capsys, request):
    """#411. The scan that PRODUCES the per-slug rows -- `root.iterdir()` filtered on
    `(p / "model.json").exists()` -- sits outside every per-slug guard #371 hardened, and
    `Path.exists()` re-raises EACCES. One legacy directory the process cannot stat into used to
    abort the whole pass with a raw `PermissionError` before any receipt was printed at all --
    invariant 15's own generalisation, one layer below where #371 already closed it once.

    Must-fire control: a healthy legacy session sorted on each side of the blocked one still
    migrates, so a fix that lost coverage of the loop body would not pass this test by accident."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("aaa-first", "FIRST")
    d = store.output_root() / "mmm-blocked"
    d.mkdir(parents=True, exist_ok=True)
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that the "
                    "legacy-root scan reports an unexaminable entry as a fact rather than as an "
                    "exception. Every other platform runs it.")
    d.chmod(0o000)
    try:
        (d / "model.json").exists()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny the model.json probe on this run (running as root?). "
                    "UNTESTED HERE: the could-not-examine arm of the legacy-root scan.")
    _legacy("zzz-last", "LAST")

    with pytest.raises(SystemExit) as ei:
        _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    assert ei.value.code == EXIT_DEGRADED
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["migrated"]) == ["aaa-first", "zzz-last"]
    assert [e["name"] for e in out["unreadable"]] == ["mmm-blocked"]
    assert out["unreadable"][0]["error"]
    assert _problem("aaa-first", 1) == "FIRST"
    assert _problem("zzz-last", 1) == "LAST"


def test_a_totally_unlistable_legacy_root_refuses_cleanly_instead_of_crashing(workspace, request):
    """Found in review of #411 itself. Wrapping only the per-entry probe inside `_scan_legacy_root`
    left `root.iterdir()` itself -- the call that lists the root in the first place -- outside any
    guard: a legacy `out/` root the process cannot even open into is a distinct case from one
    unreadable *entry* inside an otherwise-listable root (the sibling test above,
    test_the_bulk_migrate_command_degrades_an_unreadable_legacy_directory_rather_than_crashing),
    and it raised an uncaught `PermissionError` past `_cmd_session_migrate` -- the identical crash
    #411 was filed to fix, one level up. `_scan_legacy_root` still raises for this case,
    deliberately, on the same terms `_scan_session_root` already states for the canonical root;
    what changed is that the caller now turns that into a clean `SessionUnreadableError` (a
    `RequivoError`), which `cli.py`'s `app()` already knows how to report and exit 1 for -- "no
    answer", since nothing here was even examined, distinct from the partial-answer
    `EXIT_DEGRADED` the sibling test above exits with."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that a "
                    "totally unlistable legacy root converts an uncaught PermissionError into a "
                    "clean SessionUnreadableError rather than a bare traceback.")
    out_dir = store.output_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    request.addfinalizer(lambda: out_dir.chmod(0o755))
    out_dir.chmod(0o000)
    try:
        list(out_dir.iterdir())
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny iterdir() on this run (running as root?). UNTESTED "
                    "HERE: the whole-root-unlistable arm of the legacy-root scan.")

    with pytest.raises(RequivoError) as ei:
        _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    assert str(out_dir) in str(ei.value)


def test_an_unrelated_revision_zero_session_at_a_legacy_slug_is_not_called_interrupted(
        workspace, capsys):
    """Found in review of #262 itself. `current_revision == 0` alone is not evidence of a crashed
    migrate -- any ordinary session (`session init`, or discovery not yet through its first turn) can
    legitimately sit at revision 0, and if its slug happens to coincide with an `out/` legacy
    directory's, `interrupted`'s own printed remedy ("delete .requivo/sessions/<slug> and re-run")
    would destroy that session's real, unrelated work. The discriminator is the request hash:
    `create_session` stamps `request_hash` from the exact request text it is passed, so a genuine
    crash window (where `migrate_legacy` claimed the slug with the *legacy* request text) leaves that
    hash identical to the legacy request's -- and an unrelated session, created with its own request,
    does not match. Must-not-fire: the same slug, occupied by a session with a different request,
    stays `skipped_already_present`, never `interrupted`."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    d = store.legacy_dir("shared-slug")
    d.mkdir(parents=True, exist_ok=True)
    (d / "request.md").write_text("The legacy request text.", encoding="utf-8")
    (d / "model.json").write_text(json.dumps(
        _full_model(**{"problem": _slot(10, "inferred", "low", "LEGACY")})))

    SessionService().create_session("A completely different, unrelated request.", slug="shared-slug")

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    out = json.loads(capsys.readouterr().out)
    assert out["interrupted"] == []
    assert out["skipped_already_present"] == ["shared-slug"]


# ── #5: filename is a write target, so it is validated like its slug sibling ─────


# Every shape that is not a filename: traversal, a bare dot segment, both separators, an absolute
# path, a dot-prefixed name (which the staging convention and `list_session_slugs` reserve), and the
# empty string. A backslash is a separator on Windows and an ordinary character on POSIX, so it is
# refused on both rather than only on the one where it happens to escape.
ESCAPES = [
    "../../../../ESCAPED.md",
    "..",
    "sub/nested.md",
    "sub\\nested.md",
    "/etc/passwd",
    ".hidden.md",
    "",
]


def test_write_artifact_file_refuses_a_filename_that_is_not_a_filename(workspace):
    """`slug` is validated at this chokepoint so that "every surface inherits the same
    directory-traversal guard, not just FastAPI" — and the sibling parameter on the same mutating call
    had none. Nothing in-repo can reach it (every caller passes a literal or an ARTIFACT_FILENAMES
    lookup), so the test drives the function directly, which is exactly invariant 14's threat model:
    the external consumer calling the service, not the CLI being careful."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav")
    svc.update_model("trav", _full_model())
    artifacts = store.canonical_dir("trav") / "artifacts"

    # Positive control first: an ordinary export name still lands where it should. Without it, a
    # `write_artifact_file` that refused everything would satisfy every assertion below.
    assert store.write_artifact_file("trav", "epic.github.json", "{}") == artifacts / "epic.github.json"

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.write_artifact_file("trav", name, "pwned")
        assert ei.value.code == "invalid_filename", name
    assert not (workspace / "ESCAPED.md").exists()
    assert sorted(p.name for p in artifacts.iterdir()) == ["epic.github.json"]


def test_save_session_artifact_refuses_it_too_and_records_nothing(workspace):
    """The recorded filename is read back by `integrity.py` and by the artifact-show paths, so a
    poisoned value persists and is re-consumed. The refusal has to land before session.json is
    rewritten, not after."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav2")
    svc.update_model("trav2", _full_model())

    st = store.save_session_artifact("trav2", "brief", "solution-assessment.md", "# A\n",
                                     source_revision=1)
    assert st.revision == 1

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.save_session_artifact("trav2", "prd", name, "pwned", source_revision=1)
        assert ei.value.code == "invalid_filename", name

    assert not (workspace / "ESCAPED.md").exists()
    meta = store.read_meta("trav2")
    assert set(meta.artifact_status) == {"brief"}   # nothing recorded for the refused writes
    assert [p.code for p in check_session("trav2")] == []


def test_a_too_long_filename_is_refused_at_the_boundary(workspace):
    """Length is part of validity for a slug for a stated reason: the filesystem refuses an over-long
    name deep inside a write as a bare OSError instead of at the boundary. The same holds one argument
    over, and it is the one vector the traversal pattern alone does not cover."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav3")
    svc.update_model("trav3", _full_model())
    with pytest.raises(RequivoError) as ei:
        store.write_artifact_file("trav3", "a" * 300 + ".md", "x")
    assert ei.value.code == "invalid_filename"


# ── #40 (adjacent): the end-of-line anchor is not the end of the string ──────────


def test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline(workspace):
    """Found while fixing #40, and outside its footprint — called out rather than slipped in.

    Both `_SLUG_RE` and `_FILENAME_RE` ended in the end-of-line anchor, which in Python matches at
    the end of the string **or just before a trailing newline**. So a slug and a filename each ending
    in one newline were returned unchanged: two guards whose whole job is to make a separator or a
    control character unrepresentable, admitting one. The end-of-string anchor is what both
    docstrings already claim.

    One character in each pattern, and the same defect class as #40 — untrusted text carrying a line
    break past a guard — which is why it is fixed here rather than filed.
    """
    # must fire: every name the store actually writes still passes both guards
    assert store.validate_slug("leave-approval") == "leave-approval"
    for name in sorted(ARTIFACT_FILENAMES.values()) + ["epic.github.json"]:
        assert store.validate_filename(name) == name

    # must not fire: a trailing newline is not a valid name, and never was meant to be
    for bad in ("ok\n", "ok\r", "leave-approval\n"):
        with pytest.raises(RequivoError) as ei:
            store.validate_slug(bad)
        assert ei.value.code == "invalid_slug", repr(bad)
    for bad in ("prd.md\n", "prd.md\r"):
        with pytest.raises(RequivoError) as ei:
            store.validate_filename(bad)
        assert ei.value.code == "invalid_filename", repr(bad)


def test_integrity_cannot_be_made_to_print_a_line_break_by_a_recorded_filename(workspace):
    """The reachable consequence of the anchor above, and why it earns a test rather than a note.

    `integrity.py` renders the recorded filename with `!r` on three of its four lines and **bare** on
    the fourth — the one that says `artifacts/<name> is missing`. That line is guarded: it sits on
    the `elif` behind `validate_filename`, so it is only reachable by a name the guard accepted,
    which is exactly what the end-of-line anchor allowed. One trailing newline is limited leverage,
    but a receipt line that splits in two is a line the program did not write.
    """
    svc = SessionService()
    svc.create_session("Something.", slug="anch")
    svc.update_model("anch", _full_model())
    store.save_session_artifact("anch", "prd", "prd.md", "# P\n", source_revision=1)

    # must fire: a genuinely missing artifact is still reported, on one line
    (store.canonical_dir("anch") / "artifacts" / "prd.md").unlink()
    problems = check_session("anch")
    assert [p.code for p in problems] == ["missing_artifact_file"]
    assert "\n" not in problems[0].message

    # must not fire: a recorded name carrying a newline cannot split that line
    p = store.canonical_dir("anch") / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["artifact_status"]["prd"]["filename"] = "prd.md\n"
    p.write_text(json.dumps(meta), encoding="utf-8")
    reported = check_session("anch")
    assert reported, "must fire: the tampered name is still reported"
    for problem in reported:
        assert "\n" not in problem.message, problem.code


# ── #23: the same filename is a read target, and a refusal is not an absence ─────


def _session(slug: str) -> FileSessionRepository:
    """A session at revision 1, plus the repository an external consumer would hold."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, _full_model())
    return FileSessionRepository()


def test_load_artifact_refuses_a_traversal_rather_than_disclosing_the_file(workspace):
    """The read-side sibling of the two write paths above, and a different question: the write fix
    answers what this code may *create*, a read traversal answers what it may *disclose*.

    `FileSessionRepository.load_artifact` re-joined `canonical_dir(slug) / "artifacts" / filename`
    inline rather than going through `artifact_path`, one layer above the chokepoint — which is
    exactly why the sweep that closed the writes in #21 did not reach it.

    Driven through the repository directly, which is invariant 14's threat model verbatim: every
    in-repo caller arrives via `ArtifactService.show` with an `ARTIFACT_FILENAMES` lookup, and
    `requivo-cloud` reuses Core as a dependency and is the consumer that does not."""
    repo = _session("read-trav")

    # ESCAPES[0] resolves four levels up from artifacts/, i.e. to <workspace>. Put a real, readable
    # file exactly there: without it, a `load_artifact` that merely failed to *find* anything would
    # satisfy every assertion below, and the test would prove nothing about refusal.
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    assert (workspace / "ESCAPED.md").read_text(encoding="utf-8") == "TOP SECRET"

    # Positive control: a legitimate ARTIFACT_FILENAMES value still loads, byte for byte.
    store.save_session_artifact("read-trav", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)
    assert repo.load_artifact("read-trav", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"

    # The over-long name rides the same guard here as on the write side: it is the one vector the
    # traversal shapes do not cover, and a read of it fails as a bare OSError without the boundary.
    for name in ESCAPES + ["a" * 300 + ".md"]:
        with pytest.raises(RequivoError) as ei:
            repo.load_artifact("read-trav", name)
        assert ei.value.code == "invalid_filename", name
        # The refusal has to name what it refused, or a caller holding several names cannot tell
        # which one was rejected. Read off `details` rather than the message: the length branch of
        # `validate_filename` states the count and not the name, and truncates the one it records.
        # Asserting instead that the secret is absent from the message would be unfalsifiable — the
        # raise happens before any read, so no content is ever in scope for the message to leak.
        assert name.startswith(ei.value.details["filename"]), name


def test_a_refused_read_raises_where_a_missing_artifact_returns_none(workspace):
    """The judgment this issue turned on. `artifact_path()` raises and `load_artifact` returns None,
    so routing one through the other forces a choice, and the tempting one is the quiet answer:
    returning None for a rejected traversal too would make it indistinguishable from an artifact
    nobody has generated yet. That is not hypothetical — reproducing the defect, a traversal that
    resolved to no file returned None exactly as an ungenerated artifact does, and only the depth of
    the `..` chain separated disclosure from a plausible-looking absence.

    So all three states are asserted together, because each only means anything against the other
    two: content for a saved artifact, None for a legitimate name with no file behind it, and a
    raise for a name that is not a filename."""
    repo = _session("read-3state")
    store.save_session_artifact("read-3state", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)

    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"
    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["prd"]) is None   # never generated

    with pytest.raises(RequivoError) as ei:
        repo.load_artifact("read-3state", "../../../../ESCAPED.md")
    assert ei.value.code == "invalid_filename"


def test_core_owns_the_read_guard_so_the_next_reader_cannot_forget_it(workspace):
    """#21 put the write guard at `artifact_path()` in Core rather than at its callers, for the
    reason `_child_of` gives: a rule applied per-caller is a rule the next caller forgets. The read
    side is that sentence's own proof, so the fix goes to Core too and this drives Core directly
    rather than through the adapter — a guard that lived only in `FileSessionRepository` would leave
    Core with a write-only chokepoint and the next reader re-joining the path a third time."""
    _session("read-core")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")

    store.write_artifact_file("read-core", "epic.github.json", "{}")
    assert store.read_artifact_file("read-core", "epic.github.json") == "{}"
    assert store.read_artifact_file("read-core", "epic.json") is None   # a real name, no file

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.read_artifact_file("read-core", name)
        assert ei.value.code == "invalid_filename", name


def test_an_artifact_round_trips_non_ascii_content(workspace, monkeypatch):
    """The other half of the line this change rewrites. `_atomic_write` passes `encoding="utf-8"`
    explicitly and the read beside it passed none, so it decoded with the *locale's* — `LC_ALL=C`, or
    a DBCS Windows shell, and a generated artifact dies on its first em-dash. Every artifact this
    engine writes is full of them.

    The plain round trip below is only a regression pin: on a UTF-8 locale it passes with or without
    the explicit `encoding=`, which is to say the control cannot fire. So the fallback is forced and
    *measured* first, reusing `test_boundaries`' helper rather than restating it — the same shape,
    and the same loud skip where the force does not take, because a control that cannot fail is worse
    than no control."""
    repo = _session("read-utf8")
    body = "# Brief\n\nAn em-dash — a café — and a curly quote: “ready”.\n"
    store.save_session_artifact("read-utf8", "brief", ARTIFACT_FILENAMES["brief"], body,
                                source_revision=1)
    assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body

    # Everything above is set up under the ambient encoding; only the read is forced, so a session.json
    # or model_schema.json read cannot fail for reasons that have nothing to do with the artifact.
    with monkeypatch.context() as m:
        if not _force_default_encoding(m, workspace, "ascii"):
            pytest.skip(
                "the ambient default encoding could not be forced on this interpreter (CPython "
                "dropped _bootlocale in 3.10 and resolves the locale encoding in C), so this control "
                "cannot fire here. UNTESTED ON THIS INTERPRETER: that read_artifact_file passes an "
                "explicit encoding rather than taking the locale's. The 3.9 leg of the CI matrix "
                "does test it."
            )
        p = store.artifact_path("read-utf8", ARTIFACT_FILENAMES["brief"])
        with pytest.raises(UnicodeDecodeError):
            # Deliberately bare: this read IS the thing under test, performing the defect so the
            # assertion can catch it. Passing `encoding=` here would bypass the forced locale
            # entirely and the `raises` could never fire -- which is exactly what a mechanical sweep
            # did to it, invisibly on 3.10+ (where the force does not take and the test skips) and
            # fatally on the 3.9 leg. Registered in `_LOCALE_DEFAULT_BY_DESIGN` in test_encoding.py.
            p.read_text()   # what the repository's own line did, meeting the locale it would meet
        assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body


# ── #464: _atomic_write must disable universal-newline translation on write ──────


def test_atomic_write_passes_newline_empty_to_disable_translation(tmp_path, monkeypatch):
    """#464. `_atomic_write` wrote via `tmp.write_text(content, encoding="utf-8")` -- text mode with
    `newline=None`, which on write translates every '\n' character in the content to `os.linesep`.
    On POSIX `os.linesep` is '\n', so the translation is a no-op and invisible to this suite; on
    Windows it is '\r\n', and a lone CR already in the content (a provider reply that carries one --
    see #460) becomes '\r\r\n' on disk, a line the document never had. `newline=""` disables the
    translation outright and writes the string's own bytes on every platform -- the direct analogue
    of what `encoding="utf-8"` already does one keyword along (invariant 16).

    The corruption itself cannot be reproduced by running this suite on this platform: confirmed by
    hand before writing this test that monkeypatching `os.linesep` before an ordinary
    `open(path, "w")` changes nothing about what lands on disk here -- the translation target is not
    read from the mutable `os.linesep` attribute at call time on this interpreter. So the assertion
    below is on the mechanism the issue's own fix direction names -- which `newline=` keyword reaches
    the write -- rather than on the corrupted bytes themselves. REASONED, NOT OBSERVED that this
    prevents the Windows corruption; OBSERVED that the call now asks for no translation at all.

    Spied on `Path.open` and not on `Path.write_text` (#469). The first fix wrote
    `write_text(content, encoding="utf-8", newline="")`, and that keyword reached `write_text` only
    in 3.10 while this project's floor is 3.9 -- a `TypeError` on every write, on the one function
    every persistence path calls, which is 518 failures on each of three CI legs. The write goes
    through `.open()` now, which has always taken `newline=`, so this spy follows it there. The
    version rule itself is guarded as a class rather than at this one site, by
    `test_no_text_call_passes_a_keyword_the_declared_floor_rejects` in tests/test_encoding.py."""
    captured = {}
    real_open = Path.open

    def spy(self, *args, **kwargs):
        if ".probe.json." in self.name:
            captured["newline"] = kwargs.get("newline", "NOT PASSED")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    store._atomic_write(tmp_path / "probe.json", "a line\r\nwith an embedded CR\r\nand a plain one\n")

    assert "newline" in captured, "the write to the temp file was never observed by the spy"
    assert captured["newline"] == "", (
        f"_atomic_write must pass newline='' to disable the universal-newline translation that "
        f"corrupts a lone CR on Windows; got newline={captured['newline']!r}")


def test_atomic_write_still_writes_the_content_correctly_with_translation_disabled(tmp_path):
    """Positive control for the assertion above: `newline=""` must not simply break the write. An
    ordinary document with only '\n' line endings must round-trip byte-for-byte, exactly as it did
    before -- the fix disables a translation that was already a no-op for this content on POSIX, so
    this must stay true after it."""
    store._atomic_write(tmp_path / "doc.md", "# Title\n\nA body line.\n")
    assert (tmp_path / "doc.md").read_bytes() == b"# Title\n\nA body line.\n"


# ── #22: session_lock() must not materialise the session it guards ──────────────


def _ghost_locking_calls() -> dict:
    """Every route that takes the session lock on a slug the caller has not proven exists.

    Named as a table rather than tested one by one because the defect was never in any of them: it
    was in the lock, and each of these is only a way to reach it. A route added later that locks
    before it reads belongs here, not in a test of its own."""

    def take_the_lock(slug):
        with store.session_lock(slug):
            pass

    # The keys become slugs, so they are hyphenated: an underscore is not a legal slug character and
    # `validate_slug` would refuse the name before the lock could be reached at all.
    return {
        "session-lock": take_the_lock,
        "save-revision": lambda slug: store.save_revision(slug, _engine_output()),
        "save-session-artifact": lambda slug: store.save_session_artifact(
            slug, "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1),
        "mark-stale": lambda slug: ArtifactService(FileSessionRepository()).mark_stale(slug, ["problem"]),
    }


def _engine_output(**overrides):
    from requivo.core.contracts import EngineOutput
    return EngineOutput.model_validate(_full_model(**overrides))


def test_a_lock_on_a_slug_with_no_session_leaves_no_trace(workspace):
    """The lock created `canonical_dir(slug)` before opening `.lock` inside it, so taking it on a slug
    with no session left a directory behind holding nothing else. That directory is invisible to
    `list_session_slugs` (no session.json) and non-empty, so `create_session`'s rename — the *only*
    claim on a slug under invariant 11 — lost to a session nobody had created, and the user was told
    one already existed that neither they nor the tool could see.

    The property pinned here is the general one, not the symptom: a lock that fails, on a slug that
    has no session, leaves the store exactly as it found it. `real` is the positive control — without
    a session the same fixture *does* put on disk, every assertion below is also satisfied by a
    workspace pointed somewhere nothing is ever written."""
    SessionService().create_session("A real request.", slug="real")
    before = sorted(p.name for p in store.session_root().iterdir())
    assert before == ["real"], "the control session is not where this test is looking"

    for name, call in _ghost_locking_calls().items():
        with pytest.raises(RequivoError) as ei:
            call(f"ghost-{name}")
        assert ei.value.code == "session_not_found", name
        assert not store.canonical_dir(f"ghost-{name}").exists(), name

    assert sorted(p.name for p in store.session_root().iterdir()) == before
    assert store.list_session_slugs() == ["real"]


def test_a_session_deleted_before_the_lock_is_granted_is_refused(workspace, monkeypatch):
    """The race an existence check taken *before* the lock cannot close.

    This used to be closed by accident: the lock file lived inside the session, so `os.open` raised
    `FileNotFoundError` when the directory had gone and that arm mapped it onto "no such session".
    #113 moved the lock out of the session directory, and with it that accident — opening
    `.requivo/locks/<slug>.lock` says nothing at all about whether `<slug>` is a session. So the
    check moved to where it is authoritative, *after* the lock is held, and this test moved with it.

    The deletion is forced into the window rather than raced for, so the arm is executed on every leg
    of the matrix instead of being reasoned about. Patching `_acquire` puts it exactly where the
    check now is: the session exists when the fd is opened and is gone by the time the lock is
    granted — the one ordering the old arm could not have caught."""
    SessionService().create_session("A real request.", slug="vanishing")
    real_acquire = store_lock._acquire

    def deleting_acquire(fd, slug):
        shutil.rmtree(store.canonical_dir(slug))
        return real_acquire(fd, slug)

    monkeypatch.setattr(store_lock, "_acquire", deleting_acquire)

    with pytest.raises(RequivoError) as ei:
        with store.session_lock("vanishing"):
            pass                                    # pragma: no cover - the lock must not be granted
    assert ei.value.code == "session_not_found"
    assert not store.canonical_dir("vanishing").exists(), "the refusal must not recreate it"
    assert store.list_session_slugs() == []
    # And the lock file it left behind is outside the session root, so it takes no slug with it.
    assert store.lock_path("vanishing").exists()
    assert not store.lock_root().is_relative_to(store.session_root())


def test_a_slug_a_failed_lock_touched_can_still_be_created(workspace):
    """The reproduction from the issue, end to end. `list_session_slugs` and `create_session` have to
    agree about whether a slug is taken — the refusal was false precisely because they did not."""
    with pytest.raises(RequivoError):
        store.save_session_artifact("later", "brief", ARTIFACT_FILENAMES["brief"], "x", source_revision=1)

    assert "later" not in store.list_session_slugs()
    meta = store.create_session("later", "A request that arrives afterwards.")
    assert meta.current_revision == 0
    assert store.list_session_slugs() == ["later"]
    assert store.session_request("later") == "A request that arrives afterwards."


def test_a_migration_onto_such_a_slug_is_performed_not_reported_as_skipped(workspace, capsys):
    """Why this is more than a misleading message. `migrate_legacy` claims its slug through
    `create_session`, and the bulk sweep turns `SessionExistsError` into `skipped_already_present` —
    a row that reads as a decision. A ghost directory made the sweep report a session it had refused
    to migrate as one that was already there, and the legacy work silently never landed."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("stale-lock", "LEGACY")
    with pytest.raises(RequivoError):
        store.save_revision("stale-lock", _engine_output())

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migrated"] == ["stale-lock"]
    assert receipt["skipped_already_present"] == []
    assert _problem("stale-lock") == "LEGACY"


def test_the_lock_still_guards_a_session_that_exists(workspace):
    """The other direction, and the one a fix here can break silently. The lock's job is the compound
    mutations on sessions that *do* exist: `save_revision` and `save_session_artifact` write files
    under a session directory while holding it, and the service layer nests it around several core
    calls.

    The lock file itself lives at `.requivo/locks/<slug>.lock` since #113, *outside* the session it
    guards — that is what lets `session import --force` rename the directory while holding it. The
    session directory is asserted to be clean of one, because "the lock still works" and "the lock
    moved" have to be one test: a change that quietly put it back inside would pass either half
    alone."""
    svc = SessionService()
    svc.create_session("A real request.", slug="live")
    svc.update_model("live", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL")}))

    lock_file = store.lock_path("live")
    assert lock_file.exists(), "a writer that holds the lock leaves the lockfile behind"
    assert lock_file == store.lock_root() / "live.lock"
    assert not (store.canonical_dir("live") / ".lock").exists(), (
        "the lock is back inside the directory `session import --force` renames")

    with store.session_lock("live"):
        # Re-entrant within the thread: the service takes it around a whole update and every core
        # call inside takes it again. A guard that refused the second acquisition would deadlock.
        store.save_session_artifact("live", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n",
                                    source_revision=1)
        rev, meta = store.save_revision(
            "live", _engine_output(**{"problem": _slot(90, "explicit", "high", "REAL v2")}))

    assert (rev, meta.current_revision) == (2, 2)
    assert _problem("live") == "REAL v2"
    assert store.read_meta("live").artifact_status["brief"].revision == 1
    assert [p.code for p in check_session("live")] == []


@pytest.mark.skipif(store.fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows "
                     "equivalent here, and the msvcrt branch already had a bounded wait. "
                     "REASONED, NOT OBSERVED on Windows -- see #265.")
def test_a_contended_lock_raises_within_the_deadline_instead_of_hanging(workspace, monkeypatch):
    """#265. `_LOCK_TIMEOUT_SECONDS` was honoured only in the `msvcrt` branch; on POSIX,
    `fcntl.flock(fd, fcntl.LOCK_EX)` blocked forever with no message, so a stuck holder (a SIGSTOPped
    process, a debugger, an NFS-mounted workspace) froze the CLI on the primary platforms instead of
    raising the `SessionLockedError` Windows already had. The deadline is shortened so this proves
    the bound rather than the hang.

    The contending holder opens its own file descriptor on the same lock file rather than going
    through `session_lock` -- `flock` is scoped to the *open file description*, not the thread or
    the process, so a second `os.open` in this same test process contends for real, without needing
    a second process or thread to hold the lock."""
    SessionService().create_session("A real request.", slug="contended")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 0.3)

    lock_file = store.lock_path("contended")
    store.ensure_store_dir(lock_file.parent)
    holder_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    store.fcntl.flock(holder_fd, store.fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(RequivoError) as ei:
            with store.session_lock("contended"):
                pass  # pragma: no cover - must never be granted while the holder is live
        elapsed = time.monotonic() - started
    finally:
        store.fcntl.flock(holder_fd, store.fcntl.LOCK_UN)
        os.close(holder_fd)

    assert ei.value.code == "session_locked"
    assert "contended" in str(ei.value)
    # Bounded, not instant (a spin that returns before the holder ever really contended would prove
    # nothing) and not the unbounded hang it replaces (an unpatched 30s deadline here would make
    # this assertion the reason the whole suite takes half a minute to fail).
    assert 0.25 <= elapsed < 5.0, elapsed

    # The session is otherwise unharmed: once the holder releases, an ordinary acquisition succeeds.
    with store.session_lock("contended"):
        pass


def test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice(workspace,
                                                                                   monkeypatch):
    """The POSIX branch moved from one blocking `flock` call to a polling loop (#265); this pins that
    the re-entrancy invariant 9 relies on is unaffected, because it is decided one layer above
    `_acquire` and never reaches it on a nested call.

    `session_lock`'s own `_held_locks` depth counter is what makes nested acquisition safe -- a
    second `with session_lock(slug):` on the same thread increments the counter and returns without
    calling `_acquire` again at all. So the assertion is that `_acquire` runs exactly once for two
    nested holds, with a deadline short enough that a defect reintroducing a real second wait would
    time out this test rather than silently pass it."""
    svc = SessionService()
    svc.create_session("A real request.", slug="nested")
    svc.update_model("nested", _full_model())
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 0.3)
    calls: list[str] = []
    real_acquire = store_lock._acquire

    def counting_acquire(fd, slug):
        calls.append(slug)
        return real_acquire(fd, slug)

    monkeypatch.setattr(store_lock, "_acquire", counting_acquire)

    with store.session_lock("nested"):
        with store.session_lock("nested"):
            store.save_session_artifact("nested", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n",
                                        source_revision=1)

    assert calls == ["nested"], (
        f"a nested acquisition on the same thread must not call _acquire again: {calls}")


@pytest.mark.skipif(store.fcntl is None, reason="POSIX-only branch. REASONED, NOT OBSERVED on "
                     "Windows -- see #265.")
def test_a_non_contention_lock_error_fails_immediately_instead_of_waiting_out_the_deadline(
        workspace, monkeypatch):
    """Caught in review before this shipped: a first draft caught a bare `OSError` around the poll
    loop, which also catches `ENOLCK`, `EBADF` or a filesystem that refuses `flock` outright -- none
    of which will ever resolve by waiting. Masking one of those behind the retry loop for up to 30
    seconds and then raising `SessionLockedError` ("locked by another process") would trade a loud,
    honest failure for a quiet, misleading one. Only `BlockingIOError` -- what `flock(..., LOCK_NB)`
    raises for genuine contention -- may be retried; everything else must still fail immediately,
    exactly as the single blocking call this loop replaced already did.

    `OSError(errno.ENOLCK, ...)` stands in for "the kernel is out of lock resources" -- a real
    condition `flock` can raise that retrying can never fix."""
    import errno

    SessionService().create_session("A real request.", slug="broken-lock")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 10.0)  # would dominate the test if hit
    real_flock = store.fcntl.flock

    def refusing_flock(fd, op):
        if op & store.fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")
        return real_flock(fd, op)

    monkeypatch.setattr(store.fcntl, "flock", refusing_flock)

    started = time.monotonic()
    with pytest.raises(OSError) as ei:
        with store.session_lock("broken-lock"):
            pass  # pragma: no cover - must never be granted
    elapsed = time.monotonic() - started

    assert ei.value.errno == errno.ENOLCK
    assert not isinstance(ei.value, RequivoError), (
        "a kernel resource error must surface as what it is, not be relabelled as SessionLockedError")
    # Immediate, not the 10s deadline this test set specifically so a masked error would be visible.
    assert elapsed < 1.0, elapsed


