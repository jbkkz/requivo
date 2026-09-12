"""End-to-end tests of `requivo.deterministic.sessions` — `session init`, `list`, `show`, `migrate`
and `verify`.

Split out of `test_cli_deterministic.py` by #141. `session export` and `session import` are the other
half of the same module and live in `test_cli_session_archives.py`; the reason they are a file of
their own is written there. The shared harness is `tests/_cli_harness.py`.
"""
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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── the acceptance scenario ─────────────────────────────────────────────────────


def test_session_init_creates_a_session(workspace):
    r = _run_json(["session", "init", "Build a leave approval system.", "--slug", "leave", "--json"])
    assert r["slug"] == "leave"
    assert store.session_exists("leave")
    assert store.read_meta("leave").current_revision == 0


def test_session_show_reads_freshness_from_the_dependency_graph_not_the_revision(workspace, tmp_path):
    # `session show` used to call an artifact stale whenever the session had moved past its source
    # revision — which contradicted `artifact list` and the status JSON in the same binary, and made
    # every artifact look out of date after any unrelated change. The stale flag is the whole rule.
    _run(["session", "init", "X.", "--slug", "s"])
    proposal = tmp_path / "m.json"
    proposal.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(proposal)])
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\n")
    _run(["artifact", "save", "s", "--type", "prd", "--file", str(prd), "--revision", "1"])

    # Move the session on via a slot the PRD does not consume: revision 2, PRD inputs untouched.
    proposal.write_text(json.dumps(_full_model(
        **{"current_process": _slot(80, "explicit", "high", "as-is described")})))
    _run(["model", "apply", "s", str(proposal)])

    out = _run(["session", "show", "s"])
    assert "revision 2" in out and "rev 1" in out   # provenance still says where it came from…
    assert "STALE" not in out                       # …but it is not stale, and both views agree
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is False


def test_session_list_and_show(workspace, tmp_path):
    _run(["session", "init", "First.", "--slug", "one"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "one", str(p)])
    listing = _run_json(["session", "list", "--json"])
    assert any(s["slug"] == "one" and s["revision"] == 1 for s in listing["sessions"])
    assert listing["degraded"] == 0
    shown = _run_json(["session", "show", "one", "--json"])
    assert shown["slug"] == "one" and shown["format_version"] == 1


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                     "'con' already on disk, which Windows itself refuses to create at the OS level "
                     "regardless of anything Requivo's own code does (see "
                     "core/persistence/identifiers.py's comment above _RESERVED_DEVICE_NAMES). "
                     "REASONED, NOT OBSERVED on an actual "
                     "Windows machine; it follows from the documented behaviour #221 already relies "
                     "on for the reserved-name refusal itself.")
def test_a_reserved_slug_already_on_disk_is_readable_by_list_show_and_verify(workspace):
    # #372: `.requivo/sessions/con/` already on disk (created before #221 shipped, or on a platform
    # that never refused the name) must stay reachable through every read verb this module owns,
    # `session export` and `session import` excluded — those live in `test_cli_session_archives.py`,
    # a file this lane does not own this round; `core/persistence/`'s own
    # `test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it`
    # covers the lock `session export` takes. Built by hand, not through `session init`, which must
    # keep refusing to *create* one — that half is pinned in `test_persistence_guards.py`.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    listing = _run_json(["session", "list", "--json"])
    assert listing["degraded"] == 0
    assert any(s["slug"] == "con" and s["readable"] for s in listing["sessions"])

    shown = _run_json(["session", "show", "con", "--json"])
    assert shown["slug"] == "con"

    verified = _run_json(["session", "verify", "con", "--json"])
    assert verified["ok"] is True


def test_session_migrate_moves_legacy_sessions(workspace, tmp_path):
    # Seed a legacy out/<slug>/ session, then bulk-migrate it into the canonical store.
    legacy = store.legacy_dir("legacy-one")
    legacy.mkdir(parents=True)
    legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    legacy.joinpath("request.txt").write_text("Legacy request.")

    r = _run_json(["session", "migrate", "--json"])
    assert "legacy-one" in r["migrated"]
    assert store.session_exists("legacy-one")
    assert store.read_meta("legacy-one").current_revision == 1
    assert legacy.joinpath("model.json").exists()  # originals preserved


