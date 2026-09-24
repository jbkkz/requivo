"""The store: root bookkeeping (#211, #320), `migrate_legacy` (#4, #262, #411), the filename chokepoint
(#5, #21, #23, #36, #40), a torn save (#261), slugs and the loader; `_atomic_write` is in test_persistence_lock.py."""
from __future__ import annotations

import ast
import builtins
import io
import json
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from _fakes import deny_access, full_model, printed, seed_session, simulate_py314_denied_path, slot
from test_source_form import _force_default_encoding  # the one control that can move the ambient encoding

from requivo.cli import _build_parser, _wrote
from requivo.core import persistence as store
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import (
    InvalidFilenameError,
    InvalidSlugError,
    ModelUnreadableError,
    RequivoError,
    SessionNotFoundError,
)
from requivo.core.integrity import check_session
from requivo.core.persistence import derive_slug, load_model, validate_filename, validate_slug
from requivo.core.persistence import store as store_module
from requivo.deterministic._shared import EXIT_DEGRADED
from requivo.deterministic.sessions import _cmd_session_migrate
from requivo.deterministic.sessions.lifecycle import _legacy_request_text
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")


def _legacy(slug: str, marker: str, request_text: str | None = None) -> Path:
    """A legacy out/<slug>/ session whose `problem` slot is identifiable."""
    d = store.legacy_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text(json.dumps(full_model(problem=slot(10, "inferred", "low", marker))), encoding="utf-8")
    if request_text is not None:
        (d / "request.md").write_text(request_text, encoding="utf-8")
    return d


def _problem(slug: str, revision: int | None = None) -> str:
    out = store.load_session_model(slug) if revision is None else store.load_revision_model(slug, revision)
    return out.model["problem"].value


def _session(slug: str, value: str = "") -> FileSessionRepository:
    """A session at revision 1, plus the repository an external consumer would hold."""
    seed_session(slug, "Something.", problem=slot(80, "explicit", "high", value))
    return FileSessionRepository()


def _migrate() -> tuple[dict, int]:
    """`session migrate --json` through its command function, so a refusal reaches the test: receipt and exit code."""
    buf, code = io.StringIO(), 0
    with redirect_stdout(buf):
        try:
            _cmd_session_migrate(SimpleNamespace(json=True), None)
        except SystemExit as e:
            code = int(e.code or 0)
    return json.loads(buf.getvalue()), code


# ── the store root (#211, #320) ───────────────────────────────────────────────


def test_the_privacy_gitignore_is_written_once_and_never_restored(workspace):
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()
    svc = SessionService()
    svc.create_session("A leave approval system", slug="first")
    assert marker.read_text(encoding="utf-8").splitlines()[-1] == "*", "the pattern is the self-ignoring `*`"
    marker.unlink()                                         # deleted on purpose: the team commits sessions
    svc.create_session("A room booking tool", slug="second")
    svc.update_model("second", full_model())
    assert not marker.exists(), "a later session operation restored an ignore file the user deleted"
    marker.write_text("sessions/secret-*\n", encoding="utf-8")   # edited on purpose: survives byte for byte
    svc.create_session("A third thing", slug="third")
    assert marker.read_text(encoding="utf-8") == "sessions/secret-*\n"


def _creates_a_tree(node: ast.Call) -> bool:
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    return name == "makedirs" or (name == "mkdir" and any(k.arg == "parents" for k in node.keywords))


def test_no_store_directory_is_created_outside_ensure_store_dir():
    """The guard behind #211: fixing every call site leaves the next one."""
    src = Path(__file__).resolve().parent.parent / "src" / "requivo"
    exempt = {("core/persistence/store.py", "create_session"), ("core/persistence/store.py", "ensure_store_dir")}
    seen, offenders, modules = set(), [], sorted(src.rglob("*.py"))
    for path in modules:
        rel = path.relative_to(src).as_posix()
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and _creates_a_tree(node):
                        (seen.add if (rel, fn.name) in exempt else offenders.append)(
                            (rel, fn.name) if (rel, fn.name) in exempt else f"{rel}:{node.lineno} in {fn.name}()")
    assert len(modules) > 20, f"the scan found only {len(modules)} modules under {src.as_posix()}"
    assert not offenders, "these create a directory tree without `ensure_store_dir` (#211):\n  " + "\n  ".join(offenders)
    assert seen == exempt, f"an exemption names no real call site: {sorted(exempt - seen)}"


