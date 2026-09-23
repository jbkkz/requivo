"""`doctor`, `schema` and `context` (#141): credentials, cards, the session root's non-sessions (#67) and the
lock root's residue (#180) -- every reported state keeps one test; the tier itself is frozen."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from _credentials import _no_credentials
from _fakes import deny_access, run_cli, run_cli_exit, run_cli_json, seed_session

from conftest import symlink_or_skip
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError, SessionLockedError
from requivo.deterministic import doctor as det
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

_NO_LEVER = "REASONED, NOT OBSERVED on Windows: a reserved device name cannot exist on disk there, so neither can this fixture"
_NEEDS_POSIX_NAMES = pytest.mark.skipif(store.fcntl is None, reason=_NO_LEVER)
_NEEDS_SYMLINK = pytest.mark.skipif(os.name == "nt", reason="os.symlink needs elevated privileges on Windows by default")
_NEEDS_CHAIN = pytest.mark.skipif(
    not hasattr(__import__("anthropic._client", fromlist=["default_credentials"]), "default_credentials"),
    reason="the installed anthropic SDK has no profile discovery chain; an unloadable ANTHROPIC_PROFILE is unreachable")
_DENIED = PermissionError("Permission denied")

_BARE_META = {"session_id": "deadbeef", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
              "provider": None, "model_name": None, "context_cards": None, "current_revision": 0,
              "format_version": 1, "revisions": [], "artifact_status": {}}


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check -- the one carrying a tick."""
    return next(ln for ln in text.splitlines() if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


def _doctor(section: str | None = None):
    r = run_cli_json(["doctor", "--json"])
    return r[section] if section else r


def _doctor_when(target, name, value, section=None):
    """`doctor` as JSON and as text, with `target.name` patched for the duration of both calls."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(target, name, value)
        return _doctor(section), run_cli(["doctor"])


def _bare_session(name: str, request: bool = True) -> Path:
    """A session directory written by hand, under a name `create_session` may refuse today (#372)."""
    d = store.session_root() / name
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    if request:
        (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({**_BARE_META, "slug": name}), encoding="utf-8")
    return d


def _lock_ghost(name: str = "leave-approval") -> Path:
    """A session directory as an older Requivo left one: the name taken, holding only `.lock`."""
    d = store.session_root() / name
    d.mkdir(parents=True)
    (d / ".lock").touch()
    return d


def _lock_files(*names: str) -> Path:
    """Empty files under the lock root, as `session_lock`/`_discovery_guard` or a stray writer would leave them."""
    lr = store.lock_root()
    lr.mkdir(parents=True, exist_ok=True)
    for name in names:
        (lr / name).write_text("", encoding="utf-8")
    return lr


def _take_lock(slug: str) -> None:
    with store.session_lock(slug):
        pass


def _deny(directory: Path, mode: int, what: str, request) -> None:
    """Deny `what` with a POSIX mode bit, probed by `iterdir` (which 3.14's `exists()` swallows), or skip naming it."""
    request.addfinalizer(lambda: directory.chmod(0o755))
    if os.name == "nt":
        pytest.skip(f"POSIX mode bits do not deny {what} on Windows; that arm is untested here")
    directory.chmod(mode)
    try:
        list(directory.iterdir())
    except OSError:
        return
    pytest.skip(f"chmod did not deny {what} here (running as root?); that arm is untested on this run")


def _raise(exc):
    def _boom(*a, **k):
        raise exc
    return _boom


@pytest.fixture
def card(tmp_path, monkeypatch) -> Path:
    """One card, `lost-domain`, in a context directory of its own, with a session grounded on it."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n", encoding="utf-8")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))
    run_cli(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])
    return cards


# ── credentials and the model source ──────────────────────────────────────────────


def test_doctor_still_says_no_api_key_when_none_is_configured_at_all(monkeypatch):
    """The must-not-fire twin of the unloadable-profile case, and a missing key is never a hard failure."""
    _no_credentials(monkeypatch)
    r = _doctor()
    assert r["schema"]["ok"] and r["schema"]["slots"] > 0 and "sessions" in r["workspace"]
    assert r["provider_anthropic"] == {**r["provider_anthropic"], "api_key_present": False, "credential_problem": None}
    assert "no API key" in _check_line(run_cli(["doctor"]), "anthropic")
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")  # #268: `model.source` asks `current_model_name()`
    assert _doctor("model") == {"name": "claude-opus-4-8", "source": "env"}


def test_doctor_reports_a_bearer_token_as_a_credential_present(monkeypatch):
    """#332: `ANTHROPIC_AUTH_TOKEN` is a credential the runner accepts (#201)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    assert _doctor("provider_anthropic")["api_key_present"] is True


@_NEEDS_CHAIN
def test_doctor_names_the_remedy_for_an_unloadable_profile_rather_than_no_api_key(monkeypatch):
    """#365: "no credential" and "a configured credential that cannot load" are two answers, not one False."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")
    r = _doctor("provider_anthropic")
    assert r["api_key_present"] is False and "could not load the credential configuration" in r["credential_problem"]
    assert "a-profile-that-does-not-exist" in r["credential_problem"], "the SDK's own reason names the profile"
    text = run_cli(["doctor"])
    assert "no API key" not in _check_line(text, "anthropic"), "the wrong remedy for an unloadable profile"
    assert "could not be loaded" in text and "a-profile-that-does-not-exist" in text


# ── doctor's own failures must not render as green ticks (#12) ───────────────────


def test_doctor_reports_where_the_write_lock_lives():
    """#113 moved the lock out of the session directory; a convention the diagnostic does not report is answered wrong."""
    r = _doctor("workspace")
    assert r["locks"] == str(store.lock_root()) and r["sessions"] == str(store.session_root())
    assert not store.lock_root().is_relative_to(store.session_root()) and "locks" in run_cli(["doctor"])


def test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one():
    """Three states, three renderings; a card failure is never written into `schema["error"]` (#12)."""
    healthy, healthy_text = _doctor("context"), run_cli(["doctor"])
    assert healthy["ok"] is True and healthy["status"] == "ok" and healthy["count"] > 0 and healthy["error"] is None
    empty, empty_text = _doctor_when(det, "available_cards", list)
    assert empty["context"]["ok"] is False and empty["context"]["status"] == "empty" and empty["context"]["count"] == 0
    broken, broken_text = _doctor_when(det, "available_cards", _raise(OSError("boom")))
    assert broken["context"]["ok"] is False and broken["context"]["status"] == "unreadable"
    assert "boom" in (broken["context"]["error"] or "") and "boom" in broken_text
    for r in (empty, broken):
        assert r["schema"]["ok"] is True and r["schema"]["error"] is None
    assert "✅" in _check_line(healthy_text, "context cards")
    assert "✅" not in _check_line(empty_text, "context cards") and "✅" not in _check_line(broken_text, "context cards")
    assert healthy_text != empty_text != broken_text


def test_doctor_tells_an_empty_workspace_from_an_unreadable_one():
    """`_session_health` used to answer `total: 0` for a root it could not list (#67)."""
    empty, empty_text = _doctor("sessions"), run_cli(["doctor"])
    assert empty["total"] == 0 and empty["readable"] is True and empty["error"] is None and empty["non_sessions"] == []
    unreadable, unreadable_text = _doctor_when(det.store, "scan_session_root", _raise(_DENIED), "sessions")
    assert unreadable["readable"] is False and unreadable["total"] is None and unreadable["non_sessions"] is None
    assert "Permission denied" in (unreadable["error"] or "") and "Permission denied" in unreadable_text
    assert "✅" in _check_line(empty_text, "sessions") and "✅" not in _check_line(unreadable_text, "sessions")
    assert "0 in this workspace" in empty_text and "0 in this workspace" not in unreadable_text
    assert "unreadable" in unreadable_text


def test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty(card, request):
    """The `unreadable` state reached by what makes a directory unreadable; the session is not accused (#12)."""
    healthy = _doctor()
    assert healthy["context"]["status"] == "ok" and "lost-domain" in healthy["context_cards"]
    assert healthy["sessions"]["cards_checked"] is True and healthy["sessions"]["unresolved_cards"] == {}
    _deny(card, 0o000, "reads", request)
    broken, broken_text = _doctor(), run_cli(["doctor"])
    assert broken["context"]["status"] == "unreadable" and broken["context"]["ok"] is False
    assert "lost-domain" not in broken["context_cards"]
    assert broken["sessions"]["cards_checked"] is False and broken["sessions"]["unresolved_cards"] == {}
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "✅" not in _check_line(broken_text, "sessions") and "not checked" in _check_line(broken_text, "sessions")


def test_doctor_and_verify_flag_a_session_whose_context_card_is_gone(card):
    """A session's `context_cards` are validated once, at creation (#13); a lost card is not an integrity problem."""
    healthy, healthy_verify = _doctor("sessions"), run_cli_json(["session", "verify", "s", "--json"])
    assert healthy["unresolved_cards"] == {} and healthy["inconsistent"] == {}
    assert healthy_verify["ok"] is True and healthy_verify["context_cards"]["checked"] is True
    assert healthy_verify["context_cards"]["problem"] is None
    healthy_text = run_cli(["session", "verify", "s"])
    (card / "lost-domain.md").unlink()
    broken = _doctor("sessions")
    assert broken["unresolved_cards"]["s"]["code"] == "unknown_context_card"
    assert "lost-domain" in broken["unresolved_cards"]["s"]["details"]["unknown"]
    assert broken["inconsistent"] == {} and "✅" not in _check_line(run_cli(["doctor"]), "sessions")
    out, code = run_cli_exit(["session", "verify", "s", "--json"])
    broken_text, text_code = run_cli_exit(["session", "verify", "s"])
    report = json.loads(out)
    assert code == 1 and text_code == 1 and report["ok"] is False and report["problems"] == []
    assert report["context_cards"]["checked"] is True and report["context_cards"]["problem"]["code"] == "unknown_context_card"
    assert "lost-domain" in broken_text and "lost-domain" not in healthy_text
    assert "REQUIVO_CONTEXT_DIR" in broken_text, "the reader is not told how to recover"


# ── something under the session root that is not a session (#67) ─────────────────


def test_doctor_names_what_is_under_the_session_root_and_is_not_a_session():
    """Invariant 11: what was found, never what it was taken to mean, and the count does not absorb it (#67)."""
    clean = _doctor("sessions")
    assert clean["non_sessions"] == [] and "other entries" not in run_cli(["doctor"])
    _lock_ghost()
    found = _doctor("sessions")
    assert [e["name"] for e in found["non_sessions"]] == ["leave-approval"]
    entry = found["non_sessions"][0]
    assert entry["kind"] == "directory" and entry["entries"] == [".lock"] and entry["entry_count"] == 1
    assert entry["error"] is None and entry["slug_shaped"] is True
    assert found["total"] == 0 and found["readable"] is True and found["inconsistent"] == {}
    text = run_cli(["doctor"])
    assert "leave-approval" in text and ".lock" in text
    assert "✅" in _check_line(text, "sessions") and "🟡" in _check_line(text, "other entries")
    over, at_limit = "a" * (store.MAX_SLUG_LENGTH + 1), "b" * store.MAX_SLUG_LENGTH  # `slug_shaped` is pattern + length
    _lock_ghost(over), _lock_ghost(at_limit)
    by_name = {e["name"]: e for e in _doctor("sessions")["non_sessions"]}
    assert by_name[at_limit]["slug_shaped"] is True and by_name[over]["slug_shaped"] is False
    with pytest.raises(InvalidSlugError):
        store.canonical_dir(over)


@pytest.mark.parametrize("populate, kind, entries, count, hint", [
    (_lock_ghost, "directory", [".lock"], 1, "will not get it"),
    (lambda: (store.session_root() / "leave-approval").write_text("half a download\n", encoding="utf-8"),
     "file", None, None, "will not get it"),
    (lambda: (store.session_root() / "leave-approval").mkdir(), "directory", [], 0, "an empty directory"),
], ids=["lock-ghost", "file", "empty-directory"])
def test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens(populate, kind, entries, count, hint):
    """The finding is only worth a line because of what it costs, so the cost is pinned (#67)."""
    store.session_root().mkdir(parents=True, exist_ok=True)
    populate()
    entry = _doctor("sessions")["non_sessions"][0]
    assert entry["kind"] == kind and entry["entries"] == entries and entry["entry_count"] == count
    assert entry["error"] is None and entry["slug_shaped"] is True
    assert hint in run_cli(["doctor"])
    meta = SessionService().create_session("We would like a leave approval system.", slug="leave-approval")
    if entries == [] and os.name != "nt":
        assert meta.slug == "leave-approval", "rename(2) replaces an empty destination directory"
    else:
        assert meta.slug.startswith("leave-approval-"), "the silent hash-suffixed substitution the hint warns of"


def test_a_symlink_is_reported_as_one_and_its_target_is_not_read(tmp_path):
    """`Path.is_dir()` follows a symlink; this answer must not (#67)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret-project.md").touch()
    store.session_root().mkdir(parents=True)
    symlink_or_skip(store.session_root() / "leave-approval", elsewhere, target_is_directory=True)
    entry = _doctor("sessions")["non_sessions"][0]
    assert entry["kind"] == "symlink" and entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None and entry["slug_shaped"] is True
    assert "secret-project.md" not in run_cli(["doctor"]), "the target's contents were listed into the report"


@_NEEDS_POSIX_NAMES
def test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken():
    """#408: `create_session` loses its rename to a `con` directory exactly like any other taken name."""
    _lock_ghost("con")
    (store.session_root() / "leave-approval").mkdir()
    by_name = {e["name"]: e for e in _doctor("sessions")["non_sessions"]}
    assert by_name["con"]["slug_shaped"] is True and by_name["leave-approval"]["slug_shaped"] is True
    assert "[name taken]" in run_cli(["doctor"])
    meta = SessionService().create_session("A request.", slug="con")
    assert meta.slug != "con" and meta.slug.startswith("con-")


def test_the_name_taken_hint_names_what_import_does_about_it():
    """#114, and the composition defect it would otherwise have shipped."""
    from requivo.core.errors import ImportDestinationOccupiedError

    (store.session_root() / "leave-approval").mkdir(parents=True)
    hint = run_cli(["doctor"])
    assert "[name taken]" in hint and ImportDestinationOccupiedError.code in hint
    assert "only symptom" not in hint and "plus a hash" in hint


def test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty(request):
    """The third state one level below the one `_session_health` already has (#80)."""
    d = _lock_ghost()
    readable = _doctor("sessions")["non_sessions"][0]
    assert readable["entries"] == [".lock"] and readable["error"] is None
    _deny(d, 0o111, "listing", request)
    denied, denied_text = _doctor("sessions")["non_sessions"][0], run_cli(["doctor"])
    assert denied["kind"] == "directory" and denied["entries"] is None and denied["entry_count"] is None
    assert "Permission denied" in (denied["error"] or "") and "Permission denied" in denied_text
    assert "empty directory" not in denied_text


def test_a_name_read_off_disk_cannot_forge_a_line_of_the_report_that_names_it():
    """#40 in a new render site: the entry's own name, the names it holds, and a forged name holding a session.json."""
    d = _lock_ghost()
    forged = "evil\n     └─ ok: all clear"
    try:
        (d / "x\n  ✅ forged          all clear").touch()
        (store.session_root() / "y\n  ✅ forged          all clear").mkdir()
        (store.session_root() / forged).mkdir()
    except OSError:  # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably)")
    (store.session_root() / forged / "session.json").write_text('{"not": "valid session metadata"}', encoding="utf-8")
    text = run_cli(["doctor"])
    lines = text.splitlines()
    assert text.count("\\n") >= 3, "a newline reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in lines and "     └─ ok: all clear`" not in lines
    assert any("inconsistent" in ln for ln in lines), "the session.json entry really reached the sessions bucket"
    assert len([ln for ln in lines if ln.startswith("     └─ ")]) == 3, "one row per entry: " + text
    entries = {e["name"]: e for e in _doctor("sessions")["non_sessions"]}
    assert any("\n" in n for n in entries["leave-approval"]["entries"]), "`--json` reports the raw name losslessly"


def test_the_parts_of_the_session_root_are_one_partition():
    """The three parts of the session root come out of one predicate (#67); `session list` never names a non-session."""
    run_cli(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()
    scanned_slugs, scanned_others, scanned_blind = store.scan_session_root()
    slugs, others, blind = set(scanned_slugs), {e.name for e in scanned_others}, {e.name for e in scanned_blind}
    assert slugs == set(store.list_session_slugs()) == {"real"} and others == {"leave-approval"} and blind == set()
    on_disk = {p.name for p in store.session_root().iterdir()}
    assert on_disk - (slugs | others | blind) == {".real.new-1-abcdef12"}
    assert [r["slug"] for r in run_cli_json(["session", "list", "--json"])["sessions"]] == ["real"]
    text = run_cli(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken(monkeypatch):
    """#263/#265: a lock timeout is not an integrity problem and does not earn the broken glyph."""
    run_cli(["session", "init", "A real one.", "--slug", "locked-one", "--json"])
    healthy = _doctor("sessions")
    assert healthy["inconsistent"] == {} and healthy.get("locked", {}) == {}
    assert "✅" in _check_line(run_cli(["doctor"]), "sessions")
    real_inspect = det.inspect_session

    def locked_for_our_slug(slug):
        if slug == "locked-one":
            raise SessionLockedError("session 'locked-one' is locked by another process", details={"slug": slug})
        return real_inspect(slug)

    found, text = _doctor_when(det, "inspect_session", locked_for_our_slug, "sessions")
    assert found["inconsistent"] == {} and "locked-one" in found.get("locked", {})
    line = _check_line(text, "sessions")
    assert "❌" not in line and "🟡" in line and "locked" in line.lower()


def test_a_note_does_not_move_the_sessions_glyph():
    """`noted` is deliberately absent from the glyph expression in `_print_sessions` (#260)."""
    seed_session("s")
    ArtifactService().save("s", "prd", "# PRD\n", source_revision=1)
    d = store.canonical_dir("s")
    raw = json.loads((d / "session.json").read_text(encoding="utf-8"))
    raw["artifact_status"]["risk-register"] = dict(raw["artifact_status"]["prd"], filename="risk-register.md")
    (d / "session.json").write_text(json.dumps(raw), encoding="utf-8")
    (d / "artifacts" / "risk-register.md").write_text("# Risk register\n", encoding="utf-8")
    r = _doctor("sessions")
    assert r["inconsistent"] == {} and r["notes"] == {"s": ["unknown_artifact_type"]}
    assert "✅" in _check_line(run_cli(["doctor"]), "sessions")


def test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick():
    """Review finding on #483."""
    from requivo.core.persistence import UnexaminableEntry

    run_cli(["session", "init", "A real one.", "--slug", "real", "--json"])
    real_scan = store.scan_session_root
    one_blind = lambda: (*real_scan()[:2], [UnexaminableEntry(name="ghost", error="Permission denied")])  # noqa: E731
    r, text = _doctor_when(det.store, "scan_session_root", one_blind, "sessions")
    assert r["inconsistent"] == {} and r["error"] is None and [e["name"] for e in r["unexaminable"]] == ["ghost"]
    line = _check_line(text, "sessions")
    assert "✅" not in line and "🟡" in line


# ── the lock root's residue (#180) ──────────────────────────────────────────────

_CLEAN_LOCKS = {"readable": True, "error": None, "total": 0, "sessions_checked": True,
                "unmatched": [], "unexpected": [], "unexaminable": []}


@pytest.mark.parametrize("arrange, total", [
    (lambda: None, 0),
    (lambda: (run_cli(["session", "init", "Something.", "--slug", "s", "--json"]), _take_lock("s")), 1),
    (lambda: (run_cli(["session", "init", "Something.", "--slug", "s", "--json"]), _take_lock("s"),
              run_cli(["session", "delete", "s", "--json"])), 0),
], ids=["clean", "live-session", "after-delete"])
def test_a_session_removed_through_session_delete_leaves_no_lock_residue(arrange, total):
    """The must-not-fire controls: no lock, a matched lock, and `session delete` unlinking its own (#238)."""
    arrange()
    r = _doctor("locks")
    assert r["total"] == total and r["unmatched"] == [] and r["unexpected"] == []
    if total == 0:
        assert r == _CLEAN_LOCKS
    text = run_cli(["doctor"])
    assert "✅" in _check_line(text, "locks") and "no matching session" not in text and "orphan" not in text.lower()


def test_a_lock_whose_session_was_deleted_by_hand_is_named_but_not_concluded():
    """The residue is named; the report never draws the conclusion the directory alone cannot support (#180)."""
    run_cli(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    shutil.rmtree(store.canonical_dir("s"))
    r = _doctor("locks")
    assert r["total"] == 1 and r["unmatched"] == ["s"] and r["unexpected"] == [] and r["unexaminable"] == []
    text = run_cli(["doctor"])
    assert "🟡" in _check_line(text, "locks") and "no matching session" in text
    assert "orphan" not in text.lower() and "leftover" not in text.lower()


@pytest.mark.parametrize("populate, unexpected", [
    (lambda lr: ((lr / "not-a-lock.txt").write_text("stray\n", encoding="utf-8"), (lr / "sub").mkdir()),
     ["not-a-lock.txt", "sub"]),
    (lambda lr: (lr / "s.discovering").mkdir(), ["s.discovering"]),
    (lambda lr: (lr / "Not Valid.discovering").write_text("", encoding="utf-8"), ["Not Valid.discovering"]),
], ids=["stray-file-and-dir", "guard-shaped-directory", "malformed-guard-stem"])
def test_an_entry_under_lock_root_that_is_not_a_lock_file_is_named_as_unexpected(populate, unexpected):
    """Nothing but `session_lock` and `_discovery_guard` write here; a shape neither produces is unexpected (#391)."""
    populate(_lock_files())
    r = _doctor("locks")
    assert r["total"] == 0 and sorted(r["unexpected"]) == unexpected
    text = run_cli(["doctor"])
    assert "🟡" in _check_line(text, "locks") and all(name in text for name in unexpected)


def test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags():
    """#391: `_discovery_guard`'s `<slug>.discovering` is a file this release's own code wrote."""
    from requivo.services.discovery import _discovery_guard_path

    run_cli(["session", "init", "Something.", "--slug", "s", "--json"])
    guard = _discovery_guard_path("s", store.Store(store.workspace_root()))
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.touch()
    assert _doctor("locks")["unexpected"] == []
    text = run_cli(["doctor"])
    assert "✅" in _check_line(text, "locks") and "s.discovering" not in text


def test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue():
    """#401/#409: `con`'s own files are recognised; `nul.lock`/`nul.discovering` with no session are unmatched, never residue."""
    # No skipif: this fixture was observed to materialise on GitHub's windows-latest runners (#582).
    _bare_session("con")
    lr = _lock_files("con.lock", "con.discovering", "Bad Stem.lock", "nul.lock", "nul.discovering")
    (lr / "not-a-lock.txt").write_text("stray", encoding="utf-8")
    r = _doctor("locks")
    assert r["total"] == 2 and r["unmatched"] == ["nul"] and r["unexpected"] == ["Bad Stem.lock", "not-a-lock.txt"]
    text = run_cli(["doctor"])
    assert "con.lock" not in text and "con.discovering" not in text
    assert "not a lock file Requivo recognises" in text and "not-a-lock.txt" in text
    assert "nul — no session currently named that" in text


@_NEEDS_POSIX_NAMES
def test_a_reserved_lock_stems_classification_survives_the_session_being_deleted():
    """#409's own mechanism: the lock file's provenance does not change when its session goes."""
    d = _bare_session("nul", request=False)
    _take_lock("nul")
    assert (store.lock_root() / "nul.lock").exists()
    before = _doctor("locks")
    assert before["total"] == 1 and before["unmatched"] == [] and before["unexpected"] == []
    shutil.rmtree(d)
    after = _doctor("locks")
    assert after["total"] == 1 and after["unmatched"] == ["nul"] and after["unexpected"] == []


@_NEEDS_SYMLINK
def test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it(workspace):
    """A symlink at either lock name is unexpected and not followed (invariant 17, #391), and a verdict about
    one entry is not decided by a sibling entry's state (#401)."""
    outside = workspace / "elsewhere.txt"
    outside.write_text("not a lock", encoding="utf-8")
    lr = _lock_files("a.discovering", "b.discovering")
    (lr / "a.lock").symlink_to(outside)
    (lr / "sneaky.discovering").symlink_to(outside)
    r = _doctor("locks")
    assert r["unexpected"] == ["a.lock", "sneaky.discovering"] and r["total"] == 0


@_NEEDS_POSIX_NAMES
def test_a_reserved_lock_stem_no_longer_probes_the_session_root(monkeypatch):
    """#409 removed `_is_lock_stem`'s call into `session_root()`: shape is all it asks."""
    _lock_files("con.lock")
    monkeypatch.setattr(store, "_probe", _raise(store.SessionUnreadableError("could not determine whether session exists")))
    r = _doctor("locks")
    assert r["total"] == 1 and r["unmatched"] == ["con"] and r["unexpected"] == [] and r["unexaminable"] == []


def test_the_lock_root_being_unlistable_is_not_reported_as_no_residue():
    """The same third state every other check in this report has (#180)."""
    clean, clean_text = _doctor("locks"), run_cli(["doctor"])
    assert clean["readable"] is True and clean["total"] == 0
    broken, broken_text = _doctor_when(det.store, "scan_lock_root", _raise(_DENIED), "locks")
    assert broken["readable"] is False and broken["total"] is None and broken["unmatched"] is None
    assert "Permission denied" in (broken["error"] or "") and "unreadable" in broken_text
    assert "✅" in _check_line(clean_text, "locks") and "✅" not in _check_line(broken_text, "locks")


def test_a_lock_for_a_session_that_exists_but_is_unexaminable_is_not_claimed_as_unmatched(request):
    """`list_slugs()` answers *confirmed sessions* alone (#80's own distinction)."""
    run_cli(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    deny_access(store.canonical_dir("s"), request, "that an unexaminable session is not read as a missing one")
    r, text = _doctor(), run_cli(["doctor"])
    assert r["sessions"]["unexaminable"] and r["sessions"]["unexaminable"][0]["name"] == "s"
    assert r["locks"]["unmatched"] == [] and "no matching session" not in text
    assert "✅" in _check_line(text, "locks")


def test_lock_matching_is_not_claimed_when_the_session_list_itself_could_not_be_read():
    """`unmatched` answers a question that needs the current session list (#272: patched on the `Store` class)."""
    run_cli(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    r, text = _doctor_when(det.store.Store, "list_session_slugs", _raise(_DENIED), "locks")
    assert r["readable"] is True and r["total"] == 1 and r["sessions_checked"] is False and r["unmatched"] is None
    assert "🟡" in _check_line(text, "locks") and "not checked" in text


# ── context ─────────────────────────────────────────────────────────────────────


def test_context_can_be_asked_for_by_session():
    """A session's card selection is held constant across its turns, and the two selectors are alternatives."""
    run_cli(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    run_cli(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow, wide = run_cli(["context", "--session", "narrow"]), run_cli(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow and len(narrow) < len(wide)
    assert narrow == run_cli(["context", "--cards", "b2b-platform"])
    with pytest.raises(SystemExit):
        run_cli(["context", "--session", "narrow", "--cards", "b2b-platform"])
