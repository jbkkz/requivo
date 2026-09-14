"""The tracked `.claude/` layer must stay inert for anyone without the maintainer's plugins.

Issue #2 reported that `.claude/jit-context/tools/01-oss/supertool-required.md` -- which declares
`tool: Read|Edit|Write|Glob|Grep`, `match: ~.*`, `mode: block` -- blocks every native file operation
for a contributor who clones this repository. Measured, it does not, and the reason is worth pinning
rather than re-deriving:

A jit-context rule is **data**. The only thing that reads it is a `PreToolUse` hook, and that hook is
registered by the `claude-jit-context` plugin, in that plugin's own `hooks/hooks.json`, as
`bash ${CLAUDE_PLUGIN_ROOT}/scripts/pre-tool-hook.sh`. A Claude Code project acquires hooks from
exactly four places: the user's own settings, the project's `.claude/settings.json`, the project's
`.claude/settings.local.json`, and an installed plugin. This repository ships nothing into the first
three. So without the plugin there is no hook, nothing reads the layer, and every native Read, Edit,
Write, Glob and Grep works normally.

`settings.local.json` is the one of those a contributor could commit by accident -- it is where a
personal hook would be written, and `.gitignore` now excludes it. It was *not* excluded when this
file was first written; it merely looked excluded, because the maintainer's machine carries a global
`~/.config/git/ignore` entry for it that no contributor has. The guard below scans whatever is
tracked, so it catches such a commit either way, but the exclusion stops the accident happening.

That inertness is a property of **this repository**, not of the plugin, and it is one commit away
from being false. A `hooks` block added to the tracked `.claude/settings.json`, or a hook script
committed under `.claude/`, would make the barrier #2 described real for every contributor -- and it
would do so silently, because the maintainer, who has the plugin, sees identical behaviour either
way. Nothing in CI would notice. This file is the guard.

And it was already false while saying so (#215). The guard read `hooks` and nothing else, so when a
`statusLine` block was added to the tracked settings in #186 -- `python3 "$CLAUDE_PROJECT_DIR"/.oss/statusline.py`,
1,922 lines of maintainer tooling whose refresh path forks off forge calls carrying whatever
credentials the machine holds -- it ran for everyone who cloned, once per assistant message, and the
suite stayed green. The script it named sits *outside* `.claude/`, so the tracked-script scan could
not see it either. Naming one key of a document whose vocabulary this repository does not own is a
guard against one instance, never against the class.

So the tracked project settings now carry nothing at all, and three guards hold that from three
directions, each with its own must-fire control:
`test_the_tracked_project_settings_carry_only_allowlisted_keys` refuses any top-level key,
`test_no_tracked_settings_document_names_anything_to_execute` refuses a command at any depth under
any key name, and `test_no_tracked_settings_document_enables_a_plugin` refuses an `enabledPlugins`
block -- because a plugin registers hooks from its own manifest, so committing the switch commits
the hooks, which is the barrier #2 reported at one remove. The maintainer's own copy of both lives
in `.claude/settings.local.json`, which `.gitignore` excludes and
`test_the_untracked_local_settings_file_stays_untracked` keeps out of the index.

Following the rule `test_boundaries.py` sets out: **an empty scan is an error, not an answer.**
`git ls-files` over a directory that has been renamed or removed prints nothing and exits 0, and
`assert not []` is an all-clear nobody earned. `_tracked_settings_documents` carries that refusal for
every guard that walks the JSON, so a settings file leaving the index cannot turn them all green --
and, because the refusal is itself an absence-assertion, it has a control of its own in
`test_the_empty_scan_refusal_fires_when_there_is_nothing_to_scan`. The rule applies to the function
that enforces the rule.

Two more controls were added by the review of this change, and both name a real trap rather than a
hypothetical one. `test_the_documentation_matcher_refuses_a_key_hiding_inside_a_longer_word`: `env`
is a Claude Code settings key and a substring of `environment`, so a bare `in` would have reported
an undocumented `env` block as documented -- the exact mistake the baseline guard below avoids by
demanding two tokens. And `test_a_settings_key_cannot_forge_a_line_in_this_file_s_own_failure_output`:
the failure messages here interpolate dotted paths built from settings *keys*, which are text a
contributor wrote, into output a CI log and this project's triage agents read. It is safe today only
because the paths go in as a container and `repr` escapes the newline; that was nobody's decision
until this test made it one.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The layer these guards are about. If it moves, the scan below finds nothing and says so rather
# than passing green over an empty set.
RULE_LAYER = ".claude/jit-context/tools/01-oss/supertool-required.md"
PROJECT_SETTINGS = ".claude/settings.json"
# The per-machine half, named here so it can be asserted *absent* from what is tracked. An exclusion
# is not a guarantee -- a forced stage overrides it, and this very path was un-excluded here for a
# release while merely looking excluded, because the maintainer's machine carries a global ignore
# entry for it that no contributor has.
PROJECT_SETTINGS_LOCAL = ".claude/settings.local.json"

# Top-level keys the tracked project settings may carry. Empty on purpose, and the emptiness is the
# point: nothing this repository commits should configure a contributor's Claude Code at all, so
# every key is a deliberate, reviewed addition rather than a line in a diff nobody reads. Widening
# this set means editing CONTRIBUTING.md's `.claude/` section in the same change -- that paragraph is
# what tells a cloner what the tracked directory does to their machine, and it went stale silently
# once already (#215).
ALLOWED_SETTINGS_KEYS = frozenset()

# Key names whose value is something Claude Code runs. `hooks` and `statusLine` are the two that
# exist today and `command` is the leaf both spell it with, so a third key nobody has written yet is
# caught the moment it carries one. Case-folded: settings keys are camelCase by convention, and a
# convention is not a guarantee about a file a person hand-edits.
COMMAND_KEYS = {"hooks", "statusline", "command"}

# Suffixes a hook command could plausibly name. A hook is a shell command, so this is a heuristic --
# but a committed script is the only way one reaches a contributor from this repository, and every
# tracked file under `.claude/` today is data (`.md`, `.tsv`, `.json`). The Windows spellings are
# here even though every CI job in this repo runs on ubuntu-latest: a contributor's pull request is
# what this guard reads, and their machine is not this matrix.
EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl",
    ".exe", ".bat", ".cmd", ".ps1",
}


def _looks_executable(rel: str) -> bool:
    """Does a tracked path look like a script a hook could be pointed at?

    Named, like `_declares_hooks`, so the control below can prove it fires. Case-folded because a
    `.SH` committed from a case-insensitive filesystem is the same file.
    """
    return Path(rel).suffix.lower() in EXECUTABLE_SUFFIXES


def _declares_hooks(data: object) -> bool:
    """Does a parsed settings document register hooks?

    Kept as a named function so the positive control below can prove it fires. A guard whose only
    exercise is the clean case reports coverage it does not have.
    """
    return isinstance(data, dict) and bool(data.get("hooks"))


def _command_surface(data: object, path: str = "") -> list[str]:
    """Every place in a settings document that names something for Claude Code to run.

    Two nets, because either one alone is a guess about a vocabulary this repository does not own.
    The *key* net catches `hooks`, `statusLine` and a bare `command` wherever they sit; the *value*
    net catches the `{"type": "command", ...}` shape, under whatever key a future Claude Code spells
    it with. `_declares_hooks` is the one-key form this replaces the general case of, and it stays
    because #2's answer is quoted against it by name.

    Returns dotted paths rather than a bool so a failure names the line and not just the file. An
    empty value is not surface -- `{"hooks": {}}` registers nothing, which is the reading
    `_declares_hooks` already takes, and disagreeing here would make one of the two wrong.
    """
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
    """Is `key` named in `prose` *as a key*, rather than merely occurring inside some word?

    A bare `key in prose` is the mistake `test_the_contributor_baseline_is_written_down` warns about
    a few functions down, and it is not hypothetical here: `env` is a real Claude Code settings key
    and a substring of `environment`, which CONTRIBUTING.md already uses, so the naive check would
    call a wholly undocumented `env` block documented. The convention relied on instead is the one
    CONTRIBUTING.md already follows -- a settings key is written inside a backtick span -- which is
    narrower than a word boundary and is what a reader scans for anyway.
    """
    return f"`{key}`" in prose


def _enables_plugins(data: object) -> bool:
    """Does a parsed settings document switch a plugin on?

    Not executable surface in itself, and testing it as if it were would miss the point. A plugin
    registers its own hooks from its own manifest, so a tracked enablement is the barrier #2
    reported at one remove: this repository ships no hook, and the line switches on four things that
    do. The marketplace those names resolved through lived only in the maintainer's *user* settings,
    so a cloner got dangling entries rather than running code -- but that is a fact about the
    contributor's machine, not a property this repository can promise (#215).
    """
    return isinstance(data, dict) and bool(data.get("enabledPlugins"))


def _tracked_under_dot_claude() -> list[str]:
    """Every path git tracks under `.claude/`, or a loud skip when git cannot be asked.

    `tracked` is the load-bearing word: `.claude/settings.local.json` and
    `.claude/jit-context/.discovery/` exist on the maintainer's disk and never reach a contributor,
    so a filesystem walk would answer a different question than the one asked here.
    """
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
    """The content git tracks at `rel` -- read from the index, never from the working copy.

    The question this file asks is what a *clone* gets, and a working-tree read answers a different
    one. CONTRIBUTING.md tells a contributor they may delete `.claude/` in their working copy; doing
    that leaves the path still listed by `git ls-files` while `Path.read_text` raises
    `FileNotFoundError`, so the documented advice and the guard shipped in the same commit collided
    and turned a local `pytest tests/` into an error. Reading the index is not a workaround for that
    -- it is the reading that matches the claim, and it happens to be immune to the working copy.
    """
    out = subprocess.run(
        ["git", "show", f":{rel}"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return out.decode("utf-8")


def _tracked_settings_documents(tracked=None) -> list:
    """Every tracked JSON document under `.claude/`, parsed -- and a failure when there are none.

    The empty-scan refusal lives in here rather than in each caller because every caller needs it,
    and a helper is the one place it cannot be forgotten on the next guard somebody adds. It is an
    `assert`, so it still fires inside the calling test's own frame under `-k` or test sharding,
    which is the reason the sibling guards keep their own copy of the same check.

    `tracked` exists only so that refusal can have a must-fire control of its own
    (`test_the_empty_scan_refusal_fires_when_there_is_nothing_to_scan`). An empty-scan assert is an
    absence-assertion like every other guard in this file, and this file's own rule is that one
    without a control passes just as happily broken as clean -- so the helper that carries the rule
    for everybody else has to obey it too. Production callers pass nothing and get the real listing.
    """
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
    """The scan set must be non-empty and must still contain the two files these guards are about.

    Without this, renaming `.claude/` or deleting the rule layer turns every assertion below into a
    loop over nothing, which passes.
    """
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
    """The must-fire half of `test_the_repository_registers_no_hooks`.

    That test asserts an absence, and an absence-assertion passes just as happily when the detector
    is broken as when the tree is clean. This is what tells those two apart.
    """
    registers = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}}
    assert _declares_hooks(registers), "the detector missed a settings file that plainly registers a hook"
    assert not _declares_hooks({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _declares_hooks({"hooks": {}}), "an empty hooks block registers nothing"
    assert not _declares_hooks(["hooks"]), "a non-mapping document registers nothing"


def test_the_script_detector_fires_on_the_shapes_a_hook_could_point_at():
    """The must-fire half of `test_no_hook_script_is_tracked_under_dot_claude`.

    Every tracked file under `.claude/` today is data, so that guard's clean run exercises none of
    this classifier. Without a control it is a set literal nothing has ever read in anger.
    """
    for rel in (".claude/hooks/pre.sh", ".claude/x.py", ".claude/x.PS1", ".claude/nested/a.bat"):
        assert _looks_executable(rel), f"{rel!r} should be flagged as a script"
    for rel in (".claude/settings.json", ".claude/jit-context/tools/01-oss/00-index.tsv",
                ".claude/remember/identity.md", ".claude/no-suffix"):
        assert not _looks_executable(rel), f"{rel!r} is data and must not be flagged"


def test_the_command_detector_fires_on_every_shape_that_names_something_to_run():
    """The must-fire half of `test_no_tracked_settings_document_names_anything_to_execute`.

    The instance that got through was a `statusLine` -- but a control that only replays it leaves
    the class unguarded, which is the whole of #215. The nested case earns the second net: it names
    no key from `COMMAND_KEYS` at any level and is caught by the `{"type": "command"}` shape alone."""
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
    """The must-fire half of `test_no_tracked_settings_document_enables_a_plugin`.

    Same asymmetry as the hook control above: the guard asserts an absence, and an absence passes
    just as happily when the detector is broken as when the tree is clean.
    """
    assert _enables_plugins({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _enables_plugins({"enabledPlugins": {}}), "an empty block enables nothing"
    assert not _enables_plugins({"statusLine": {"type": "command", "command": "x"}})
    assert not _enables_plugins(["enabledPlugins"]), "a non-mapping document enables nothing"


def test_the_documentation_matcher_refuses_a_key_hiding_inside_a_longer_word():
    """The must-fire half of `test_every_tracked_project_settings_key_is_described_to_contributors`.
    `env` is the real case: a Claude Code settings key and a substring of `environment`, a word
    CONTRIBUTING.md already uses. A bare `key in prose` check would call an undocumented `env` block
    documented -- the same shape the sibling guard rejected by requiring a full token."""
    assert _documented_in("env", "the `env` block sets environment variables for the session")
    assert not _documented_in("env", "run it in a clean environment before opening a pull request")
    assert not _documented_in("statusLine", "a statusLine, written in prose with no code span")
    assert _documented_in("statusLine", "the `statusLine` key names a command")


def test_the_empty_scan_refusal_fires_when_there_is_nothing_to_scan():
    """The must-fire half of the `assert documents` inside `_tracked_settings_documents`. That helper
    carries the empty-scan rule for every guard below it -- an absence-assertion passes just as
    happily broken as clean. Removing the assert would turn every guard that reads it into a loop
    over nothing -- green, having looked at no file at all."""
    with pytest.raises(AssertionError, match="scanned no tracked JSON"):
        _tracked_settings_documents([])
    with pytest.raises(AssertionError, match="scanned no tracked JSON"):
        _tracked_settings_documents([".claude/jit-context/tools/01-oss/00-index.tsv"])


def test_a_settings_key_cannot_forge_a_line_in_this_file_s_own_failure_output():
    """A key in a settings document is text a contributor wrote, and it lands in a CI job log. The
    failure message interpolates the offending dotted paths built from those keys -- a key carrying a
    newline could open a line at column 0 of output a triage agent then reads (invariant 14's class).
    It happens not to, only because the paths go in as a *container* and `repr` escapes the newline."""
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
    """Tracked settings must not register a `PreToolUse` (or any other) hook.

    This is the whole of #2's answer. The `mode: block` rule under `.claude/jit-context/` is inert
    for a contributor precisely because nothing this repository ships can invoke it. Register a hook
    here and that stops being true on the next clone.
    """
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
    """The second route to the same barrier: a committed script for a hook to point at.

    Everything tracked under `.claude/` today is data a plugin may read. Nothing there is code this
    repository asks anyone to run.
    """
    tracked = _tracked_under_dot_claude()
    # The must-fire control lives in a sibling test, which is not enough: run this function alone,
    # under `-k`, or under any test sharding, and an empty scan would report a clean tree it never
    # looked at. The guard belongs in the same function as the assertion it protects.
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    offenders = [rel for rel in tracked if _looks_executable(rel)]
    assert not offenders, (
        f"executable-looking files tracked under .claude/: {offenders}. Nothing under .claude/ is "
        "code a contributor runs; if that changed, say so in CONTRIBUTING.md first."
    )


def test_the_contributor_baseline_is_written_down():
    """The measurement is only useful if the person who hits the directory can read it.

    A contributor who opens `.claude/` finds a rule declaring `mode: block` over every file
    operation and has no way to know it cannot reach them. CONTRIBUTING.md says so; this keeps the
    two from drifting apart while the guards above stay green.
    """
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    # Two tokens rather than one. `.claude/` alone would be satisfied by any incidental later
    # mention anywhere in the file; requiring the mechanism word as well means the assertion is
    # about the explanation existing, not about the string appearing. Deliberately not pinned to a
    # sentence: prose drifts, and a test that fails on a reworded paragraph gets deleted.
    for token in (".claude/", "jit-context"):
        assert token in contributing, (
            f"CONTRIBUTING.md must tell a contributor what the tracked .claude/ directory is and "
            f"that none of it is required to contribute -- {token!r} is missing (issue #2)"
        )


def test_the_tracked_project_settings_carry_only_allowlisted_keys():
    """`.claude/settings.json` may carry only keys this file names, and today it names none. This is the
    class guard the two detectors below cannot be: they know what a command and a plugin enablement
    look like *today*, and Claude Code's settings vocabulary is not this repository's to freeze -- a
    key nobody here has heard of fails this one, the right default for a file that configures every
    contributor's editor before they have read it (#215)."""
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
    """No tracked JSON under `.claude/` may name a command, at any depth, under any key. This is the
    guard `test_the_repository_registers_no_hooks` should have been. That one reads `hooks` and only
    `hooks`, so #186's `statusLine` -- pointing at an 88KB maintainer script *outside* `.claude/`,
    beyond the tracked-script scan too -- reached every contributor once per assistant message with
    the suite green (#215)."""
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
    """Nothing this repository tracks may switch a Claude Code plugin on. A plugin registers hooks from
    its own manifest, so committing the switch commits the hooks -- the barrier #2 reported, one
    remove further out than the guards above look. It also decides for a contributor which
    third-party marketplace entries their editor turns on, which is not a decision a checkout gets to
    make (#215)."""
    offenders = [rel for rel, data in _tracked_settings_documents() if _enables_plugins(data)]
    assert not offenders, (
        f"tracked settings enable plugins: {offenders}. Maintainer plugins belong in the user's own "
        "settings or in .claude/settings.local.json; this repository's own maintenance loop reads "
        "them from the user level, so tracking them buys nothing and costs every cloner."
    )


def test_the_untracked_local_settings_file_stays_untracked():
    """`.claude/settings.local.json` is where the personal half goes, so it must never be committed.
    `.gitignore` excludes it and an exclusion is not a guarantee: it can be overridden on the command
    line, and this very path was un-excluded here for a release while merely *looking* excluded (the
    maintainer's machine carries a global ignore entry no contributor has) -- this is the assertion
    that does not depend on the exclusion being right."""
    tracked = _tracked_under_dot_claude()
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    assert PROJECT_SETTINGS_LOCAL not in tracked, (
        f"{PROJECT_SETTINGS_LOCAL} is tracked. That file is per-machine by Claude Code's own "
        "convention, and it is where this repository's maintainer keeps the statusline and plugin "
        "enablement that #215 moved out of the tracked settings. Committing it runs one developer's "
        "setup for everyone who clones."
    )


def test_every_tracked_project_settings_key_is_described_to_contributors():
    """A key that ships to a cloner must be a key the cloner can read about. Vacuous while the tracked
    settings are empty -- but the set it iterates is the same set
    `test_the_tracked_project_settings_carry_only_allowlisted_keys` pins to empty, so the two cannot
    both be quietly wrong. It exists because CONTRIBUTING.md's own description went stale exactly
    that way, once, while a `statusLine` sat undocumented beside it (#215)."""
    data = json.loads(_tracked_content(PROJECT_SETTINGS))
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    undocumented = sorted(key for key in data if not _documented_in(key, contributing))
    assert not undocumented, (
        f"{PROJECT_SETTINGS} carries key(s) {undocumented} that CONTRIBUTING.md never names. A "
        "contributor who opens the tracked .claude/ directory has to be able to find out what it "
        "does to their machine; a key nobody documented is one they can only find by reading JSON."
    )