def test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt(workspace, monkeypatch):
    """#320: one transient error must not switch the guarantee off for good."""
    real_open = builtins.open

    def refuse_the_marker(path, mode="r", *a, **kw):
        if str(path).endswith(".gitignore") and "x" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", refuse_the_marker)
    with pytest.raises(RequivoError) as ei:
        SessionService().create_session("A confidential client request.", slug="one")
    assert ei.value.code != "", "the failure must be structured, not a bare OSError"
    assert not (workspace / ".requivo").exists(), "the store root outlived the failed marker write"
    monkeypatch.setattr(builtins, "open", real_open)
    SessionService().create_session("A confidential client request.", slug="one")
    assert (workspace / ".requivo" / ".gitignore").exists()


def test_the_store_root_is_created_without_probing_whether_it_exists(workspace, monkeypatch):
    """#320: `exists()` can raise EACCES, so the root is decided without it, and an OSError is structured."""
    called: list[str] = []
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, *a, **kw: (called.append(str(self)), real_exists(self, *a, **kw))[1])
    SessionService().create_session("Something.", slug="probe")
    assert not any(c.endswith(".requivo") for c in called), called
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(RequivoError):
        store.ensure_store_dir(workspace / ".requivo" / "sessions")


# ── migrate_legacy (#4, #262, #411) ───────────────────────────────────────────


def test_migrating_onto_a_live_session_is_refused_rather_than_overwriting_it():
    """#4: `migrate_legacy` checked only that the *legacy* model existed; a revision-0 shell is a claim too."""
    svc = SessionService()
    svc.create_session("A real request.", slug="dup")
    svc.update_model("dup", full_model(problem=slot(80, "explicit", "high", "REAL v1")))
    svc.update_model("dup", full_model(problem=slot(90, "explicit", "high", "REAL v2")))
    _legacy("dup", "LEGACY")
    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("dup")
    assert ei.value.code == "session_exists"
    meta = store.read_meta("dup")
    assert (meta.current_revision, [r.revision for r in meta.revisions]) == (2, [1, 2])
    assert (_problem("dup", 1), _problem("dup", 2), _problem("dup")) == ("REAL v1", "REAL v2", "REAL v2")
    assert check_session("dup") == []
    assert (store.legacy_dir("dup") / "model.json").exists()   # the originals are preserved on refusal too

    SessionService().create_session("A real request.", slug="fresh", provider="claude-code")
    claimed = store.read_meta("fresh").session_id
    _legacy("fresh", "LEGACY")
    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("fresh")
    assert ei.value.code == "session_exists"
    meta = store.read_meta("fresh")
    assert (meta.session_id, meta.current_revision, meta.provider) == (claimed, 0, "claude-code")


