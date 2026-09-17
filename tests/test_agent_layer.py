"""The tracked `.claude/` layer must stay inert for anyone without the maintainer's plugins (#2)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The layer these guards are about.
RULE_LAYER = ".claude/jit-context/tools/01-oss/supertool-required.md"
PROJECT_SETTINGS = ".claude/settings.json"
# The per-machine half, named here so it can be asserted *absent* from what is tracked.
PROJECT_SETTINGS_LOCAL = ".claude/settings.local.json"

# Top-level keys the tracked project settings may carry (#215).
ALLOWED_SETTINGS_KEYS = frozenset()

# Key names whose value is something Claude Code runs.
COMMAND_KEYS = {"hooks", "statusline", "command"}

# Suffixes a hook command could plausibly name.
EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl",
    ".exe", ".bat", ".cmd", ".ps1",
}


def _looks_executable(rel: str) -> bool:
    """Does a tracked path look like a script a hook could be pointed at?"""
    return Path(rel).suffix.lower() in EXECUTABLE_SUFFIXES


def _declares_hooks(data: object) -> bool:
    """Does a parsed settings document register hooks?"""
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
    """Does a parsed settings document switch a plugin on (#2)?"""
    return isinstance(data, dict) and bool(data.get("enabledPlugins"))


def _tracked_under_dot_claude() -> list[str]:
    """Every path git tracks under `.claude/`, or a loud skip when git cannot be asked."""
    if not (REPO / ".git").exists():
        pytest.skip(
            "no .git here, so the tracked file set cannot be enumerated -- "
            "the hook-registration guards for .claude/ went unchecked in this run"
        )
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", ".claude"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - environment
        pytest.skip(f"git ls-files failed ({exc}); the .claude/ guards went unchecked in this run")
    return [p for p in out.split("\0") if p]


def _tracked_content(rel: str) -> str:
    """The content git tracks at `rel` -- read from the index, never from the working copy."""
    out = subprocess.run(
        ["git", "show", f":{rel}"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return out.decode("utf-8")


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
    assert documents, (
        "scanned no tracked JSON under .claude/ -- expected at least the project settings. Every "
        "guard reading this set would otherwise pass over nothing, which is an all-clear nobody "
        "earned. If the project settings are deliberately no longer tracked, point this helper at "
        "whatever a clone now receives instead of letting it answer green about an empty set."
    )
    return documents


# -- the positive controls, first: a guard that cannot fire is not a guard ----------------------


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The scan set must be non-empty and must still contain the two files these guards are about."""
    tracked = _tracked_under_dot_claude()
    assert tracked, "scanned no tracked files under .claude/ -- the guards below would pass vacuously"
    assert PROJECT_SETTINGS in tracked, (
        f"{PROJECT_SETTINGS} is not tracked any more; this guard is checking a file set that has "
        "moved, and its all-clear is meaningless until it is pointed at the new one"
    )
    assert RULE_LAYER in tracked, (
        f"{RULE_LAYER} is not tracked any more. That may well be correct -- issue #2 argued for "
        "exactly that -- but update this file so the reasoning above stops describing a tree that "
        "no longer exists"
    )


def test_the_hook_detector_fires_on_a_settings_file_that_does_register_hooks():
    """The must-fire half of `test_the_repository_registers_no_hooks`."""
    registers = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}}
    assert _declares_hooks(registers), "the detector missed a settings file that plainly registers a hook"
    assert not _declares_hooks({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _declares_hooks({"hooks": {}}), "an empty hooks block registers nothing"
    assert not _declares_hooks(["hooks"]), "a non-mapping document registers nothing"


def test_the_script_detector_fires_on_the_shapes_a_hook_could_point_at():
    """The must-fire half of `test_no_hook_script_is_tracked_under_dot_claude`."""
    for rel in (".claude/hooks/pre.sh", ".claude/x.py", ".claude/x.PS1", ".claude/nested/a.bat"):
        assert _looks_executable(rel), f"{rel!r} should be flagged as a script"
    for rel in (".claude/settings.json", ".claude/jit-context/tools/01-oss/00-index.tsv",
                ".claude/remember/identity.md", ".claude/no-suffix"):
        assert not _looks_executable(rel), f"{rel!r} is data and must not be flagged"


def test_the_command_detector_fires_on_every_shape_that_names_something_to_run():
    """The must-fire half of `test_no_tracked_settings_document_names_anything_to_execute` (#215)."""
    status_line = {"statusLine": {"type": "command", "command": "python3 x.py"}}
    assert "statusLine" in _command_surface(status_line), "a statusLine command is executable surface"
    hooks = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}}
    assert _command_surface(hooks), "a hooks block is executable surface"
    nested = {"someFutureKey": {"nested": [{"type": "command", "command": "curl example.invalid"}]}}
    assert _command_surface(nested), "the value net must catch a command under a key no list names"
    assert _command_surface({"STATUSLINE": {"command": "x"}}), "key matching must be case-folded"
    for inert in ({}, {"enabledPlugins": {"oss@dpt-plugins": True}}, {"hooks": {}},
                  {"statusLine": {}}, {"permissions": {"allow": ["Bash(ls)"]}}, ["hooks"], "hooks"):
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
    with pytest.raises(AssertionError, match="scanned no tracked JSON"):
        _tracked_settings_documents([])
    with pytest.raises(AssertionError, match="scanned no tracked JSON"):
        _tracked_settings_documents([".claude/jit-context/tools/01-oss/00-index.tsv"])


def test_a_settings_key_cannot_forge_a_line_in_this_file_s_own_failure_output():
    """A key in a settings document is text a contributor wrote, and it lands in a CI job log."""
    forged = {"a\n::error::forged": {"type": "command", "command": "x"}}
    surface = _command_surface(forged)
    assert surface, "control: the detector must fire here, or the message below never renders"
    rendered = f"tracked settings name something to execute: {{'.claude/settings.json': {surface}}}"
    assert "\n" not in rendered, (
        "a settings key opened a line of its own in this file's failure output; interpolate the "
        f"paths as a container so repr escapes them, not as joined text. Rendered: {rendered!r}"
    )


# -- the guards themselves ----------------------------------------------------------------------


def test_the_repository_registers_no_hooks():
    """Tracked settings must not register a `PreToolUse` (or any other) hook (#2)."""
    checked = 0
    for rel in _tracked_under_dot_claude():
        if not rel.endswith(".json"):
            continue
        checked += 1
        try:
            data = json.loads(_tracked_content(rel))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{rel!r}: tracked settings must be readable JSON ({exc})")
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.fail(f"{rel!r}: git lists it but could not produce its content ({exc})")
        assert not _declares_hooks(data), (
            f"{rel!r} registers hooks. A tracked hook runs for everyone who clones this repository, "
            "including a contributor with none of the maintainer's plugins installed -- which is "
            "the barrier issue #2 reported and measurement found absent. If this is deliberate, it "
            "needs to be documented in CONTRIBUTING.md as a hard requirement to contribute."
        )
    assert checked, "scanned no tracked JSON under .claude/ -- expected at least the project settings"


def test_no_hook_script_is_tracked_under_dot_claude():
    """The second route to the same barrier: a committed script for a hook to point at."""
    tracked = _tracked_under_dot_claude()
    # The must-fire control lives in a sibling test, which is not enough.
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    offenders = [rel for rel in tracked if _looks_executable(rel)]
    assert not offenders, (
        f"executable-looking files tracked under .claude/: {offenders}. Nothing under .claude/ is "
        "code a contributor runs; if that changed, say so in CONTRIBUTING.md first."
    )


def test_the_contributor_baseline_is_written_down():
    """The measurement is only useful if the person who hits the directory can read it."""
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    # Two tokens rather than one.
    for token in (".claude/", "jit-context"):
        assert token in contributing, (
            f"CONTRIBUTING.md must tell a contributor what the tracked .claude/ directory is and "
            f"that none of it is required to contribute -- {token!r} is missing (issue #2)"
        )


def test_the_tracked_project_settings_carry_only_allowlisted_keys():
    """`.claude/settings.json` may carry only keys this file names, and today it names none (#215)."""
    tracked = _tracked_under_dot_claude()
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    assert PROJECT_SETTINGS in tracked, (
        f"{PROJECT_SETTINGS} is not tracked any more. That is a stronger state than this guard asks "
        "for, and it leaves the assertion below with nothing to read -- point this test at whatever "
        "a clone now receives rather than deleting it, or the class goes unwatched again."
    )
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    assert isinstance(data, dict), f"{PROJECT_SETTINGS} must be a JSON object, not {type(data).__name__}"
    extra = sorted(set(data) - ALLOWED_SETTINGS_KEYS)
    assert not extra, (
        f"{PROJECT_SETTINGS} carries key(s) {extra} that this guard does not allow. A tracked "
        "project settings key configures Claude Code for everyone who clones this repository, "
        "including a contributor with none of the maintainer's plugins installed. If it belongs to "
        "one machine it goes in .claude/settings.local.json, which .gitignore excludes for exactly "
        "that reason. If it really must ship, add it to ALLOWED_SETTINGS_KEYS and say what it does "
        "in CONTRIBUTING.md's `.claude/` section in the same change (#215)."
    )


def test_no_tracked_settings_document_names_anything_to_execute():
    """No tracked JSON under `.claude/` may name a command, at any depth, under any key (#186)."""
    offenders = {}
    for rel, data in _tracked_settings_documents():
        found = _command_surface(data)
        if found:
            offenders[rel] = found
    assert not offenders, (
        f"tracked settings name something to execute: {offenders}. A command in a tracked settings "
        "document runs on the machine of everyone who clones this repository -- the barrier issue "
        "#2 reported and measurement found absent, made real. Personal automation belongs in "
        ".claude/settings.local.json, which .gitignore excludes."
    )


def test_no_tracked_settings_document_enables_a_plugin():
    """Nothing this repository tracks may switch a Claude Code plugin on (#2)."""
    offenders = [rel for rel, data in _tracked_settings_documents() if _enables_plugins(data)]
    assert not offenders, (
        f"tracked settings enable plugins: {offenders}. Maintainer plugins belong in the user's own "
        "settings or in .claude/settings.local.json; this repository's own maintenance loop reads "
        "them from the user level, so tracking them buys nothing and costs every cloner."
    )


def test_the_untracked_local_settings_file_stays_untracked():
    """`.claude/settings.local.json` is where the personal half goes, so it must never be committed."""
    tracked = _tracked_under_dot_claude()
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    assert PROJECT_SETTINGS_LOCAL not in tracked, (
        f"{PROJECT_SETTINGS_LOCAL} is tracked. That file is per-machine by Claude Code's own "
        "convention, and it is where this repository's maintainer keeps the statusline and plugin "
        "enablement that #215 moved out of the tracked settings. Committing it runs one developer's "
        "setup for everyone who clones."
    )


def test_every_tracked_project_settings_key_is_described_to_contributors():
    """A key that ships to a cloner must be a key the cloner can read about (#215)."""
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    undocumented = sorted(key for key in data if not _documented_in(key, contributing))
    assert not undocumented, (
        f"{PROJECT_SETTINGS} carries key(s) {undocumented} that CONTRIBUTING.md never names. A "
        "contributor who opens the tracked .claude/ directory has to be able to find out what it "
        "does to their machine; a key nobody documented is one they can only find by reading JSON."
    )
