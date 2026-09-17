"""`session init/list/show/migrate/rescope/delete` (#141), `verify`/`restore` (#210), every CLI route to a
missing session (#243) and `resolve_default_session` (#541)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from _fakes import full_model, run_cli, run_cli_exit, run_cli_json, run_cli_stdin, seed_session, slot

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import _REPLACE_ATTEMPTS, SESSION_FORMAT_VERSION, canonical_dir
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

_NEEDS_POSIX_NAMES = pytest.mark.skipif(
    store.fcntl is None, reason="REASONED, NOT OBSERVED on Windows: a directory literally named 'con' cannot exist there")


def _init(slug: str = "s", *extra: str) -> None:
    run_cli(["session", "init", "Something.", "--slug", slug, *extra, "--json"])


def _apply(slug: str, tmp_path: Path, name: str = "p.json", **slots) -> None:
    p = tmp_path / name
    p.write_text(json.dumps(full_model(**slots)), encoding="utf-8")
    run_cli(["model", "apply", slug, str(p)])


def _legacy(slug: str, request: bytes = b"A healthy legacy request.", root: Path | None = None) -> Path:
    d = root or store.legacy_dir(slug)
    d.mkdir(parents=True)
    d.joinpath("model.json").write_text(json.dumps(full_model()), encoding="utf-8")
    d.joinpath("request.md" if b"\xff" in request else "request.txt").write_bytes(request)
    return d


def _verify(slug: str, json_out: bool = True):
    """`session verify`, as `(payload or text, exit code)`."""
    out, code = run_cli_exit(["session", "verify", slug, *(["--json"] if json_out else [])])
    return (json.loads(out) if json_out else out), code


def _edit_meta(slug: str, **fields) -> None:
    p = canonical_dir(slug) / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.update(fields)
    p.write_text(json.dumps(data), encoding="utf-8")


def _replace_fails(monkeypatch, denials: int | None) -> dict:
    """`Path.replace` raises `PermissionError` on the first `denials` calls (every call when None); returns the counter."""
    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if denials is None or attempts["n"] <= denials:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    return attempts


# ── init, list, show ────────────────────────────────────────────────────────────


def test_session_init_json_reports_the_revision(tmp_path):
    """`init` is idempotent: re-running it on the same request returns the session as it now stands."""
    r = run_cli_json(["session", "init", "Build a leave approval system.", "--json"])
    assert r["revision"] == 0 and store.session_exists(r["slug"])
    assert store.read_meta(r["slug"]).current_revision == 0
    _apply(r["slug"], tmp_path)
    again = run_cli_json(["session", "init", "Build a leave approval system.", "--json"])
    assert again["slug"] == r["slug"] and again["revision"] == 1


def test_session_list_and_show(tmp_path):
    _init("one")
    _apply("one", tmp_path)
    listing = run_cli_json(["session", "list", "--json"])
    assert any(s["slug"] == "one" and s["revision"] == 1 for s in listing["sessions"]) and listing["degraded"] == 0
    shown = run_cli_json(["session", "show", "one", "--json"])
    assert shown["slug"] == "one" and shown["format_version"] == 1


def test_session_show_reads_freshness_from_the_dependency_graph_not_the_revision(tmp_path):
    """Invariant 1 on the CLI surface: moving a slot the PRD does not consume leaves it fresh."""
    _init()
    _apply("s", tmp_path)
    (tmp_path / "prd.md").write_text("# PRD\n", encoding="utf-8")
    run_cli(["artifact", "save", "s", "--type", "prd", "--file", str(tmp_path / "prd.md"), "--revision", "1"])
    _apply("s", tmp_path, current_process=slot(80, "explicit", "high", "as-is described"))
    out = run_cli(["session", "show", "s"])
    assert "revision 2" in out and "rev 1" in out and "STALE" not in out
    assert run_cli_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is False


@_NEEDS_POSIX_NAMES
def test_a_reserved_slug_already_on_disk_is_readable_by_list_show_and_verify():
    """#372: a `con` session created before #221 stays reachable through every read verb here."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None, "context_cards": None,
        "current_revision": 0, "format_version": 1, "revisions": [], "artifact_status": {}}), encoding="utf-8")
    listing = run_cli_json(["session", "list", "--json"])
    assert listing["degraded"] == 0 and any(s["slug"] == "con" and s["readable"] for s in listing["sessions"])
    assert run_cli_json(["session", "show", "con", "--json"])["slug"] == "con"
    assert _verify("con")[0]["ok"] is True