def test_migrating_a_free_slug_still_works():
    """The positive control for the refusals above; provenance is recovered, not invented."""
    legacy = _legacy("free", "LEGACY")
    (legacy / "request.txt").write_text("Legacy request.", encoding="utf-8")
    (legacy / "prd.md").write_text("# Legacy PRD\n", encoding="utf-8")
    (legacy / "session.json").write_text(json.dumps(
        {"created_at": "2026-01-02T03:04:05Z", "provider": "anthropic", "model_name": "claude-x"}), encoding="utf-8")
    meta = store.migrate_legacy("free")
    d = store.canonical_dir("free")
    assert (meta.current_revision, _problem("free", 1), _problem("free"), meta.artifact_status["prd"].revision) == (1, "LEGACY", "LEGACY", 1)
    assert (d / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"
    assert (d / "request.md").read_text(encoding="utf-8") == "Legacy request."
    assert check_session("free") == [] and (legacy / "model.json").exists()
    assert (meta.created_at, meta.provider, meta.model_name) == ("2026-01-02T03:04:05Z", "anthropic", "claude-x")
    assert meta.session_id == store.read_meta("free").session_id


@pytest.mark.parametrize("kind", ["model", "request", "artifact"])
def test_migration_does_not_drop_an_unreadable_legacy_file_on_py314(monkeypatch, kind):
    """#636: an inaccessible source file cannot be silently omitted from migration."""
    legacy = _legacy("denied-legacy", "LEGACY", request_text="Existing request.")
    if kind == "artifact":
        (legacy / "prd.md").write_text("# Existing PRD\n", encoding="utf-8")
    denied = legacy / {"model": "model.json", "request": "request.md", "artifact": "prd.md"}[kind]
    simulate_py314_denied_path(monkeypatch, denied)
    with pytest.raises(PermissionError):
        store.migrate_legacy("denied-legacy")


def test_interrupted_migration_check_cannot_call_a_denied_request_empty_on_py314(monkeypatch):
    """#636: interrupted-migration detection must not compare against an invented empty request."""
    legacy = _legacy("denied-request", "LEGACY", request_text="Existing request.")
    simulate_py314_denied_path(monkeypatch, legacy / "request.md")
    with pytest.raises(PermissionError):
        _legacy_request_text(legacy)


def test_the_bulk_migrate_command_skips_a_slug_that_is_already_taken():
    """A refusal degrades one row rather than aborting the pass (invariant 15, applied to a loop)."""
    seed_session("aaa-taken", "A real request.", problem=slot(80, "explicit", "high", "REAL"))
    _legacy("aaa-taken", "LEGACY")
    _legacy("zzz-free", "LEGACY")
    out, code = _migrate()
    assert (code, out["migrated"], out["skipped_already_present"]) == (0, ["zzz-free"], ["aaa-taken"])
    assert (_problem("aaa-taken", 1), _problem("zzz-free", 1)) == ("REAL", "LEGACY")


@pytest.mark.parametrize("break_it", ["legacy-model", "canonical-session"])
def test_the_bulk_migrate_command_degrades_a_bad_session_rather_than_aborting(break_it):
    """#262: an unparseable legacy `model.json`, or a canonical `session.json` that cannot be read, is one row."""
    _legacy("aaa-first", "FIRST")
    _legacy("mmm-broken", "NEVER-COPIED")
    broken = store.legacy_dir("mmm-broken") / "model.json" if break_it == "legacy-model" else store.canonical_dir("mmm-broken") / "session.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not valid json", encoding="utf-8")
    _legacy("zzz-last", "LAST")
    out, code = _migrate()
    assert (code, sorted(out["migrated"]), out["skipped_already_present"]) == (EXIT_DEGRADED, ["aaa-first", "zzz-last"], [])
    assert [e["slug"] for e in out["errors"]] == ["mmm-broken"] and out["errors"][0]["error"]
    assert (_problem("aaa-first", 1), _problem("zzz-last", 1)) == ("FIRST", "LAST")


def test_an_interrupted_migration_is_reported_distinctly_from_already_present():
    """#262: a revision-0 shell whose request matches the legacy one is the crash window, not a taken slug."""
    _legacy("half-done", "NEVER-COPIED")
    SessionService().create_session("", slug="half-done")   # `migrate_legacy`'s own fallback request text
    out, code = _migrate()
    assert (code, out["migrated"], out["skipped_already_present"], out["interrupted"]) == (EXIT_DEGRADED, [], [], ["half-done"])
    assert SessionService().repo.read_meta("half-done").current_revision == 0


