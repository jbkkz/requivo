"""End-to-end tests of `session verify` and `session restore` — the documented recovery path (#210)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _full_model, _run, _run_json, _run_stdin, _slot

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.persistence import _REPLACE_ATTEMPTS

# ── session integrity at the boundary ────────────────────────────────────────


def test_session_verify_reports_a_broken_history_and_exits_non_zero(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    assert _run_json(["session", "verify", "s", "--json"])["ok"] is True

    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]


# ── session verify: three answers, three exit codes (#86) ────────────────────────
#
# `_card_health` already renders three states and `_cmd_session_verify` collapsed two of them into exit 1. These tests patch `check_selection` rather than denying reads on a real card directory: the state under test is "the card layer raised", the raise is what the verb branches on, and a POSIX mode bit is one platform's way of producing it.


def _cards_unreadable(monkeypatch) -> None:
    """The card layer itself cannot be enumerated, so `check_selection` propagates rather than returning a
    verdict — `_card_health`'s `{"checked": False}` arm, which is *we could not look* (#556)."""
    from requivo.core.errors import ContextUnreadableError
    from requivo.deterministic import remedies as det

    def _boom(only):
        raise ContextUnreadableError(
            "the context-card directory exists but cannot be read: denied",
            details={"directory": "walled"})

    monkeypatch.setattr(det, "check_selection", _boom)


