"""Plugin/CLI version-skew detection for the shared preflight (#251)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SCRIPTS = ROOT / "plugins" / "claude-code" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

import version_skew  # noqa: E402
from version_skew import BEHIND, COULD_NOT_LOOK, IN_STEP, check, compare  # noqa: E402
from version_skew import tested_against_version as read_tested_against_version  # noqa: E402

MANIFEST = ROOT / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json"
REASONING = ROOT / "plugins" / "claude-code" / "REASONING.md"


def _doctor_json(version: str) -> str:
    return json.dumps({"requivo_version": version, "python_version": "3.12.0"})


# -- the comparison itself, both directions (the must-fire / must-not-fire pair) -------------


def test_a_newer_cli_is_in_step_and_silent():
    result = compare("1.4.0", "1.3.0")
    assert result.state == IN_STEP
    assert "1.3.0" in result.message or "1.4.0" in result.message


def test_an_equal_cli_is_in_step():
    result = compare("1.3.0", "1.3.0")
    assert result.state == IN_STEP


def test_an_older_cli_is_behind_and_warns():
    """The positive control for the case above -- without this, `IN_STEP` could be returned no matter what the
    code does, and the two tests above would still pass."""
    result = compare("1.2.0", "1.3.0")
    assert result.state == BEHIND
    assert "1.2.0" in result.message and "1.3.0" in result.message


def test_behind_never_recommends_refusing():
    result = compare("1.0.0", "1.3.0")
    assert "refuse" not in result.message.lower()
    assert "stop" not in result.message.lower()


# -- the could-not-look arm: never the same as in-step ----------------------------------------


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        (None, "the `requivo` command was not found on PATH"),
        ("", None),
        ("not json at all {{{", None),
        (json.dumps({"python_version": "3.12.0"}), None),
    ],
    ids=[
        "doctor-call-failed-outright",
        "empty-doctor-output",
        "unparseable-doctor-json",
        "doctor-json-missing-requivo-version",
    ],
)
def test_doctor_input_that_cannot_be_read_is_could_not_look(stdout, error):
    result = check(stdout, error)
    assert result.state == COULD_NOT_LOOK


def test_could_not_look_never_reads_as_in_step():
    """The bar from the brief: could-not-read must never render as 'versions match'."""
    for result in (
        check(None, "not found"),
        check("", None),
        check("{broken", None),
        check(json.dumps({}), None),
    ):
        assert result.state != IN_STEP
        assert "not" in result.message.lower() or "could" in result.message.lower()


def test_a_readable_doctor_report_is_not_could_not_look():
    """The positive control on the arm above: a guard that reported could-not-look for everything would pass
    every assertion above it and say nothing true about a healthy install."""
    result = check(_doctor_json("1.3.0"), None)
    assert result.state != COULD_NOT_LOOK


# -- reading the plugin's own declared version, live, never a second literal ------------------


def test_tested_against_version_reads_the_real_manifest():
    version = read_tested_against_version(MANIFEST)
    manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    assert version == manifest_version


def test_tested_against_version_is_could_not_look_shaped_when_the_manifest_is_bad(tmp_path):
    bad = tmp_path / "plugin.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        read_tested_against_version(bad)


def test_check_reads_the_manifest_end_to_end(monkeypatch):
    """The whole flow: doctor output in, manifest read live, a verdict out."""
    result = check(_doctor_json("0.1.0"), None, manifest_path=MANIFEST)
    assert result.state == BEHIND


# -- review findings (self-review, #299/#251/#347): both against the module's own contract ---


def test_a_non_version_shaped_manifest_value_is_could_not_look_not_in_step(tmp_path):
    """Found in self-review. `_parse_version` tolerates a non-numeric TRAILING component (`.dev0`, `-rc1`)."""
    bad = tmp_path / "plugin.json"
    bad.write_text(json.dumps({"version": "unreleased"}), encoding="utf-8")
    result = check(_doctor_json("1.3.0"), None, manifest_path=bad)
    assert result.state == COULD_NOT_LOOK, (
        f"a non-version-shaped manifest value must not render as IN_STEP, got state={result.state} "
        f"message={result.message!r}"
    )


def test_a_non_version_shaped_cli_version_is_could_not_look():
    """The other side of the same collapse: `requivo_version` itself not being version-shaped."""
    result = check(_doctor_json("unknown"), None)
    assert result.state == COULD_NOT_LOOK


def test_differing_precision_is_not_reported_as_behind():
    """Found in self-review. Tuple comparison of different lengths makes a true PREFIX read as smaller."""
    result = compare("1.3", "1.3.0")
    assert result.state == IN_STEP, (
        f"'1.3' and '1.3.0' should compare equal (differing precision, same release), got "
        f"state={result.state} message={result.message!r}"
    )


# -- the prose preflight must not duplicate the version as a literal --------------------------


def test_reasoning_md_names_the_skew_check_without_hardcoding_a_version():
    """REASONING.md is what Claude actually reads at runtime."""
    text = REASONING.read_text(encoding="utf-8")
    assert "version_skew.py" in text or "requivo_version" in text, (
        "REASONING.md's preflight does not mention the version-skew comparison at all"
    )
    assert ".claude-plugin/plugin.json" in text, (
        "REASONING.md must point at the manifest as the source of the tested-against version, "
        "not restate a number"
    )
    import re
    # A bare X.Y.Z anywhere in this file's prose is exactly the duplicated literal this test exists to catch.
    stray_versions = re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])", text)
    assert not stray_versions, (
        f"REASONING.md hardcodes what looks like a version number: {stray_versions} -- read it "
        f"from the manifest instead, or this drifts the day the manifest is bumped"
    )


# -- main()'s subprocess arm: the third failure mode, and its must-fire twins (#363) ----------
#
# `subprocess.TimeoutExpired` inherits `SubprocessError -> Exception`, NOT `OSError` (#251).
#
# Reachable, not theoretical: #263 makes `doctor` take the per-slug session write lock with `_LOCK_TIMEOUT_SECONDS = 30.0`, the same 30s this module passes to `subprocess.run(timeout=...)`.
#
# These monkeypatch `subprocess.run` rather than spawning a real process.


class _FakeCompletedProcess:
    """The one attribute `main()` reads off `subprocess.run`'s return value."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_main_reports_could_not_look_on_subprocess_timeout(monkeypatch, capsys):
    """The bug itself: a timeout must produce COULD_NOT_LOOK and exit 3, not an uncaught TimeoutExpired."""

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["requivo", "doctor", "--json"], timeout=30)

    monkeypatch.setattr(version_skew.subprocess, "run", _raise_timeout)
    exit_code = version_skew.main()
    message = capsys.readouterr().out

    assert exit_code == COULD_NOT_LOOK
    assert "traceback" not in message.lower()