def test_an_unrelated_revision_zero_session_at_a_legacy_slug_is_not_called_interrupted():
    """#262: `current_revision == 0` alone is not evidence of a crashed migrate."""
    _legacy("shared-slug", "LEGACY", request_text="The legacy request text.")
    SessionService().create_session("A completely different, unrelated request.", slug="shared-slug")
    out, _ = _migrate()
    assert (out["interrupted"], out["skipped_already_present"]) == ([], ["shared-slug"])


def test_the_bulk_migrate_command_degrades_an_unreadable_legacy_directory_rather_than_crashing(request):
    """#411: the scan that produces the per-slug rows reports an entry it could not examine as a fact."""
    _legacy("aaa-first", "FIRST")
    d = store.output_root() / "mmm-blocked"
    d.mkdir(parents=True, exist_ok=True)
    deny_access(d, request, "the could-not-examine arm of the legacy-root scan")
    _legacy("zzz-last", "LAST")
    out, code = _migrate()
    assert (code, sorted(out["migrated"])) == (EXIT_DEGRADED, ["aaa-first", "zzz-last"])
    assert [e["name"] for e in out["unreadable"]] == ["mmm-blocked"] and out["unreadable"][0]["error"]
    assert (_problem("aaa-first", 1), _problem("zzz-last", 1)) == ("FIRST", "LAST")


def test_a_totally_unlistable_legacy_root_refuses_cleanly_instead_of_crashing(request):
    """#411: `root.iterdir()` itself failing is a clean refusal naming the root, not a traceback."""
    out_dir = store.output_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    deny_access(out_dir, request, "the whole-root-unlistable arm of the legacy-root scan")
    with pytest.raises(RequivoError) as ei:
        _migrate()
    assert str(out_dir) in str(ei.value)


# ── the filename chokepoint (#5, #21, #23, #36, #40) ──────────────────────────

# Every shape that is not a filename: traversal, dot segments, both separators, absolute and Windows-shaped
# paths, a dot-prefixed name, the empty string, upper case and an over-long name.
ESCAPES = ["../../../../ESCAPED.md", "..", "sub/nested.md", "sub\\nested.md", "/etc/passwd", ".hidden.md", "",
           r"..\..\ESCAPED.md", r"c:\windows\win.ini", "PRD.MD", "a" * 300 + ".md"]

_DOORS = {
    "write_artifact_file": lambda slug, name: store.write_artifact_file(slug, name, "pwned"),
    "save_session_artifact": lambda slug, name: store.save_session_artifact(slug, "prd", name, "pwned", source_revision=1),
    "read_artifact_file": lambda slug, name: store.read_artifact_file(slug, name),
    "load_artifact": lambda slug, name: FileSessionRepository().load_artifact(slug, name),
    "artifact_path": lambda slug, name: store.artifact_path(slug, name),
}


@pytest.mark.parametrize("door", sorted(_DOORS))
def test_every_artifact_door_refuses_a_name_that_is_not_a_filename(workspace, door):
    """#5, #21: the guard lives in Core's `artifact_path`, so every read, write and display inherits it."""
    _session("trav")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    for name in ESCAPES:
        with pytest.raises(InvalidFilenameError) as ei:
            _DOORS[door]("trav", name)
        assert name.startswith(ei.value.details["filename"]), name   # the refusal names what it refused
    assert (workspace / "ESCAPED.md").read_text(encoding="utf-8") == "TOP SECRET"
    assert list((store.canonical_dir("trav") / "artifacts").iterdir()) == []
    assert store.read_meta("trav").artifact_status == {} and check_session("trav") == []