def test_session_verify_exits_four_when_it_could_not_check_the_product_context(workspace, monkeypatch):
    """*Checked, and it is broken* and *could not check* are two different answers and had one exit code."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    healthy = _run_json(["session", "verify", "s", "--json"])
    assert healthy["ok"] is True                                    # must fire
    assert healthy["context_cards"]["checked"] is True

    _cards_unreadable(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 4
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert report["problems"] == []                                 # nothing is wrong inside it
    assert report["context_cards"]["checked"] is False
    assert report["context_cards"]["problem"] is None

    # the same number on the human surface: the exit code is not a property of `--json`
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s"], client=None)
    assert e.value.code == 4
    assert "Could not check" in buf.getvalue()


def test_session_verify_exits_four_when_the_lock_could_not_be_taken(workspace, monkeypatch):
    """The sibling of the test above, on the other new probe (#263, #265)."""
    from requivo.core.errors import SessionLockedError
    from requivo.deterministic.sessions import verify as sessions_mod

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    healthy = _run_json(["session", "verify", "s", "--json"])
    assert healthy["ok"] is True                                    # must fire
    assert healthy["session"]["checked"] is True

    def locked(slug):
        raise SessionLockedError(f"session '{slug}' is locked by another process; retry in a moment",
                                 details={"slug": slug})

    monkeypatch.setattr(sessions_mod, "inspect_session", locked)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 4
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert report["problems"] == []                                 # not a claim the session is broken
    assert report["session"]["checked"] is False
    assert "locked" in report["session"]["error"].lower()

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s"], client=None)
    assert e.value.code == 4
    assert "Could not examine" in buf.getvalue()


def test_session_verify_lets_a_firm_negative_outrank_a_partial_one(workspace, monkeypatch):
    """Both at once: a session that really is inconsistent *and* whose product context could not be checked."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()

    # must fire: with the cards readable this is the ordinary firm negative
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1
    assert json.loads(buf.getvalue())["context_cards"]["checked"] is True

    _cards_unreadable(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1, "a partial answer displaced a complete one"
    report = json.loads(buf.getvalue())
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]
    assert report["context_cards"]["checked"] is False


def test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken(workspace, tmp_path):
    """The other firm negative, and the one most easily confused with the new 4."""
    cards = tmp_path / "cards"
    cards.mkdir()
    card = cards / "lost-domain.md"
    card.write_text("# Lost domain\n", encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])
        assert _run_json(["session", "verify", "s", "--json"])["ok"] is True   # must fire
        card.unlink()

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", "s", "--json"], client=None)

        # The text branch as well, and not for symmetry.
        text = io.StringIO()
        with redirect_stdout(text), pytest.raises(SystemExit) as e_text:
            app(["session", "verify", "s"], client=None)

    assert e.value.code == 1
    assert e_text.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["context_cards"]["checked"] is True
    assert report["context_cards"]["problem"]["code"] == "unknown_context_card"


# ── session restore: the documented recovery path (#210) ────────────────────
# Before this, `session verify` diagnosed a torn model.json and stopped there.


def _apply_two_revisions(workspace, tmp_path):
    """Init a session and apply two revisions, the second changing `workflow`."""
    _run(["session", "init", "Something.", "--slug", "s"])
    p1, p2 = tmp_path / "p1.json", tmp_path / "p2.json"
    p1.write_text(json.dumps(_full_model()))
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "moved")})))
    _run(["model", "apply", "s", str(p1)])
    _run(["model", "apply", "s", str(p2)])
    return "s"


def test_session_verify_names_the_restorable_revision_and_restore_repairs_it(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    rev2 = (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")

    # tear model.json out from under its own hash -- the exact scenario #210 was filed about
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", slug], client=None)
    assert e.value.code == 1
    text = buf.getvalue()
    assert "model_is_not_the_last_revision" in text
    # the recovery path is named, not just the diagnosis -- the file and the exact command
    assert "revisions/0002-model.json" in text
    assert f"requivo session restore {slug}" in text

    out = _run(["session", "restore", slug])
    assert "revision 2" in out
    assert (d / "model.json").read_text(encoding="utf-8") == rev2

    assert _run_json(["session", "verify", slug, "--json"])["ok"] is True


def test_session_verify_says_it_could_not_check_whether_restore_would_help(workspace, tmp_path,
                                                                            monkeypatch):
    """Found in review: `_restore_remedy_line` takes its own, later, unlocked read of the session metadata."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    from requivo.core.errors import SessionLockedError
    from requivo.services.sessions import SessionService
    real_meta = SessionService.meta

    def _boom(self, s):
        if s == slug:
            raise SessionLockedError(f"session '{s}' is locked by another process; retry in a moment",
                                     details={"slug": s})
        return real_meta(self, s)

    monkeypatch.setattr(SessionService, "meta", _boom)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", slug], client=None)
    assert e.value.code == 1
    text = buf.getvalue()
    assert "model_is_not_the_last_revision" in text          # the diagnosis still ran
    assert "Could not check whether" in text                 # the third state, printed
    assert "requivo session restore" not in text              # never a fabricated suggestion


def test_session_verify_names_no_remedy_for_a_problem_restore_cannot_fix(workspace, tmp_path):
    """The must-fire control for the test above: the remedy line is scoped to the codes `session restore` can
    actually address, not printed for every problem."""
    slug = _apply_two_revisions(workspace, tmp_path)
    (store.canonical_dir(slug) / "revisions" / "0001-model.json").unlink()

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", slug], client=None)
    assert e.value.code == 1
    text = buf.getvalue()
    assert "missing_revision_file" in text
    assert "session restore" not in text


def test_session_restore_defaults_to_the_newest_readable_revision(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    _run(["session", "restore", slug])
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")


def test_session_restore_skips_a_broken_revision_when_searching_for_the_default(workspace, tmp_path):
    """Falling back to an older revision is a *partial* repair, and the receipt says so."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    p3 = tmp_path / "p3.json"
    p3.write_text(json.dumps(_full_model(**{"permissions": _slot(80, "explicit", "high", "HR only")})))
    _run(["model", "apply", slug, str(p3)])  # revision 3

    (d / "revisions" / "0003-model.json").write_text("{not json", encoding="utf-8")  # broken
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    out = _run(["session", "restore", slug])
    assert "revision 2" in out
    assert "partial repair" in out.lower()
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")

    # honest afterwards: model.json now holds a real historical state, but this is not the full repair.
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        app(["session", "verify", slug, "--json"], client=None)
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert any(p["code"] == "model_is_not_the_last_revision" for p in report["problems"])


def test_session_restore_default_search_also_skips_a_tampered_revision(workspace, tmp_path):
    """The hash check, not just the parse check."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    (d / "model.json").write_text(f.read_text(encoding="utf-8"))  # torn, needs a repair

    out = _run(["session", "restore", slug])
    assert "revision 1" in out
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0001-model.json").read_text(encoding="utf-8")


def test_session_restore_accepts_an_explicit_revision_even_when_a_newer_one_is_healthy(workspace,
                                                                                        tmp_path):
    """A named target is honoured exactly, never silently upgraded to the newest."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)

    _run(["session", "restore", slug, "--revision", "1"])
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0001-model.json").read_text(encoding="utf-8")


def _tamper_hash(f: Path) -> None:
    before = f.read_text(encoding="utf-8")
    f.write_text(before.replace('"completeness": 0', '"completeness": 5', 1), encoding="utf-8")


def _delete_revision_file(f: Path) -> None:
    f.unlink()


def _corrupt_json(f: Path) -> None:
    f.write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("revision, corrupt", [
    ("1", _tamper_hash),
    ("99", None),
    ("1", _delete_revision_file),
    ("1", _corrupt_json),
], ids=["hash-mismatch", "out-of-range", "missing-file", "unparseable"])
def test_session_restore_refuses_a_broken_explicit_target(workspace, tmp_path, revision, corrupt):
    """Four ways an explicit `--revision N` target can fail to resolve cleanly (#555)."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    before = (d / "model.json").read_text(encoding="utf-8")
    if corrupt is not None:
        corrupt(d / "revisions" / "0001-model.json")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug, "--revision", revision], client=None)
    assert e.value.code == 1
    assert (d / "model.json").read_text(encoding="utf-8") == before, \
        "a refused restore must not touch model.json"


def test_session_restore_refuses_when_nothing_in_the_history_is_readable(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    for f in (d / "revisions").glob("*.json"):
        f.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug], client=None)
    assert e.value.code == 1