# ── migrate ─────────────────────────────────────────────────────────────────────


def test_session_migrate_moves_legacy_sessions():
    legacy = _legacy("legacy-one")
    r = run_cli_json(["session", "migrate", "--json"])
    assert "legacy-one" in r["migrated"] and store.session_exists("legacy-one")
    assert store.read_meta("legacy-one").current_revision == 1
    assert legacy.joinpath("model.json").exists()  # originals preserved


def test_session_migrate_survives_one_undecodable_legacy_request_beside_a_healthy_session():
    """#371: an undecodable legacy `request.md` used to abort the whole pass; "bad" sorts before "zzz-good"."""
    _legacy("bad", b"legacy \xff\xfe request")
    store.create_session("bad", "Whatever the canonical request happened to be.")
    good = _legacy("zzz-good")
    out, code = run_cli_exit(["session", "migrate", "--json"])
    assert code == 4  # EXIT_DEGRADED: the receipt still printed, in full
    r = json.loads(out)
    assert "zzz-good" in r["migrated"] and store.session_exists("zzz-good")
    assert [err["slug"] for err in r["errors"]] == ["bad"] and good.joinpath("model.json").exists()


@_NEEDS_POSIX_NAMES
def test_session_migrate_survives_a_reserved_name_legacy_directory_beside_a_healthy_one():
    """#371, found in review: `repo.exists("con")` itself raises, and that is one degraded row."""
    _legacy("con", root=store.output_root() / "con")
    _legacy("zzz-good")
    out, code = run_cli_exit(["session", "migrate", "--json"])
    assert code == 4
    r = json.loads(out)
    assert "zzz-good" in r["migrated"] and store.session_exists("zzz-good")
    assert [err["slug"] for err in r["errors"]] == ["con"]
    assert not (store.session_root() / "con").exists()  # refused, never half-created


# ── rescope (#168) ──────────────────────────────────────────────────────────────


def test_session_rescope_records_a_new_revision(tmp_path):
    """Before any model the session stays at revision 0; after one, a rescope is a revision."""
    _init("s", "--context", "b2b-platform")
    r = run_cli_json(["session", "rescope", "s", "--context", "event-ops", "--json"])
    assert r["revision"] == 0 and r["changed"] is True and store.read_meta("s").revisions == []
    _apply("s", tmp_path)                                         # revision 1
    r = run_cli_json(["session", "rescope", "s", "--context", "b2b-platform", "--json"])
    assert r == {"slug": "s", "previous_context_cards": ["event-ops"], "context_cards": ["b2b-platform"],
                 "revision": 2, "changed": True}
    assert store.read_meta("s").current_revision == 2 and store.read_meta("s").context_cards == ["b2b-platform"]
    assert run_cli_json(["session", "show", "s", "--json"])["context_cards"] == ["b2b-platform"]


def test_session_rescope_rejects_an_unknown_card():
    _init()
    out, code = run_cli_exit(["session", "rescope", "s", "--context", "made-up", "--json"])
    assert code == 1 and json.loads(out)["code"] == "unknown_context_card"
    assert store.read_meta("s").context_cards is None  # refused before anything was written


def test_session_rescope_requires_context():
    _init()
    with pytest.raises(SystemExit) as e:
        app(["session", "rescope", "s"], client=None)
    assert e.value.code == 2  # argparse: a required argument is missing