def test_the_artifact_doors_still_serve_a_real_name_and_answer_none_for_a_missing_one():
    """The positive control, and the three-state read: content, `None` for never generated, a raise for a refusal."""
    repo = _session("doors")
    artifacts = store.canonical_dir("doors") / "artifacts"
    assert store.write_artifact_file("doors", "epic.github.json", "{}") == artifacts / "epic.github.json"
    assert store.read_artifact_file("doors", "epic.github.json") == "{}"
    assert store.read_artifact_file("doors", "epic.json") is None
    assert store.save_session_artifact("doors", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n", source_revision=1).revision == 1
    assert repo.load_artifact("doors", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"
    assert repo.load_artifact("doors", ARTIFACT_FILENAMES["prd"]) is None
    assert set(store.read_meta("doors").artifact_status) == {"brief"} and check_session("doors") == []


def test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline():
    """Found while fixing #40: `$` matches before a trailing newline, `\\Z` does not."""
    assert validate_slug("leave-approval") == "leave-approval"
    for name in sorted(ARTIFACT_FILENAMES.values()) + ["epic.github.json"]:
        assert validate_filename(name) == name
    for bad in ("ok\n", "ok\r", "leave-approval\n"):
        with pytest.raises(InvalidSlugError):
            validate_slug(bad)
    for bad in ("prd.md\n", "prd.md\r"):
        with pytest.raises(InvalidFilenameError):
            validate_filename(bad)


def _artifact_status(slug: str, **entries) -> None:
    """Rewrite entries of a session's recorded `artifact_status`, the way a hand edit or an import can."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["artifact_status"].update(entries)
    p.write_text(json.dumps(meta), encoding="utf-8")


def test_integrity_cannot_be_made_to_print_a_line_break_by_a_recorded_filename():
    """The reachable consequence of the anchor above (#40)."""
    _session("anch")
    store.save_session_artifact("anch", "prd", "prd.md", "# P\n", source_revision=1)
    (store.canonical_dir("anch") / "artifacts" / "prd.md").unlink()
    problems = check_session("anch")
    assert [p.code for p in problems] == ["missing_artifact_file"] and "\n" not in problems[0].message
    _artifact_status("anch", prd=dict(store.read_meta("anch").artifact_status["prd"].__dict__, filename="prd.md\n"))
    reported = check_session("anch")
    assert reported and all("\n" not in problem.message for problem in reported)


def test_an_artifact_round_trips_non_ascii_content(workspace, monkeypatch):
    """`load_artifact` names its encoding; under an ASCII locale the bare read it replaced fails."""
    repo = _session("read-utf8")
    body = "# Brief\n\nAn em-dash — a café — and a curly quote: “ready”.\n"
    store.save_session_artifact("read-utf8", "brief", ARTIFACT_FILENAMES["brief"], body, source_revision=1)
    assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body
    with monkeypatch.context() as m:
        if not _force_default_encoding(m, workspace, "ascii"):
            pytest.skip("the ambient default encoding could not be forced on this interpreter. UNTESTED ON THIS "
                        "INTERPRETER: that read_artifact_file passes an explicit encoding; the 3.9 leg tests it.")
        with pytest.raises(UnicodeDecodeError):
            store.artifact_path("read-utf8", ARTIFACT_FILENAMES["brief"]).read_text()   # the defect, deliberately bare
        assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body


def _recorded(filename: str) -> store.ArtifactStatus:
    """The `ArtifactStatus` a display site is handed; `filename` is an unconstrained `str` on it."""
    return store.ArtifactStatus(revision=1, filename=filename, updated_at="2026-08-19T00:00:00Z")


@pytest.mark.parametrize("site", ["cli._wrote", "artifact save"])
def test_a_display_site_prints_only_a_path_inside_the_session(workspace, tmp_path, monkeypatch, site):
    """#23, #36: a path that is only printed is still a path this code built, so both sites use the chokepoint."""
    _session("say-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "say-where", "--type", "brief", "--file", str(doc), "--revision", "1"]

    def shown(filename: str | None) -> str:
        if site == "cli._wrote":
            status = _recorded(ARTIFACT_FILENAMES["prd"] if filename is None else filename)
            return printed(_wrote, "say-where", SimpleNamespace(status=status), "PRD")
        if filename is not None:
            monkeypatch.setattr(ArtifactService, "save", lambda *a, **k: _recorded(filename))
        ns = _build_parser().parse_args(argv)
        return printed(ns.func, ns, None)   # the command function itself, so the refusal reaches the test

    # The positive control first, and it is the load-bearing half: a real name prints, inside artifacts/.
    expected = ARTIFACT_FILENAMES["brief" if site == "artifact save" else "prd"]
    assert str(store.canonical_dir("say-where") / "artifacts" / expected) in shown(None)
    for name in ESCAPES:
        with pytest.raises(InvalidFilenameError):
            shown(name)


# ── a torn save_revision (#261) ───────────────────────────────────────────────


class _InjectedCrash(BaseException):
    """A process death, modelled."""


@contextmanager
def _crashing_after(after: int):
    """Let `after` writes through, then refuse the rest: ENOSPC, a SIGKILL, a pulled plug."""
    real, attempted = store_module._atomic_write, []

    def crashing(path, content):
        attempted.append(path.name)
        if len(attempted) > after:
            raise _InjectedCrash(f"simulated death after write {after}")
        return real(path, content)

    with pytest.MonkeyPatch.context() as mp, pytest.raises(_InjectedCrash):
        mp.setattr(store_module, "_atomic_write", crashing)
        yield attempted
    assert len(attempted) == after + 1, f"the injection did not fire where aimed: {attempted}"


def _tear_revision_two(*, after: int) -> list:
    """A session at revision 1 holding 'first', interrupted `after` writes into revision 2."""
    seed_session("s", "Something.", problem=slot(10, "explicit", "low", "first"))
    model = store.load_session_model("s")
    model.model["problem"].value = "second"
    with _crashing_after(after) as attempted:
        store.save_revision("s", model)
    return attempted


def _tear_first_apply(slug: str, *, after: int) -> None:
    SessionService().create_session("Something.", slug=slug)
    with _crashing_after(after):
        SessionService().update_model(slug, full_model(problem=slot(10, "explicit", "low", "one")))


def _current_model_is_the_recorded_revision(slug: str) -> bool:
    meta = store.read_meta(slug)
    payload = (store.canonical_dir(slug) / "model.json").read_text(encoding="utf-8")
    return store.content_hash(payload) == meta.revisions[meta.current_revision - 1].model_hash


def test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision():
    """The window `save_revision` writes the frozen revision file first in order to make benign (#261)."""
    attempted = _tear_revision_two(after=1)
    assert store.read_meta("s").current_revision == 1 and _current_model_is_the_recorded_revision("s")
    assert _problem("s") == "first"
    assert {p.code for p in check_session("s")} == {"orphan_revision_file"}
    # The mechanism: the one write that landed went to `revisions/`, which no read path consults.
    orphan = store.canonical_dir("s") / "revisions" / "0002-model.json"
    assert attempted[0] == orphan.name and orphan.is_file() and "second" in orphan.read_text(encoding="utf-8")


def test_the_next_apply_reclaims_the_orphan_and_verifies_clean():
    """The revision number was never spent, so the orphan is overwritten by the next apply."""
    _tear_revision_two(after=1)
    SessionService().update_model("s", full_model(problem=slot(20, "explicit", "low", "healed")))
    assert (store.read_meta("s").current_revision, _problem("s"), _problem("s", 2)) == (2, "healed", "healed")
    assert check_session("s") == []


def test_the_windows_the_reorder_does_not_close_are_still_reported_as_inconsistent():
    """After both payload writes, and on the revision 0 -> 1 arm `migrate_legacy` takes (#261)."""
    _tear_revision_two(after=2)
    assert store.read_meta("s").current_revision == 1 and not _current_model_is_the_recorded_revision("s")
    assert {p.code for p in check_session("s")} == {"orphan_revision_file", "model_is_not_the_last_revision"}
    _tear_first_apply("gap-one", after=1)
    assert store.read_meta("gap-one").current_revision == 0
    assert not (store.canonical_dir("gap-one") / "model.json").exists()
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("gap-one")     # "no model yet", which is the truth
    assert {p.code for p in check_session("gap-one")} == {"orphan_revision_file"}
    _tear_first_apply("gap-two", after=2)
    assert store.read_meta("gap-two").current_revision == 0
    assert {p.code for p in check_session("gap-two")} == {"orphan_revision_file", "model_without_revision"}


# ── slugs (#245, #221, #372) ──────────────────────────────────────────────────


@pytest.mark.parametrize(("request_text", "expected"), [
    ("We'd like an invoice created automatically when signed", "invoice-created-automatically-signed"),
    ("!!!", "discovery"),
    ("We need a way to track vendor invoices.", "track-vendor-invoices"),        # #245: content words, not the opening
    ("We need a leave approval system.", "leave-approval-system"),
    ("Nous aimerions un système d'approbation des congés payés", "systeme-approbation-conges-payes"),
    ("Podríamos automatizar la aprobación de vacaciones", "automatizar-aprobacion-vacaciones"),
    ("Ein Genehmigungssystem für Urlaubsanträge", "genehmigungssystem-urlaubsantrage"),
    ("We need it", "we-need-it"),                                              # nothing but stopwords: keep them
    ("We need a way to", "we-need-a-way-to"),
])
def test_derive_slug_yields_the_documented_handle(request_text, expected):
    """Five content tokens, diacritics folded rather than split, a stopword-only request kept as is (#245)."""
    assert derive_slug(request_text) == expected
    assert validate_slug(expected) == expected


def test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep():
    """#245: the guard the comment above `_SLUG_STOPWORDS` needed rather than a second copy of it."""
    from requivo.core.persistence import _SLUG_STOPWORDS
    assert not {"son", "hay", "sin", "man", "war", "bin", "hat"} & _SLUG_STOPWORDS, "ordinary English content words"
    assert {"the", "nous", "der", "para"} <= _SLUG_STOPWORDS
    assert derive_slug("Track the son of the account owner") == "track-son-account-owner"


def test_folding_expands_a_latin_letter_that_carries_no_combining_mark():
    """#245: NFKD does not decompose ß or Œ, so they are expanded by hand."""
    assert (derive_slug("Straßenverkehr melden"), derive_slug("Œkosystem pflegen")) == ("strassenverkehr-melden", "oekosystem-pflegen")


def test_a_non_latin_request_still_derives_the_documented_discovery_fallback():
    """#245: the residual limit, stated rather than fixed."""
    assert (derive_slug("休暇承認システムが必要です"), derive_slug("Нам нужна система одобрения отпусков")) == ("discovery", "discovery")


_RESERVED = ("con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)))


def test_reserved_windows_device_names_are_refused_as_slugs():
    """#221, #372: refused by `validate_slug` and by `canonical_dir`, the door `create_session` takes; not a slug at all is refused before any filesystem touch."""
    for bad in ("../../escaped", "a/b", "..", ".", "", "/abs", "Upper", "under_score", *_RESERVED, *(n.upper() for n in _RESERVED)):
        with pytest.raises(InvalidSlugError):
            validate_slug(bad)
        with pytest.raises(InvalidSlugError):
            store.canonical_dir(bad)
    for ok in ("leave-approval", "console", "com0", "lpt", "con-approval", "prnter"):
        assert validate_slug(ok) == ok
    with pytest.raises(InvalidSlugError):
        store.create_session("con", "A request that would slug to a reserved name.")
    with pytest.raises(InvalidSlugError):
        with store.session_lock("nul"):
            pass  # pragma: no cover - refused before the body ever runs


def test_reserved_windows_device_names_are_refused_as_filename_stems():
    """The stem before the first dot is what Windows reads, whatever the extension (#221)."""
    for bad in ("con.md", "CON.MD", "nul.txt", "lpt1.json", "com9.tar.gz"):
        with pytest.raises(InvalidFilenameError):
            validate_filename(bad)
    for ok in ("prd.md", "console.md", "config.md"):
        assert validate_filename(ok) == ok


@pytest.mark.skipif(store.fcntl is None, reason="Windows itself refuses a directory named 'con', so a session "
                     "already on disk under a reserved name is a state only another platform can reach (#372). "
                     "REASONED, NOT OBSERVED.")
def test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it():
    """#372: reading tolerates the existing directory, and an idempotent re-init returns it rather than creating one."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None, "context_cards": None,
        "current_revision": 0, "format_version": 1, "revisions": [], "artifact_status": {}}), encoding="utf-8")
    assert store.session_exists("con") is True and store.canonical_dir("con") == d
    assert store.read_meta("con").slug == "con" and "con" in store.list_session_slugs()
    assert store.session_request("con") == "A request captured before #221 shipped."
    with store.session_lock("con"):                        # `session export`'s own read-consistency lock
        pass
    assert SessionService().create_session("A request captured before #221 shipped.", slug="con").session_id == "deadbeef"
    with pytest.raises(InvalidSlugError):                  # the must-not-fire control, in the same fixture
        store.canonical_dir("nul")


# ── the loader (#204) ─────────────────────────────────────────────────────────


def test_load_model_rejects_invalid_model(tmp_path):
    """#204: a refusal, no longer a `ValidationError`; a bare model.json has no session or revision to name."""
    bad = tmp_path / "model.json"
    bad.write_text(json.dumps({"questions": [], "summary": {}}), encoding="utf-8")   # required `model` missing
    with pytest.raises(ModelUnreadableError) as ei:
        load_model(bad)
    assert ei.value.details == {"path": str(bad)}


@pytest.mark.parametrize("corruption", ["", "{", '{"model": {}, "questions": [], "summary"', "not json at all"])
def test_a_corrupt_model_is_a_structured_error_from_every_door(corruption):
    """Four ways of being corrupt, and every door into a model; the two that know the session name the remedy."""
    slug = "corrupt-model"
    _session(slug)
    d = store.canonical_dir(slug)
    for target in (d / "model.json", d / "revisions" / "0001-model.json"):
        target.write_text(corruption, encoding="utf-8")
    doors = {"session": lambda: store.load_session_model(slug), "revision": lambda: store.load_revision_model(slug, 1),
             "file": lambda: load_model(d / "model.json")}
    for name, call in doors.items():
        with pytest.raises(ModelUnreadableError) as ei:
            call()
        assert str(d) in str(ei.value), name
        if name != "file":
            assert f"requivo session verify {slug}" in str(ei.value) and "revisions/" in str(ei.value)
            assert ei.value.details["slug"] == slug
        if name == "revision":
            assert ei.value.details["revision"] == 1


def test_a_missing_model_is_not_reported_as_a_corrupt_one():
    SessionService().create_session("A leave approval system.", slug="no-model-yet")
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("no-model-yet")   # revision 0: no model.json has been written


@pytest.mark.parametrize("kind", ["session-model", "revision-model", "request"])
def test_a_denied_session_file_is_not_called_missing_on_py314(monkeypatch, kind):
    """#636: metadata denial must not turn an existing session file into a missing one."""
    slug = "blocked-file"
    store.create_session(slug, "An existing request.")
    d = store.canonical_dir(slug)
    if kind == "session-model":
        denied = d / "model.json"
        denied.write_text("{}", encoding="utf-8")
    elif kind == "revision-model":
        denied = d / "revisions" / "0001-model.json"
        denied.write_text("{}", encoding="utf-8")
    else:
        denied = d / "request.md"
    simulate_py314_denied_path(monkeypatch, denied)
    with pytest.raises(PermissionError):
        if kind == "session-model":
            store.load_session_model(slug)
        elif kind == "revision-model":
            store.load_revision_model(slug, 1)
        else:
            store.session_request(slug)
