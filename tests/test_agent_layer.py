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
HOOKS = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}}
PLUGINS = {"enabledPlugins": {"oss@dpt-plugins": True}}


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
        if rel.endswith(".json"):
            try:
                documents.append((rel, json.loads(_tracked_content(rel))))
            except (json.JSONDecodeError, OSError, subprocess.CalledProcessError) as exc:
                pytest.fail(f"{rel!r}: tracked settings must be readable JSON git can produce ({exc})")
    assert documents, "scanned no tracked JSON under .claude/ -- expected at least the project settings"
    return documents


# -- the positive controls, first: a guard that cannot fire is not a guard ----------------------


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The scan set must still contain the two files these guards are about, and never the personal half (#215)."""
    tracked = _tracked_under_dot_claude()
    assert PROJECT_SETTINGS in tracked, f"{PROJECT_SETTINGS} is not tracked any more; point this guard at the new file set"
    assert RULE_LAYER in tracked, f"{RULE_LAYER} is not tracked any more (#2 argued for that); update this file to match"
    assert PROJECT_SETTINGS_LOCAL not in tracked, f"{PROJECT_SETTINGS_LOCAL} is tracked: one developer's setup for everyone"


@pytest.mark.parametrize(("detector", "fires", "inert"), [
    (_declares_hooks, [HOOKS], [PLUGINS, {"hooks": {}}, ["hooks"]]),
    (_looks_executable, [".claude/hooks/pre.sh", ".claude/x.py", ".claude/x.PS1", ".claude/nested/a.bat"],
     [".claude/settings.json", ".claude/jit-context/tools/01-oss/00-index.tsv", ".claude/remember/identity.md", ".claude/no-suffix"]),
    (_command_surface, [{"statusLine": {"type": "command", "command": "python3 x.py"}}, HOOKS, {"STATUSLINE": {"command": "x"}},
                        {"someFutureKey": {"nested": [{"type": "command", "command": "curl example.invalid"}]}}],
     [{}, PLUGINS, {"hooks": {}}, {"statusLine": {}}, {"permissions": {"allow": ["Bash(ls)"]}}, ["hooks"], "hooks"]),
    (_enables_plugins, [PLUGINS], [{"enabledPlugins": {}}, {"statusLine": {"type": "command", "command": "x"}}, ["enabledPlugins"]]),
    (lambda case: _documented_in(*case), [("env", "the `env` block sets variables"), ("statusLine", "the `statusLine` key names a command")],
     [("env", "run it in a clean environment first"), ("statusLine", "a statusLine, written in prose with no code span")]),
], ids=["hooks", "scripts", "commands", "plugins", "documented-as-a-key"])
def test_each_detector_fires_on_its_shapes_and_stays_quiet_on_the_inert_ones(detector, fires, inert):
    """The must-fire halves of the guards below, one detector per row (#2, #215)."""
    for case in fires:
        assert detector(case), f"{case!r} should be flagged"
    for case in inert:
        assert not detector(case), f"{case!r} names nothing"
    assert "statusLine" in _command_surface({"statusLine": {"type": "command", "command": "python3 x.py"}})


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


@pytest.mark.parametrize(("detector", "what"), [
    (_declares_hooks, "registers hooks; if deliberate, CONTRIBUTING.md must name it as a requirement"),
    (_command_surface, f"names something to execute; personal automation goes in {PROJECT_SETTINGS_LOCAL}"),
    (_enables_plugins, "enables a plugin; maintainer plugins belong at the user level"),
], ids=["no-hooks", "nothing-to-execute", "no-plugin-enabled"])
def test_no_tracked_settings_document_names_anything_to_run_for_a_cloner(detector, what):
    """A tracked hook, command or plugin runs for everyone who clones, plugins or not (#2, #186)."""
    offenders = {rel: found for rel, data in _tracked_settings_documents() if (found := detector(data))}
    assert not offenders, f"{sorted(offenders)} {what}: {offenders}"


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
    """`.claude/settings.json` may carry only keys this file names and CONTRIBUTING.md describes (#215)."""
    assert PROJECT_SETTINGS in _tracked_under_dot_claude(), f"{PROJECT_SETTINGS} is not tracked; point this test at what a clone receives"
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    assert isinstance(data, dict), f"{PROJECT_SETTINGS} must be a JSON object, not {type(data).__name__}"
    extra = sorted(set(data) - ALLOWED_SETTINGS_KEYS)
    assert not extra, f"{PROJECT_SETTINGS} carries key(s) {extra} this guard does not allow; a per-machine key goes in {PROJECT_SETTINGS_LOCAL}"
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    undocumented = sorted(key for key in data if not _documented_in(key, contributing))
    assert not undocumented, f"{PROJECT_SETTINGS} carries key(s) {undocumented} that CONTRIBUTING.md never names"