def test_session_migrate_survives_one_undecodable_legacy_request_beside_a_healthy_session(workspace):
    # #371: a legacy `request.md` that is not valid UTF-8 used to abort the whole pass with a raw
    # traceback and no receipt at all, taking every other slug in the sweep down with it. Two members
    # in one fixture, deliberately -- a fixture with only the broken one would pass on the pre-fix
    # code too (nothing else was there to prove survived), and a fixture with only the healthy one
    # never reaches the bug at all.
    #
    # "bad" sorts before "zzz-good", so on the pre-fix code the traceback fires before the healthy
    # slug is even reached -- reproducing "no receipt printed at all" rather than "one row missing".
    bad_legacy = store.legacy_dir("bad")
    bad_legacy.mkdir(parents=True)
    bad_legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    bad_legacy.joinpath("request.md").write_bytes(b"legacy \xff\xfe request")
    # A canonical session already occupies the slug, at revision 0 -- the branch that reads
    # `_legacy_request_text` to decide `interrupted` vs. `skipped` (see `_cmd_session_migrate`).
    store.create_session("bad", "Whatever the canonical request happened to be.")

    good_legacy = store.legacy_dir("zzz-good")
    good_legacy.mkdir(parents=True)
    good_legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    good_legacy.joinpath("request.txt").write_text("A healthy legacy request.")

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "migrate", "--json"], client=None)
    assert e.value.code == 4  # EXIT_DEGRADED — the receipt still printed, in full, ahead of it

    r = json.loads(buf.getvalue())
    assert "zzz-good" in r["migrated"]
    assert store.session_exists("zzz-good")
    assert [err["slug"] for err in r["errors"]] == ["bad"]
    assert good_legacy.joinpath("model.json").exists()  # originals preserved either way


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                     "'con' already on disk (under the legacy out/ root here), which Windows itself "
                     "refuses to create at the OS level. REASONED, NOT OBSERVED -- same limit as the "
                     "sibling #372 fixtures.")
def test_session_migrate_survives_a_reserved_name_legacy_directory_beside_a_healthy_one(workspace):
    # #371 (found in review of that same fix): `repo.exists(slug)` -- the check that decides whether
    # a slug is "occupied" -- is itself outside any per-slug guard, and it resolves through
    # `canonical_dir`, which #372 lets refuse a *legacy-only* slug that is a reserved Windows device
    # name (correctly: migrating one would create a brand-new reserved-name directory, which #221 and
    # invariant 11 both say must stay refused). What must not happen is that refusal escaping the loop
    # uncaught -- the identical "abort the whole pass, no receipt" shape #371 closed for the two reads
    # a few lines further in. `app()`'s own top-level `except RequivoError` catches it before it
    # becomes a raw traceback, but the effect on this verb is the same one #371 fixed: exit 1 with
    # the generic error envelope instead of `4` with the migrate receipt, and no per-slug outcome for
    # anything in the sweep -- not even "zzz-good", sorted after "con" and never reached.
    # `store.legacy_dir("con")` would itself refuse -- nothing exists there yet either, so building
    # the fixture has to bypass the same guard the test is about, exactly like the persistence-level
    # #372 tests do for a canonical session.
    con_legacy = store.output_root() / "con"
    con_legacy.mkdir(parents=True)
    con_legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    con_legacy.joinpath("request.txt").write_text("A legacy request under a reserved name.")

    good_legacy = store.legacy_dir("zzz-good")
    good_legacy.mkdir(parents=True)
    good_legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    good_legacy.joinpath("request.txt").write_text("A healthy legacy request.")

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "migrate", "--json"], client=None)
    assert e.value.code == 4  # EXIT_DEGRADED — the receipt still printed, in full, ahead of it

    r = json.loads(buf.getvalue())
    assert "zzz-good" in r["migrated"]
    assert store.session_exists("zzz-good")
    assert [err["slug"] for err in r["errors"]] == ["con"]
    # `store.session_exists("con")` would itself raise for this same reason -- the reserved name was
    # never created, so it is still refused, not tolerated -- so the raw path is the honest check.
    assert not (store.session_root() / "con").exists()  # refused, never half-created


# ── the revision contract on the CLI surface ────────────────────────────────────
# These are the primitives the Claude Code skills drive, so their JSON shape is part of the contract:
# a skill reads `revision`, reasons, then hands it back on apply and on save.


