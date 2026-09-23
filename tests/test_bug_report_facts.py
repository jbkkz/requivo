"""One paste is a bug report: version, OS, model (#247, #268)."""
from __future__ import annotations

import argparse
import platform

import pytest
from _fakes import run_cli, run_cli_json

import requivo
from requivo.cli import _build_parser, app

pytestmark = pytest.mark.usefixtures("workspace")


def test_the_version_flag_prints_the_version_exits_zero_and_touches_no_workspace(workspace, capsys):
    """argparse's `version` action exits 0 through SystemExit; a fresh install run from anywhere writes nothing."""
    with pytest.raises(SystemExit) as exc:
        app(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.strip() == f"requivo {requivo.__version__}"
    assert not (workspace / ".requivo").exists()


def test_the_version_flag_declares_nothing_that_test_version_sites_cannot_see():
    """The flag is a read of `requivo.__version__`, never a fifth place the number is written."""
    (version,) = [a.version for a in _build_parser()._actions if isinstance(a, argparse._VersionAction)]
    assert version == f"requivo {requivo.__version__}"  # must fire: a hardcoded number would still contain the version


def test_doctor_reports_the_platform_and_the_model(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    report = run_cli_json(["doctor", "--json"])
    assert report["os"] == platform.platform() and report["model"]["source"] == "default"
    assert report["model"]["name"] and report["model"]["name"] != "claude-opus-4-8", "the override tests below must override"
    assert {"requivo_version", "python_version", "assets", "schema", "provider_anthropic", "workspace"} <= set(report)


@pytest.mark.parametrize("env, expected", [
    ({"MODEL": "claude-opus-4-8"}, {"name": "claude-opus-4-8", "source": "env"}),
    ({"MODEL": "some-other-tools-model", "REQUIVO_MODEL": "claude-opus-4-8"}, {"name": "claude-opus-4-8", "source": "env"}),
    ({"REQUIVO_MODEL": ""}, {"name": "", "source": "env"}),
], ids=["bare-MODEL", "REQUIVO_MODEL-wins", "REQUIVO_MODEL-set-but-empty"])
def test_doctor_reports_an_override_as_one_with_268s_precedence(monkeypatch, env, expected):
    """`name` and `source` are read from the same fact, or they can disagree (#247, #268)."""
    monkeypatch.delenv("MODEL", raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    report = run_cli_json(["doctor", "--json"])["model"]
    assert report == expected


def test_a_model_override_that_is_set_but_empty_is_reported_as_one(monkeypatch):
    """An exported MODEL is an override whether or not it has a value, and the human view says so."""
    monkeypatch.setenv("MODEL", "")
    assert run_cli_json(["doctor", "--json"])["model"] == {"name": "", "source": "env"}
    text = run_cli(["doctor"])
    assert "MODEL is set but empty" in text and "✅ model" not in text, "an empty model id must not render as healthy"


def test_the_doctor_human_view_shows_both_rows(monkeypatch):
    """A `--json` key nobody prints is a fact a bug reporter still has to know to ask for."""
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    text = run_cli(["doctor"])
    assert platform.platform() in text and "claude-opus-4-8" in text and "MODEL" in text