def test_main_distinguishes_a_timeout_from_a_missing_binary(monkeypatch, capsys):
    """The judgment call from the brief: a timeout and a missing binary both land in COULD_NOT_LOOK (#263)."""

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["requivo", "doctor", "--json"], timeout=30)

    monkeypatch.setattr(version_skew.subprocess, "run", _raise_timeout)
    assert version_skew.main() == COULD_NOT_LOOK
    timeout_message = capsys.readouterr().out

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("requivo")

    monkeypatch.setattr(version_skew.subprocess, "run", _raise_not_found)
    assert version_skew.main() == COULD_NOT_LOOK
    missing_message = capsys.readouterr().out

    assert timeout_message != missing_message
    assert "PATH" in missing_message and "PATH" not in timeout_message
    assert "30" in timeout_message, (
        f"the timeout message should name how long it waited, got {timeout_message!r}"
    )


def test_main_still_reports_could_not_look_on_a_plain_os_error(monkeypatch, capsys):
    """Acceptance criteria: the other two arms behave as before."""

    def _raise_os_error(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(version_skew.subprocess, "run", _raise_os_error)
    exit_code = version_skew.main()
    message = capsys.readouterr().out

    assert exit_code == COULD_NOT_LOOK
    assert "permission denied" in message


def test_main_must_fire_control_a_genuine_skew_still_reports_skew(monkeypatch, capsys):
    """The must-fire twin the brief asks for, paired with the three could-not-look arms above."""
    real_plugin_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    old_version = "0.0.1"
    assert old_version != real_plugin_version  # guard the fixture's own assumption

    def _fake_run(*args, **kwargs):
        return _FakeCompletedProcess(stdout=_doctor_json(old_version))

    monkeypatch.setattr(version_skew.subprocess, "run", _fake_run)
    exit_code = version_skew.main()
    message = capsys.readouterr().out

    assert exit_code == BEHIND
    assert old_version in message