def test_session_init_json_reports_the_revision(workspace, tmp_path):
    r = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert r["revision"] == 0  # a fresh session has no model yet

    (tmp_path / "p.json").write_text(json.dumps(_full_model()))
    _run(["model", "apply", r["slug"], str(tmp_path / "p.json"), "--json"])
    # `init` is idempotent: re-running it on the same request returns the session as it now stands,
    # so a caller about to apply learns it is no longer at revision 0.
    again = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert again["slug"] == r["slug"]
    assert again["revision"] == 1


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
# `_card_health` already renders three states and `_cmd_session_verify` collapsed two of them into
# exit 1. These tests patch `check_selection` rather than denying reads on a real card directory:
# the state under test is "the card layer raised", the raise is what the verb branches on, and a
# POSIX mode bit is one platform's way of producing it. The real filesystem route is already covered
# above by `test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty`, loudly skipped where
# the platform cannot deny a read; pinning the exit code to that route too would leave this rule
# untested on Windows.


def _cards_unreadable(monkeypatch) -> None:
    """The card layer itself cannot be enumerated, so `check_selection` propagates rather than
    returning a verdict — `_card_health`'s `{"checked": False}` arm, which is *we could not look*."""
    from requivo.core.errors import ContextUnreadableError
    from requivo.deterministic import doctor as det

    def _boom(only):
        raise ContextUnreadableError(
            "the context-card directory exists but cannot be read: denied",
            details={"directory": "walled"})

    monkeypatch.setattr(det, "check_selection", _boom)


def test_session_verify_exits_four_when_it_could_not_check_the_product_context(workspace, monkeypatch):
    """*Checked, and it is broken* and *could not check* are two different answers and had one exit
    code. The verb already printed them differently — the second has a glyph of its own — and then
    exited 1 beside a session that really is inconsistent, in the command whose whole job is to say
    whether a session is sound.

    4 rather than a new 5: the code already means *the work was done and part of the answer was
    unreachable*, and a code per verb rebuilds the problem 4 was introduced to solve.

    The clean run is asserted first and is the control. Without it this test would pass equally well
    against a verb that exits 4 unconditionally.
    """
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
    """The sibling of the test above, on the other new probe (#263, #265): a lock `inspect_session`
    could not take within the deadline is *could not check*, exactly like an unreadable product
    context, and must not be reported as `problems` -- that is the accusation shape #263 exists to
    remove. Same structure as `test_session_verify_exits_four_when_it_could_not_check_the_product_context`:
    the clean run is the must-fire control, and the exit code is checked on both surfaces."""
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
    """Both at once: a session that really is inconsistent *and* whose product context could not be
    checked. It exits **1**, not 4.

    A script gating on *is this usable* wants the definite answer, and there is one — the session is
    broken, and no reading of the cards could make it sound again. 4 would understate a finding that
    is complete. `--json` still carries both facts, so nothing is withheld at either code.
    """
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
    """The other firm negative, and the one most easily confused with the new 4: the cards *were*
    read and the selection does not resolve. That is a complete answer about a session that is not
    usable, so it stays at 1 — 4 is for the answer nobody could produce."""
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

        # The text branch as well, and not for symmetry: it is the only arm that binds a local to a
        # card-problem *code* string, so an exit status computed into a name that collides with it
        # leaves SystemExit carrying `unknown_context_card` instead of a number. Reproduced while
        # writing this change.
        text = io.StringIO()
        with redirect_stdout(text), pytest.raises(SystemExit) as e_text:
            app(["session", "verify", "s"], client=None)

    assert e.value.code == 1
    assert e_text.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["context_cards"]["checked"] is True
    assert report["context_cards"]["problem"]["code"] == "unknown_context_card"


# ── session restore: the documented recovery path (#210) ────────────────────
# Before this, `session verify` diagnosed a torn model.json and stopped there -- the fact that
# `revisions/` holds every applied model, and that an earlier one can be copied over the broken one,
# lived nowhere a user reading the output would find it. `session restore` is the explicit,
# user-invoked repair; `session verify`'s remedy line is what points a reader at it.


