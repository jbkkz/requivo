"""End-to-end tests of `session export` and `session import` — the archive half of
`requivo.deterministic.sessions`.

Split out of `test_cli_deterministic.py` by #141, and split away from the rest of the `session` noun
for a reason that is about the tests rather than about a line count: import is the one verb whose
input comes from outside the workspace, so it is the only one with a threat model. It owns four
fixtures nothing else reads, and twenty-one of the original file's seventy-three tests are refusals
of an archive. The rest of the noun is in `test_cli_sessions.py`.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
import shutil
import threading
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _SESSIONS_ROW, _full_model, _run, _run_json, _run_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError
from requivo.core.persistence import _REPLACE_ATTEMPTS
from requivo.deterministic.sessions.archives import MAX_ARCHIVE_FILES


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


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
    just put the session there.

    `imported` and `into` are **gone**, not kept as duplicates. Keeping both would leave the wrong
    spelling in the payload a reader copies from, and the whole reason this ships in the 1.0 release
    is that removing a `--json` key is breaking after it.

    **`path` is the session's own directory, not the session root**, and that is a correction to the
    issue as filed, which proposed `str(root)` — the value `into` carried. `session init --json`'s
    `path` is `canonical_dir(slug)`, and `session import`'s own human-readable line already prints
    `canonical_dir(slug)`. Renaming the key while leaving the container underneath it would give
    `path` two meanings across two verbs of the same noun — the defect this issue exists to close,
    reintroduced under the harmonised name and harder to see, because the key would then look right.
    """
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
    """#219. `MAX_ARCHIVE_FILES` and `MAX_ARCHIVE_BYTES` are both computed over `z.infolist()` with
    directory entries filtered out (`_inspect_archive`'s own `infos = [i for i in z.infolist() if not
    i.is_dir()]`), so an archive built from nothing but directory entries declares zero files and
    ~zero bytes and used to sail past both caps -- while the extraction loop still iterated the full
    `z.infolist()` and created every one of them, an inode/dir-creation exhaustion DoS through a door
    neither file-only cap covers. Lowered here for speed; the parametrized case below exercises the
    same `problem` code with the real ceiling. Must-fire, not just "raises": the refusal has to
    happen at `_inspect_archive`, before any extraction, so none of the (many) directories this
    archive names -- nor the session itself -- may exist afterwards."""
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
    all -- it walks real files only (`_cmd_session_export`'s `if f.is_file()`) -- but the fix must not
    refuse an archive that legitimately carries a handful, as long as the total stays under the
    ceiling. Without this, a cap that merely rejects everything with a directory entry in it would
    also make this test pass, which is why the assertion is a successful import rather than an
    absence of one."""
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
    reds are the refusal firing and not the fixture failing to build anything at all.

    Also the family question. `InvalidArchiveError` is an `InvalidSessionError`, so the arm on either
    side of it on this code path — `unreadable_archive` and `inconsistent_archive` — is caught by the
    same `except`, which is the asymmetry #101 is about. It is deliberately *not* an
    `InvalidModelError` any more; that is the breaking half, recorded in `changelog.d/101`."""
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
    and 400 instead — a *conflict with the store's current state* reported as a malformed proposal.

    The `--force` half is asserted in the same test on purpose: a refusal that cannot be lifted is a
    different product, and a code change must not turn the escape hatch off."""
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
    """The table in `docs/cli.md` under *Importing a session*, asserted rather than described.

    Eight codes reach this verb and each answers a different question. They are checked together
    because the defect #101 fixes was not any one of them being wrong — it was two of them being the
    *same* code while their neighbours on the identical code path had names of their own. A table
    that drifts back into a single handle fails here before a consumer discovers it.

    It was seven until #114 added `import_destination_occupied`, and the count is load-bearing rather
    than decoration: this test *is* the drift guard for that table, so a code that reaches the verb
    and is missing here is a row a consumer can hit and no test asserts. The count going stale while
    every assertion stayed green is how the eighth nearly shipped unenumerated — found by review.
    """
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
    a top-level directory carrying a newline ends the line and writes the next one at column 0.

    Two directories are needed to reach this arm at all, which is why the archive carries an innocent
    second one."""
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


# ── #113: a forced replacement is serialised against the writers of the session it replaces ─────
#
# `save_revision` resolves the session directory once and then writes by *pathname*; `session_lock`
# holds an fd on an *inode*. Those two descriptions agree only while nothing renames the directory,
# and `_swap_in` renames it. So a writer sitting inside `save_revision` while a forced import runs
# goes on writing into the *newly imported* directory, and a third process opening the lock finds a
# different inode and acquires immediately — two writers holding one slug's lock, which is invariant
# 9's own failure mode.
#
# The guard is that the lock no longer lives inside the directory being renamed (`.requivo/locks/`),
# so `_swap_in` holds it like every other compound write. A test asserting only that the import
# succeeded passes on the defect; these two assertions do not.


def _paused_between_the_writes(monkeypatch, slug: str, at_the_gate, release):
    """Freeze `save_revision` on `slug` between its file writes and its metadata write.

    That gap is where the damage lands: revisions/NNNN-model.json and model.json are already on
    disk, `session.json` is about to be written through a freshly resolved `canonical_dir(slug)`,
    and the lock is held across the whole of it. Patching `write_meta` rather than sleeping keeps
    the window deterministic on every leg instead of timing-dependent on the slow ones.

    **Patched on the `Store` class, not the module function** (#272). `store.save_revision` is now
    a thin ambient-default wrapper over `Store.save_revision`, which calls `self.write_meta(...)` —
    a method lookup on the class, not the module-level `write_meta` name -- so patching the module
    function no longer intercepts it. The class attribute is what every instance's `self.write_meta`
    actually resolves to, ambient or explicit alike, which is what makes this the correct target now."""
    real_write_meta = store.Store.write_meta

    def paused(self, s, meta):
        if s == slug and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, s, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)


