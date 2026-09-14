"""`doctor` (and `session list`) on what is under the session root that is not a session — #67.

Split out of `test_cli_doctor.py` by #555, once that file outgrew one module; `test_cli_doctor.py`
covers the verb's own health checks (credentials, model source, context cards), and
`test_cli_doctor_lock_residue.py` covers the lock-root partition (#180). The shared harness is
`tests/_cli_harness.py`; `_check_line` is duplicated from `test_cli_doctor.py` rather than imported,
per this suite's own convention of keeping test-module helpers local (`tests/_fakes.py` makes the
argument).

Two tests here drive `session list` and the store's own three-way partition rather than `doctor`.
They are the same finding read from the other side — a listing must not grow a row for something
that is not a session — and they share `_lock_ghost` and the #67 narrative with the rest of this
file, so they stay where that argument is written down.
"""
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
    """The status line for the named doctor check — the one carrying a tick.

    Matched on the two-space indent a check line has, because the indented detail lines beneath it
    mention the same words (`     sessions        <path>` sits right above `  ✅ sessions …`), and a
    tick asserted against the wrong line is an assertion about nothing."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


# ── something under the session root that is not a session (#67) ────────────────
#
# The state under test cannot be produced by the current code: #22 stopped `session_lock` creating
# the session directory it opened `.lock` inside, which is exactly why these are only ever found on
# disk and never in a fresh run. So the fixture builds one by hand. Going through `session_lock`
# instead would assert against a state this version cannot reach, and would go green on the day the
# report stopped working.


def _lock_ghost(name: str = "leave-approval") -> Path:
    """A session directory as an older Requivo left one: the name taken, holding only `.lock`."""
    d = store.session_root() / name
    d.mkdir(parents=True)
    (d / ".lock").touch()
    return d


def test_doctor_names_what_is_under_the_session_root_and_is_not_a_session(workspace):
    """Nothing could see one of these: `list_session_slugs` filters on `session.json`, so a directory
    holding only `.lock` reached no verb at all -- `doctor` printed a green `0 in this workspace`
    over it. Invariant 11's second half (#67): `scan_session_root` answers from one listing so two
    scans don't miss a name landing between them; `doctor` reports what is there, never concluding
    what it is."""
    clean = _run_json(["doctor", "--json"])["sessions"]
    assert clean["non_sessions"] == [], "the control: an untouched workspace must produce no finding"
    assert "other entries" not in _run(["doctor"])

    _lock_ghost()
    found = _run_json(["doctor", "--json"])["sessions"]

    # What was found, never what it was taken to mean: there is no `is_lock_ghost` key anywhere. A
    # half-extracted archive is this shape too, and the directory is the only evidence there is.
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
    """The finding is only worth a line because of what it costs, so the cost is pinned rather than
    described. `create_session`'s rename is the only claim on a slug (invariant 11) and it loses to a
    non-empty directory, after which `SessionService` falls through to its hash-suffixed candidate:
    the user gets a session under a name they did not ask for, with nothing saying why."""
    _lock_ghost()
    assert "will not get it" in _run(["doctor"]), "the report names the finding but not its cost"

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug != "leave-approval"
    assert meta.slug.startswith("leave-approval-")


def test_a_file_where_a_session_name_would_go_costs_the_same_and_is_named_as_a_file(workspace):
    """Swept rather than assumed: the rename onto an existing *file* fails too, `d.exists()` is true,
    and the caller gets the identical substitution. Reporting only directories would have left an
    identical symptom with an identical remedy invisible, so each entry says what it is instead of
    the report assuming they are all directories."""
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
    """Make `directory` traversable but not listable — `--x`, the mode under which `stat` on a child
    succeeds and `iterdir` does not — or skip loudly naming what went untested.

    Deliberately not `chmod 000`, which denies the `session.json` probe in `_scan_session_root` as
    well and so exercises a *different* state: the entry never reaches `_describe_non_session` at
    all, because the partition above it could not decide what the entry is. That is #80, fixed since,
    and it has its own module — `tests/test_persistence_scan.py`. What this fixture is for is the
    entry the partition *did* place, whose contents then could not be listed."""
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
    "directory"`, and `iterdir` beneath it listed the **target's** filenames into a report about this
    workspace. Found by review; a symlink is a third shape this module already treats as the case a
    containment guard has to answer for (invariant 17)."""
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
    """`slug_shaped` asked `_SLUG_RE` alone, and validity is the pattern **and** the length: an
    81-character kebab-case directory matched the pattern and was marked `[name taken]`, under a
    sentence promising a silent hash-suffixed substitution, and `canonical_dir` refuses that name
    outright instead. Found by review. The 80-character sibling beside it is the must-fire
    control: same shape, one character shorter, and it *is* reachable."""
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
    """#408: `_describe_non_session` asked `is_slug` for `slug_shaped` -- the unconditional,
    creation-time refusal -- so a `con` directory holding no `session.json` read `slug_shaped: False`
    and `doctor` stayed silent, while `create_session('con', ...)` disagrees via #372's conditional
    read-time rule -- exactly the `[name taken]` consequence. `leave-approval` is the must-fire
    control."""
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
    """The one shape whose cost is platform-dependent, and the report deliberately does not try to be
    clever: POSIX `rename(2)` replaces an empty destination so `create_session` still wins the name
    here, Windows refuses any existing destination so it does not. `slug_shaped` therefore does not
    exempt an empty directory -- occasionally conservative beats right-on-one-platform-silent-on-
    another."""
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
    """The third state one level below the one `_session_health` already has: the root listed fine,
    this directory did not. `entries: []` would say we looked and it holds nothing — the one reading
    that makes the finding worthless, since on POSIX a directory holding nothing is the single shape
    that does not cost the caller its slug at all (`rename(2)` replaces an empty destination)."""
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
    """#40 in a new render site. The entry's own name and the names it holds are both read off disk,
    untrusted exactly as a stored context-card name is. Printed bare, one carrying a newline does not
    merely look odd: it ends the line and starts another at whatever column it chooses, immediately
    under a row of `doctor`'s own output."""
    d = _lock_ghost()
    try:
        (d / "x\n  ✅ forged          all clear").touch()
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of an entry name is untested on this run")

    text = _run(["doctor"])
    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in text.splitlines()

    # `--json` was never affected and must stay that way: json.dumps escapes a control character
    # before it can reach a line of its own, so the finding keeps its bytes verbatim.
    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert any("\n" in n for n in entry["entries"])

    # The other permutation, on the entry's *own* name rather than a name it holds. The two reach
    # the report through different f-strings, so one covering the other is an assumption.
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
    # Without this the assertions below would pass against a report that never mentioned it at all.
    assert any("inconsistent" in ln for ln in lines), text

    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "     └─ ok: all clear`" not in lines, "a stored name wrote a line of doctor's report"
    # One row for one entry. The forged text is built to look like a second, so counting the rows is
    # what separates *escaped* from *merely reordered*.
    assert len([ln for ln in lines if ln.startswith("     └─ ")]) == 1, text