def _apply_two_revisions(workspace, tmp_path):
    """Init a session and apply two revisions, the second changing `workflow` -- the fixture every
    test below builds on. Returns the slug."""
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
    """Found in review: `_restore_remedy_line` takes its own, later, unlocked read of the session
    metadata to search for a restore target -- a second read after `inspect_session`'s own, already
    released by the time this one runs. A session that locks up in the gap between the two must not
    read as "nothing to suggest", which is the stronger, wrong claim that a silent `None` made. The
    third state is a printed line, not a silent absence -- the same rule `session.checked` already
    follows a few lines up in this same verb."""
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
    """The must-fire control for the test above: the remedy line is scoped to the codes `session
    restore` can actually address, not printed for every problem. A missing revision *file* is a
    broken history, which restoring model.json does nothing about."""
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
    """Falling back to an older revision is a *partial* repair, and the receipt says so -- found in
    review: the original version of this test only checked that the fallback landed on the right
    file, and would have passed unchanged against a claim that `session verify` reads clean
    afterwards, which is false here (revision 3's own content is genuinely gone)."""
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

    # honest afterwards: model.json now holds a real historical state, but this is not the full
    # repair -- revision 3's own content is unrecoverable, and verify must keep saying so rather
    # than reading as fixed.
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        app(["session", "verify", slug, "--json"], client=None)
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert any(p["code"] == "model_is_not_the_last_revision" for p in report["problems"])


def test_session_restore_default_search_also_skips_a_tampered_revision(workspace, tmp_path):
    """The hash check, not just the parse check: a revision file hand-edited after being frozen still
    parses as a valid model, and the default search must not trust it any more than it trusts one
    that fails to parse at all."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    (d / "model.json").write_text(f.read_text(encoding="utf-8"))  # torn, needs a repair

    out = _run(["session", "restore", slug])
    assert "revision 1" in out
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0001-model.json").read_text(encoding="utf-8")


def test_session_restore_refuses_an_explicit_revision_whose_hash_no_longer_matches(workspace,
                                                                                    tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    f = d / "revisions" / "0001-model.json"
    before = f.read_text(encoding="utf-8")
    f.write_text(before.replace('"completeness": 0', '"completeness": 5', 1))
    model_before = (d / "model.json").read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug, "--revision", "1"], client=None)
    assert e.value.code == 1
    assert (d / "model.json").read_text(encoding="utf-8") == model_before, \
        "a refused restore must not touch model.json"


def test_session_restore_accepts_an_explicit_revision_even_when_a_newer_one_is_healthy(workspace,
                                                                                        tmp_path):
    """A named target is honoured exactly, never silently upgraded to the newest -- the whole point
    of `--revision` is picking one deliberately."""
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)

    _run(["session", "restore", slug, "--revision", "1"])
    assert (d / "model.json").read_text(encoding="utf-8") == \
        (d / "revisions" / "0001-model.json").read_text(encoding="utf-8")


def test_session_restore_refuses_an_out_of_range_revision(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    before = (d / "model.json").read_text(encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "restore", slug, "--revision", "99"], client=None)
    assert e.value.code == 1
    assert (d / "model.json").read_text(encoding="utf-8") == before, \
        "a refused restore must not touch model.json"


def test_session_restore_refuses_a_missing_target_revision_file(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "revisions" / "0001-model.json").unlink()
    before = (d / "model.json").read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug, "--revision", "1"], client=None)
    assert e.value.code == 1
    assert (d / "model.json").read_text(encoding="utf-8") == before


def test_session_restore_refuses_an_unparseable_target_revision_file(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    (d / "revisions" / "0001-model.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug, "--revision", "1"], client=None)
    assert e.value.code == 1


def test_session_restore_refuses_when_nothing_in_the_history_is_readable(workspace, tmp_path):
    slug = _apply_two_revisions(workspace, tmp_path)
    d = store.canonical_dir(slug)
    for f in (d / "revisions").glob("*.json"):
        f.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        app(["session", "restore", slug], client=None)
    assert e.value.code == 1


def test_session_restore_survives_a_transient_permission_error(workspace, tmp_path, monkeypatch):
    """Windows' `rename` can fail with a transient `PermissionError` when a scanner or the Search
    Indexer briefly opens the destination microseconds after it is written -- the same cause
    invariant 18's `_atomic_write` retries for in `core/persistence/atomic.py`. `_replace_with_retry` is
    this module's own small statement of the identical shape, for the one write here that is not
    routed through that helper."""
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
    """Bounded, and the bound is the point: a genuinely unwritable destination must still fail
    loudly and quickly rather than hang forever on a retry that never gives up."""
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
    # The attempt count is what makes this a test of the *retry* rather than of `replace`: without
    # it every assertion here holds identically with the loop deleted (found in review of #483).
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
    """The acceptance criterion, checked directly: restoring is model.json catching up with a history
    that was already the truth, not a new fact about the session -- no new revision file, no bump to
    `current_revision`, no new entry in the provenance log."""
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


# ── session rescope (#168) ───────────────────────────────────────────────────
# The verb `docs/context-cards.md` used to say did not exist: re-scoping a session's context cards
# without hand-editing `session.json`.


def test_session_rescope_records_a_new_revision(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--context", "b2b-platform"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p)])                 # revision 1

    r = _run_json(["session", "rescope", "s", "--context", "event-ops", "--json"])
    assert r == {"slug": "s", "previous_context_cards": ["b2b-platform"],
                "context_cards": ["event-ops"], "revision": 2, "changed": True}
    assert store.read_meta("s").current_revision == 2
    assert store.read_meta("s").context_cards == ["event-ops"]

    shown = _run_json(["session", "show", "s", "--json"])
    assert shown["context_cards"] == ["event-ops"]


def test_session_rescope_before_any_model_stays_at_revision_zero(workspace):
    _run(["session", "init", "Something.", "--slug", "s"])

    r = _run_json(["session", "rescope", "s", "--context", "event-ops", "--json"])
    assert r["revision"] == 0
    assert r["changed"] is True
    assert store.read_meta("s").revisions == []


def test_session_rescope_rejects_an_unknown_card(workspace):
    _run(["session", "init", "Something.", "--slug", "s"])

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "rescope", "s", "--context", "made-up", "--json"], client=None)
    assert e.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["code"] == "unknown_context_card"
    assert store.read_meta("s").context_cards is None  # refused before anything was written


def test_session_rescope_requires_context(workspace):
    _run(["session", "init", "Something.", "--slug", "s"])

    with pytest.raises(SystemExit) as e:
        app(["session", "rescope", "s"], client=None)
    assert e.value.code == 2  # argparse: a required argument is missing


def test_session_rescope_to_all_cards_reports_none(workspace):
    _run(["session", "init", "Something.", "--slug", "s", "--context", "b2b-platform"])

    r = _run_json(["session", "rescope", "s", "--context", "", "--json"])
    assert r["context_cards"] is None
    assert store.read_meta("s").context_cards is None


def test_session_rescope_reports_when_nothing_changed(workspace):
    _run(["session", "init", "Something.", "--slug", "s", "--context", "event-ops"])

    out = _run(["session", "rescope", "s", "--context", "event-ops"])
    assert "nothing changed" in out.lower()
    r = _run_json(["session", "rescope", "s", "--context", "event-ops", "--json"])
    assert r["changed"] is False


def test_session_rescope_recovers_a_session_whose_card_no_longer_resolves_here(workspace, tmp_path):
    """The scenario the issue was filed about: a card that only exists on one machine. Before this
    verb the documented recovery was hand-editing `session.json`; now `session verify` fails loudly,
    `session rescope` fixes it without touching a file directly, and `session verify` passes again."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n", encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain"])
        (cards / "lost-domain.md").unlink()  # simulate: this card does not exist here anymore

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            app(["session", "verify", "s", "--json"], client=None)
        assert json.loads(buf.getvalue())["ok"] is False

        _run(["session", "rescope", "s", "--context", "b2b-platform"])

        assert _run_json(["session", "verify", "s", "--json"])["ok"] is True
        assert store.read_meta("s").context_cards == ["b2b-platform"]


