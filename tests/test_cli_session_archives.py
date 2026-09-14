"""End-to-end tests of `session import`'s archive-shape validation — the untrusted-input half of
`requivo.deterministic.sessions`.

Split out of `test_cli_deterministic.py` by #141, and split away from the rest of the `session` noun
for a reason that is about the tests rather than about a line count: import is the one verb whose
input comes from outside the workspace, so it is the only one with a threat model. #555 split this
file again, once it outgrew one module: the concurrency and rename-window races (#113, #111, #114)
moved to `test_cli_session_archives_races.py`, which duplicates `_zip`/`_good_entries`/`_import_error`
from here rather than importing them, per this suite's own convention of keeping test-module helpers
local (`tests/_fakes.py` makes the argument). This file keeps the shape-of-the-archive-itself story:
size/count/entry caps, unsafe paths, "not a session at all", the full `invalid_archive` taxonomy, and
collision handling.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _SESSIONS_ROW, _full_model, _run, _run_json, _run_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError
from requivo.deterministic.sessions.archives import MAX_ARCHIVE_FILES

# ── session import ──────────────────────────────────────────────────────────────
# Import takes a file from outside the workspace and turns it into a session, so it is the one command
# whose input is genuinely untrusted. Nothing may land in the store before the archive has been checked.


def _zip(path, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


def _good_entries(slug="imported", revision=0):
    meta = {"format_version": 1, "session_id": "abc", "slug": slug, "created_at": "t",
            "updated_at": "t", "current_revision": revision}
    entries = {f"{slug}/session.json": json.dumps(meta), f"{slug}/request.md": "A request."}
    if revision:
        entries[f"{slug}/model.json"] = json.dumps(_full_model())
    return entries


def test_export_import_round_trip(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _run(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _run_json(["session", "import", str(tmp_path / "s.zip"), "--json"])
    assert r["slug"] == "s" and r["replaced"] is False
    assert store.read_meta("s").current_revision == 1


def test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do(workspace, tmp_path,
                                                                                 monkeypatch):
    """#84. `session import --json` spelled the session `imported` and its location `into`; every
    sibling verb spells them `slug` and (for the one that reports a directory) `path`. A consumer
    looping over session verbs and reading `row["slug"]` got a `KeyError` from the one verb that had
    just put the session there."""
    init = _run_json(["session", "init", "Something.", "--slug", "s", "--json"])
    _run(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _run_json(["session", "import", str(tmp_path / "s.zip"), "--json"])

    assert r.keys() == {"slug", "path", "replaced"}
    assert "imported" not in r and "into" not in r
    assert r["slug"] == "s"
    assert r["path"] == str(store.canonical_dir("s"))
    # the directory, not the root that holds it — and it is the same string the verb prints
    assert Path(r["path"]).name == "s"
    assert r["path"] != str(store.session_root())

    # must fire: `path` means the same thing here as it does on the verb that creates a session
    assert set(init) >= {"slug", "path"}
    assert Path(init["path"]).name == "s"


def test_import_refuses_a_directory_name_that_is_not_a_valid_slug(workspace, tmp_path):
    """The reviewer's case: an archive whose folder is `bad slug` unpacked happily and then broke every
    later `session list`. A directory name becomes a slug, so it faces the same validation as any."""
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    assert store.list_session_slugs() == []          # and nothing was written
    assert _run_json(["session", "list", "--json"])["sessions"] == []


@pytest.mark.parametrize("entry", [
    "../escape/session.json",          # traversal via a parent segment
    "/absolute/session.json",          # an absolute path
    "..\\windows\\session.json",       # a Windows separator zipfile does not treat as a boundary
    "loose.json",                      # not inside a session directory at all
])
def test_import_refuses_unsafe_entries(workspace, tmp_path, entry):
    _zip(tmp_path / "evil.zip", {entry: "{}"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "evil.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_holding_more_than_one_session(workspace, tmp_path):
    _zip(tmp_path / "two.zip", {**_good_entries("one"), **_good_entries("two")})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "two.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_that_is_too_large_or_too_many_files(workspace, tmp_path):
    from requivo.deterministic.sessions.archives import MAX_ARCHIVE_FILES

    many = {f"s/artifacts/f{i}.md": "x" for i in range(MAX_ARCHIVE_FILES + 1)}
    _zip(tmp_path / "many.zip", many)
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "many.zip"), "--json"])

    # A zip bomb compresses to nothing and expands past the ceiling; the cap is on the expanded size.
    _zip(tmp_path / "big.zip", {"s/session.json": "0" * (64 * 1024 * 1024 + 1)})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "big.zip"), "--json"])
    assert store.list_session_slugs() == []


def _dir_entries(slug: str, n: int, prefix: str = "d") -> dict:
    """`n` bare directory entries under `<slug>/nested/`, each mapping to `""` -- `zipfile.writestr`
    gives a name ending in `/` the directory bit `ZipInfo.is_dir()` actually tests (the filename
    suffix, not `external_attr`), so this is a real directory entry from the reader's point of view,
    not merely a path that looks like one."""
    return {f"{slug}/nested/{prefix}{i:05d}/": "" for i in range(n)}


def test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries(
        workspace, tmp_path, monkeypatch):
    """#219: `MAX_ARCHIVE_FILES`/`MAX_ARCHIVE_BYTES` are both computed over `z.infolist()` with
    directory entries filtered out, so an archive built from nothing but directory entries declared
    zero files/bytes and sailed past both caps, while the extraction loop still created every one of
    them -- an inode/dir-creation DoS neither file-only cap covers. Must-fire: refused at
    `_inspect_archive`, before any extraction, so nothing this archive names may exist afterwards."""
    from requivo.deterministic.sessions import archives as det
    monkeypatch.setattr(det, "MAX_ARCHIVE_ENTRIES", 50)

    entries = {**_good_entries("s"), **_dir_entries("s", 500)}
    _zip(tmp_path / "dirbomb.zip", entries)

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dirbomb.zip"), "--json"])
    assert store.list_session_slugs() == []
    assert not (store.session_root() / "s").exists()


def test_an_archive_with_directory_entries_just_under_the_cap_still_imports(
        workspace, tmp_path, monkeypatch):
    """Positive control for the cap above. A real `session export` never writes a directory entry at
    all, but the fix must not refuse an archive that legitimately carries a handful under the
    ceiling. Without this, a cap that rejects everything with a directory entry in it would also
    pass, which is why the assertion is a successful import rather than an absence of one."""
    from requivo.deterministic.sessions import archives as det
    monkeypatch.setattr(det, "MAX_ARCHIVE_ENTRIES", 50)

    entries = {**_good_entries("s"), **_dir_entries("s", 30)}   # 2 files + 30 dirs = 32, under 50
    _zip(tmp_path / "ok.zip", entries)

    r = _run_json(["session", "import", str(tmp_path / "ok.zip"), "--json"])
    assert r["slug"] == "s"
    assert store.list_session_slugs() == ["s"]


def test_import_refuses_an_archive_that_is_not_a_session(workspace, tmp_path):
    # Extraction succeeding is not the same as having imported a session. Import used to declare
    # success on the strength of the extraction alone.
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])

    _zip(tmp_path / "badjson.zip", {"s/session.json": "{not json"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "badjson.zip"), "--json"])

    # A session that disagrees with itself about its own identity.
    _zip(tmp_path / "mismatch.zip", {**_good_entries("claimed")})
    with zipfile.ZipFile(tmp_path / "mismatch2.zip", "w") as z:
        meta = json.loads(_good_entries("claimed")["claimed/session.json"])
        meta["slug"] = "something-else"
        z.writestr("claimed/session.json", json.dumps(meta))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "mismatch2.zip"), "--json"])

    # A session claiming a model it does not carry.
    _zip(tmp_path / "noModel.zip", {"s/session.json": json.dumps(
        {"format_version": 1, "session_id": "a", "slug": "s", "created_at": "t", "updated_at": "t",
         "current_revision": 3})})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "noModel.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_a_collision_unless_forced(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dup.zip"), "--json"])
    assert store.read_meta("dup").current_revision == 1        # the original is untouched
    assert "The original." in store.session_request("dup")

    r = _run_json(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])
    assert r["replaced"] is True
    assert store.read_meta("dup").current_revision == 0        # genuinely replaced, not merged
    assert "A request." in store.session_request("dup")


# ── the archive-shape refusals name the archive, not the model (#101) ───────────
#
# #82 split `invalid_session` into a nine-arm family on the principle that a code must name its fact,
# and gave `unreadable_archive` and `inconsistent_archive` codes of their own. The seven shape
# refusals *between* those two arms — same function, same code path — kept `InvalidModelError`, whose
# docstring reads "a proposed model is structurally or semantically invalid". `cli.py` serializes
# `to_dict()` on every `--json` verb, so a consumer scripting `session import --json` read one handle
# for *my zip is too big*, *that slug is taken* and *your proposal is malformed*: three remedies
# behind one code, on the page that tells them to assert on the code and never on the message.
#
# One code and not seven, because the seven share a remedy — *give me a different archive*. What a
# single code owes in exchange is the thing #82 was actually about: `details` must not vary silently
# under it. `details["problem"]` is on every arm, so a consumer that needs the distinction branches
# on a key that is always there rather than on a `KeyError`.


def _import_error(archive) -> dict:
    """Drive the real CLI and return the structured envelope a `--json` consumer sees."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "import", str(archive), "--json"], client=None)
    assert e.value.code == 1
    return json.loads(buf.getvalue())


