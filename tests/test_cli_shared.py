"""The parts of `requivo.deterministic` that are not a verb: `__init__.register` and `_shared.input` (#141)."""
from __future__ import annotations

import io
import json

import pytest
from _cli_harness import _full_model, _run, _run_stdin

from requivo.cli import _build_parser
from requivo.core import persistence as store


def test_new_verbs_are_bound_in_the_parser():
    cases = [
        (["doctor"], "_cmd_doctor"),
        (["session", "init", "r"], "_cmd_session_init"),
        (["session", "migrate"], "_cmd_session_migrate"),
        (["model", "apply", "s", "p.json"], "_cmd_model_apply"),
        (["model", "validate", "p.json"], "_cmd_model_validate"),
        (["artifact", "save", "s", "--type", "prd", "--file", "f"], "_cmd_artifact_save"),
    ]
    for argv, fname in cases:
        assert _build_parser().parse_args(argv).func.__name__ == fname


# ── documents on stdin ──────────────────────────────────────────────────────────
# `-` exists so a caller holding content does not have to invent a file for it.




def test_a_proposal_can_be_applied_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    proposal = json.dumps(_full_model())
    r = json.loads(_run_stdin(["model", "validate", "-", "--json"], proposal, monkeypatch))
    assert r["status"] == "valid"
    applied = json.loads(_run_stdin(["model", "apply", "s", "-", "--expected-revision", "0", "--json"],
                                    proposal, monkeypatch))
    assert applied["revision"] == 1


def test_an_artifact_can_be_saved_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    r = json.loads(_run_stdin(["artifact", "save", "s", "--type", "prd", "--file", "-",
                               "--revision", "1", "--json"],
                              "# PRD\nwritten straight to stdin\n", monkeypatch))
    assert r["revision"] == 1 and r["stale"] is False
    assert "straight to stdin" in _run(["artifact", "show", "s", "--type", "prd"])


def test_a_request_can_be_created_from_stdin(workspace, monkeypatch):
    r = json.loads(_run_stdin(["session", "init", "-", "--slug", "s", "--json"],
                              "We need a leave approval system.\n", monkeypatch))
    assert r["slug"] == "s"
    assert "leave approval" in store.session_request("s")


def test_a_missing_document_path_is_an_error_not_content(workspace):
    # `model apply <session> <path>` takes a path.
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "no-such-file.json", "--json"])
    assert exc.value.code != 0


def test_stdin_is_refused_when_it_is_a_terminal(workspace, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    monkeypatch.setattr("sys.stdin", _Tty(""))
    # Without this guard the command blocks forever waiting for input nobody meant to type.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "-", "--json"])
    assert exc.value.code != 0
