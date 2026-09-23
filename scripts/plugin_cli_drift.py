"""Resolve the plugin's `requivo` invocations against a RELEASED CLI, not against this checkout (#96).

`tests/test_plugin.py` checks the skills against the working tree's parser: green by construction.
A user gets two artifacts never compared — the plugin from a marketplace pin that tracks `main`, the
CLI from the last PyPI release — so between releases they drift.

Three states: **resolved**; **drift** (a stranger installing today gets a skill that fails); and
**could-not-look** — the released surface is unknown, or the plugin tree could only be read in part
(#139). The third is neither a pass nor drift, so a surface is `None` or a populated `Surface`, never
an empty one.

The working tree classifies and the release only answers: in `requivo model apply <slug>`, whether
`apply` is a subcommand is decided by the tree (the same commit as the plugin), or a release that
dropped `model`'s subcommands would make the one mutating call grade clean.

Exits 0 resolved, 1 drift, 3 could-not-look (2 is argparse's usage error). Whether that reddens a
build is policy in `.github/workflows/plugin-validate.yml`, where the step is `continue-on-error`:
a PR adding a verb and its skill before the release is this repo's normal state. Stdlib only: the
released CLI is probed in a separate interpreter.

Usage
-----
    python scripts/plugin_cli_drift.py --released-python /path/to/released/venv/bin/python [--github]"""
from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "claude-code"
SKILLS_DIR = PLUGIN_ROOT / "skills"

RESOLVED = "resolved"
DRIFT = "drift"
COULD_NOT_LOOK = "could-not-look"

# 0, 1, 3 -- not 2, which argparse spends on a usage error, so no run without a verdict reads as one.
EXIT_RESOLVED = 0
EXIT_DRIFT = 1
EXIT_COULD_NOT_LOOK = 3

PROBE_TIMEOUT_S = 60

# The first token and an optional second, which `compare()` interprets against the working tree; a
# flag, a placeholder or a dash is never captured. `[\w-]` with `re.ASCII` is also what makes a captured
# token safe to print: no newline, colon or `#` (so no `::name::` and no `##[name]`, #176), and no
# non-ASCII to crash an unconfigured console (invariant 16). Widening it means revisiting `_log_safe`.
INVOCATION_RE = re.compile(r"requivo (\w[\w-]*)(?:[ \t]+([A-Za-z][\w-]*))?", re.ASCII)

# Run in the TARGET interpreter: one JSON object on stdout, or a non-zero exit that becomes could-not-look.
PROBE = """
import argparse, json, sys

def _subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return None

try:
    from requivo.cli import _build_parser
except Exception as exc:
    sys.stderr.write("probe: cannot import requivo.cli._build_parser: " + repr(exc))
    raise SystemExit(1)

try:
    from importlib.metadata import version
    installed = version("requivo")
except Exception:
    installed = "unknown"

top = _subcommands(_build_parser())
if top is None:
    sys.stderr.write("probe: the root parser exposes no subcommands")
    raise SystemExit(1)

json.dump({"version": installed,
           "verbs": {name: (None if _subcommands(p) is None else sorted(_subcommands(p)))
                     for name, p in top.items()}}, sys.stdout)
"""


class Surface(NamedTuple):
    """A CLI's verb tree: `verbs[verb]` is its subcommands, or None when it has none (not an empty set)."""

    version: str
    verbs: dict[str, set[str] | None]


class Finding(NamedTuple):
    invocation: str
    sources: list[str]
    reason: str


class Report(NamedTuple):
    state: str
    findings: list[Finding]
    checked: int
    detail: str


class Sources(NamedTuple):
    """What the walk found, and what it could not decide about — together, so no caller takes one without the other."""

    paths: list[Path]
    unreadable: list[str]


# An invocation is `(verb, second_token_or_None)`; annotations are strings (`__future__`), safe on 3.9.


