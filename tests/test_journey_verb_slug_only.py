"""#402 -- eight of the ten journey verbs advertised a model.json path they could not open, and
`SessionService.resolve_slug` mined one anyway: `p.parent.name`, unconditionally, whether or not the
file existed. A nonexistent path was reported on under a slug carved out of its own parent
directory -- a name the user never typed -- and worse, a session that happened to share that name
was operated on silently.

`status` and `impact` are unaffected: they resolve a real path through `cli.py`'s `_resolve_ref`,
which reads the file's own bytes directly and never goes near `resolve_slug`. The other eight --
`answer`, `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release` -- never open the file
they are handed at all: they resolve a *slug* and then read and write the store's own copy
(`ArtifactService.save` refuses anything that is not `has_meta(slug)`, so a loose file has nowhere to
file an artifact against). So a path was never a meaningful input for these eight, and the fix is to
say so before ever mining one: `SessionService.resolve_slug(..., accept_path=False)` refuses a
path-shaped reference outright, naming exactly what was given.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from _cli_harness import _full_model

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import SessionNotFoundError
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _fails(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        # client=None is the default-construction path, not a poison pill (#419); the conftest
        # net is what guarantees an accidental provider reach refuses cleanly on every machine.
        app(argv, client=None)
    assert exc.value.code == 1
    return capsys.readouterr().err


# ── `SessionService.resolve_slug` itself ──────────────────────────────────────


def test_resolve_slug_refuses_a_model_json_path_when_the_caller_opted_out(tmp_path):
    """The eight write verbs pass `accept_path=False`. A model.json-shaped reference is refused
    outright, naming exactly what was given -- never mined for a slug."""
    ref = str(tmp_path / "loose" / "model.json")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref, accept_path=False)
    assert exc.value.details["ref"] == ref
    assert "loose" not in str(exc.value).replace(ref, ""), (
        "the refusal must not name a slug mined from the path's parent directory")


def test_resolve_slug_still_accepts_a_bare_slug_when_paths_are_refused():
    """Must-fire control: refusing paths must not refuse an ordinary slug too."""
    assert SessionService().resolve_slug("leave-approval", accept_path=False) == "leave-approval"


def test_the_path_refusal_cannot_forge_a_second_line_of_its_own_message():
    """Found in review of this same change: the refusal echoes `ref` twice, and the first mention
    went through `display_token` while the second did not -- so a reference carrying a real newline
    and a real ANSI escape introducer forged a second, differently-coloured line of output that never
    came from this refusal (#40, invariant 14). Both mentions must escape it identically."""
    hostile = "evil/model.json\n\x1b[31mFAKE ERROR: session corrupted\x1b[0m"
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(hostile, accept_path=False)
    rendered = str(exc.value)
    assert "\n" not in rendered, f"a raw newline in the refusal can forge a line of its own: {rendered!r}"
    assert "\x1b" not in rendered, f"a raw ANSI escape survived into the refusal: {rendered!r}"
    # must fire: the escaping must not have thrown the reference away outright.
    assert "evil" in rendered and "model.json" in rendered


def test_resolve_slug_no_longer_mines_a_nonexistent_model_json_path(tmp_path):
    """The root cause, independent of `accept_path`: a caller that *does* still want path support
    (every `deterministic/` verb) must not get a slug carved out of a file that was never written --
    that slug can coincidentally name a real session, which is the worse half of #402."""
    ref = str(tmp_path / "loose" / "model.json")
    assert SessionService().resolve_slug(ref) == ref   # named as given, not mined


def test_resolve_slug_still_mines_a_real_saved_model_json(tmp_path):
    """Must-fire control: the existence gate must not break the legitimate case -- a real model.json
    really does live under its own session's directory name."""
    d = tmp_path / "leave-approval"
    d.mkdir()
    (d / "model.json").write_text("{}", encoding="utf-8")
    assert SessionService().resolve_slug(str(d / "model.json")) == "leave-approval"


@pytest.fixture
def _unreadable_model_json(tmp_path, request):
    """A directory `resolve_slug`'s `is_file()` probe cannot see into, on the same terms as
    `tests/test_unexaminable_entries.py`'s `blocked` fixture: `chmod 000` denies the `x` bit, so a
    stat on the `model.json` inside it raises `PermissionError`, not `False`.

    Found in review of #402: the new `Path.is_file()` gate that stops a nonexistent path from being
    mined re-raises everything that is not ENOENT/ENOTDIR (`core/persistence/identifiers.py`'s
    `_probe` exists for exactly this shape) -- so an unreadable directory used to escape as a bare
    traceback instead
    of the clean refusal every other verb failure produces."""
    d = tmp_path / "noaccess"
    d.mkdir()
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that "
                    "resolve_slug converts a PermissionError probing a model.json path into a "
                    "clean SessionNotFoundError rather than an uncaught traceback.")
    d.chmod(0o000)
    ref = str(d / "model.json")
    try:
        Path(ref).is_file()
    except PermissionError:
        return ref
    pytest.skip("chmod 000 did not deny the is_file() probe on this run (running as root?). "
                "UNTESTED HERE: the could-not-tell arm of resolve_slug's model.json branch.")