def test_session_rescope_to_all_cards_reports_none():
    _init("s", "--context", "b2b-platform")
    assert run_cli_json(["session", "rescope", "s", "--context", "", "--json"])["context_cards"] is None
    assert store.read_meta("s").context_cards is None


def test_session_rescope_reports_when_nothing_changed():
    _init("s", "--context", "event-ops")
    assert "nothing changed" in run_cli(["session", "rescope", "s", "--context", "event-ops"]).lower()
    assert run_cli_json(["session", "rescope", "s", "--context", "event-ops", "--json"])["changed"] is False


def test_session_rescope_recovers_a_session_whose_card_no_longer_resolves_here(tmp_path):
    """The scenario #168 was filed about: a card that only exists on one machine."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _init("s", "--context", "lost-domain")
        (cards / "lost-domain.md").unlink()
        assert _verify("s")[0]["ok"] is False
        run_cli(["session", "rescope", "s", "--context", "b2b-platform"])
        assert _verify("s")[0]["ok"] is True and store.read_meta("s").context_cards == ["b2b-platform"]


# ── delete (#238) ───────────────────────────────────────────────────────────────


def test_session_delete_removes_the_session():
    """And recreating the same slug afterwards succeeds (#238's own acceptance criterion)."""
    _init("reused")
    r = run_cli_json(["session", "delete", "reused", "--json"])
    assert r == {"slug": "reused", "deleted": True} or (r["slug"] == "reused" and r["deleted"] is True)
    assert not store.session_exists("reused") and "reused" not in store.list_session_slugs()
    r = run_cli_json(["session", "init", "A completely different request.", "--slug", "reused", "--json"])
    assert r["slug"] == "reused" and store.session_request("reused") == "A completely different request."


def test_session_delete_refuses_a_nonexistent_slug_with_session_not_found():
    out, code = run_cli_exit(["session", "delete", "does-not-exist", "--json"])
    assert code == 1 and json.loads(out)["code"] == "session_not_found"


# ── every CLI route to a missing session (#243) ─────────────────────────────────

_VERBS = [["status", "no-such-session"], ["answer", "no-such-session", "some answers"], ["impact", "no-such-session"],
          ["brief", "no-such-session"], ["prd", "no-such-session"], ["criteria", "no-such-session"],
          ["epic", "no-such-session"], ["release", "no-such-session"], ["stories", "no-such-session"],
          ["estimate", "no-such-session"], ["session", "show", "no-such-session"],
          ["session", "verify", "no-such-session"], ["session", "export", "no-such-session"]]


def _fails(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        app(argv, client=None)
    assert exc.value.code == 1
    return capsys.readouterr().err


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: "-".join(a[:2]))
def test_every_cli_route_to_a_missing_session_names_the_root_and_the_listing_command(argv, capsys):
    """The two facts a user needs; `canonical` (the retired `out/` layout) is never said (#243)."""
    err = _fails(argv, capsys)
    assert str(store.session_root()) in err and "requivo session list" in err and "--workspace" in err, argv
    assert "canonical" not in err.lower()


def test_the_structured_envelope_still_carries_the_published_code_and_slug(capsys):
    """Message text is not the contract; `code` and `details` are (`docs/compatibility.md`)."""
    with pytest.raises(SystemExit):
        app(["status", "no-such-session", "--json"], client=None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "session_not_found" and payload["details"]["ref"] == "no-such-session"


def test_a_reference_carrying_a_control_character_cannot_write_its_own_line(capsys):
    """A refusal echoes raw argv, escaped rather than dropped (#40)."""
    err = _fails(["status", "ok\nAll clear, nothing to see."], capsys)
    assert "\nAll clear" not in err and "All clear" in err


def test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly():
    """`display_token` inside `no_session_message` is a second line of defence (#40)."""
    line = store.no_session_message("ok\nAll clear, nothing to see.")
    assert "\nAll clear" not in line and "All clear" in line
    assert store.no_session_message("leave-approval").startswith("no session named leave-approval")


# ── resolve_default_session (#541) ──────────────────────────────────────────────


def test_no_session_raises_and_names_run():
    with pytest.raises(SessionNotFoundError) as exc_info:
        SessionService().resolve_default_session()
    assert "run" in str(exc_info.value)


def test_exactly_one_session_is_the_default_with_nothing_to_list():
    seed_session("only-one", analysed=False)
    resolution = SessionService().resolve_default_session()
    assert resolution.default == "only-one" and resolution.candidates == []


def test_several_sessions_default_to_the_most_recently_written():
    """`updated_at` breaks the tie, never directory mtime (#541)."""
    seed_session("older", analysed=False)
    seed_session("newer", analysed=False)
    _edit_meta("older", updated_at="2020-01-01T00:00:00Z")
    _edit_meta("newer", updated_at="2030-01-01T00:00:00Z")
    resolution = SessionService().resolve_default_session()
    assert resolution.default == "newer" and {e.slug for e in resolution.candidates} == {"older", "newer"}


def test_a_degraded_row_among_several_does_not_hide_the_others():
    """Invariant 15 at the resolver: a row `read_meta` refuses is a candidate, and a readable sibling wins."""
    seed_session("healthy", analysed=False)
    seed_session("broken", analysed=False)
    _edit_meta("broken", format_version=SESSION_FORMAT_VERSION + 1)
    resolution = SessionService().resolve_default_session()
    assert resolution.default == "healthy"
    by_slug = {e.slug: e for e in resolution.candidates}
    assert set(by_slug) == {"healthy", "broken"} and by_slug["broken"].readable is False and by_slug["broken"].error


# ── verify: three answers, three exit codes (#86) ──────────────────────────────


def _cards_unreadable(monkeypatch) -> None:
    """The card layer raised: `_card_health`'s `{"checked": False}` arm, *we could not look* (#556)."""
    from requivo.core.errors import ContextUnreadableError
    from requivo.deterministic import remedies as det

    def _boom(only):
        raise ContextUnreadableError("the context-card directory exists but cannot be read", details={"directory": "walled"})

    monkeypatch.setattr(det, "check_selection", _boom)


def _locked(slug):
    from requivo.core.errors import SessionLockedError
    raise SessionLockedError(f"session '{slug}' is locked by another process; retry in a moment", details={"slug": slug})


def test_session_verify_reports_a_broken_history_and_exits_non_zero(monkeypatch):
    _init()
    run_cli_stdin(["model", "apply", "s", "-", "--json"], json.dumps(full_model()), monkeypatch)
    assert _verify("s")[0]["ok"] is True
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()
    report, code = _verify("s")
    assert code == 1 and report["ok"] is False
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]


def test_session_verify_exits_four_when_it_could_not_check_the_product_context(monkeypatch):
    """*Checked, and it is broken* and *could not check* are two answers and had one exit code (#86)."""
    _init()
    healthy = _verify("s")[0]
    assert healthy["ok"] is True and healthy["context_cards"]["checked"] is True
    _cards_unreadable(monkeypatch)
    report, code = _verify("s")
    assert code == 4 and report["ok"] is False and report["problems"] == []
    assert report["context_cards"]["checked"] is False and report["context_cards"]["problem"] is None
    text, code = _verify("s", json_out=False)
    assert code == 4 and "Could not check" in text


def test_session_verify_exits_four_when_the_lock_could_not_be_taken(monkeypatch):
    """The sibling of the test above, on the other probe (#263, #265)."""
    from requivo.deterministic.sessions import verify as sessions_mod

    _init()
    assert _verify("s")[0]["session"]["checked"] is True
    monkeypatch.setattr(sessions_mod, "inspect_session", _locked)
    report, code = _verify("s")
    assert code == 4 and report["ok"] is False and report["problems"] == []
    assert report["session"]["checked"] is False and "locked" in report["session"]["error"].lower()
    text, code = _verify("s", json_out=False)
    assert code == 4 and "Could not examine" in text


def test_session_verify_lets_a_firm_negative_outrank_a_partial_one(monkeypatch):
    """A session that is inconsistent *and* whose product context could not be checked exits 1 (#86)."""
    _init()
    run_cli_stdin(["model", "apply", "s", "-", "--json"], json.dumps(full_model()), monkeypatch)
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()
    report, code = _verify("s")
    assert code == 1 and report["context_cards"]["checked"] is True
    _cards_unreadable(monkeypatch)
    report, code = _verify("s")
    assert code == 1, "a partial answer displaced a complete one"
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]
    assert report["context_cards"]["checked"] is False


