"""One paste is a bug report: version, OS, model (#247)."""
from __future__ import annotations

import platform

import pytest
from _cli_harness import _run, _run_json

import requivo
from requivo.cli import app

# `workspace` (tmp_path + REQUIVO_WORKSPACE/REQUIVO_OUTPUT_DIR) is the shared fixture from conftest.py (#555).

# -- requivo --version -----------------------------------------------------------------------


def test_the_version_flag_prints_the_version_and_exits_zero(capsys):
    """argparse's `version` action exits 0 through SystemExit, which is success and not a failure."""
    with pytest.raises(SystemExit) as exc:
        app(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"requivo {requivo.__version__}"


def test_the_version_flag_declares_nothing_that_test_version_sites_cannot_see():
    """The flag must be a *read* of `requivo.__version__`, not a fifth place the number is written."""
    parser_version = _version_action_string()
    assert requivo.__version__ in parser_version
    # Must fire: a hardcoded "1.2.0" would satisfy the line above on the day it is written.
    assert parser_version == f"requivo {requivo.__version__}"


def _version_action_string() -> str:
    import argparse

    from requivo.cli import _build_parser

    for action in _build_parser()._actions:
        if isinstance(action, argparse._VersionAction):
            return action.version
    raise AssertionError("_build_parser() registers no --version action")


def test_the_version_flag_works_with_no_workspace_and_no_sessions(tmp_path, monkeypatch, capsys):
    """The state a bug reporter is actually in: a fresh install, run from anywhere."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        app(["--version"])
    assert exc.value.code == 0
    assert not (tmp_path / ".requivo").exists()


# -- doctor: the OS and the model --------------------------------------------------------------


def test_doctor_reports_the_platform_and_the_model(workspace, monkeypatch):
    """The two facts the bug template asks for by hand."""
    monkeypatch.delenv("MODEL", raising=False)
    report = _run_json(["doctor", "--json"])
    assert report["os"] == platform.platform()
    assert report["model"]["source"] == "default"
    assert report["model"]["name"]
    # Still there: the keys a consumer already reads.
    assert {"requivo_version", "python_version", "assets", "schema", "provider_anthropic",
            "workspace"} <= set(report)


def test_doctor_distinguishes_a_model_override_from_the_default(workspace, monkeypatch):
    """The distinction is the point, not the string."""
    monkeypatch.delenv("MODEL", raising=False)
    default = _run_json(["doctor", "--json"])["model"]

    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    overridden = _run_json(["doctor", "--json"])["model"]

    assert default["source"] == "default"
    assert overridden["source"] == "env"
    assert overridden["name"] == "claude-opus-4-8"
    assert overridden["name"] != default["name"], (
        "the fixture no longer overrides anything -- pick a model id that is not the default")


def test_a_model_override_that_is_set_but_empty_is_reported_as_one(workspace, monkeypatch):
    """`name` and `source` are read from the same fact or they can disagree, and they did."""
    monkeypatch.setenv("MODEL", "")
    report = _run_json(["doctor", "--json"])
    assert report["model"]["source"] == "env", (
        "an exported MODEL is an override whether or not it has a value")
    # And `name` still reports what a call would actually send, which is the empty string.
    assert report["model"]["name"] == ""

    text = _run(["doctor"])
    assert "MODEL is set but empty" in text
    assert "✅ model" not in text, "an empty model id must not render as a healthy row"


def test_doctor_prefers_requivo_model_over_bare_model_and_reports_it_as_an_override(
    workspace, monkeypatch
):
    """#268's own precedence, read back through the same public fact this file's other tests pin rather than
    through `current_model_name()` directly."""
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    report = _run_json(["doctor", "--json"])["model"]
    assert report == {"name": "claude-opus-4-8", "source": "env"}


def test_a_requivo_model_override_that_is_set_but_empty_is_reported_as_one(workspace, monkeypatch):
    """The `REQUIVO_MODEL` twin of `test_a_model_override_that_is_set_but_empty_is_reported_as_one`."""
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "")
    report = _run_json(["doctor", "--json"])["model"]
    assert report == {"name": "", "source": "env"}


def test_the_doctor_human_view_shows_both_rows(workspace, monkeypatch):
    """A `--json` key nobody prints is a fact a bug reporter still has to know to ask for."""
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    text = _run(["doctor"])
    assert platform.platform() in text
    assert "claude-opus-4-8" in text
    assert "MODEL" in text          # the override is named as one, not silently rendered