def test_an_unreadable_model_json_path_refuses_cleanly_instead_of_crashing(_unreadable_model_json):
    """The must-fire half: without the `except OSError` guard this raises a bare `PermissionError`
    that escapes `cli.py`'s `app()` (it only catches `RequivoError`/`KeyboardInterrupt`/
    `UnicodeEncodeError`) as an unhandled traceback."""
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(_unreadable_model_json)
    assert exc.value.details["ref"] == _unreadable_model_json


# ── the eight write verbs, end to end ─────────────────────────────────────────

_WRITE_VERB_ARGV = [
    ["answer", "some answers"],
    ["brief"],
    ["prd"],
    ["stories"],
    ["estimate"],
    ["criteria"],
    ["epic"],
    ["release"],
]


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_a_nonexistent_model_json_path_is_refused_naming_the_path(tail, workspace, capsys):
    """The reproduction in the issue, run through every one of the eight verbs: the path given, not
    a slug carved out of it, is what the refusal names."""
    verb, *rest = tail
    ref = str(workspace / "loose" / "model.json")
    err = _fails([verb, ref, *rest], capsys)
    assert ref in err, f"`requivo {verb}` does not name the path it was given: {err!r}"


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_a_nonexistent_model_json_path_does_not_silently_use_an_unrelated_real_session(
        tail, workspace, capsys):
    """The worse half. A session named `loose` really exists elsewhere in the workspace; the path
    the user gave points nowhere and its parent happens to share that name. This must never resolve
    to the real `loose` session -- paired with the must-fire positive below, which proves `loose` is
    reachable by its own slug, so this is not merely a harness that never runs anything."""
    store.create_session("loose", "an unrelated real session")
    verb, *rest = tail
    ref = str(workspace / "loose" / "model.json")   # does not exist; "loose" only coincides in name
    err = _fails([verb, ref, *rest], capsys)
    assert "no session named loose" not in err.lower(), (
        f"`requivo {verb}` reported on the unrelated real session instead of the given path: {err!r}")
    assert ref in err


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_the_real_session_is_still_reachable_by_its_own_slug(tail, workspace, capsys):
    """Must-fire control for the pair above: resolution by slug still succeeds, so the negative
    result above is not an artifact of a harness that refuses everything. Resolution succeeding means
    the failure (if any, past this point) is no longer `session_not_found`."""
    store.create_session("loose", "a real session, referenced by its own slug")
    verb, *rest = tail
    with pytest.raises(SystemExit):
        app([verb, "loose", *rest], client=None)
    err = capsys.readouterr().err
    assert "no session named" not in err.lower(), (
        f"`requivo {verb} loose` failed to resolve its own real session: {err!r}")