def test_session_restore_survives_a_transient_permission_error(workspace, tmp_path, monkeypatch):
    """Windows' `rename` can fail with a transient `PermissionError` when a scanner or the Search Indexer
    briefly opens the destination microseconds after it is written."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    out = _run(["session", "restore", slug])
    assert "revision 2" in out
    assert attempts["n"] == 3, "the restore did not actually go through the retry path"
    expected = (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")
    assert (d / "model.json").read_text(encoding="utf-8") == expected


def test_session_restore_still_gives_up_on_a_permanent_permission_error(workspace, tmp_path,
                                                                          monkeypatch):
    """Bounded, and the bound is the point: a genuinely unwritable destination must still fail loudly and
    quickly rather than hang forever on a retry that never gives up."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))
    torn = (d / "model.json").read_text(encoding="utf-8")
    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    with pytest.raises(PermissionError):
        app(["session", "restore", slug], client=None)
    # The attempt count is what makes this a test of the *retry* rather than of `replace` (#483).
    assert attempts["n"] == _REPLACE_ATTEMPTS, (
        f"expected exactly {_REPLACE_ATTEMPTS} attempts before giving up, got {attempts['n']}")
    assert (d / "model.json").read_text(encoding="utf-8") == torn  # unreplaced, not half-written
    assert not list(d.glob(".*restore.tmp")), "scratch left behind after a failed restore"


def test_session_restore_refuses_a_session_with_no_applied_revision_yet(workspace):
    _run(["session", "init", "Something.", "--slug", "s"])
    with pytest.raises(SystemExit) as e:
        app(["session", "restore", "s"], client=None)
    assert e.value.code == 1


def test_session_restore_does_not_touch_the_revision_log(workspace, tmp_path):
    """The acceptance criterion, checked directly: restoring is model.json catching up with a history that was
    already the truth, not a new fact about the session."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "model.json").write_text((d / "revisions" / "0001-model.json").read_text(encoding="utf-8"))

    before = store.read_meta(slug)
    revision_files_before = sorted(p.name for p in (d / "revisions").glob("*.json"))

    _run(["session", "restore", slug])

    after = store.read_meta(slug)
    assert after.current_revision == before.current_revision == 2
    assert [r.model_dump() for r in after.revisions] == [r.model_dump() for r in before.revisions]
    assert sorted(p.name for p in (d / "revisions").glob("*.json")) == revision_files_before