def parse_surface(payload: str) -> Surface | None:
    """Read the probe's stdout; None — never an empty Surface — for anything unreadable.

        Every string is `_log_safe`d: the tree half introspects a working tree a fork PR can edit, and its
        names reach a bare `print` (`test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own`).
        Sanitising keys is lossless: `INVOCATION_RE` tokens could never match a changed name.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("verbs")
    if not isinstance(raw, dict) or not raw:
        return None
    verbs: dict[str, set[str] | None] = {}
    for name, subs in raw.items():
        verbs[_log_safe(str(name))] = None if subs is None else {_log_safe(str(s)) for s in subs}
    return Surface(version=_log_safe(str(data.get("version") or "unknown")), verbs=verbs)


def cli_surface(python_executable: str) -> Surface | None:
    """Introspect the Requivo installed for `python_executable`. None means could-not-look."""
    try:
        proc = subprocess.run([python_executable, "-c", PROBE], capture_output=True,
                              encoding="utf-8", timeout=PROBE_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_surface(proc.stdout or "")


def _one_line(text: str) -> str:
    """Collapse every whitespace run to one space (`test_the_sanitiser_collapses_whitespace_and_breaks_both_command_forms`)."""
    return " ".join(text.split())


# The runner parses `::name::data` after `TrimStart()` (a line start matters) and `##[name]data`
# anywhere via `IndexOf` (actions/runner ActionCommand.cs, #175) — so killing newlines alone left
# #176 open (test_a_skill_directory_name_cannot_forge_the_legacy_command_form). A colon RUN is broken
# whole, which keeps `_log_safe` idempotent for `_annotate`'s backstop.
_COLON_RUN_RE = re.compile(r":{2,}")


def _log_safe(text: str) -> str:
    """Make one untrusted value unreadable as a workflow command by EITHER parser, wherever it lands.

        Applied where untrusted text enters (a directory name, a path in an error, a probe verb name), so
        every consumer inherits it (the #40 class). Idempotent. Both `::` and `##[` are spaced apart rather
        than deleted, so the value still reads as what was on disk (#147); a lone colon is untouched.
        Guards: `test_a_skill_directory_name_cannot_forge_the_legacy_command_form`,
        `test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own`,
        `test_an_unreadable_path_cannot_forge_a_line_of_its_own`.
    """
    out = _COLON_RUN_RE.sub(lambda run: " ".join(run.group()), _one_line(text))
    return out.replace("##[", "## [")


def _collect_file(candidate: Path, paths: list[Path], unreadable: list[str]) -> None:
    """Sort one candidate into walked, absent, or could-not-look (`is_file()` swallows `OSError`)."""
    try:
        if stat.S_ISREG(candidate.stat().st_mode):
            paths.append(candidate)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        unreadable.append(_log_safe(f"{candidate}: {exc.strerror or exc}"))


def invocation_sources(plugin_root) -> Sources:
    """The plugin files whose `requivo` calls are *executed*, and the paths the walk could not decide.

        Every `SKILL.md` plus `REASONING.md`, which holds the preflight every skill runs first (#93, #542).
        A partial walk is could-not-look, never `resolved`
        (`test_main_reports_could_not_look_when_it_could_only_walk_part_of_the_plugin`, #139). The plugin
        README is prose, left out by decision (#138) and covered by
        `test_the_plugin_readme_names_only_verbs_this_checkout_has`.
    """
    root = Path(plugin_root)
    paths: list[Path] = []
    unreadable: list[str] = []

    skills = root / "skills"
    try:
        entries = sorted(skills.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        entries = []                       # decided: this plugin root has no skills directory
    except OSError as exc:                 # EACCES on the directory itself, and every skill with it
        entries = []
        unreadable.append(_log_safe(f"{skills}: {exc.strerror or exc}"))

    for entry in entries:
        _collect_file(entry / "SKILL.md", paths, unreadable)
    _collect_file(root / "REASONING.md", paths, unreadable)
    return Sources(paths=paths, unreadable=unreadable)


def _label(path: Path) -> str:
    """What a finding calls its file: "brief" for `skills/brief/SKILL.md`, else its own name; `_log_safe`d."""
    return _log_safe(path.parent.name if path.name == "SKILL.md" else path.name)


def referenced_invocations(paths) -> dict[tuple[str, str | None], list[str]]:
    """Every `requivo ...` the given files mention, mapped to the files that mention it."""
    found: dict[tuple[str, str | None], list[str]] = {}
    for raw in paths:
        path = Path(raw)
        label = _label(path)
        for match in INVOCATION_RE.finditer(path.read_text(encoding="utf-8")):
            sources = found.setdefault((match.group(1), match.group(2)), [])
            if label not in sources:
                sources.append(label)
    return found


def tree_typos(referenced: dict[tuple[str, str | None], list[str]], tree: Surface) -> list[Finding]:
    """The in-tree half: a bare word after a verb that has a subcommand group is a subcommand claim.

        `compare()` drops unclassified second words, so `requivo model rebase` would pass it; after
        `model`, `session` or `artifact` argparse takes the next positional as a subcommand, so this is
        checkable with no false positives. Verbs without a group are left alone: that is where prose lives.
    """
    findings: list[Finding] = []
    for verb, token in sorted(referenced, key=lambda k: (k[0], k[1] or "")):
        subs = tree.verbs.get(verb)
        if token is None or subs is None or token in subs:
            continue
        findings.append(Finding(f"requivo {verb} {token}", sorted(referenced[(verb, token)]),
                                "`requivo {}` has no `{}` subcommand in this checkout; it offers {}".format(
                                    verb, token, ", ".join(sorted(subs)))))
    return findings


def compare(referenced: dict[tuple[str, str | None], list[str]], tree: Surface | None,
            released: Surface | None) -> Report:
    """Three states (see the module docstring); an unclassified second word is `tree_typos()`'s question."""
    if tree is None or not tree.verbs:
        return Report(COULD_NOT_LOOK, [], 0,
                      "the working tree's own CLI could not be introspected, so there is nothing to "
                      "read the plugin's invocations as. This says nothing about the release.")
    if released is None or not released.verbs:
        return Report(COULD_NOT_LOOK, [], 0,
                      "the released CLI could not be introspected. This is not a clean result and it "
                      "is not evidence of drift either -- the question is unanswered for this run.")
    if not referenced:
        return Report(COULD_NOT_LOOK, [], 0,
                      "no `requivo` invocations were found in the plugin's files. An empty set "
                      "passes every check by having nothing to check, so it is reported as a failure "
                      "to look rather than as a clean bill.")

    findings: list[Finding] = []
    for verb, token in sorted(referenced, key=lambda k: (k[0], k[1] or "")):
        sources = sorted(referenced[(verb, token)])
        tree_subs = tree.verbs.get(verb)
        is_subcommand = token is not None and tree_subs is not None and token in tree_subs
        name = f"requivo {verb} {token}" if is_subcommand else f"requivo {verb}"

        if verb not in released.verbs:
            findings.append(Finding(name, sources, f"the released CLI ({released.version}) has no `requivo {verb}`"))
            continue
        if not is_subcommand:
            continue
        released_subs = released.verbs[verb]
        if released_subs is None:
            findings.append(Finding(name, sources, (
                f"`requivo {verb}` takes no subcommands in the released CLI ({released.version}), but the plugin calls "
                f"`{name}`")))
        elif token not in released_subs:
            findings.append(Finding(name, sources, (
                "`{}` is not a subcommand of `requivo {}` in the released CLI ({}), which offers "
                "{}").format(token, verb, released.version, ", ".join(sorted(released_subs)))))

    return Report(DRIFT if findings else RESOLVED, findings, len(referenced), "")


def _harden_streams() -> None:
    """What `requivo/streams.py` does for the product, for a script that cannot import it (#174).

        `backslashreplace`, never `replace`, so a crash at `print` cannot overwrite a verdict already made.
        Not `golden_lib.configure_output()`: that imports `requivo`, and this must reach could-not-look even
        when the tree install failed (`test_the_module_stays_stdlib_only`). A stream it cannot reach is
        named on stderr (`test_harden_streams_names_a_stream_it_could_not_reach`,
        `test_a_strict_console_reports_a_real_finding_as_could_not_look_when_streams_are_not_hardened`).
    """
    for stream, name in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # a stream someone replaced with a plain object
            _note_could_not_harden(name, f"{type(stream).__name__} has no reconfigure()")
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError) as exc:    # already detached, or a stream that refuses
            _note_could_not_harden(name, f"{type(exc).__name__}: {exc}")


def _note_could_not_harden(name: str, reason: str) -> None:
    """`_harden_streams`'s third state, written so the note itself survives an unencodable reason.

        `test_note_could_not_harden_survives_a_console_that_cannot_encode_its_own_reason`.
    """
    message = f"  ! {name} could not be hardened against a character it cannot encode ({reason})\n"
    try:
        sys.stderr.write(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stderr, "encoding", None) or "ascii"
        try:
            sys.stderr.write(message.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        except (ValueError, OSError, UnicodeError):
            return
    except (OSError, ValueError):
        return
    try:
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _annotate(github: bool, title: str, message: str) -> None:
    """A one-line GitHub Actions annotation; `_log_safe` is a backstop over already-sanitised parts."""
    if github:
        print(f"::warning title={title}::{_log_safe(message)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--released-python", required=True,
                        help="interpreter of an environment with a RELEASED requivo installed")
    parser.add_argument("--tree-python", default=sys.executable,
                        help="interpreter that can import this checkout's requivo (default: this one)")
    parser.add_argument("--plugin", default=str(PLUGIN_ROOT),
                        help="the plugin root (its skills and REASONING.md are what get walked)")
    parser.add_argument("--github", action="store_true",
                        help="also emit GitHub Actions annotations")
    args = parser.parse_args(argv)
    _harden_streams()

    # Any crash is could-not-look: an unhandled exception would exit 1 and read as drift.
    try:
        return _run(args)
    except Exception as exc:                       # noqa: BLE001 - the point is that it is total
        # `_log_safe` on the exception text: it carries paths, and stderr is parsed like stdout.
        detail = (f"the drift check raised {type(exc).__name__} and could not complete, so nothing was compared. This "
                  f"is not a clean result and it is not evidence of drift: {_log_safe(str(exc))}")
        print(detail, file=sys.stderr)
        _annotate(args.github, "Plugin/CLI drift check could not look", detail)
        return EXIT_COULD_NOT_LOOK


def _run(args) -> int:
    tree = cli_surface(args.tree_python)
    released = cli_surface(args.released_python)
    sources = invocation_sources(args.plugin)
    referenced = referenced_invocations(sources.paths)
    report = compare(referenced, tree=tree, released=released)

    # Asked here, not only in the tests: `compare()` alone printed `resolved` over `requivo model rebase`.
    # Both groups count toward the exit code, and the state line is computed after this.
    typos = tree_typos(referenced, tree) if report.state != COULD_NOT_LOOK else []
    state = DRIFT if (report.state == RESOLVED and typos) else report.state

    # Could-not-look downgrades `resolved`, never a firm negative (invariant 15;
    # test_drift_in_the_part_it_could_read_outranks_the_part_it_could_not).
    if state == RESOLVED and sources.unreadable:
        state = COULD_NOT_LOOK

    # Sanitised: text from argv is still text this function did not author.
    print(f"plugin root   : {_log_safe(str(args.plugin))}")
    print(f"files walked  : {len(sources.paths)}")
    # Printed even at zero: a count shown only when interesting cannot be told from a check that stopped.
    print(f"unreadable    : {len(sources.unreadable)}")
    print("tree CLI      : {}".format(tree.version if tree else "COULD NOT INTROSPECT"))
    print("released CLI  : {}".format(released.version if released else "COULD NOT INTROSPECT"))
    print(f"invocations   : {len(referenced)}")
    print(f"state         : {state}")
    print("")

    # Named before any verdict, on every path: otherwise an unreadable only-file reads as "no invocations"
    # (test_the_reason_names_the_unreadable_path_when_nothing_could_be_extracted).
    if sources.unreadable:
        detail = ("{} plugin path(s) could not be read, so this run walked a subset of the plugin and "
                  "everything below is a verdict over that subset rather than over the plugin: {}. That "
                  "is not a clean result and it is not evidence of drift either.").format(
                      len(sources.unreadable), "; ".join(sources.unreadable))
        # Each path was `_log_safe`d on entry: this bare `print` puts nothing before it (#176).
        print(detail)
        _annotate(args.github, "Plugin/CLI drift check could not look", detail)

    if report.state == COULD_NOT_LOOK:
        print(report.detail)
        _annotate(args.github, "Plugin/CLI drift check could not look", report.detail)
        return EXIT_COULD_NOT_LOOK

    def _show(findings, heading):
        if not findings:
            return
        print(heading)
        for finding in findings:
            # The two lines #176 was about: every value here was sanitised where it entered, so a NEW value
            # reaching this line must go through `_log_safe` first.
            print("  {}   (referenced by: {})".format(finding.invocation, ", ".join(finding.sources)))
            print(f"      {finding.reason}")
        print("")

    _show(report.findings, "Present in the plugin, absent from the released CLI:")
    _show(typos, "Present in the plugin, absent from BOTH the released CLI and this checkout:")

    if report.findings:
        summary = ("The plugin on this branch makes {} invocation(s) the released CLI ({}) does not "
                   "have: {}. A user who installs the plugin from a marketplace and requivo from "
                   "PyPI today gets a skill that fails. If these ship in the next release that is "
                   "expected and needs no action; if they were removed from the CLI, the plugin is "
                   "broken now.").format(len(report.findings), released.version,
                                         "; ".join(f.invocation for f in report.findings))
        print(summary)
        _annotate(args.github, "Plugin/CLI drift", summary)

    if typos:
        summary = ("The plugin names {} subcommand(s) that exist in neither the released CLI ({}) "
                   "nor this checkout: {}. That is not release skew -- it is an invocation that "
                   "resolves nowhere, so it is broken on this branch as well as for anyone "
                   "installing today.").format(len(typos), released.version,
                                               "; ".join(f.invocation for f in typos))
        print(summary)
        _annotate(args.github, "Plugin names a command that does not exist", summary)

    if report.findings or typos:
        return EXIT_DRIFT

    if sources.unreadable:
        return EXIT_COULD_NOT_LOOK

    print(f"Every invocation the plugin makes resolves against the released CLI ({released.version}).")
    return EXIT_RESOLVED


if __name__ == "__main__":
    sys.exit(main())