def test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken(tmp_path):
    """The other firm negative, and the one most easily confused with the new 4 (#86)."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _init("s", "--context", "lost-domain")
        assert _verify("s")[0]["ok"] is True
        (cards / "lost-domain.md").unlink()
        report, code = _verify("s")
        _, text_code = _verify("s", json_out=False)
    assert code == 1 and text_code == 1
    assert report["context_cards"]["checked"] is True
    assert report["context_cards"]["problem"]["code"] == "unknown_context_card"


# ── restore: the documented recovery path (#210) ───────────────────────────────


def _two_revisions(tmp_path) -> Path:
    """A session at revision 2, the second revision moving `workflow`; returns its directory."""
    _init()
    _apply("s", tmp_path, "p1.json")
    _apply("s", tmp_path, "p2.json", workflow=slot(80, "explicit", "high", "moved"))
    return store.canonical_dir("s")


def _revision(d: Path, n: int) -> str:
    return (d / "revisions" / f"{n:04d}-model.json").read_text(encoding="utf-8")


def _tear(d: Path) -> str:
    """Put revision 1 in model.json under revision 2's metadata: the exact scenario #210 was filed about."""
    (d / "model.json").write_text(_revision(d, 1), encoding="utf-8")
    return _revision(d, 1)


def _tamper_hash(f: Path) -> None:
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1), encoding="utf-8")


