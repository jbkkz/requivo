"""One paste is a bug report: version, OS, model (#247).

`.github/ISSUE_TEMPLATE/bug.md` asks a reporter for the Requivo version, the OS, and the model in
use. For a tool with no telemetry that list *is* the diagnostic channel, and a reporter could
assemble none of it in one command: `requivo --version` did not exist at all (argparse answered
"the following arguments are required: <command>" and exited 2), and `doctor` reported Python and
Requivo but neither the platform nor whether `MODEL` was overridden.

Two halves, and they are tested together because they are one story rather than two features.

**`--version` reads `requivo.__version__` and never a literal.** `tests/test_version_sites.py`
exists because an unguarded README badge sat fifteen releases stale, and it enforces agreement
across the four files that *declare* the version. A fifth declaration in `cli.py` would be outside
its scan set -- a version site with no guard, added by the change whose whole subject is telling
people the right version. So the assertion below is that the flag agrees with the dunder, which is
what makes it a *read* rather than a site.

**The doctor keys are additive.** Invariant 8 makes adding a `--json` key free and renaming one
expensive, so the existing keys are asserted still present in the same test that asserts the new
ones -- the cheap half of a promise nobody would otherwise re-check.
"""
from __future__ import annotations

import platform

import pytest
from _cli_harness import _run, _run_json

import requivo
from requivo.cli import app

# `workspace` (tmp_path + REQUIVO_WORKSPACE/REQUIVO_OUTPUT_DIR) is the shared fixture from
# conftest.py (#555) -- this file used to declare its own byte-identical copy.

# -- requivo --version -----------------------------------------------------------------------


def test_the_version_flag_prints_the_version_and_exits_zero(capsys):
    """argparse's `version` action exits 0 through SystemExit, which is success and not a failure --
    worth asserting, because the old behaviour also raised SystemExit and it meant the opposite."""
    with pytest.raises(SystemExit) as exc:
        app(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"requivo {requivo.__version__}"


def test_the_version_flag_declares_nothing_that_test_version_sites_cannot_see():
    """The flag must be a *read* of `requivo.__version__`, not a fifth place the number is written.

    Asserted by identity with the dunder rather than against a literal: a literal here would agree
    on the day it was written and drift on the next release, which is the exact failure
    `test_version_sites.py` was built for -- reproduced inside the change that closes it."""
    parser_version = _version_action_string()
    assert requivo.__version__ in parser_version
    # Must fire: a hardcoded "1.2.0" would satisfy the line above on the day it is written. This one
    # fails the moment the string stops being derived.
    assert parser_version == f"requivo {requivo.__version__}"


def _version_action_string() -> str:
    import argparse

    from requivo.cli import _build_parser

    for action in _build_parser()._actions:
        if isinstance(action, argparse._VersionAction):
            return action.version
    raise AssertionError("_build_parser() registers no --version action")


def test_the_version_flag_works_with_no_workspace_and_no_sessions(tmp_path, monkeypatch, capsys):
    """The state a bug reporter is actually in: a fresh install, run from anywhere. `--version` must
    not need a session store, must not create one, and must not read a slug."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        app(["--version"])
    assert exc.value.code == 0
    assert not (tmp_path / ".requivo").exists()


# -- doctor: the OS and the model --------------------------------------------------------------


def test_doctor_reports_the_platform_and_the_model(workspace, monkeypatch):
    """The two facts the bug template asks for by hand. Both additive keys, so the assertion below
    also pins the ones that were already there -- invariant 8 makes adding free and renaming a
    breaking change, and this is the cheapest place that promise is checked."""
    monkeypatch.delenv("MODEL", raising=False)
    report = _run_json(["doctor", "--json"])
    assert report["os"] == platform.platform()
    assert report["model"]["source"] == "default"
    assert report["model"]["name"]
    # Still there: the keys a consumer already reads.
    assert {"requivo_version", "python_version", "assets", "schema", "provider_anthropic",
            "workspace"} <= set(report)


def test_doctor_distinguishes_a_model_override_from_the_default(workspace, monkeypatch):
    """The distinction is the point, not the string. A reporter who has `MODEL` set in a shell
    profile they forgot about is the case this row exists for -- and a row that printed the resolved
    name alone would look identical in both states."""
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
    """`name` and `source` are read from the same fact or they can disagree, and they did.
    `current_model_name()` falls back only when `MODEL` is **absent**; an exported-but-empty `MODEL`
    produced `{"name": "", "source": "default"}`, naming neither the default nor the real override.
    Both now key on presence, with no tick printed over an empty model id."""
    monkeypatch.setenv("MODEL", "")
    report = _run_json(["doctor", "--json"])
    assert report["model"]["source"] == "env", (
        "an exported MODEL is an override whether or not it has a value")
    # And `name` still reports what a call would actually send, which is the empty string -- not the
    # default it is not going to use. Reporting `claude-sonnet-5` here would be the comfortable lie.
    assert report["model"]["name"] == ""

    text = _run(["doctor"])
    assert "MODEL is set but empty" in text
    assert "✅ model" not in text, "an empty model id must not render as a healthy row"


def test_doctor_prefers_requivo_model_over_bare_model_and_reports_it_as_an_override(
    workspace, monkeypatch
):
    """#268's own precedence, read back through the same public fact this file's other tests pin
    rather than through `current_model_name()` directly -- `doctor` is what a bug reporter actually
    pastes, so this is the shape a regression would actually be caught in."""
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    report = _run_json(["doctor", "--json"])["model"]
    assert report == {"name": "claude-opus-4-8", "source": "env"}


def test_a_requivo_model_override_that_is_set_but_empty_is_reported_as_one(workspace, monkeypatch):
    """The `REQUIVO_MODEL` twin of `test_a_model_override_that_is_set_but_empty_is_reported_as_one`.

    Both `current_model_name()` and `doctor_report()`'s `source` check key on presence
    (`is not None`), not truthiness -- a truthy check on `""` would silently report the comfortable
    lie that nothing was overridden, for the variable every doc now teaches first."""
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "")
    report = _run_json(["doctor", "--json"])["model"]
    assert report == {"name": "", "source": "env"}


def test_the_doctor_human_view_shows_both_rows(workspace, monkeypatch):
    """A `--json` key nobody prints is a fact a bug reporter still has to know to ask for. The human
    rendering is what a paste actually contains."""
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    text = _run(["doctor"])
    assert platform.platform() in text
    assert "claude-opus-4-8" in text
    assert "MODEL" in text          # the override is named as one, not silently rendered