def test_a_forced_import_serialises_against_a_concurrent_writer(workspace, tmp_path, monkeypatch):
    """#113, both halves.

    **Contamination.** The imported `session.json` must still carry the *imported* identity. On the
    defect the paused writer's `write_meta` resolves `<root>/dup` after the swap and stamps the
    replaced session's `session_id` and revision log onto the import.

    **Mutual exclusion.** The swap must not happen at all while a writer holds the lock, and no
    third caller may hold it alongside that writer. On the defect the import runs straight through —
    it takes no lock — and the swap then puts a *different inode* at the lock path, so a third
    caller acquires at once while the writer still holds the old, now-unlinked one.

    The two exclusion assertions are negative, so they carry their positive controls in the same
    fixture: once the writer is released the import must complete **and** the probe must acquire. A
    thread that died on its first line satisfies every "did not happen" assertion here, and nothing
    else would notice.

    The probe deliberately starts *after* the import's window has elapsed. Started alongside it, the
    probe usually opens the old lock file before the swap and blocks on the writer, which is the
    right answer reached for the wrong reason — measured: that ordering passed on the unfixed tree."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    resident_id = store.read_meta("dup").session_id
    _zip(tmp_path / "dup.zip", _good_entries("dup"))          # a different session, session_id "abc"
    assert resident_id != "abc", "the two sessions must be distinguishable by identity"

    at_the_gate, release = threading.Event(), threading.Event()
    _paused_between_the_writes(monkeypatch, "dup", at_the_gate, release)

    failures: list[BaseException] = []

    def _capture(fn):
        def run():
            try:
                fn()
            except BaseException as e:                        # noqa: BLE001 - reported, not swallowed
                failures.append(e)
        return run

    writer = threading.Thread(target=_capture(
        lambda: store.save_revision("dup", _engine_output_for_dup())), daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"

    imported = threading.Event()

    def _import_then_signal():
        _run(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])
        imported.set()

    importer = threading.Thread(target=_capture(_import_then_signal), daemon=True)
    importer.start()

    # Long enough for an unguarded import to have finished the whole swap; under the guard it is
    # still blocked on the writer's lock. Measured at ~0.1s unguarded, so 1.5s is not a close call.
    assert not imported.wait(1.5), (
        "the forced import replaced the session while a writer held its lock")

    probe_acquired = threading.Event()
    probe = threading.Thread(target=_capture(
        lambda: _take_the_lock("dup", probe_acquired)), daemon=True)
    probe.start()
    assert not probe_acquired.wait(0.5), (
        "a third caller acquired the lock while a writer held it — the swap moved the lock away")

    release.set()
    for t in (writer, importer, probe):
        t.join(timeout=20)
    assert not [t for t in (writer, importer, probe) if t.is_alive()], "a thread did not finish"
    assert failures == [], f"a worker raised: {failures}"
    # The positive controls for the two negatives above: released, both must actually happen.
    assert imported.is_set(), "the positive control failed: the import never completed at all"
    assert probe_acquired.is_set(), "the positive control failed: the probe never took the lock at all"

    meta = store.read_meta("dup")
    assert meta.session_id == "abc", "the import inherited the replaced session's identity"
    assert meta.current_revision == 0 and meta.revisions == []
    assert store.list_session_slugs() == ["dup"]
    assert _run_json(["session", "verify", "dup", "--json"])["ok"] is True


def _take_the_lock(slug: str, acquired) -> None:
    with store.session_lock(slug):
        acquired.set()


def _engine_output_for_dup():
    from requivo.core.contracts import EngineOutput

    return EngineOutput.model_validate(_full_model())



def test_export_excludes_the_lock_file_and_waits_for_the_writer(workspace, tmp_path, monkeypatch):
    """An export reads several files that must agree with each other. Read outside the lock, it can
    combine an old session.json with a new model.json — an archive that is internally inconsistent and
    only says so on import. And `.lock` is this machine's coordination, not part of the session: it
    has no meaning in an archive and would import as a session component.

    Since #113 a live writer no longer leaves a `.lock` in the session at all — it is at
    `.requivo/locks/<slug>.lock`, outside every session directory, and the export walks only the
    session. The exclusion stays and is still asserted, for the residue: a session written by an
    older Requivo carries one, and a hand-made archive can carry one too. So the fixture plants it
    rather than waiting for a writer to, which is also the only way this half of the test can still
    fail.

    **The lock-wait assertion has two ways to be vacuous, and #293 is both of them.** `held.is_set()`
    used to be read *after* `t.join(timeout=10)` — but `join()` blocks until the writer thread has
    finished, and the writer always calls `held.set()` on its way out regardless of whether export
    ever waited for it, so the read after `join()` is True whether or not the guard fired. Confirmed
    by monkeypatching `store.session_lock` to a no-op and re-running the original assertion order: it
    still passed. So the read now happens the instant `session export` returns, before `t.join()` —
    which can only be True if export's own read blocked on the lock the writer was holding. And the
    `time.sleep(0.05)` that used to stand in for "the writer has the lock by now" is a scheduling
    guess with no positive control of its own; a slow runner can let export start before the writer
    ever acquires, which would make this test measure nothing while still reading green. `acquired`
    is the control: the writer signals the instant it takes the lock, and export does not start
    until that signal is observed, so a hang on `acquired.wait` fails loudly instead of the test
    passing over uncontended code."""
    import time

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    assert store.lock_path("s").exists(), "the writer left its lock outside the session"
    assert not (store.canonical_dir("s") / ".lock").exists()
    (store.canonical_dir("s") / ".lock").touch()               # legacy residue, as an older one left it

    acquired = threading.Event()
    held = threading.Event()

    def hold_the_lock():
        with store.session_lock("s"):
            acquired.set()
            time.sleep(0.4)
            held.set()

    t = threading.Thread(target=hold_the_lock)
    t.start()
    assert acquired.wait(10), "the writer never took the lock — contention was never reached"
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])
    # Read before join(): join() waits for the writer to finish regardless of whether export waited
    # for it, so held would read True either way by the time join() returns. Read here, it can only
    # be set if export's own read blocked until the writer released the lock.
    assert held.is_set(), "the export read the session while a writer held it"
    t.join(timeout=10)

    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert not [n for n in names if ".lock" in n]
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names


def test_session_export_survives_a_transient_permission_error(workspace, tmp_path, monkeypatch):
    """The same cause invariant 18's `_atomic_write` and `session restore`'s `_replace_with_retry`
    already retry: on Windows the final `rename` of the freshly written archive can fail with a
    transient `PermissionError` when a scanner or the Search Indexer briefly opens it microseconds
    after it lands (#524). `_cmd_session_export`'s `tmp.replace(dest)` now goes through the same
    helper, the identical shape to `test_session_restore_survives_a_transient_permission_error`."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)

    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])
    assert attempts["n"] == 3, "the export did not actually go through the retry path"
    assert dest.exists(), "the retried rename never landed the archive"
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names