def _corrupt_json(f: Path) -> None:
    f.write_text("{not json", encoding="utf-8")


def test_session_verify_names_the_restorable_revision_and_restore_repairs_it(tmp_path):
    """The recovery path is named, not just the diagnosis, and the default restore is the newest revision."""
    d = _two_revisions(tmp_path)
    rev2 = _revision(d, 2)
    _tear(d)
    text, code = _verify("s", json_out=False)
    assert code == 1 and "model_is_not_the_last_revision" in text
    assert "revisions/0002-model.json" in text and "requivo session restore s" in text
    assert "revision 2" in run_cli(["session", "restore", "s"])
    assert (d / "model.json").read_text(encoding="utf-8") == rev2
    assert _verify("s")[0]["ok"] is True


def test_session_verify_says_it_could_not_check_whether_restore_would_help(tmp_path, monkeypatch):
    """`_restore_remedy_line` takes its own unlocked read; a locked one is the third state, never a fabricated remedy."""
    _tear(_two_revisions(tmp_path))
    real_meta = SessionService.meta
    monkeypatch.setattr(SessionService, "meta", lambda self, s: _locked(s) if s == "s" else real_meta(self, s))
    text, code = _verify("s", json_out=False)
    assert code == 1 and "model_is_not_the_last_revision" in text
    assert "Could not check whether" in text and "requivo session restore" not in text


def test_session_verify_names_no_remedy_for_a_problem_restore_cannot_fix(tmp_path):
    """The must-fire control: the remedy line is scoped to the codes `session restore` can address."""
    d = _two_revisions(tmp_path)
    (d / "revisions" / "0001-model.json").unlink()
    text, code = _verify("s", json_out=False)
    assert code == 1 and "missing_revision_file" in text and "session restore" not in text