def test_status_and_impact_still_open_a_model_json_path_directly(workspace):
    """The two verbs this issue leaves untouched: they read the file's own bytes and never go near
    `resolve_slug`, so they are immune to this bug by construction and their wider help stays true."""
    import io
    from contextlib import redirect_stdout

    from requivo.core.contracts import EngineOutput

    loose = workspace / "elsewhere" / "model.json"
    loose.parent.mkdir(parents=True)
    loose.write_text(EngineOutput.model_validate(_full_model()).model_dump_json(), encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        app(["status", str(loose)], client=None)
    assert "UNDERSTANDING" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        app(["impact", str(loose)], client=None)
    assert "DEPENDENCY MAP" in buf.getvalue()

# ── the directory branch, one over from the model.json/session.json branch (#414) ─────────────


def test_resolve_slug_refuses_a_directory_that_is_not_a_session(tmp_path):
    """#414. `resolve_slug`'s directory branch used to mine ANY directory's own name --
    `p.exists() and p.is_dir()`, with nothing checking whether a session actually lives behind
    it. An arbitrary directory reference must be refused naming the path as given, never a slug
    carved from a segment of it, on the same terms #402 already holds for the model.json/
    session.json branch."""
    d = tmp_path / "elsewhere" / "loose"
    d.mkdir(parents=True)
    (d / "unrelated.txt").write_text("nothing session-shaped in here")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(str(d))
    assert exc.value.details["ref"] == str(d)


def test_resolve_slug_still_mines_a_real_session_directory(tmp_path):
    """Must-fire control: a directory that really is a session (it carries its own session.json
    or model.json) must still resolve by its own name -- the fix must not refuse everything."""
    d = tmp_path / "leave-approval"
    d.mkdir()
    (d / "session.json").write_text("{}", encoding="utf-8")
    assert SessionService().resolve_slug(str(d)) == "leave-approval"


def test_resolve_slug_still_mines_a_real_legacy_session_directory(tmp_path):
    """Must-fire control, the legacy-shaped sibling: a directory carrying its own model.json (the
    legacy marker, not the canonical session.json) is exactly as real a session and must still
    resolve by its own name."""
    d = tmp_path / "legacy-slug"
    d.mkdir()
    (d / "model.json").write_text("{}", encoding="utf-8")
    assert SessionService().resolve_slug(str(d)) == "legacy-slug"


def test_a_directory_reference_does_not_silently_use_an_unrelated_real_session(workspace, capsys):
    """The worse half, matching #402's own pattern one branch over. A session named `loose`
    really exists in the canonical store; the directory the user gave is a genuinely different,
    unrelated directory that merely shares that final path segment and holds nothing
    session-shaped. This must never resolve to the real `loose` session -- paired with the
    must-fire positive control below, which proves `loose` stays reachable by its own slug, so
    this is not merely a harness that refuses everything."""
    store.create_session("loose", "an unrelated real session")
    ref_dir = workspace / "elsewhere" / "loose"
    ref_dir.mkdir(parents=True)
    (ref_dir / "unrelated.txt").write_text("not a session")

    with pytest.raises(SystemExit) as exc:
        app(["session", "show", str(ref_dir)], client=None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no session named loose" not in err.lower(), (
        f"`requivo session show` reported on the unrelated real session instead of the given "
        f"directory: {err!r}")
    assert str(ref_dir) in err


def test_the_real_session_stays_reachable_by_its_own_slug_past_the_directory_guard(
        workspace, capsys):
    """Must-fire control for the pair above: resolution by slug still succeeds after the
    directory-branch fix, so the negative result above is not an artifact of a harness that
    refuses everything. `session show` on a real, existing slug does not raise -- it prints the
    receipt and returns -- so the control is that it runs clean rather than that it exits."""
    store.create_session("loose", "a real session, referenced by its own slug")
    app(["session", "show", "loose"], client=None)
    out = capsys.readouterr().out
    assert "no session named" not in out.lower(), (
        f"`requivo session show loose` failed to resolve its own real session: {out!r}")
    assert "loose" in out


@pytest.fixture
def _unreadable_session_directory(tmp_path, request):
    """A directory `resolve_slug`'s directory-branch marker probe cannot see into, on the same
    terms as `_unreadable_model_json` above and `test_unexaminable_entries.py`'s `blocked`
    fixture: `chmod 000` denies the `x` bit, so a stat on a marker file inside it raises
    `PermissionError`, not `False`."""
    d = tmp_path / "noaccess-dir"
    d.mkdir()
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that "
                    "resolve_slug converts a PermissionError probing a directory's own marker "
                    "file into a clean SessionNotFoundError rather than an uncaught traceback.")
    d.chmod(0o000)
    try:
        (d / "session.json").exists()
    except PermissionError:
        return d
    pytest.skip("chmod 000 did not deny the session.json probe on this run (running as root?). "
                "UNTESTED HERE: the could-not-tell arm of resolve_slug's directory branch.")


def test_an_unreadable_session_directory_refuses_cleanly_instead_of_crashing(
        _unreadable_session_directory):
    """The must-fire half: without an `except OSError` guard around the marker probe this raises
    a bare `PermissionError` that escapes `cli.py`'s `app()` as an unhandled traceback."""
    ref = str(_unreadable_session_directory)
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref)
    assert exc.value.details["ref"] == ref


def test_a_directory_reference_under_a_blocked_ancestor_refuses_cleanly_too(tmp_path, request):
    """Found in review of #414 itself. The referenced directory's own contents being unreadable is
    not the only way this branch's probes can raise: `p.exists()`/`p.is_dir()` on the *entry gate*
    -- unchanged by this fix, and still outside any `try` -- independently stat `p` itself, which
    re-raises `PermissionError` when an ANCESTOR of the reference denies traversal, a distinct case
    from the referenced directory's own contents being blocked (which is all `_unreadable_session_
    directory` above exercises). A directory that is otherwise perfectly healthy -- it carries its
    own `session.json` -- must still refuse cleanly rather than crash, purely because something
    above it on the path could not be traversed."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that the "
                    "directory branch's entry gate (not just its marker probe) converts an "
                    "ancestor PermissionError into a clean SessionNotFoundError.")
    parent = tmp_path / "blocked-parent"
    parent.mkdir()
    target = parent / "session-slug"
    target.mkdir()
    (target / "session.json").write_text("{}", encoding="utf-8")
    request.addfinalizer(lambda: parent.chmod(0o755))
    parent.chmod(0o000)
    ref = str(target)
    try:
        Path(ref).exists()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 on the parent did not deny the exists() probe on this run "
                    "(running as root?). UNTESTED HERE: the ancestor-blocked arm of the "
                    "directory branch's entry gate.")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref)
    assert exc.value.details["ref"] == ref

