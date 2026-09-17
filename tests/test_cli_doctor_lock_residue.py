"""`doctor`'s lock-root residue report — #180."""
from __future__ import annotations

import json
import os
import shutil

import pytest
from _cli_harness import _run, _run_json

from requivo.core import persistence as store


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check — the one carrying a tick."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


def _take_lock(slug: str) -> None:
    """Materialise `<slug>.lock` on disk the way `session_lock` actually does."""
    with store.session_lock(slug):
        pass


def test_a_clean_workspace_reports_no_lock_residue(workspace):
    """The must-not-fire control: no lock files at all, so the check ticks and nothing is named."""
    r = _run_json(["doctor", "--json"])["locks"]
    assert r == {"readable": True, "error": None, "total": 0, "sessions_checked": True,
                 "unmatched": [], "unexpected": [], "unexaminable": []}
    text = _run(["doctor"])
    assert "✅" in _check_line(text, "locks")
    assert "no matching session" not in text
    assert "orphan" not in text.lower()


def test_a_lock_whose_session_still_exists_is_not_flagged(workspace):
    """The must-fire harness's positive control on the *matched* side."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == []
    assert "✅" in _check_line(_run(["doctor"]), "locks")


def test_a_session_removed_through_session_delete_leaves_no_lock_residue(workspace):
    """The must-not-fire control against a fresh false positive (#238)."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    _run(["session", "delete", "s", "--json"])

    r = _run_json(["doctor", "--json"])["locks"]
    assert r == {"readable": True, "error": None, "total": 0, "sessions_checked": True,
                 "unmatched": [], "unexpected": [], "unexaminable": []}
    assert "✅" in _check_line(_run(["doctor"]), "locks")


def test_a_lock_whose_session_was_deleted_by_hand_is_named_but_not_concluded(workspace):
    """The ordinary way this residue still arises, even with `session delete` (#238) unlinking its own lock
    file cleanly: a directory removed by hand (or by an older Requivo) goes, and the lock file."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    shutil.rmtree(store.canonical_dir("s"))

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == ["s"]
    assert r["unexpected"] == [] and r["unexaminable"] == []

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "s" in text and "no matching session" in text
    # The load-bearing refusal (#180): this report never draws the conclusion the directory alone cannot support.
    assert "orphan" not in text.lower()
    assert "leftover" not in text.lower()


def test_an_entry_under_lock_root_that_is_not_a_lock_file_is_named_as_unexpected(workspace):
    """Nothing but `session_lock` writes here, so anything else."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "not-a-lock.txt").write_text("stray\n", encoding="utf-8")
    (store.lock_root() / "sub").mkdir()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 0
    assert sorted(r["unexpected"]) == ["not-a-lock.txt", "sub"]

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "not-a-lock.txt" in text and "sub" in text


def test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags(workspace):
    """#391: `_discovery_guard` (`services/discovery.py`, #209) writes `<slug>.discovering` into `lock_root()`
    and never unlinks it, correctly."""
    from requivo.services.discovery import _discovery_guard_path

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    guard = _discovery_guard_path("s", store.Store(store.workspace_root()))
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.touch()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == [], (
        "a file this release's own code wrote must not read as unrecognised residue"
    )

    text = _run(["doctor"])
    assert "✅" in _check_line(text, "locks")
    assert "s.discovering" not in text


def test_a_directory_shaped_like_a_discovery_guard_is_still_unexpected(workspace):
    """The must-fire control paired with the test above."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "s.discovering").mkdir()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["s.discovering"]

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "s.discovering" in text


def test_a_malformed_discovering_stem_is_still_unexpected(workspace):
    """A `.discovering`-suffixed name whose stem is not a valid slug is not a shape `_discovery_guard_path`
    could ever produce -- it validates the slug before joining the suffix."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "Not Valid.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["Not Valid.discovering"]


@pytest.mark.parametrize("suffix", [".lock", ".discovering"],
                         ids=["#391-lock-symlink", "#391-discovering-symlink"])
def test_a_symlink_at_a_lock_name_is_reported_and_not_followed(workspace, suffix):
    """The same symlink care `_scan_session_root`'s non-session partition carries (invariant 17, #391)."""
    if os.name == "nt":
        pytest.skip("os.symlink needs elevated privileges on Windows by default")
    store.lock_root().mkdir(parents=True)
    target = workspace / "elsewhere.txt"
    target.write_text("not a lock or guard file\n", encoding="utf-8")
    (store.lock_root() / f"sneaky{suffix}").symlink_to(target)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 0
    assert r["unexpected"] == [f"sneaky{suffix}"]


def test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue(workspace):
    """#401, the third instance of #372's sweep gap and #391's defect one predicate over."""
    # No skipif here, deliberately: this exact fixture (mkdir("con"), "con.lock"/"con.discovering"/ "nul.lock") was OBSERVED to materialise for real on GitHub's windows-latest runners (#582's CI log), contradicting the "Windows refuses this at the OS level" reasoning the removed skip carried -- see that PR's body for the evidence.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    lr = store.lock_root()
    lr.mkdir(parents=True, exist_ok=True)
    (lr / "con.lock").write_text("", encoding="utf-8")          # what `session_lock` writes
    (lr / "con.discovering").write_text("", encoding="utf-8")   # what `_discovery_guard` writes
    (lr / "not-a-lock.txt").write_text("stray", encoding="utf-8")   # control: nothing wrote this
    (lr / "Bad Stem.lock").write_text("", encoding="utf-8")     # control: malformed stem
    (lr / "nul.lock").write_text("", encoding="utf-8")          # reserved, no `nul` session (#409)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 2, "con's own lock file, and nul's -- both are lock-stem-shaped"
    assert r["unmatched"] == ["nul"], "con's session exists right now; nul's does not"
    assert r["unexpected"] == ["Bad Stem.lock", "not-a-lock.txt"], (
        "the two files this release's own code wrote for `con` must stop being reported as "
        "residue, `nul.lock` moves to `unmatched` rather than staying residue too (#409), and "
        "nothing else may stop being reported with them"
    )

    text = _run(["doctor"])
    assert "con.lock" not in text and "con.discovering" not in text
    assert "not a lock file Requivo recognises" in text  # said about the two remaining controls
    assert "not-a-lock.txt" in text
    assert "nul — no session currently named that" in text


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs files literally named 'nul.lock' "
                     "and 'nul.discovering' on disk, and Windows resolves a reserved device name "
                     "taken before the first dot -- `lock_path`'s own comment states that rule for "
                     "`con.lock`, and `_reserved_stem` enforces it -- so both writes would open the "
                     "NUL device, succeed, and leave nothing for `iterdir` to find. REASONED, NOT "
                     "OBSERVED on an actual Windows machine; it follows from the same documented "
                     "behaviour #221 relies on. UNTESTED ON WINDOWS: that a lock file for a "
                     "reserved stem with no session behind it is recognised, not residue. It is "
                     "also unreachable there -- the file cannot exist, and neither can the session "
                     "the sibling test above needs -- so neither #401 nor #409 changes anything on "
                     "that platform.")