def _lower_the_byte_ceiling(mp):
    """The real 64 MiB ceiling is driven end-to-end by
    `test_import_refuses_an_archive_that_is_too_large_or_too_many_files`, which asserts this same
    code. Paying 64 MiB of allocation a second time to re-read the same branch buys nothing, so the
    ceiling moves instead of the archive — `_inspect_archive` reads the module global at call time."""
    from requivo.deterministic.sessions import archives as det
    mp.setattr(det, "MAX_ARCHIVE_BYTES", 32)


def _lower_the_entries_ceiling(mp, value=50):
    """The real ceiling (`MAX_ARCHIVE_ENTRIES`) is driven end-to-end by
    `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries`. Same
    reasoning as `_lower_the_byte_ceiling`: paying that scale twice buys nothing here."""
    from requivo.deterministic.sessions import archives as det
    mp.setattr(det, "MAX_ARCHIVE_ENTRIES", value)


@pytest.mark.parametrize("label, problem, build", [
    ("no entries at all", "empty",
     lambda p, mp: _zip(p, {})),
    ("more entries (directories included) than the entry ceiling", "too_many_entries",
     lambda p, mp: (_lower_the_entries_ceiling(mp),
                    _zip(p, {**_good_entries("s"), **_dir_entries("s", 51)}))),
    ("more entries than the ceiling", "too_many_files",
     lambda p, mp: _zip(p, {f"s/artifacts/f{i}.md": "x"
                            for i in range(MAX_ARCHIVE_FILES + 1)})),
    ("expands past the byte ceiling", "too_large",
     lambda p, mp: (_lower_the_byte_ceiling(mp), _zip(p, {"s/session.json": "0" * 64}))),
    ("a parent segment", "unsafe_entry",
     lambda p, mp: _zip(p, {"../escape/session.json": "{}"})),
    ("an absolute path", "unsafe_entry",
     lambda p, mp: _zip(p, {"/absolute/session.json": "{}"})),
    ("a Windows separator zipfile does not treat as a boundary", "unsafe_entry",
     lambda p, mp: _zip(p, {"..\\windows\\session.json": "{}"})),
    ("an entry loose at the root", "entry_outside_session_directory",
     lambda p, mp: _zip(p, {"loose.json": "{}"})),
    ("two session directories", "multiple_sessions",
     lambda p, mp: _zip(p, {**_good_entries("one"), **_good_entries("two")})),
])
def test_an_archive_shaped_wrong_is_refused_as_an_archive(workspace, tmp_path, monkeypatch,
                                                          label, problem, build):
    """#101. Every one of the seven shape refusals, driven through the CLI a consumer actually calls."""
    archive = tmp_path / "case.zip"
    build(archive, monkeypatch)

    err = _import_error(archive)
    assert err["code"] == "invalid_archive", f"{label} still answers {err['code']}"
    assert err["details"]["problem"] == problem, label
    assert store.list_session_slugs() == []


