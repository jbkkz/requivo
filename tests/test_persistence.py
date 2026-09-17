"""The store's write/read guards: `migrate_legacy` (#4, #262, #411), the filename-as-write-target chokepoint
(#5, #40, #23, #36) and the atomic-write newline guard (#464)."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

# The one control in this repo that can actually move the ambient default encoding.
from test_source_form import _force_default_encoding

from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.cli import _build_parser, _wrote
from requivo.core import persistence as store
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError
from requivo.core.integrity import check_session
from requivo.deterministic._shared import EXIT_DEGRADED
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


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


def _session(slug: str) -> FileSessionRepository:
    """A session at revision 1, plus the repository an external consumer would hold."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, _full_model())
    return FileSessionRepository()


# Every shape that is not a filename: traversal, a bare dot segment, both separators, an absolute path, a dot-prefixed name (which the staging convention and `list_session_slugs` reserve), and the empty string.
ESCAPES = [
    "../../../../ESCAPED.md",
    "..",
    "sub/nested.md",
    "sub\\nested.md",
    "/etc/passwd",
    ".hidden.md",
    "",
]



# ── #4, #262, #411: migrate_legacy() must not overwrite a live session ──────────


def test_migrating_onto_a_live_session_is_refused_rather_than_overwriting_it(workspace):
    """`migrate_legacy` checked only that the *legacy* model existed."""
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
    """The other half of the claim. A session created but never analysed holds no revision to destroy."""
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
    """The positive control for both refusals above."""
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
    """The sweep reports `migrated` and `skipped_already_present`, so a refusal has to degrade that one row
    rather than abort the pass — the rule invariant 15 states for a listing, applied to a loop."""
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
    """#262. One legacy session with an unparseable `model.json` must not abort the whole pass."""
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
    """#262. `migrate_legacy` claims the slug via `create_session` and only afterwards."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("half-done", "NEVER-COPIED")
    # The crash window `migrate_legacy` documents: the slug is claimed but the model was never applied.
    # -- `_legacy` writes no request.md/request.txt, so `migrate_legacy` would have claimed this slug
    # with request="" (its own fallback), and the interrupted/unrelated discriminator compares exactly this against the legacy request text.
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
    """Found in review of #262 itself. `repo.read_meta(slug)` the call that decides `skipped_already_present`
    vs `interrupted` for an occupied slug"""
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
    """#411. The scan that PRODUCES the per-slug rows."""
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
    """Found in review of #411 itself. Wrapping only the per-entry probe inside `_scan_legacy_root` left
    `root.iterdir()` itself the call that lists the root in the first place"""
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
    """Found in review of #262 itself. `current_revision == 0` alone is not evidence of a crashed migrate."""
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


# ── #5: filename is a write target, so it is validated like its slug sibling ────


def test_write_artifact_file_refuses_a_filename_that_is_not_a_filename(workspace):
    """`slug` is validated at this chokepoint so that "every surface inherits the same directory-traversal
    guard, not just FastAPI" — and the sibling parameter on the same mutating call had none."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav")
    svc.update_model("trav", _full_model())
    artifacts = store.canonical_dir("trav") / "artifacts"

    # Positive control first: an ordinary export name still lands where it should.
    assert store.write_artifact_file("trav", "epic.github.json", "{}") == artifacts / "epic.github.json"

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.write_artifact_file("trav", name, "pwned")
        assert ei.value.code == "invalid_filename", name
    assert not (workspace / "ESCAPED.md").exists()
    assert sorted(p.name for p in artifacts.iterdir()) == ["epic.github.json"]


def test_save_session_artifact_refuses_it_too_and_records_nothing(workspace):
    """The recorded filename is read back by `integrity.py` and by the artifact-show paths."""
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
    """Length is part of validity for a slug for a stated reason."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav3")
    svc.update_model("trav3", _full_model())
    with pytest.raises(RequivoError) as ei:
        store.write_artifact_file("trav3", "a" * 300 + ".md", "x")
    assert ei.value.code == "invalid_filename"


def test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline(workspace):
    """Found while fixing #40, and outside its footprint — called out rather than slipped in."""
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
    """The reachable consequence of the anchor above, and why it earns a test rather than a note."""
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