def test_a_lock_file_for_a_reserved_name_with_no_session_on_disk_is_recognised_not_residue(
        workspace):
    """#409, correcting #401's own conditional control."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "nul.lock").write_text("", encoding="utf-8")
    (store.lock_root() / "nul.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1, "nul.lock is a recognised lock; nul.discovering is not a lock at all"
    assert r["unmatched"] == ["nul"], "an ordinary orphaned lock -- no session claims it right now"
    assert r["unexpected"] == [], "not residue: a reserved stem is still a shape this store writes"


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory and lock files "
                     "literally named 'nul' / 'nul.lock' / 'nul.discovering' on disk, which "
                     "Windows itself refuses to materialise (see the sibling tests above). "
                     "REASONED, NOT OBSERVED on an actual Windows machine. UNTESTED ON WINDOWS: "
                     "the core mechanism #409 fixes, and unreachable there since a reserved-name "
                     "session can never exist to be deleted in the first place.")
def test_a_reserved_lock_stems_classification_survives_the_session_being_deleted(workspace):
    """#409's own mechanism, reproduced end to end."""
    d = store.session_root() / "nul"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "nul", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    with store.session_lock("nul"):
        pass    # writes lock_root()/'nul.lock' via the real code path; never unlinks it on exit

    assert (store.lock_root() / "nul.lock").exists()

    before = _run_json(["doctor", "--json"])["locks"]
    assert before["total"] == 1 and before["unmatched"] == [], "the nul session still exists"
    assert before["unexpected"] == [], "the must-fire control: recognised while the session is live"

    shutil.rmtree(d)   # the session is gone; the lock file's provenance has not changed

    after = _run_json(["doctor", "--json"])["locks"]
    assert after["total"] == 1, "the lock file's own shape has not changed"
    assert after["unmatched"] == ["nul"], "an orphaned lock, exactly like any other -- not residue"
    assert after["unexpected"] == [], (
        "must not read as 'not a lock file Requivo recognises' just because nul's session is gone"
    )


@pytest.mark.skipif(os.name == "nt", reason="os.symlink needs elevated privileges on Windows by "
                    "default, and the fixture also needs a file literally named 'a.lock', which is "
                    "fine there -- the symlink is the blocker, not the name. UNTESTED ON WINDOWS: "
                    "that a stray symlink beside a guard file does not change how the guard file "
                    "itself is classified.")