def test_session_export_still_gives_up_on_a_permanent_permission_error(workspace, tmp_path,
                                                                          monkeypatch):
    """Bounded, and the bound is the point: a genuinely unwritable destination must still fail
    loudly and quickly, and leave no completed archive behind under a scratch name — the failure
    mode #524 was filed for is a finished export that reports a traceback while `finally:
    tmp.unlink(missing_ok=True)` quietly deletes it."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)

    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    dest = tmp_path / "s.zip"
    with pytest.raises(PermissionError):
        app(["session", "export", "s", "-o", str(dest)], client=None)
    # The attempt count is what makes this a test of the *retry* rather than of `replace`: without
    # it every assertion here holds identically with the loop deleted (found in review of #483).
    assert attempts["n"] == _REPLACE_ATTEMPTS, (
        f"expected exactly {_REPLACE_ATTEMPTS} attempts before giving up, got {attempts['n']}")
    assert not dest.exists()
    assert not list(tmp_path.glob(".*.part")), "scratch left behind after a failed export"


# ── #111: a session created while the archive was being read is not destroyed ───
#
# `session import` used to decide the collision question twice: `session_exists(slug) and not
# --force` before the extraction, and `replaced = target.exists()` after it. Between those two
# decisions sits the whole unzip, and a session created in that window was moved aside and then
# `rmtree`d — destroyed by an import whose user was never asked for `--force`, because at the moment
# they would have been asked there was nothing to force past.
#
# Invariant 9 in the one verb that writes a whole session: a precondition is held across the writes
# it authorises. The two arms below are what holds it — the free-slug arm claims by rename, which is
# invariant 11's rule and the only thing that makes the window safe rather than merely narrow.


def test_a_session_created_during_the_extraction_window_is_refused_not_destroyed(
        workspace, tmp_path, monkeypatch):
    """The defect, driven through the real CLI.

    `_validate_extracted` runs after the archive is on disk and before anything is moved into place,
    so patching it is the honest way to stand inside the window without reaching into the store."""
    from requivo.deterministic.sessions import archives as det

    _zip(tmp_path / "race.zip", _good_entries("race"))
    real = det._validate_extracted

    def _claim_the_slug_mid_import(d, slug):
        real(d, slug)
        # A concurrent creator wins the slug while this import is still in scratch space.
        if not store.session_exists(slug):
            _run(["session", "init", "The one that was already here.", "--slug", slug, "--json"])
    monkeypatch.setattr(det, "_validate_extracted", _claim_the_slug_mid_import)

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "race.zip"), "--json"])

    # The whole point: the session that appeared is still there, unmodified.
    assert store.session_exists("race") is True
    assert "The one that was already here." in store.session_request("race")


def test_that_window_refusal_names_the_conflict_rather_than_a_move_failure(
        workspace, tmp_path, monkeypatch):
    """`session_exists` / 409, the same code the guard would have raised — not `import_move_failed`.

    The caller is entitled to the answer they would have got had the timing been different, and the
    remedy is the same one: pass `--force`. `import_move_failed` would send them looking at their
    filesystem for a fault that is not there."""
    from requivo.deterministic.sessions import archives as det

    _zip(tmp_path / "race.zip", _good_entries("race"))
    real = det._validate_extracted

    def _claim_the_slug_mid_import(d, slug):
        real(d, slug)
        if not store.session_exists(slug):
            _run(["session", "init", "Mine.", "--slug", slug, "--json"])
    monkeypatch.setattr(det, "_validate_extracted", _claim_the_slug_mid_import)

    envelope = _import_error(tmp_path / "race.zip")
    assert envelope["code"] == "session_exists", envelope
    assert envelope["details"]["slug"] == "race"
    assert "--force" in envelope["message"]


def test_the_ordinary_import_arms_still_work_so_the_guard_is_not_a_blanket_refusal(
        workspace, tmp_path, monkeypatch):
    """The positive control for both arms. A fix that refused every import, or that never replaced,
    would pass the two tests above and fail here."""
    _zip(tmp_path / "fresh.zip", _good_entries("fresh"))
    r = _run_json(["session", "import", str(tmp_path / "fresh.zip"), "--json"])
    assert r["replaced"] is False
    assert store.session_exists("fresh") is True

    r = _run_json(["session", "import", str(tmp_path / "fresh.zip"), "--force", "--json"])
    assert r["replaced"] is True
    assert store.session_exists("fresh") is True


# ── a stray directory at the slug answers the same on every platform (#114) ─────
#
# The free-slug arm claims the slug with `os.replace`, and that call is where the platforms part
# company. On POSIX an **empty** destination directory is replaced silently; on Windows `os.replace`
# is `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`, which Microsoft documents as unusable when
# either name is a directory, so *any* existing destination fails there — empty or not. One stray
# `mkdir` therefore imported on macOS and failed on Windows, and the Windows failure arrived as
# `import_move_failed`: *could not move the imported session into place*. Both halves are defects,
# and the second is the worse one, because it names a cause that is not the cause.


@pytest.mark.parametrize("label, populate", [
    # ASCII ids on purpose: a parametrize label becomes a node id, which is written to a console
    # whose codepage is not the source file's.
    ("empty - the case the two platforms disagree about", lambda d: None),
    ("holding a file", lambda d: (d / "junk.txt").write_text("x", encoding="utf-8")),
    ("holding only a stray .lock", lambda d: (d / ".lock").write_text("", encoding="utf-8")),
])
def test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform(workspace, tmp_path,
                                                                           label, populate):
    """#114. All three rows are one answer now — `import_destination_occupied`, before the rename is
    attempted, so the verdict is the guard's rather than the platform's.

    The claim is still the rename (invariant 11). This guard only ever *refuses*, so it cannot
    authorise an import the rename would have lost; what it removes is the platform from the answer,
    and `import_move_failed` from a sentence that was never about a move.
    """
    _zip(tmp_path / "stray.zip", _good_entries("stray"))
    target = store.canonical_dir("stray")
    target.mkdir(parents=True)
    populate(target)
    before = sorted(p.name for p in target.iterdir())

    err = _import_error(tmp_path / "stray.zip")
    assert err["code"] == "import_destination_occupied", f"{label}: {err}"
    assert err["details"]["slug"] == "stray"
    assert str(target) in err["details"]["path"]
    # the old message sent the reader at their filesystem looking for a fault that is not there
    assert "could not move" not in err["message"], label
    # …and it must not offer the one remedy that cannot work: `--force` replaces a *session*, and
    # the whole point of this arm is that there is no session here
    assert "does not apply here" in err["message"], label

    # nothing was imported, and the directory the caller put there is untouched — an import does not
    # delete a directory it cannot interpret
    assert store.list_session_slugs() == []
    assert sorted(p.name for p in target.iterdir()) == before

    # must fire: with the stray gone the same archive lands. Without this the assertion above would
    # pass just as well against an import broken some other way, or against a harness that never
    # built an archive at all.
    shutil.rmtree(target)
    assert _run_json(["session", "import", str(tmp_path / "stray.zip"), "--json"])["slug"] == "stray"


def test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure(
        workspace, tmp_path, monkeypatch):
    """The second half of #114, and why the guard is called from two places rather than one.

    The pre-check runs *before* the rename, so a stray directory that lands during the window
    between them reaches the `except OSError` arm instead — which knew only *a session appeared* and
    *a move failed*, and answered the second for a destination that holds no session.

    The stray is non-empty on purpose. An empty one is precisely the case POSIX's `os.replace`
    swallows, so on this leg the rename would succeed and the test would exercise nothing.
    """
    _zip(tmp_path / "late.zip", _good_entries("late"))
    target = store.canonical_dir("late")
    real_replace = Path.replace
    armed = [True]

    def _a_stray_lands_in_the_window(self, dest):
        if armed[0] and Path(dest) == target:
            target.mkdir(parents=True, exist_ok=True)
            (target / "junk.txt").write_text("x", encoding="utf-8")
        return real_replace(self, dest)

    monkeypatch.setattr(Path, "replace", _a_stray_lands_in_the_window)
    err = _import_error(tmp_path / "late.zip")
    assert err["code"] == "import_destination_occupied", err
    assert err["details"]["slug"] == "late"
    assert "could not move" not in err["message"], err
    assert store.list_session_slugs() == []

    # must fire: disarm the patch, clear the stray, and the same archive lands. The patch is turned
    # off with a flag rather than `monkeypatch.undo()`: `workspace` takes the same function-scoped
    # `monkeypatch` object, so undoing here would also revert `REQUIVO_WORKSPACE` and import the
    # session into whatever directory the suite happens to be running from.
    armed[0] = False
    shutil.rmtree(target)
    assert _run_json(["session", "import", str(tmp_path / "late.zip"), "--json"])["slug"] == "late"


def test_the_occupied_destination_is_a_conflict_with_the_store_and_not_a_malformed_session():
    """Where the new code sits in the vocabulary, asserted rather than described.

    Not an `InvalidSessionError`: that family means *a session on disk is malformed*, and here there
    is no session at all — the ten-arm enumeration in
    `test_every_arm_of_the_family_names_a_distinct_fact` would otherwise go red for a condition that
    is not one of its own. Not a `SessionExistsError` either, because that code carries a remedy
    (*pass --force*) which does nothing against a directory the store never made.
    """
    from requivo.core.errors import (
        ImportDestinationOccupiedError,
        InvalidSessionError,
        RequivoError,
        SessionExistsError,
    )

    assert ImportDestinationOccupiedError.code == "import_destination_occupied"
    assert issubclass(ImportDestinationOccupiedError, RequivoError)
    assert not issubclass(ImportDestinationOccupiedError, InvalidSessionError)
    assert not issubclass(ImportDestinationOccupiedError, SessionExistsError)


def test_import_into_a_fresh_workspace_writes_the_privacy_gitignore(workspace, tmp_path):
    """#211's second door. `create_session` is the call that normally brings `.requivo/` into
    existence, and it is not the only one: `session import` reaches a workspace that may have none,
    creates the session root, and moves an archive into it. A guarantee written at creation alone
    would be absent for exactly the user who received a colleague's session before running one of
    their own -- and that archive holds *their* client's request text verbatim, which is the whole
    point of the ignore file.

    Invariant 14's sentence, one directory up: creation is not the only door."""
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()

    _zip(tmp_path / "s.zip", _good_entries("s"))
    _run(["session", "import", str(tmp_path / "s.zip"), "--json"])

    assert store.list_session_slugs() == ["s"]
    assert marker.exists(), "session import created .requivo/ without the privacy marker"
