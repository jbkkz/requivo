"""`doctor` (and `session list`) on what is under the session root that is not a session — #67."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _full_model, _run, _run_json, _run_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError
from requivo.services.sessions import SessionService


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check — the one carrying a tick."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


# ── something under the session root that is not a session (#67) ────────────────
#
# The state under test cannot be produced by the current code (#22).


def _lock_ghost(name: str = "leave-approval") -> Path:
    """A session directory as an older Requivo left one: the name taken, holding only `.lock`."""
    d = store.session_root() / name
    d.mkdir(parents=True)
    (d / ".lock").touch()
    return d


def test_doctor_names_what_is_under_the_session_root_and_is_not_a_session(workspace):
    """Nothing could see one of these: `list_session_slugs` filters on `session.json` (#67)."""
    clean = _run_json(["doctor", "--json"])["sessions"]
    assert clean["non_sessions"] == [], "the control: an untouched workspace must produce no finding"
    assert "other entries" not in _run(["doctor"])

    _lock_ghost()
    found = _run_json(["doctor", "--json"])["sessions"]

    # What was found, never what it was taken to mean: there is no `is_lock_ghost` key anywhere.
    assert [e["name"] for e in found["non_sessions"]] == ["leave-approval"]
    entry = found["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [".lock"] and entry["entry_count"] == 1
    assert entry["error"] is None
    assert entry["slug_shaped"] is True, "the name is one `create_session` can be asked for"

    # It is still not a session, and the session count must not quietly absorb it.
    assert found["total"] == 0 and found["readable"] is True
    assert found["inconsistent"] == {}

    text = _run(["doctor"])
    assert "leave-approval" in text and ".lock" in text
    assert "✅" in _check_line(text, "sessions"), "0 sessions is still the honest count"
    assert "🟡" in _check_line(text, "other entries")


def test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens(workspace):
    """The finding is only worth a line because of what it costs, so the cost is pinned rather than described."""
    _lock_ghost()
    assert "will not get it" in _run(["doctor"]), "the report names the finding but not its cost"

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug != "leave-approval"
    assert meta.slug.startswith("leave-approval-")


def test_a_file_where_a_session_name_would_go_costs_the_same_and_is_named_as_a_file(workspace):
    """Swept rather than assumed: the rename onto an existing *file* fails too."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").write_text("half a download\n", encoding="utf-8")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "file"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed here; there is simply nothing to look inside"
    assert entry["slug_shaped"] is True

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug.startswith("leave-approval-")


def _deny_listing(directory: Path) -> None:
    """Make `directory` traversable but not listable (#80)."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny listing on Windows — the entry-level "
                    "could-not-look arm is untested on this platform")
    directory.chmod(0o111)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod --x did not deny listing here (running as root?) — the entry-level "
                "could-not-look arm is untested on this run")


def test_a_symlink_is_reported_as_one_and_its_target_is_not_read(workspace, tmp_path):
    """`Path.is_dir()` follows a symlink, so a link at a slug name pointing elsewhere reported `kind:
    "directory"`."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret-project.md").touch()
    store.session_root().mkdir(parents=True)
    try:
        (store.session_root() / "leave-approval").symlink_to(elsewhere, target_is_directory=True)
    except OSError:                            # pragma: no cover - Windows without developer mode
        pytest.skip("this platform refuses an unprivileged symlink — the symlink arm is untested "
                    "on this run")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "symlink", "is_dir() follows the link; this answer must not"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed — we declined to follow it"
    assert entry["slug_shaped"] is True, "the name is still taken, whatever it points at"
    assert "secret-project.md" not in _run(["doctor"]), (
        "the target's contents were listed into a report about this workspace")


def test_a_name_too_long_to_be_a_slug_is_not_marked_as_taken(workspace):
    """`slug_shaped` asked `_SLUG_RE` alone, and validity is the pattern **and** the length."""
    over = "a" * (store.MAX_SLUG_LENGTH + 1)
    at_limit = "b" * store.MAX_SLUG_LENGTH
    for name in (over, at_limit):
        (store.session_root() / name).mkdir(parents=True)
        (store.session_root() / name / ".lock").touch()

    by_name = {e["name"]: e for e in _run_json(["doctor", "--json"])["sessions"]["non_sessions"]}
    assert by_name[at_limit]["slug_shaped"] is True, "the control: this one really is reachable"
    assert by_name[over]["slug_shaped"] is False

    # And the claim the flag stands for is the one the code makes: a refusal, not a substitution.
    with pytest.raises(InvalidSlugError):
        store.canonical_dir(over)


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                     "'con' already on disk, which Windows itself refuses to create at the OS level "
                     "regardless of anything Requivo's own code does (see core/persistence/identifiers.py's "
                     "comment above _RESERVED_DEVICE_NAMES). REASONED, NOT OBSERVED on an actual "
                     "Windows machine; it follows from the documented behaviour #221 already relies "
                     "on for the reserved-name refusal itself. UNTESTED ON WINDOWS: the whole of "
                     "#408, and unreachable there, since a `con` directory can never exist to be "
                     "described in the first place.")
def test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken(workspace):
    """#408: `_describe_non_session` asked `is_slug` for `slug_shaped`."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "con").mkdir()
    (store.session_root() / "con" / ".lock").touch()   # non-empty: rename(2) would collide, not win
    (store.session_root() / "leave-approval").mkdir()

    by_name = {e["name"]: e for e in _run_json(["doctor", "--json"])["sessions"]["non_sessions"]}
    assert by_name["con"]["slug_shaped"] is True, (
        "create_session would lose its rename to this directory, exactly like any other taken name"
    )
    assert by_name["leave-approval"]["slug_shaped"] is True, "the control: unaffected by the fix"

    text = _run(["doctor"])
    assert "[name taken]" in text

    meta = SessionService().create_session("A request.", slug="con")
    assert meta.slug != "con"
    assert meta.slug.startswith("con-"), "the silent hash-suffixed substitution the hint warns of"


def test_an_empty_directory_is_still_reported_and_still_marked(workspace):
    """The one shape whose cost is platform-dependent, and the report deliberately does not try to be clever."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").mkdir()

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [] and entry["entry_count"] == 0
    assert entry["error"] is None, "we looked, and it is empty — not the same as could not look"
    assert entry["slug_shaped"] is True
    assert "an empty directory" in _run(["doctor"])

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    if os.name == "nt":                                     # pragma: no cover - platform-dependent
        assert meta.slug.startswith("leave-approval-"), "os.rename refuses any existing destination"
    else:
        assert meta.slug == "leave-approval", "rename(2) replaces an empty destination directory"


def test_the_name_taken_hint_names_what_import_does_about_it(workspace):
    """#114, and the composition defect it would otherwise have shipped."""
    from requivo.core.errors import ImportDestinationOccupiedError

    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").mkdir()
    hint = _run(["doctor"])

    assert "[name taken]" in hint, "the control: the marked row and its hint really were printed"
    assert ImportDestinationOccupiedError.code in hint, (
        "the hint does not say what `session import` now does about this directory")
    assert "only symptom" not in hint, "the hint still claims the hash-suffixed name is the only one"
    # the older consequence is still true and must not have been dropped on the way
    assert "plus a hash" in hint


def test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty(workspace):
    """The third state one level below the one `_session_health` already has."""
    d = _lock_ghost()

    # The must-fire control, on the same directory, with only its mode changing.
    readable = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert readable["entries"] == [".lock"] and readable["error"] is None

    _deny_listing(d)
    try:
        denied = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
        denied_text = _run(["doctor"])
    finally:
        d.chmod(0o755)

    assert denied["kind"] == "directory", "we can stat it; we cannot list it"
    assert denied["entries"] is None and denied["entry_count"] is None
    assert "Permission denied" in (denied["error"] or "")
    assert "empty directory" not in denied_text
    assert "Permission denied" in denied_text


def test_a_name_read_off_disk_cannot_forge_a_line_of_the_report_that_names_it(workspace):
    """#40 in a new render site. The entry's own name and the names it holds are both read off disk."""
    d = _lock_ghost()
    try:
        (d / "x\n  ✅ forged          all clear").touch()
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of an entry name is untested on this run")

    text = _run(["doctor"])
    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in text.splitlines()

    # `--json` was never affected and must stay that way.
    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert any("\n" in n for n in entry["entries"])

    # The other permutation, on the entry's *own* name rather than a name it holds.
    (store.session_root() / "y\n  ✅ forged          all clear").mkdir()
    both = _run(["doctor"])
    assert both.count("\\n") >= 2, "the directory's own name reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in both.splitlines()


def test_a_forged_name_that_holds_a_session_json_cannot_forge_a_line_either(workspace):
    """The permutation the tests either side of this one do not reach, and the gap was real."""
    forged = "evil\n     └─ ok: all clear"
    try:
        d = store.session_root() / forged
        d.mkdir(parents=True)
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of a session name is untested on this run")
    (d / "session.json").write_text('{"not": "valid session metadata"}', encoding="utf-8")

    text = _run(["doctor"])
    lines = text.splitlines()

    # must fire: the entry really did reach the *sessions* bucket rather than the non-session one.
    assert any("inconsistent" in ln for ln in lines), text

    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "     └─ ok: all clear`" not in lines, "a stored name wrote a line of doctor's report"
    # One row for one entry.
    assert len([ln for ln in lines if ln.startswith("     └─ ")]) == 1, text


def test_session_list_does_not_call_one_of_these_a_session(workspace):
    """The other half of the partition, and why this is not `session list`'s finding to report."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()

    rows = _run_json(["session", "list", "--json"])["sessions"]
    assert [r["slug"] for r in rows] == ["real"]
    assert store.list_session_slugs() == ["real"]

    text = _run(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_the_parts_of_the_session_root_are_one_partition(workspace):
    """The three parts of the session root come out of one predicate (#67)."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()

    scanned_slugs, scanned_others, scanned_blind = store.scan_session_root()
    slugs = set(scanned_slugs)
    others = {e.name for e in scanned_others}
    blind = {e.name for e in scanned_blind}
    on_disk = {p.name for p in store.session_root().iterdir()}

    # must fire: the slugs half of the scan agrees with the dedicated reader.
    assert slugs == set(store.list_session_slugs())

    assert slugs == {"real"} and others == {"leave-approval"}
    assert blind == set(), "nothing here is unexaminable; the populated case is its own module"
    assert slugs & others == set() and slugs & blind == set() and others & blind == set()
    assert on_disk - (slugs | others | blind) == {".real.new-1-abcdef12"}


def test_doctor_and_verify_flag_a_session_whose_context_card_is_gone(workspace, tmp_path):
    """A session's `context_cards` are validated once, at creation (#13)."""
    cards = tmp_path / "cards"
    cards.mkdir()
    card = cards / "lost-domain.md"
    card.write_text("# Lost domain\n\nSome product context.\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])

        # ── healthy: the card is where the session left it ────────────────────
        healthy_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert healthy_doctor["unresolved_cards"] == {}
        assert healthy_doctor["inconsistent"] == {}
        healthy_verify = _run_json(["session", "verify", "s", "--json"])
        assert healthy_verify["ok"] is True
        assert healthy_verify["context_cards"]["checked"] is True
        assert healthy_verify["context_cards"]["problem"] is None
        healthy_text = _run(["session", "verify", "s"])

        # ── broken: the card is gone, and nothing else changed ────────────────
        card.unlink()

        broken_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert "s" in broken_doctor["unresolved_cards"]
        assert broken_doctor["unresolved_cards"]["s"]["code"] == "unknown_context_card"
        assert "lost-domain" in broken_doctor["unresolved_cards"]["s"]["details"]["unknown"]
        # It is not an *integrity* problem: the directory still tells the truth about itself.
        assert broken_doctor["inconsistent"] == {}
        assert "✅" not in _check_line(_run(["doctor"]), "sessions")

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", "s", "--json"], client=None)
        assert e.value.code == 1
        report = json.loads(buf.getvalue())
        assert report["ok"] is False
        assert report["problems"] == []            # nothing is wrong *inside* the directory
        assert report["context_cards"]["checked"] is True
        assert report["context_cards"]["problem"]["code"] == "unknown_context_card"

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            app(["session", "verify", "s"], client=None)
        broken_text = buf.getvalue()

    assert healthy_text != broken_text
    assert "lost-domain" in broken_text and "lost-domain" not in healthy_text
    assert "REQUIVO_CONTEXT_DIR" in broken_text, "the reader is not told how to recover"


def test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken(workspace, monkeypatch):
    """#263/#265: a first draft of `SessionLockedError` handling in `_session_health` still built a
    default-severity `IntegrityProblem`, driving the same ❌ glyph a genuinely broken session gets."""
    from requivo.core.errors import SessionLockedError
    from requivo.deterministic import doctor as doctor_mod

    _run(["session", "init", "A real one.", "--slug", "locked-one", "--json"])

    healthy = _run_json(["doctor", "--json"])["sessions"]
    assert healthy["inconsistent"] == {}
    assert healthy.get("locked", {}) == {}
    assert "✅" in _check_line(_run(["doctor"]), "sessions")

    real_inspect = doctor_mod.inspect_session

    def locked_for_our_slug(slug):
        if slug == "locked-one":
            raise SessionLockedError(
                "session 'locked-one' is locked by another process; retry in a moment",
                details={"slug": slug})
        return real_inspect(slug)

    monkeypatch.setattr(doctor_mod, "inspect_session", locked_for_our_slug)

    found = _run_json(["doctor", "--json"])["sessions"]
    assert found["inconsistent"] == {}, (
        f"a lock timeout must not be reported as an integrity problem: {found['inconsistent']}")
    assert "locked-one" in found.get("locked", {})

    text = _run(["doctor"])
    sessions_line = _check_line(text, "sessions")
    assert "❌" not in sessions_line, (
        "a session that is merely locked must not earn the same glyph as a broken one")
    assert "🟡" in sessions_line
    assert "locked" in sessions_line.lower()


def test_a_note_does_not_move_the_sessions_glyph(workspace, monkeypatch):
    """`noted` is deliberately absent from the glyph expression in `_print_sessions`."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _run_stdin(["artifact", "save", "s", "--type", "prd", "--file", "-", "--revision", "1",
                "--json"], "# PRD", monkeypatch)
    d = store.canonical_dir("s")
    raw = json.loads((d / "session.json").read_text(encoding="utf-8"))
    raw["artifact_status"]["risk-register"] = dict(raw["artifact_status"]["prd"],
                                                    filename="risk-register.md")
    (d / "session.json").write_text(json.dumps(raw), encoding="utf-8")
    (d / "artifacts" / "risk-register.md").write_text("# Risk register\n", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["sessions"]
    assert r["inconsistent"] == {} and r["notes"] == {"s": ["unknown_artifact_type"]}

    text = _run(["doctor"])
    sessions_line = _check_line(text, "sessions")
    assert "✅" in sessions_line, (
        "a note must not earn the same middle glyph as a could-not-look finding")


def test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick(workspace):
    """Review finding on #483."""
    from requivo.core.persistence import UnexaminableEntry
    from requivo.deterministic import doctor as det

    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    real_scan = store.scan_session_root

    def _one_blind_entry():
        slugs, non_sessions, _blind = real_scan()
        return slugs, non_sessions, [UnexaminableEntry(name="ghost", error="Permission denied")]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det.store, "scan_session_root", _one_blind_entry)
        r = _run_json(["doctor", "--json"])["sessions"]
        text = _run(["doctor"])

    assert r["inconsistent"] == {} and r["error"] is None
    assert [e["name"] for e in r["unexaminable"]] == ["ghost"]
    sessions_line = _check_line(text, "sessions")
    assert "✅" not in sessions_line, sessions_line
    assert "🟡" in sessions_line, sessions_line


def test_context_can_be_asked_for_by_session(workspace):
    # A session's card selection is held constant across its turns.
    _run(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    _run(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow = _run(["context", "--session", "narrow"])
    wide = _run(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow
    assert len(narrow) < len(wide)          # the subset really is a subset
    assert narrow == _run(["context", "--cards", "b2b-platform"])

    with pytest.raises(SystemExit):         # the two selectors are alternatives
        _run(["context", "--session", "narrow", "--cards", "b2b-platform"])


