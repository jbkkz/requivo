"""The tracked `.claude/` layer must stay inert for anyone without the maintainer's plugins (#2, #186, #215)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RULE_LAYER = ".claude/jit-context/tools/01-oss/supertool-required.md"
PROJECT_SETTINGS = ".claude/settings.json"
PROJECT_SETTINGS_LOCAL = ".claude/settings.local.json"   # the per-machine half, asserted absent from what is tracked
# Top-level keys the tracked project settings may carry (#215); each one is described in CONTRIBUTING.md.
ALLOWED_SETTINGS_KEYS = frozenset()
COMMAND_KEYS = {"hooks", "statusline", "command"}        # key names whose value is something Claude Code runs
EXECUTABLE_SUFFIXES = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl", ".exe", ".bat", ".cmd", ".ps1"}


def _looks_executable(rel: str) -> bool:
    return Path(rel).suffix.lower() in EXECUTABLE_SUFFIXES


def _declares_hooks(data: object) -> bool:
    return isinstance(data, dict) and bool(data.get("hooks"))


def _command_surface(data: object, path: str = "") -> list[str]:
    """Every place in a settings document that names something for Claude Code to run (#2)."""
    hits: list[str] = []
    if isinstance(data, dict):
        if data.get("type") == "command" and data.get("command"):
            hits.append(path or "<root>")
        for key, value in data.items():
            child = f"{path}.{key}" if path else key
            if isinstance(key, str) and key.lower() in COMMAND_KEYS and value:
                hits.append(child)
            hits.extend(_command_surface(value, child))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            hits.extend(_command_surface(value, f"{path}[{index}]"))
    return list(dict.fromkeys(hits))


def _documented_in(key: str, prose: str) -> bool:
    """Is `key` named in `prose` *as a key*, rather than merely occurring inside some word?"""
    return f"`{key}`" in prose


def _enables_plugins(data: object) -> bool:
    return isinstance(data, dict) and bool(data.get("enabledPlugins"))


def _tracked_under_dot_claude() -> list[str]:
    """Every path git tracks under `.claude/`, or a loud skip when git cannot be asked; never an empty set."""
    if not (REPO / ".git").exists():
        pytest.skip("no .git here, so the tracked file set cannot be enumerated -- the .claude/ guards went unchecked")
    try:
        out = subprocess.run(["git", "ls-files", "-z", ".claude"], cwd=REPO, capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - environment
        pytest.skip(f"git ls-files failed ({exc}); the .claude/ guards went unchecked in this run")
    tracked = [p for p in out.split("\0") if p]
    assert tracked, "scanned no tracked files under .claude/ -- every guard over them would pass vacuously"
    return tracked


def _tracked_content(rel: str) -> str:
    """The content git tracks at `rel` -- read from the index, never from the working copy."""
    return subprocess.run(["git", "show", f":{rel}"], cwd=REPO, capture_output=True, check=True).stdout.decode("utf-8")


def _tracked_settings_documents(tracked=None) -> list:
    """Every tracked JSON document under `.claude/`, parsed -- and a failure when there are none."""
    documents = []
    for rel in (_tracked_under_dot_claude() if tracked is None else tracked):
        if not rel.endswith(".json"):
            continue
        try:
            documents.append((rel, json.loads(_tracked_content(rel))))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{rel!r}: tracked settings must be readable JSON ({exc})")
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.fail(f"{rel!r}: git lists it but could not produce its content ({exc})")
    assert documents, ("scanned no tracked JSON under .claude/ -- expected at least the project settings; point this "
                       "helper at whatever a clone now receives rather than answering green about an empty set")
    return documents


# -- the positive controls, first: a guard that cannot fire is not a guard ----------------------


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The scan set must still contain the two files these guards are about."""
    tracked = _tracked_under_dot_claude()
    assert PROJECT_SETTINGS in tracked, f"{PROJECT_SETTINGS} is not tracked any more; point this guard at the new file set"
    assert RULE_LAYER in tracked, f"{RULE_LAYER} is not tracked any more (#2 argued for that); update this file to match"


def test_the_hook_detector_fires_on_a_settings_file_that_does_register_hooks():
    """The must-fire half of `test_the_repository_registers_no_hooks`."""
    assert _declares_hooks({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}})
    assert not _declares_hooks({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _declares_hooks({"hooks": {}}), "an empty hooks block registers nothing"
    assert not _declares_hooks(["hooks"]), "a non-mapping document registers nothing"


def test_the_script_detector_fires_on_the_shapes_a_hook_could_point_at():
    """The must-fire half of `test_no_hook_script_is_tracked_under_dot_claude`."""
    for rel in (".claude/hooks/pre.sh", ".claude/x.py", ".claude/x.PS1", ".claude/nested/a.bat"):
        assert _looks_executable(rel), f"{rel!r} should be flagged as a script"
    for rel in (".claude/settings.json", ".claude/jit-context/tools/01-oss/00-index.tsv", ".claude/remember/identity.md",
                ".claude/no-suffix"):
        assert not _looks_executable(rel), f"{rel!r} is data and must not be flagged"


def test_the_command_detector_fires_on_every_shape_that_names_something_to_run():
    """The must-fire half of `test_no_tracked_settings_document_names_anything_to_execute` (#215)."""
    assert "statusLine" in _command_surface({"statusLine": {"type": "command", "command": "python3 x.py"}})
    assert _command_surface({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}})
    assert _command_surface({"someFutureKey": {"nested": [{"type": "command", "command": "curl example.invalid"}]}})
    assert _command_surface({"STATUSLINE": {"command": "x"}}), "key matching must be case-folded"
    for inert in ({}, {"enabledPlugins": {"oss@dpt-plugins": True}}, {"hooks": {}}, {"statusLine": {}},
                  {"permissions": {"allow": ["Bash(ls)"]}}, ["hooks"], "hooks"):
        assert not _command_surface(inert), f"{inert!r} names nothing to execute"


def test_the_plugin_enablement_detector_fires_on_a_settings_file_that_switches_one_on():
    """The must-fire half of `test_no_tracked_settings_document_enables_a_plugin`."""
    assert _enables_plugins({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _enables_plugins({"enabledPlugins": {}}), "an empty block enables nothing"
    assert not _enables_plugins({"statusLine": {"type": "command", "command": "x"}})
    assert not _enables_plugins(["enabledPlugins"]), "a non-mapping document enables nothing"


def test_the_documentation_matcher_refuses_a_key_hiding_inside_a_longer_word():
    """The must-fire half of `test_every_tracked_project_settings_key_is_described_to_contributors`."""
    assert _documented_in("env", "the `env` block sets environment variables for the session")
    assert not _documented_in("env", "run it in a clean environment before opening a pull request")
    assert not _documented_in("statusLine", "a statusLine, written in prose with no code span")
    assert _documented_in("statusLine", "the `statusLine` key names a command")


def test_the_empty_scan_refusal_fires_when_there_is_nothing_to_scan():
    """The must-fire half of the `assert documents` inside `_tracked_settings_documents`."""
    for tracked in ([], [".claude/jit-context/tools/01-oss/00-index.tsv"]):
        with pytest.raises(AssertionError, match="scanned no tracked JSON"):
            _tracked_settings_documents(tracked)


def test_a_settings_key_cannot_forge_a_line_in_this_file_s_own_failure_output():
    """A key in a settings document is text a contributor wrote, and it lands in a CI job log."""
    surface = _command_surface({"a\n::error::forged": {"type": "command", "command": "x"}})
    assert surface, "control: the detector must fire here, or the message below never renders"
    rendered = f"tracked settings name something to execute: {{'.claude/settings.json': {surface}}}"
    assert "\n" not in rendered, f"a settings key opened a line of its own; interpolate paths as a container: {rendered!r}"


# -- the guards themselves ----------------------------------------------------------------------


def test_the_repository_registers_no_hooks():
    """A tracked hook runs for everyone who clones, including a contributor with none of the maintainer's plugins (#2)."""
    for rel, data in _tracked_settings_documents():
        assert not _declares_hooks(data), f"{rel!r} registers hooks; if deliberate, CONTRIBUTING.md must name it as a requirement"


def test_no_hook_script_is_tracked_under_dot_claude():
    """The second route to the same barrier: a committed script for a hook to point at."""
    offenders = [rel for rel in _tracked_under_dot_claude() if _looks_executable(rel)]
    assert not offenders, f"executable-looking files tracked under .claude/: {offenders}; nothing there is code a contributor runs"


def test_the_contributor_baseline_is_written_down():
    """The measurement is only useful if the person who hits the directory can read it."""
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for token in (".claude/", "jit-context"):
        assert token in contributing, f"CONTRIBUTING.md must say what the tracked .claude/ directory is; {token!r} is missing (#2)"


def test_the_tracked_project_settings_carry_only_allowlisted_keys():
    """`.claude/settings.json` may carry only keys this file names, and today it names none (#215)."""
    assert PROJECT_SETTINGS in _tracked_under_dot_claude(), f"{PROJECT_SETTINGS} is not tracked; point this test at what a clone receives"
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    assert isinstance(data, dict), f"{PROJECT_SETTINGS} must be a JSON object, not {type(data).__name__}"
    extra = sorted(set(data) - ALLOWED_SETTINGS_KEYS)
    assert not extra, (f"{PROJECT_SETTINGS} carries key(s) {extra} this guard does not allow: a per-machine key goes in "
                       f"{PROJECT_SETTINGS_LOCAL}; one that must ship is allowlisted and described in CONTRIBUTING.md (#215)")


def test_no_tracked_settings_document_names_anything_to_execute():
    """No tracked JSON under `.claude/` may name a command, at any depth, under any key (#186)."""
    offenders = {rel: found for rel, data in _tracked_settings_documents() if (found := _command_surface(data))}
    assert not offenders, f"tracked settings name something to execute: {offenders}; personal automation goes in {PROJECT_SETTINGS_LOCAL}"


def test_no_tracked_settings_document_enables_a_plugin():
    """Nothing this repository tracks may switch a Claude Code plugin on (#2)."""
    offenders = [rel for rel, data in _tracked_settings_documents() if _enables_plugins(data)]
    assert not offenders, f"tracked settings enable plugins: {offenders}; maintainer plugins belong at the user level"


def test_the_untracked_local_settings_file_stays_untracked():
    """`.claude/settings.local.json` is where the personal half goes, so it must never be committed (#215)."""
    assert PROJECT_SETTINGS_LOCAL not in _tracked_under_dot_claude(), f"{PROJECT_SETTINGS_LOCAL} is tracked: one developer's setup for everyone"


def test_every_tracked_project_settings_key_is_described_to_contributors():
    """A key that ships to a cloner must be a key the cloner can read about (#215)."""
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    undocumented = sorted(key for key in data if not _documented_in(key, contributing))
    assert not undocumented, f"{PROJECT_SETTINGS} carries key(s) {undocumented} that CONTRIBUTING.md never names"