@pytest.mark.parametrize("corrupt", [_corrupt_json, _tamper_hash], ids=["unparseable", "tampered-hash"])
def test_session_restore_skips_a_broken_revision_when_searching_for_the_default(tmp_path, corrupt):
    """Falling back to an older revision is a *partial* repair, and the receipt and `verify` both say so."""
    d = _two_revisions(tmp_path)
    _apply("s", tmp_path, "p3.json", permissions=slot(80, "explicit", "high", "HR only"))  # revision 3
    corrupt(d / "revisions" / "0003-model.json")
    _tear(d)
    out = run_cli(["session", "restore", "s"])
    assert "revision 2" in out and "partial repair" in out.lower()
    assert (d / "model.json").read_text(encoding="utf-8") == _revision(d, 2)
    report, code = _verify("s")
    assert code == 1 and any(p["code"] == "model_is_not_the_last_revision" for p in report["problems"])


def test_session_restore_accepts_an_explicit_revision_even_when_a_newer_one_is_healthy(tmp_path):
    """A named target is honoured exactly, never silently upgraded to the newest."""
    d = _two_revisions(tmp_path)
    run_cli(["session", "restore", "s", "--revision", "1"])
    assert (d / "model.json").read_text(encoding="utf-8") == _revision(d, 1)


@pytest.mark.parametrize("revision, corrupt", [
    ("1", _tamper_hash), ("99", None), ("1", Path.unlink), ("1", _corrupt_json),
], ids=["hash-mismatch", "out-of-range", "missing-file", "unparseable"])
def test_session_restore_refuses_a_broken_explicit_target(tmp_path, revision, corrupt):
    """Four ways an explicit `--revision N` can fail to resolve cleanly; a refused restore touches nothing (#555)."""
    d = _two_revisions(tmp_path)
    before = (d / "model.json").read_text(encoding="utf-8")
    if corrupt is not None:
        corrupt(d / "revisions" / "0001-model.json")
    assert run_cli_exit(["session", "restore", "s", "--revision", revision])[1] == 1
    assert (d / "model.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("arrange", [
    lambda tmp_path: [_corrupt_json(f) for f in (_two_revisions(tmp_path) / "revisions").glob("*.json")],
    lambda tmp_path: _init(),
], ids=["nothing-readable", "no-revision-yet"])
def test_session_restore_refuses_when_nothing_in_the_history_is_readable(tmp_path, arrange):
    arrange(tmp_path)
    assert run_cli_exit(["session", "restore", "s"])[1] == 1


def test_session_restore_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """A scanner briefly holding the destination (invariant 18's cause) is retried, and the restore lands."""
    d = _two_revisions(tmp_path)
    _tear(d)
    attempts = _replace_fails(monkeypatch, 2)
    assert "revision 2" in run_cli(["session", "restore", "s"])
    assert attempts["n"] == 3, "the restore did not actually go through the retry path"
    assert (d / "model.json").read_text(encoding="utf-8") == _revision(d, 2)


def test_session_restore_still_gives_up_on_a_permanent_permission_error(tmp_path, monkeypatch):
    """Bounded by `_REPLACE_ATTEMPTS`; the torn file is unreplaced, not half-written, and no scratch stays (#483)."""
    d = _two_revisions(tmp_path)
    torn = _tear(d)
    attempts = _replace_fails(monkeypatch, None)
    with pytest.raises(PermissionError):
        app(["session", "restore", "s"], client=None)
    assert attempts["n"] == _REPLACE_ATTEMPTS
    assert (d / "model.json").read_text(encoding="utf-8") == torn
    assert not list(d.glob(".*restore.tmp"))


def test_session_restore_does_not_touch_the_revision_log(tmp_path):
    """Restoring is model.json catching up with a history that was already the truth (#210)."""
    d = _two_revisions(tmp_path)
    _tear(d)
    before, files_before = store.read_meta("s"), sorted(p.name for p in (d / "revisions").glob("*.json"))
    run_cli(["session", "restore", "s"])
    after = store.read_meta("s")
    assert after.current_revision == before.current_revision == 2
    assert [r.model_dump() for r in after.revisions] == [r.model_dump() for r in before.revisions]
    assert sorted(p.name for p in (d / "revisions").glob("*.json")) == files_before
