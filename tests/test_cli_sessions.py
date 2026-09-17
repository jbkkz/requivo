"""End-to-end tests of `requivo.deterministic.sessions` — `session init`, `list`, `show`, `migrate`, `rescope`
and `delete` (#141)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _full_model, _run, _run_json, _slot

from requivo.cli import app
from requivo.core import persistence as store

# ── the acceptance scenario ─────────────────────────────────────────────────────


def test_session_init_creates_a_session(workspace):
    r = _run_json(["session", "init", "Build a leave approval system.", "--slug", "leave", "--json"])
    assert r["slug"] == "leave"
    assert store.session_exists("leave")
    assert store.read_meta("leave").current_revision == 0


def test_session_show_reads_freshness_from_the_dependency_graph_not_the_revision(workspace, tmp_path):
    # `session show` used to call an artifact stale whenever the session had moved past its source revision.
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
    # #372: `.requivo/sessions/con/` already on disk (created before #221 shipped, or on a platform that never refused the name) must stay reachable through every read verb this module owns, `session export` and `session import` excluded — those live in `test_cli_session_archives.py`, a file this lane does not own this round; `core/persistence/`'s own `test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it` covers the lock `session export` takes.
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
    # #371: a legacy `request.md` that is not valid UTF-8 used to abort the whole pass with a raw traceback and no receipt at all, taking every other slug in the sweep down with it.
    #
    # "bad" sorts before "zzz-good", so on the pre-fix code the traceback fires before the healthy slug is even reached -- reproducing "no receipt printed at all" rather than "one row missing".
    bad_legacy = store.legacy_dir("bad")
    bad_legacy.mkdir(parents=True)
    bad_legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    bad_legacy.joinpath("request.md").write_bytes(b"legacy \xff\xfe request")
    # A canonical session already occupies the slug, at revision 0.
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
    # #371 (found in review of that same fix): `repo.exists(slug)`.
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
    # `store.session_exists("con")` would itself raise for this same reason.
    assert not (store.session_root() / "con").exists()  # refused, never half-created


# ── the revision contract on the CLI surface ────────────────────────────────────
# These are the primitives the Claude Code skills drive, so their JSON shape is part of the contract.


def test_session_init_json_reports_the_revision(workspace, tmp_path):
    r = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert r["revision"] == 0  # a fresh session has no model yet

    (tmp_path / "p.json").write_text(json.dumps(_full_model()))
    _run(["model", "apply", r["slug"], str(tmp_path / "p.json"), "--json"])
    # `init` is idempotent: re-running it on the same request returns the session as it now stands.
    again = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert again["slug"] == r["slug"]
    assert again["revision"] == 1



# ── session rescope (#168) ───────────────────────────────────────────────────
# The verb `docs/context-cards.md` used to say did not exist.


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
    """The scenario the issue was filed about: a card that only exists on one machine."""
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
    """The issue's own acceptance criterion, verbatim."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "delete", "does-not-exist", "--json"], client=None)
    assert e.value.code == 1
    assert json.loads(buf.getvalue())["code"] == "session_not_found"


def test_session_delete_then_recreating_the_same_slug_succeeds(workspace):
    """The issue's own acceptance criterion at the CLI's own door."""
    _run(["session", "init", "The first occupant of this slug.", "--slug", "reused"])
    _run(["session", "delete", "reused"])

    r = _run_json(["session", "init", "A completely different request.", "--slug", "reused", "--json"])
    assert r["slug"] == "reused"
    assert store.session_request("reused") == "A completely different request."