def test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it(workspace):
    """A verdict about one entry must not be decided by a sibling entry's state (#401, found in review before
    the fix shipped)."""
    store.lock_root().mkdir(parents=True)
    outside = workspace / "elsewhere.txt"
    outside.write_text("not a lock", encoding="utf-8")
    (store.lock_root() / "a.discovering").write_text("", encoding="utf-8")
    (store.lock_root() / "a.lock").symlink_to(outside)
    (store.lock_root() / "b.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["a.lock"], (
        "only the symlink is a shape no writer here produces; both guard files are ordinary files "
        "`_discovery_guard` really writes"
    )
    assert r["total"] == 0  # a `.discovering` file is not a lock, and the symlink is not one either


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a file literally named 'con.lock' "
                    "on disk, and Windows resolves a reserved device name taken before the first "
                    "dot, so the write would open the CON device and leave nothing for `iterdir` "
                    "to find (`lock_path`'s own comment states that rule). REASONED, NOT OBSERVED "
                    "on an actual Windows machine. UNTESTED ON WINDOWS: that `_is_lock_stem` never "
                    "reaches `_probe` for a reserved stem any more -- and unreachable there, since "
                    "only a reserved stem was ever routed through it.")
def test_a_reserved_lock_stem_no_longer_probes_the_session_root(workspace, monkeypatch):
    """#409 removed `_is_lock_stem`'s call into `session_root()` entirely -- shape is all it asks now."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "con.lock").write_text("", encoding="utf-8")

    def _always_raises(marker, slug):
        raise store.SessionUnreadableError(
            f"could not determine whether session {slug!r} exists: Permission denied")

    monkeypatch.setattr(store, "_probe", _always_raises)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == ["con"]
    assert r["unexpected"] == [] and r["unexaminable"] == []


def test_the_lock_root_being_unlistable_is_not_reported_as_no_residue(workspace):
    """The same third state every other check in this report has."""
    from requivo.deterministic import doctor as det

    clean = _run_json(["doctor", "--json"])["locks"]
    assert clean["readable"] is True and clean["total"] == 0
    clean_text = _run(["doctor"])

    def _unreadable():
        raise PermissionError("Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det.store, "scan_lock_root", _unreadable)
        broken = _run_json(["doctor", "--json"])["locks"]
        broken_text = _run(["doctor"])

    assert broken["readable"] is False
    assert broken["total"] is None and broken["unmatched"] is None
    assert "Permission denied" in (broken["error"] or "")
    assert "✅" in _check_line(clean_text, "locks")
    assert "✅" not in _check_line(broken_text, "locks")
    assert "unreadable" in broken_text


def test_a_lock_for_a_session_that_exists_but_is_unexaminable_is_not_claimed_as_unmatched(workspace):
    """`list_slugs()` answers *confirmed sessions* alone (#80's own distinction)."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny reads on Windows")
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    d = store.canonical_dir("s")
    d.chmod(0o000)
    try:
        # The same root guard the sibling fixtures in tests/test_persistence_scan.py carry (#298).
        try:
            (d / "session.json").exists()
        except PermissionError:
            pass
        else:
            pytest.skip("chmod 000 did not deny the session.json probe on this run (running as "
                        "root?). UNTESTED HERE: that a lock for a session the workspace could not "
                        "examine is not claimed as unmatched residue.")
        r = _run_json(["doctor", "--json"])
        text = _run(["doctor"])
    finally:
        d.chmod(0o755)

    assert r["sessions"]["unexaminable"] and r["sessions"]["unexaminable"][0]["name"] == "s", (
        "the must-fire control: the sessions check really does see this as unexaminable"
    )
    assert r["locks"]["unmatched"] == [], (
        "a session the workspace could not examine is not confirmed absent, so its lock must not "
        "be reported as residue from one that no longer exists"
    )
    assert "no matching session" not in text
    assert "✅" in _check_line(text, "locks")


def test_lock_matching_is_not_claimed_when_the_session_list_itself_could_not_be_read(workspace):
    """`unmatched` answers a question that needs the current session list."""
    from requivo.deterministic import doctor as det

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")

    def _unreadable(self):
        raise PermissionError("Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        # On the `Store` class, not the module function (#272).
        mp.setattr(det.store.Store, "list_session_slugs", _unreadable)
        r = _run_json(["doctor", "--json"])["locks"]
        text = _run(["doctor"])

    assert r["readable"] is True and r["total"] == 1
    assert r["sessions_checked"] is False
    assert r["unmatched"] is None, "0 or [] here would claim the lock was checked against sessions"
    assert "🟡" in _check_line(text, "locks")
    assert "not checked" in text