def test_session_list_does_not_call_one_of_these_a_session(workspace):
    """The other half of the partition, and why this is not `session list`'s finding to report: a
    listing of sessions must not grow a row for something that is not one. The real session beside it
    is the must-fire control — without it this passes against a listing that lists nothing at all."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()

    rows = _run_json(["session", "list", "--json"])["sessions"]
    assert [r["slug"] for r in rows] == ["real"]
    assert store.list_session_slugs() == ["real"]

    text = _run(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_the_parts_of_the_session_root_are_one_partition(workspace):
    """The three parts of the session root come out of one predicate (#67), worth having as a set
    only while nothing can fall between them. Three rather than two since #80: an entry the
    predicate could not decide about belongs in neither of the other two. Read through
    `scan_session_root` since #300 -- the only way to the second part now -- so all three come from
    one listing, which is what makes the partition claim real."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()

    scanned_slugs, scanned_others, scanned_blind = store.scan_session_root()
    slugs = set(scanned_slugs)
    others = {e.name for e in scanned_others}
    blind = {e.name for e in scanned_blind}
    on_disk = {p.name for p in store.session_root().iterdir()}

    # must fire: the slugs half of the scan agrees with the dedicated reader, so the partition
    # asserted below is over the same names every other call path sees.
    assert slugs == set(store.list_session_slugs())

    assert slugs == {"real"} and others == {"leave-approval"}
    assert blind == set(), "nothing here is unexaminable; the populated case is its own module"
    assert slugs & others == set() and slugs & blind == set() and others & blind == set()
    assert on_disk - (slugs | others | blind) == {".real.new-1-abcdef12"}


def test_doctor_and_verify_flag_a_session_whose_context_card_is_gone(workspace, tmp_path):
    """A session's `context_cards` are validated once, at creation. The cards live *outside* the
    session directory, so the answer can change afterwards without the session changing — and since
    `load_context` refuses an unresolvable selection (#13), the session is hard-stopped at its next
    (paid) turn while doctor still calls it healthy."""
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
    """#263/#265, caught by review before this shipped: a first draft of the `SessionLockedError`
    handler in `_session_health` still built a default-severity `IntegrityProblem`, so `blocking()`
    kept it and it landed straight in `inconsistent` -- driving the identical ❌ glyph a genuinely
    broken session gets, which is exactly the accusation shape this whole issue family exists to
    remove. A lock timeout must land in its own bucket and the warning glyph, never the failure one."""
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
    """`noted` is deliberately absent from the glyph expression in `_print_sessions`: a note is not
    a defect and not a could-not-look, so it must not pull the tick down to the warning glyph the
    way `locked`/`blind`/`unchecked` do. It is still counted and named in the notes below the line."""
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
    """Review finding on #483: the sessions-row glyph docstring in `doctor.py` says `unexaminable`
    (`blind`) shares the middle glyph with `locked`/`unchecked`, and only the `locked` half of that
    claim had a test -- `test_persistence_scan.py`'s own doctor test never checks the glyph at
    all. A blocked entry with no other finding must not tick clean: a could-not-look reading as
    looked-and-found-nothing is the exact defect `_session_health` exists to prevent."""
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
    # A session's card selection is held constant across its turns; a later turn that reads every card
    # reasons from a wider context than the model was built on. Asking by session makes that unmissable.
    _run(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    _run(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow = _run(["context", "--session", "narrow"])
    wide = _run(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow
    assert len(narrow) < len(wide)          # the subset really is a subset
    assert narrow == _run(["context", "--cards", "b2b-platform"])

    with pytest.raises(SystemExit):         # the two selectors are alternatives
        _run(["context", "--session", "narrow", "--cards", "b2b-platform"])