# ── #238: session delete ─────────────────────────────────────────────────────────


def test_session_delete_removes_the_session(workspace):
    _run(["session", "init", "A throwaway request.", "--slug", "throwaway"])
    assert store.session_exists("throwaway")

    r = _run_json(["session", "delete", "throwaway", "--json"])
    assert r["slug"] == "throwaway"
    assert r["deleted"] is True
    assert not store.session_exists("throwaway")
    assert "throwaway" not in store.list_session_slugs()


def test_session_delete_refuses_a_nonexistent_slug_with_session_not_found(workspace):
    """The issue's own acceptance criterion, verbatim: the structured `session_not_found` error, not
    a bare traceback or a generic refusal."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "delete", "does-not-exist", "--json"], client=None)
    assert e.value.code == 1
    assert json.loads(buf.getvalue())["code"] == "session_not_found"


def test_session_delete_then_recreating_the_same_slug_succeeds(workspace):
    """The issue's own acceptance criterion at the CLI's own door: the slug claim is genuinely
    released, so a second `session init` for the identical slug must succeed."""
    _run(["session", "init", "The first occupant of this slug.", "--slug", "reused"])
    _run(["session", "delete", "reused"])

    r = _run_json(["session", "init", "A completely different request.", "--slug", "reused", "--json"])
    assert r["slug"] == "reused"
    assert store.session_request("reused") == "A completely different request."
