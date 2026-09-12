"""Detect drift between the CLI `requivo doctor --json` reports and the version this plugin was
built and tested against, for the shared preflight in `../REASONING.md` (#251).

Why this is not simply "the plugin shells out to this script"
---------------------------------------------------------------
Every skill in this plugin declares `allowed-tools: Bash(requivo:*), Read` -- a narrow grant that
lets a skill run `requivo …` without a permission prompt and nothing else. Making this script part
of the *runtime* preflight would mean widening that grant, on every skill, to also cover shelling
out to a second program -- a real security/UX decision (every extra Bash prefix a skill can run
without asking is more surface, and an unverified change to it could just as easily turn "warn and
continue" into "prompt the user before every single skill invocation") and a bigger one than this
low-risk, low-effort issue asked for. So the runtime check is prose in `REASONING.md`: Claude reads
the doctor JSON it already fetched during the preflight (already permitted) and this plugin's own
`.claude-plugin/plugin.json` via the `Read` tool (already permitted, and already how every skill
reads `REASONING.md` itself) and does the three-way comparison below by reasoning over two numbers
it already has -- no new permission, no new failure mode if a script does not exist on someone's
PATH.

This module is the tested, unambiguous specification of that comparison (so REASONING.md's prose
has a ground truth to be checked against in `tests/test_plugin_version_skew.py`, including the
could-not-read arm, which is the part that is easy to get wrong in prose and hard to verify by
reading English), and a standalone diagnostic: `python3 plugins/claude-code/scripts/version_skew.py`
runs `requivo doctor --json` itself and prints the same verdict, for a human or a CI job to run
outside a Claude Code session.

Three states, never two (#251's own trap)
------------------------------------------
`IN_STEP` (the CLI is at or ahead of the version this plugin was tested against -- say nothing,
this is the expected case and flagging it every run would be noise), `BEHIND` (the CLI is older --
warn and continue; refusing was explicitly ruled out in the issue, since the plugin is keyless and
most verbs still work across a minor), and `COULD_NOT_LOOK` (the doctor call failed outright, its
output was not readable JSON, it carried no `requivo_version`, or the plugin's own manifest could
not be read). The third state must never collapse into the first -- a preflight that reports "in
step" because ITS OWN diagnostic failed is the identical silent-absence class this project's CLAUDE.md
names as the recurring defect across this codebase (see invariant 15's listing argument, one layer
up from a session listing).

No stamped literal
-------------------
The version this plugin was "tested against" is read live from `.claude-plugin/plugin.json`'s own
`version` field rather than duplicated as a second number in REASONING.md's prose. A copy is a
second site that can drift the day the manifest is bumped and the copy is forgotten -- exactly what
`tests/test_version_sites.py` exists to catch for this project's other version declarations. Reading
the manifest directly cannot drift by construction, so there is nothing here for that guard to be
extended to catch.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"

IN_STEP = 0
BEHIND = 1
COULD_NOT_LOOK = 3


class SkewResult:
    """`state` is one of the three module constants above; `message` is what to relay (or, for
    `IN_STEP`, what a human running this as a standalone diagnostic sees -- REASONING.md instructs
    Claude to stay silent on `IN_STEP` rather than print this to the user on every run)."""

    __slots__ = ("state", "message")

    def __init__(self, state: int, message: str) -> None:
        self.state = state
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"SkewResult(state={self.state}, message={self.message!r})"


# The first component must be a real digit run -- "1", "2026" -- or this is not a version at all
# ("unreleased", "unknown", "dev"), and treating it as one is exactly the collapse this module's
# own docstring forbids (found in self-review, see the two tests this fixes in
# tests/test_plugin_version_skew.py). Everything AFTER the first component stays tolerant of a
# non-numeric trailing chunk (`.dev0`, `-rc1`), which is a real version parsing as far as it can
# rather than raising over a suffix nobody asked this advisory check to understand.
_VERSION_SHAPE_RE = re.compile(r"^\d+")


def _looks_like_a_version(value: str) -> bool:
    return bool(_VERSION_SHAPE_RE.match(value.strip()))


def _parse_version(version: str):
    """A dotted version string as a tuple of ints, tolerant of a non-numeric trailing component
    (`.dev0`, `-rc1`) -- which parses as 0 rather than raising, so an unusual version string
    degrades to 'compare what can be compared' instead of crashing an advisory preflight check.

    Callers must check `_looks_like_a_version` first (`check()` and `tested_against_version` both
    do): this function alone cannot refuse "unreleased" -- it has no digit anywhere, so every chunk
    parses to 0 and the result is indistinguishable from a real, very old version."""
    parts = []
    for chunk in re.split(r"[.\-+]", version.strip()):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts) if parts else (0,)


def tested_against_version(manifest_path: Path = MANIFEST) -> str:
    """The plugin's own declared version -- the release this plugin build was tested against.

    Raises (never returns a fake value) on anything unreadable: an absent file (OSError), invalid
    JSON (json.JSONDecodeError, itself a ValueError), a non-object payload, or a missing/blank
    `version` key (ValueError). The caller (`check`, below) is what turns that into the
    `COULD_NOT_LOOK` state; this function's job is only to refuse quietly wrong data.
    """
    with manifest_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path} is not a JSON object")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{manifest_path} declares no usable `version`")
    if not _looks_like_a_version(version):
        raise ValueError(f"{manifest_path} declares a `version` that is not version-shaped: {version!r}")
    return version


def compare(cli_version: str, plugin_version: str) -> SkewResult:
    """The two-number decision, once both are known to be readable, version-shaped strings.

    Tuples are padded to equal length before comparing -- Python compares tuples lexically, so a
    true prefix reads as smaller than what it is a prefix of ("1.3" == (1, 3) < (1, 3, 0) ==
    "1.3.0"), which would report BEHIND for two strings naming the same release (found in
    self-review; see the two version_skew tests it fixes)."""
    cli_t, plugin_t = _parse_version(cli_version), _parse_version(plugin_version)
    width = max(len(cli_t), len(plugin_t))
    cli_t = cli_t + (0,) * (width - len(cli_t))
    plugin_t = plugin_t + (0,) * (width - len(plugin_t))
    if cli_t >= plugin_t:
        return SkewResult(
            IN_STEP,
            f"requivo {cli_version} is at or ahead of {plugin_version}, the version this plugin "
            f"was tested against. In step.",
        )
    return SkewResult(
        BEHIND,
        f"This plugin was tested against requivo {plugin_version}; the installed CLI reports "
        f"{cli_version}. Most commands still work across a minor version -- if one fails with an "
        f"argparse error about an unrecognized argument, that is why. "
        f"`pip install -U requivo` (or `uv tool install --force requivo`) to catch up.",
    )


def check(
    doctor_json_text: Optional[str],
    doctor_error: Optional[str],
    manifest_path: Path = MANIFEST,
) -> SkewResult:
    """The whole decision from raw inputs a caller already has: the text `requivo doctor --json`
    printed (or `None` if the call itself could not be made) and any error observed making that
    call. The could-not-look arm is checked first and independently of what `doctor_json_text`
    looks like, so a truthy-looking string that turns out not to parse cannot fall through to a
    silent 'no skew found'.
    """
    if doctor_error:
        return SkewResult(
            COULD_NOT_LOOK,
            f"Could not determine whether this plugin is in step with the installed CLI: "
            f"{doctor_error}. This is not evidence the versions match -- it is unknown.",
        )
    if not doctor_json_text or not doctor_json_text.strip():
        return SkewResult(
            COULD_NOT_LOOK,
            "Could not determine whether this plugin is in step with the installed CLI: "
            "`requivo doctor --json` produced no output. This is not evidence the versions "
            "match -- it is unknown.",
        )
    try:
        payload = json.loads(doctor_json_text)
    except (json.JSONDecodeError, TypeError) as exc:
        return SkewResult(
            COULD_NOT_LOOK,
            f"Could not determine whether this plugin is in step with the installed CLI: "
            f"`requivo doctor --json` did not print valid JSON ({exc}). This is not evidence the "
            f"versions match -- it is unknown.",
        )
    cli_version = payload.get("requivo_version") if isinstance(payload, dict) else None
    if not isinstance(cli_version, str) or not cli_version.strip():
        return SkewResult(
            COULD_NOT_LOOK,
            "Could not determine whether this plugin is in step with the installed CLI: the "
            "doctor report carries no `requivo_version` field. This is not evidence the versions "
            "match -- it is unknown.",
        )
    if not _looks_like_a_version(cli_version):
        return SkewResult(
            COULD_NOT_LOOK,
            f"Could not determine whether this plugin is in step with the installed CLI: the "
            f"doctor report's `requivo_version` ({cli_version!r}) is not version-shaped. This is "
            f"not evidence the versions match -- it is unknown.",
        )
    try:
        plugin_version = tested_against_version(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return SkewResult(
            COULD_NOT_LOOK,
            f"Could not determine the version this plugin was tested against: {exc}. This is not "
            f"evidence the versions match -- it is unknown.",
        )
    return compare(cli_version, plugin_version)


def main(argv=None) -> int:
    """Standalone diagnostic: run `requivo doctor --json` and print the verdict. Not part of any
    skill's runtime Bash grant -- see the module docstring.

    `tests/test_plugin_version_skew.py` exercises this function's exception handling directly, by
    monkeypatching `subprocess.run` -- so the three `except` arms below, and the exit code each
    produces, are unit-tested, even though none of them spawns a real process. What stays untested
    by any suite is a *real* spawn against an actual `requivo` binary; no CI leg runs
    `python3 version_skew.py` itself as a subprocess and checks its process exit status, which is
    inherently a manual/platform check.

    Three `except` arms, not two (#363): `subprocess.TimeoutExpired` inherits `SubprocessError ->
    Exception`, not `OSError`, so it does not land in `except OSError` below -- it used to escape
    this function as a raw traceback instead of becoming `COULD_NOT_LOOK`, which is this module's
    own third state, in its own failure path. Reachable, not theoretical: #263 gives `doctor` a
    per-slug session lock with `_LOCK_TIMEOUT_SECONDS = 30.0`, the same 30s this function passes to
    `subprocess.run(timeout=...)`, so a workspace with two stuck sessions can legitimately make
    `requivo doctor --json` outlive this timeout.

    The timeout gets its own message rather than folding into the generic OSError one: a missing
    binary points a reader at their install (PATH), while a timeout points them at what is stuck
    (a held session lock) -- collapsing the two into one "could not run" sentence would send a
    reader who hit the timeout looking in the wrong place."""
    del argv
    doctor_error: Optional[str] = None
    output: Optional[str] = None
    try:
        proc = subprocess.run(
            ["requivo", "doctor", "--json"], capture_output=True, text=True, timeout=30
        )
        output = proc.stdout
    except subprocess.TimeoutExpired as exc:
        doctor_error = (
            f"`requivo doctor --json` did not finish within {exc.timeout:g}s -- it may be stuck "
            f"(e.g. a held session lock), not merely absent"
        )
    except FileNotFoundError:
        doctor_error = "the `requivo` command was not found on PATH"
    except OSError as exc:
        doctor_error = f"could not run `requivo doctor --json`: {exc}"

    result = check(output, doctor_error)
    print(result.message)
    return result.state


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