def test_load_artifact_refuses_a_traversal_rather_than_disclosing_the_file(workspace):
    """The read-side sibling of the two write paths above, and a different question (#21)."""
    repo = _session("read-trav")

    # ESCAPES[0] resolves four levels up from artifacts/, i.e. to <workspace>.
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    assert (workspace / "ESCAPED.md").read_text(encoding="utf-8") == "TOP SECRET"

    # Positive control: a legitimate ARTIFACT_FILENAMES value still loads, byte for byte.
    store.save_session_artifact("read-trav", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)
    assert repo.load_artifact("read-trav", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"

    # The over-long name rides the same guard here as on the write side.
    for name in ESCAPES + ["a" * 300 + ".md"]:
        with pytest.raises(RequivoError) as ei:
            repo.load_artifact("read-trav", name)
        assert ei.value.code == "invalid_filename", name
        # The refusal has to name what it refused, or a caller holding several names cannot tell which one was rejected.
        assert name.startswith(ei.value.details["filename"]), name


def test_a_refused_read_raises_where_a_missing_artifact_returns_none(workspace):
    """The judgment this issue turned on. `artifact_path()` raises and `load_artifact` returns None."""
    repo = _session("read-3state")
    store.save_session_artifact("read-3state", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)

    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"
    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["prd"]) is None   # never generated

    with pytest.raises(RequivoError) as ei:
        repo.load_artifact("read-3state", "../../../../ESCAPED.md")
    assert ei.value.code == "invalid_filename"


def test_core_owns_the_read_guard_so_the_next_reader_cannot_forget_it(workspace):
    """#21 put the write guard at `artifact_path()` in Core rather than at its callers."""
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
    """The other half of the line this change rewrites."""
    repo = _session("read-utf8")
    body = "# Brief\n\nAn em-dash — a café — and a curly quote: “ready”.\n"
    store.save_session_artifact("read-utf8", "brief", ARTIFACT_FILENAMES["brief"], body,
                                source_revision=1)
    assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body

    # Everything above is set up under the ambient encoding.
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
            # Deliberately bare: this read IS the thing under test, performing the defect so the assertion can catch it.
            p.read_text()   # what the repository's own line did, meeting the locale it would meet
        assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body


# ── #464: _atomic_write must disable universal-newline translation on write ─────


def test_atomic_write_passes_newline_empty_to_disable_translation(tmp_path, monkeypatch):
    """#464. `_atomic_write` wrote via `tmp.write_text(content, encoding="utf-8")`."""
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
    """Positive control for the assertion above: `newline=""` must not simply break the write."""
    store._atomic_write(tmp_path / "doc.md", "# Title\n\nA body line.\n")
    assert (tmp_path / "doc.md").read_bytes() == b"# Title\n\nA body line.\n"


# ── #36: a path that is only printed is still a path this code built ────────────


def _recorded(filename: str) -> store.ArtifactStatus:
    """The `ArtifactStatus` a display site is handed — `filename` is an unconstrained `str` on it."""
    return store.ArtifactStatus(revision=1, filename=filename, updated_at="2026-08-19T00:00:00Z")


def _run_command(argv: list) -> str:
    """Run one deterministic verb through the real parser and command function, capturing stdout."""
    ns = _build_parser().parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ns.func(ns, None)
    return buf.getvalue()


def test_artifact_save_reports_where_it_wrote_through_the_chokepoint(workspace, tmp_path, monkeypatch):
    """`artifact save`'s human branch printed the join itself (#23)."""
    _session("say-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "say-where", "--type", "brief", "--file", str(doc), "--revision", "1"]

    # Positive control first, and it is the load-bearing half.
    out = _run_command(argv)
    assert str(store.artifact_path("say-where", ARTIFACT_FILENAMES["brief"])) in out

    # And the refusal.
    for name in ESCAPES:
        monkeypatch.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
        with pytest.raises(RequivoError) as ei:
            _run_command(argv)
        assert ei.value.code == "invalid_filename", name


def test_a_generated_document_reports_its_path_through_the_chokepoint(workspace):
    """`cli.py::_wrote` is the same join, and it is the one of the two that is shared."""
    _session("wrote-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")

    # Positive control: an ordinary generated document still names its real file.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("wrote-where", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["prd"])), "PRD")
    assert str(store.artifact_path("wrote-where", ARTIFACT_FILENAMES["prd"])) in out.getvalue()

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            _wrote("wrote-where", SimpleNamespace(status=_recorded(name)), "PRD")
        assert ei.value.code == "invalid_filename", name


def test_neither_display_site_can_be_made_to_print_a_path_outside_the_session(workspace, tmp_path,
                                                                             monkeypatch):
    """The consequence the two tests above are guards for, asserted as the thing a reader cares about rather
    than as an exception type."""
    _session("stay-inside")
    artifacts = store.canonical_dir("stay-inside") / "artifacts"
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "stay-inside", "--type", "brief", "--file", str(doc), "--revision", "1"]

    for name in [r"..\..\ESCAPED.md", r"c:\windows\win.ini", "a" * 300 + ".md", "PRD.MD"]:
        with pytest.raises(RequivoError):
            _wrote("stay-inside", SimpleNamespace(status=_recorded(name)), "PRD")
        with monkeypatch.context() as m:
            m.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
            with pytest.raises(RequivoError):
                _run_command(argv)

    # must fire, on both sites: each still prints, and prints inside artifacts/, for a real name.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("stay-inside", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["epic"])), "epic")
    assert str(artifacts / ARTIFACT_FILENAMES["epic"]) in out.getvalue()
    assert str(artifacts / ARTIFACT_FILENAMES["brief"]) in _run_command(argv)
