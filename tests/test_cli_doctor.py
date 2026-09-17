"""End-to-end tests of `requivo.deterministic.doctor` — the `doctor`, `schema` and `context` verbs' own health
checks: credentials, model source, the write-lock path, and context-card health (#141)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from _cli_harness import _run, _run_json

from requivo.core import persistence as store

# ── doctor ──────────────────────────────────────────────────────────────────────


def test_doctor_runs_without_api_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # Since #334 the credential guard asks the SDK, which also discovers an active profile on disk.
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)
    r = _run_json(["doctor", "--json"])
    assert r["schema"]["ok"] and r["schema"]["slots"] > 0
    # Missing key / SDK must never be reported as a hard failure.
    assert r["provider_anthropic"]["api_key_present"] is False
    assert "sessions" in r["workspace"]


def test_doctor_reports_the_model_source_as_env_when_requivo_model_is_set(workspace, monkeypatch):
    """#268 renamed the model override's primary name, and `doctor_report()` used to decide `model.source` by
    reading bare `MODEL` again rather than asking `current_model_name()` how it actually resolved."""
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    r = _run_json(["doctor", "--json"])
    assert r["model"] == {"name": "claude-opus-4-8", "source": "env"}


def test_doctor_reports_a_bearer_token_as_a_credential_present(workspace, monkeypatch):
    """#332: doctor read `ANTHROPIC_API_KEY` alone while the runner (`new_client`) also accepts
    `ANTHROPIC_AUTH_TOKEN` (#201), so a working bearer-token install reported `api_key_present: false`."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is True


# ── #365: a second reader wants more than the bool ──────────────────────────

_NEEDS_CHAIN = pytest.mark.skipif(
    not hasattr(__import__("anthropic._client", fromlist=["default_credentials"]), "default_credentials"),
    reason=(
        "the installed anthropic SDK has no profile/federation discovery chain (the floor of "
        "`anthropic>=0.42.0,<2` predates it). UNTESTED ON THIS SDK: an unloadable ANTHROPIC_PROFILE "
        "is not reachable to name in doctor's output. See test_provider.py's own _NEEDS_CHAIN for "
        "the full reasoning -- this is the identical gate, one file over."
    ),
)


@_NEEDS_CHAIN
def test_doctor_names_the_remedy_for_an_unloadable_profile_rather_than_no_api_key(workspace, monkeypatch):
    """#365: `doctor` used to call `credential_present()` bare, flattening "no credential" and "a credential
    that is configured and unloadable" onto the same False."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is False
    problem = r["provider_anthropic"]["credential_problem"]
    assert problem is not None
    assert "could not load the credential configuration" in problem
    assert "a-profile-that-does-not-exist" in problem, "the SDK's own reason names the profile"

    text = _run(["doctor"])
    assert "no API key" not in _check_line(text, "anthropic"), (
        "the wrong remedy for an unloadable profile -- setting a variable will not fix a file the "
        "SDK could not read, and nothing here tells the reader that"
    )
    assert "could not be loaded" in text
    assert "a-profile-that-does-not-exist" in text


def test_doctor_still_says_no_api_key_when_none_is_configured_at_all(workspace, monkeypatch):
    """The must-not-fire twin, genuinely reached rather than merely asserted against silence."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)

    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is False
    assert r["provider_anthropic"]["credential_problem"] is None

    text = _run(["doctor"])
    assert "no API key" in _check_line(text, "anthropic")


# ── doctor's own failures must not render as green ticks (#12) ──────────────────
#
# Every test in this block asserts that the *healthy* and the *broken* case produce **different** output.


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check — the one carrying a tick."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


def test_doctor_reports_where_the_write_lock_lives(workspace):
    """#113 moved the write lock out of the session directory, and a convention the diagnostic does not report
    is one it answers about the wrong shape."""
    r = _run_json(["doctor", "--json"])["workspace"]
    assert r["locks"] == str(store.lock_root())
    assert r["sessions"] == str(store.session_root()), "the published key must not have moved"
    assert not store.lock_root().is_relative_to(store.session_root()), (
        "a lock root under the session root would be a permanent non-session entry there")
    assert "locks" in _run(["doctor"]), "the human rendering says it too, not only --json"


def test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one(workspace):
    """Three states, three renderings. `available_cards()` failing used to be written into `schema["error"]` —
    a *different* check's field."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise OSError("boom")

    healthy = _run_json(["doctor", "--json"])
    assert healthy["context"]["ok"] is True, "fixture is blind: the bundled cards did not load"
    assert healthy["context"]["status"] == "ok"
    assert healthy["context"]["count"] > 0 and healthy["context"]["error"] is None
    healthy_text = _run(["doctor"])

    # (a) the directory is gone — `_card_paths` skips what does not exist and returns nothing.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", list)
        empty = _run_json(["doctor", "--json"])
        empty_text = _run(["doctor"])
    assert empty["context"]["ok"] is False
    assert empty["context"]["status"] == "empty"
    assert empty["context"]["count"] == 0
    assert empty["schema"]["ok"] is True and empty["schema"]["error"] is None

    # (b) the directory cannot be read at all — a different answer again.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", _unreadable)
        broken = _run_json(["doctor", "--json"])
        broken_text = _run(["doctor"])
    assert broken["context"]["ok"] is False
    assert broken["context"]["status"] == "unreadable"
    assert "boom" in (broken["context"]["error"] or "")
    assert broken["schema"]["ok"] is True and broken["schema"]["error"] is None, (
        "a context-card failure must not be reported as a schema failure")

    # The human rendering distinguishes them too — the JSON being right is no use to a reader counting ticks.
    assert "✅" in _check_line(healthy_text, "context cards")
    assert "✅" not in _check_line(empty_text, "context cards")
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "boom" in broken_text, "the captured error was never shown to the reader"
    assert healthy_text != empty_text and empty_text != broken_text


def test_doctor_tells_an_empty_workspace_from_an_unreadable_one(workspace):
    """`_session_health` caught every exception and returned `{"total": 0, "inconsistent": {}}`."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise PermissionError("Permission denied")

    empty = _run_json(["doctor", "--json"])["sessions"]
    assert empty["total"] == 0 and empty["readable"] is True and empty["error"] is None
    assert empty["non_sessions"] == [], "we looked and there was nothing else here"
    empty_text = _run(["doctor"])

    with pytest.MonkeyPatch.context() as mp:
        # `scan_session_root`, because that is the one listing `_session_health` makes since #67.
        mp.setattr(det.store, "scan_session_root", _unreadable)
        unreadable = _run_json(["doctor", "--json"])["sessions"]
        unreadable_text = _run(["doctor"])
    assert unreadable["readable"] is False
    assert unreadable["total"] is None, "0 is a claim about the workspace; we could not look"
    assert unreadable["non_sessions"] is None, (
        "an empty list here reads as `we looked and found nothing else` — the same conflation one "
        "key along, in the arm where the root could not be listed at all (#67)")
    assert "Permission denied" in (unreadable["error"] or "")

    assert "✅" in _check_line(empty_text, "sessions")
    assert "✅" not in _check_line(unreadable_text, "sessions")
    assert "0 in this workspace" in empty_text
    assert "0 in this workspace" not in unreadable_text
    assert "unreadable" in unreadable_text and "Permission denied" in unreadable_text


def _deny_read(directory: Path) -> None:
    """Make `directory` genuinely unreadable, or skip loudly naming what went untested."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny reads on Windows — the unreadable-card-directory "
                    "path is untested on this platform")
    directory.chmod(0o000)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod 000 did not deny reads here (running as root?) — the "
                "unreadable-card-directory path is untested on this run")


def test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty(workspace, tmp_path):
    """The `unreadable` state has to be reachable by what actually makes a directory unreadable (#12)."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "walled-domain.md").write_text("# Walled domain\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "walled-domain", "--json"])

        # ── readable: the must-fire control ───────────────────────────────────
        healthy = _run_json(["doctor", "--json"])
        assert healthy["context"]["status"] == "ok"
        assert "walled-domain" in healthy["context_cards"]
        assert healthy["sessions"]["cards_checked"] is True
        assert healthy["sessions"]["unresolved_cards"] == {}

        _deny_read(cards)
        try:
            broken = _run_json(["doctor", "--json"])
            broken_text = _run(["doctor"])
        finally:
            cards.chmod(0o755)

    assert broken["context"]["status"] == "unreadable", (
        "a permission-denied card directory is not an install with no cards; the remedy differs")
    assert broken["context"]["ok"] is False
    assert "walled-domain" not in broken["context_cards"]

    # The session must not be accused of naming a card that does not exist.
    assert broken["sessions"]["cards_checked"] is False
    assert broken["sessions"]["unresolved_cards"] == {}
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "✅" not in _check_line(broken_text, "sessions"), (
        "the sessions line ticked while nobody had checked their product context")
    assert "not checked" in _check_line(broken_text, "sessions")