def test_the_shape_refusals_are_visible_to_a_consumer_that_did_not_enumerate_them(workspace, tmp_path):
    """The must-fire half of the case above: the harness can see a *good* archive land, so the seven
    reds are the refusal firing, not the fixture failing to build anything. Also the family question:
    `InvalidArchiveError` is an `InvalidSessionError`, so `unreadable_archive`/`inconsistent_archive`
    share one `except` (#101's asymmetry) -- deliberately not an `InvalidModelError`
    (`changelog.d/101`)."""
    from requivo.core.errors import InvalidArchiveError, InvalidModelError, InvalidSessionError

    assert issubclass(InvalidArchiveError, InvalidSessionError)
    assert not issubclass(InvalidArchiveError, InvalidModelError)
    assert InvalidArchiveError.code == "invalid_archive"

    # must fire: a well-formed archive still imports, so the seven refusals above mean something
    _zip(tmp_path / "good.zip", _good_entries("fine"))
    r = _run_json(["session", "import", str(tmp_path / "good.zip"), "--json"])
    assert r["slug"] == "fine"
    assert store.list_session_slugs() == ["fine"]


def test_an_occupied_slug_is_a_conflict_with_the_store_not_an_invalid_model(workspace, tmp_path,
                                                                           monkeypatch):
    """#101, the sharpest row: the vocabulary already had the right code. `session_exists` answers
    409 and its docstring is written for exactly this fact; the import path raised `invalid_model`
    and 400 instead — a *conflict with the store's current state* reported as a malformed proposal."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    err = _import_error(tmp_path / "dup.zip")
    assert err["code"] == "session_exists"
    assert err["details"] == {"slug": "dup"}
    assert "The original." in store.session_request("dup")   # and nothing was replaced

    # must fire: --force still lands, so the code above is the refusal and not a broken archive
    assert _run_json(["session", "import", str(tmp_path / "dup.zip"), "--force",
                      "--json"])["replaced"] is True


def test_every_refusal_on_the_import_path_names_what_it_is_about(workspace, tmp_path):
    """The table in `docs/cli.md` under *Importing a session*, asserted rather than described. Eight
    codes reach this verb; #101 found two of them sharing one code while their neighbours on the same
    path had names of their own. The count is load-bearing, not decoration: it is the drift guard for
    that table -- #114 added the eighth, `import_destination_occupied`, found by review after it
    nearly shipped unenumerated."""
    from requivo.core.errors import (
        ImportDestinationOccupiedError,
        InconsistentArchiveError,
        InvalidArchiveError,
        InvalidSessionError,
        SessionExistsError,
        UnreadableArchiveError,
    )

    # not a file at all — before anything is opened
    assert _import_error(tmp_path / "nowhere.zip")["code"] == "session_not_found"

    # a file that is not a zip
    (tmp_path / "text.zip").write_text("not a zip at all", encoding="utf-8")
    assert _import_error(tmp_path / "text.zip")["code"] == "unreadable_archive"

    # a zip whose shape is not an export
    _zip(tmp_path / "loose.zip", {"loose.json": "{}"})
    assert _import_error(tmp_path / "loose.zip")["code"] == "invalid_archive"

    # a zip whose one directory could not be a session
    _zip(tmp_path / "badname.zip", _good_entries("bad slug"))
    assert _import_error(tmp_path / "badname.zip")["code"] == "invalid_slug"

    # a zip whose session does not tell the truth about itself
    broken = _good_entries("claimed")
    broken["claimed/session.json"] = json.dumps(
        {**json.loads(broken["claimed/session.json"]), "slug": "something-else"})
    _zip(tmp_path / "lying.zip", broken)
    assert _import_error(tmp_path / "lying.zip")["code"] == "inconsistent_archive"

    # a zip that is fine, onto a slug that is taken
    _run(["session", "init", "The original.", "--slug", "taken", "--json"])
    _zip(tmp_path / "taken.zip", _good_entries("taken"))
    assert _import_error(tmp_path / "taken.zip")["code"] == "session_exists"

    # a zip that passed every check and could not be moved into place. The seventh code, and the one
    # this test claimed to cover while asserting six — found by the pre-1.0 release audit reading the
    # docstring against the body.
    #
    # Driven by patching `Path.replace` rather than by arranging a filesystem that refuses a rename:
    # the conditions that produce one differ per platform (ENOTEMPTY on POSIX, a held handle on
    # Windows), so a fixture would test the platform on some legs and nothing on others. The patch is
    # narrowed to the one destination under test, so the backup/restore path — which uses the same
    # call — is untouched and a failure here cannot come from the harness.
    _zip(tmp_path / "movefail.zip", _good_entries("move-fails"))
    doomed = store.canonical_dir("move-fails")
    real_replace = Path.replace

    def _refuse_only_the_target(self, dest):
        if Path(dest) == doomed:
            raise OSError(39, "Directory not empty")
        return real_replace(self, dest)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(Path, "replace", _refuse_only_the_target)
        assert _import_error(tmp_path / "movefail.zip")["code"] == "import_move_failed"
    finally:
        monkeypatch.undo()

    # must fire: the patch really was the cause, so the same archive lands once it is lifted. Without
    # this the assertion above would pass just as well against an import broken some other way.
    assert _run_json(["session", "import", str(tmp_path / "movefail.zip"), "--json"])["slug"] == "move-fails"

    # …and the eighth (#114): a zip that is fine, onto a slug held by something that is not a session
    # at all. It answers neither of its two nearest neighbours above — not `session_exists`, because
    # `--force` replaces a session and there is none here, and not `import_move_failed`, which is what
    # it used to answer and which describes a move that is not what went wrong. The `move-fails` case
    # just above is the proof that this one did not swallow it: that destination does not exist, so
    # this guard stays silent and the move failure is still reachable under its own code.
    _zip(tmp_path / "held.zip", _good_entries("held"))
    store.canonical_dir("held").mkdir(parents=True)
    assert _import_error(tmp_path / "held.zip")["code"] == "import_destination_occupied"

    # the three archive codes are one family, so `except InvalidSessionError` still catches every
    # archive refusal without enumerating them; the other three deliberately are not in it
    for cls in (UnreadableArchiveError, InvalidArchiveError, InconsistentArchiveError):
        assert issubclass(cls, InvalidSessionError), cls.__name__
    assert not issubclass(InvalidSlugError, InvalidSessionError)
    assert not issubclass(SessionExistsError, InvalidSessionError), (
        "an occupied slug is a conflict with the store, not a malformed session")
    assert not issubclass(ImportDestinationOccupiedError, InvalidSessionError), (
        "a destination holding no session is a conflict with the store, not a malformed session")

    # must fire: with all eight refusals asserted, a good archive still lands
    _zip(tmp_path / "good.zip", _good_entries("ok-one"))
    assert _run_json(["session", "import", str(tmp_path / "good.zip"), "--json"])["slug"] == "ok-one"


# A directory name inside an archive is caller text that has NOT been validated yet: `validate_slug`
# runs on the one surviving slug, after the count check, so the message that reports *more than one*
# is the single site in `_inspect_archive` that interpolates a raw, unvalidated, attacker-chosen
# string. Its two siblings on the same path already render an entry name with `!r`. Same class as
# #40 and #98, one function along.
_FORGED_SLUG = (
    "ok-session\n"
    "All clear, nothing to see.\n"
    "  ✅ sessions        0 in this workspace"
)


def test_an_archive_directory_name_cannot_write_a_line_of_the_refusal_reporting_it(workspace,
                                                                                   tmp_path):
    """Found by the audit of #101, on a line #101 edits. The refusal naming the directories it found
    is rendered to stderr by `cli.py`, and `safe_write` guards encoding, not control characters — so
    a top-level directory carrying a newline ends the line and writes the next one at column 0."""
    _zip(tmp_path / "forged.zip", {f"{_FORGED_SLUG}/session.json": "{}",
                                   "other/session.json": "{}"})

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        app(["session", "import", str(tmp_path / "forged.zip")], client=None)
    rendered = err.getvalue()

    # must fire: the refusal really did run and really did name what it found
    assert "session directories" in rendered, rendered
    assert "other" in rendered

    # the forgery does not reach the terminal as lines of its own
    assert "\nAll clear, nothing to see." not in rendered
    assert not any(_SESSIONS_ROW.match(line) for line in rendered.splitlines()), rendered
    # …and it is escaped rather than dropped, so nothing is hidden from the reader
    assert "ok-session" in rendered

    # `--json` was never exposed — json.dumps escapes a control character before it can reach a line
    out = _import_error(tmp_path / "forged.zip")
    assert out["code"] == "invalid_archive"
    assert _FORGED_SLUG in out["details"]["slugs"], "the raw name is still reported, losslessly"


def test_a_refused_import_leaves_no_scratch_directory(workspace, tmp_path):
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])
    assert list((workspace / ".requivo").glob(".import-*")) == []


def test_import_refuses_an_archive_whose_history_is_missing(workspace, tmp_path):
    """An archive can announce revision 2 and carry no `revisions/` at all — every file in it valid,
    every relationship between them false. Import checked shapes, so it accepted this and the damage
    surfaced later, somewhere unrelated. It now runs the same integrity check as `session verify`."""
    entries = _good_entries("s", revision=1)
    entries["s/session.json"] = json.dumps({
        "format_version": 1, "session_id": "abc", "slug": "s", "created_at": "t", "updated_at": "t",
        "current_revision": 2})                        # …with no revision log and no revision files
    _zip(tmp_path / "hollow.zip", entries)

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "hollow.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_a_file_that_is_not_an_archive(workspace, tmp_path):
    """`zipfile.BadZipFile` reached the user as a traceback. Every way a supplied file can be wrong
    has to arrive as a Requivo error."""
    bad = tmp_path / "notazip.zip"
    bad.write_text("this is not a zip")
    with pytest.raises(SystemExit) as e:
        _run(["session", "import", str(bad), "--json"])
    assert e.value.code == 1


def test_a_failed_forced_replacement_puts_the_original_back(workspace, tmp_path, monkeypatch):
    """`--force` used to `rmtree` the existing session and *then* move the new one in. If the move
    failed the user was left with neither: the archive refused, and the session they already had
    deleted. The old session now steps aside and only dies once the new one is in place."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    real_replace = Path.replace

    def failing_replace(self, target):
        # Only the move that brings the *imported* session into place fails; the step-aside and the
        # rollback must still work, which is the whole point.
        if ".import-" in str(self):
            raise OSError("simulated failure moving the imported session into place")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])

    assert store.session_exists("dup")
    assert store.read_meta("dup").current_revision == 1        # the original, intact
    assert "The original." in store.session_request("dup")
    assert _run_json(["session", "verify", "dup", "--json"])["ok"] is True


