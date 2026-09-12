"""Resolve the plugin's `requivo` invocations against a RELEASED CLI, not against this checkout.

Why this exists (#96)
---------------------
`tests/test_plugin.py::test_skills_reference_only_real_cli_commands` introspects
`requivo.cli._build_parser()` from the working tree and asserts every `requivo <verb>` a skill
mentions is in it. That is a checkout-against-checkout comparison, and it is green by construction:
the plugin and the parser it is checked against are the same commit.

What a user installs is two artifacts that were never compared. The plugin comes from a marketplace
listing, and a community listing pins it to a commit SHA that Anthropic's CI advances as commits land
on `main` -- so the plugin a stranger installs is routinely `main`. The CLI comes from
`uv tool install requivo`, which is the last PyPI release. Between releases the two drift by
construction, and until this script nothing measured the gap.

The three states, and the third one is the point
------------------------------------------------
  resolved        every invocation the plugin makes exists in the released CLI.
  drift           at least one does not. A stranger installing today gets a skill that fails.
  could-not-look  the question was not answered and we know it was not: the released surface is
                  unknown (PyPI unreachable, no release published, the install failed, the probe
                  could not introspect), or the plugin tree could only be walked in part -- a
                  directory this process cannot descend into hides whatever is inside it, and a
                  verdict over the subset it managed to read is not a verdict about the plugin (#139).

`could-not-look` is not a pass and it is not drift either. A released CLI read as an empty verb set
would report every single invocation as drift, which is a confident answer to a question nobody
answered; read as a pass it hides the check being broken. So a surface is `None` or a populated
`Surface`, never an empty one that grades as either.

Why the working tree classifies, and the release only answers
-------------------------------------------------------------
A skill writes `requivo model apply <slug>` and `requivo status <slug>`. In the first, `apply` is a
subcommand; in the second, `<slug>` is an argument. Deciding which is which from the RELEASED CLI
gets it wrong in the case that matters most: a release that kept `model` but dropped its subcommand
group would make `apply` look like a positional argument, and `requivo model apply` -- the one call
in the whole plugin that mutates a model -- would grade clean while being broken for every user.

So the working tree is the classifier. The tree and the plugin are the same commit, which makes the
tree the authority on what the plugin *meant*; the release is then only asked whether that meaning
still resolves. It also removes a false-positive class for free: a bare word the tree does not call a
subcommand is prose or a positional, and is never flagged.

Advisory by design -- the policy lives in the workflow, not here
---------------------------------------------------------------
This script exits 0 resolved / 1 drift / 3 could-not-look, because a caller that cannot tell them
apart is the failure it exists to prevent. Exit 2 is left to `argparse`, which spends it on a usage
error -- a run that never reached a verdict must not be spellable as one of the three.

Whether a non-zero exit should redden a build is a *policy* question, and it is answered one
directory over in `.github/workflows/plugin-validate.yml`, where the step is `continue-on-error`
and turns the code into an annotation. That split is deliberate: this
repo's normal state between releases is a pull request that adds a CLI verb and a skill using it
before the next release exists on PyPI, and a required check that reddens for that is a check that
gets overridden, then ignored, then deleted -- the same argument `plugin-validate.yml` makes at
length for running `--strict` against a pinned CLI. Keeping the policy in the workflow means changing
it is one reviewed line rather than an edit to a script other callers may rely on.

Stdlib only, and deliberately so: the released CLI is probed in a separate interpreter, and the leg
must be able to run this before deciding anything about what installs.

Usage
-----
    python scripts/plugin_cli_drift.py --released-python /path/to/released/venv/bin/python [--github]
"""
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

# 0, 1, 3 -- not 0, 1, 2. `argparse` exits 2 of its own accord on a usage error, so a
# could-not-look of 2 would be indistinguishable from "you called this script wrong", and the
# caller would report a verdict for a run that never got as far as having one. Leaving 2 to
# argparse means an unrecognised code is unambiguously a usage error, which is what the workflow's
# fallback arm says.
EXIT_RESOLVED = 0
EXIT_DRIFT = 1
EXIT_COULD_NOT_LOOK = 3

PROBE_TIMEOUT_S = 60

# The same shape `tests/test_plugin.py` has always used for the first token, plus an optional second
# one. The second is only *read* here; whether it means anything is decided in `compare()` against
# the working tree. A flag (`--json`), a placeholder (`<slug>`) and a bare stdin dash are therefore
# never captured at all, and a bare word that turns out to be a positional argument is dropped later.
#
# The character classes are also what makes the *captured token* safe to print. A SKILL.md is a
# file, so its contents are untrusted, and a token is interpolated into a GitHub Actions `::warning`
# command and into a bare `print` beside it. `[\w-]` admits no newline, no colon and no `#`, which
# is one exclusion per thing that could go wrong: no newline means no line of its own, no colon
# means no `::name::`, and no `#` means no `##[name]` -- and that last form needs neither of the
# other two, which is the whole of #176. Widening this pattern means revisiting `_log_safe`, which
# every other untrusted value in this file goes through and which a token deliberately does not.
#
# `re.ASCII` is load-bearing for the same reason. Python's `\w` is Unicode-aware by default, so
# without the flag `requivo 日本語` captures a token, which is then printed -- and this script is
# stdlib-only and never imports `requivo.streams`, so nothing has reconfigured stdout. On a Windows
# console at cp1252 that `print` raises `UnicodeEncodeError` and kills the process after the work it
# was reporting was already done, which is invariant 16 in this repo's own CLAUDE.md. Nothing is
# lost by the restriction: every verb this CLI has is ASCII, because they are argparse choices we
# declare. `_harden_streams()` covers the paths and version strings the flag cannot.
INVOCATION_RE = re.compile(r"requivo (\w[\w-]*)(?:[ \t]+([A-Za-z][\w-]*))?", re.ASCII)

# Run in the TARGET interpreter, which may hold a released Requivo of any age. It prints one JSON
# object and nothing else; any failure is a non-zero exit with a sentence on stderr, which the caller
# turns into could-not-look rather than into a verdict.
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
    """A CLI's verb tree. `verbs[verb]` is that verb's set of subcommands, or None when it has none
    at all -- a different fact from an empty set, and the one that tells an argument from a
    subcommand."""

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
    """What the walk found, and what it could not decide about.

    Two fields rather than a bare list of paths, so a caller cannot take the walked set without
    being handed the third state alongside it. That is structural on purpose: the neighbouring
    defect -- an entry point that called `compare()` and never `tree_typos()`, and printed a clean
    bill over a broken invocation -- is one `_run` already carries a paragraph about."""

    paths: list[Path]
    unreadable: list[str]


# An "invocation" throughout this module is `(verb, second_token_or_None)` -- spelled out in the
# signatures rather than aliased, because an alias is evaluated at runtime and `str | None` inside
# one would not survive the Python 3.9 leg of the matrix. Annotations are strings here (see the
# `__future__` import) and are never evaluated, so builtin generics are free in them.


def parse_surface(payload: str) -> Surface | None:
    """Read the probe's stdout. Returns None -- never an empty Surface -- for anything unreadable.

    Every string that comes out of here is `_log_safe`d, because this is the OTHER door untrusted
    text uses and it was open (#176 was reported about the first one only). The tree half of the
    probe introspects `requivo.cli._build_parser()` in the WORKING TREE, which a fork pull request
    edits as freely as it names a directory; those verb and subcommand names are interpolated into
    `tree_typos`' reason and `compare`'s, both of which reach a bare `print`. A newline in one puts
    the remainder at column 0, which reaches both of the runner's parsers rather than only the
    unanchored one.

    Sanitising the dict KEYS is free rather than lossy: the tokens they are matched against come out
    of `INVOCATION_RE`, whose word-and-dash character class cannot produce whitespace, a colon or a
    `#`, so a verb name this changes is one that could never have matched anything anyway.
    `test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own`.
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
    """Collapse every whitespace run to one space. Half of `_log_safe`, and separately useful:
    `scripts/golden_lib.py` and `scripts/golden_diff.py` each compare their own sink against this
    one by name, and neither of them prints into a log that parses workflow commands.

    Pinned by `test_the_sanitiser_collapses_whitespace_and_breaks_both_command_forms`.
    """
    return " ".join(text.split())


# The runner reads its own log twice, with two different anchors, and this file spent a release
# modelling one of them. From `actions/runner`, `src/Runner.Common/ActionCommand.cs`, read via #175:
#
#   TryParseV2  `::name parameters::data`  calls `message.TrimStart()` BEFORE testing the prefix,
#                                          so indenting contains nothing and a line start is what
#                                          matters. Killing newlines defeats this form.
#   TryParse    `##[name]data`             is `message.IndexOf("##[")`. No anchor at all: neither a
#                                          line start nor a newline is required, so killing
#                                          newlines defeats nothing here.
#
# That gap was #176: a skills entry named `brief##[error]title=...`, with no newline anywhere in it,
# reached `_show`'s bare `print` and forged an annotation stating something no tool concluded.
# `test_a_skill_directory_name_cannot_forge_the_legacy_command_form` is what goes red if this is
# removed, and it needs no newline, so it runs on every platform in the matrix.
#
# A RUN of colons is broken apart whole rather than pairwise, which is what makes `_log_safe`
# idempotent. `str.replace("::", ": :")` turns `:::` into `: ::` -- still holding a `::`, and broken
# differently by a second pass. That matters because `_annotate` applies this as a backstop to a
# message whose parts were already sanitised where they entered, so a non-idempotent rule would make
# the plain `print` and the annotation of one value disagree.
_COLON_RUN_RE = re.compile(r":{2,}")


def _log_safe(text: str) -> str:
    """Make one untrusted value unreadable as a workflow command by EITHER parser, wherever it lands.

    Applied at the point untrusted text enters this module -- a directory entry name, a path in an
    error, a verb name read back out of the probe -- rather than at each of the five places it
    leaves, so a future consumer inherits the guarantee instead of having to remember it. This
    repository has had exactly this defect before: invariant 14's #40, a stored card name forging a
    line at column 0 of `doctor`'s own output.

    The result is idempotent, which is what lets `_annotate` apply it as a backstop over a message
    whose parts were already sanitised: see the note on `_COLON_RUN_RE` above.

    Both keys are broken, and the second one is a deliberate widening of what `_one_line` alone
    bought. Collapsing whitespace already means a value cannot *start* a line, which is all
    `TryParseV2` needs -- but that is a guarantee about the layout of the five format strings below,
    not about the value, and it evaporates the day one of them prints a label first. `##[` has no
    such excuse at any column. So the property is put on the value: no result of this function is a
    workflow command in either form, whatever precedes it.

    Spaced apart rather than deleted, so the value still reads as what was on disk -- the same
    spelling `.github/workflows/plugin-validate.yml` uses on `claude --version` (#147). A lone colon
    is untouched, which matters because every path and every reason line here carries one.

    `test_a_skill_directory_name_cannot_forge_the_legacy_command_form` is the end-to-end guard and
    it runs on every platform, `test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own` is
    the same class on the other entry point, and
    `test_an_unreadable_path_cannot_forge_a_line_of_its_own` is the newline-borne half.
    """
    out = _COLON_RUN_RE.sub(lambda run: " ".join(run.group()), _one_line(text))
    return out.replace("##[", "## [")


def _collect_file(candidate: Path, paths: list[Path], unreadable: list[str]) -> None:
    """Sort one candidate into walked, absent, or could-not-look.

    `Path.is_file()` cannot express the third: it swallows `OSError` and returns False, so a file
    behind a directory this process cannot descend into is indistinguishable from one that is not
    there. Only an error that *decides* the question -- the path is absent, or a parent is not a
    directory -- is a plain no here; anything else is the walk saying it could not look.
    """
    try:
        if stat.S_ISREG(candidate.stat().st_mode):
            paths.append(candidate)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        unreadable.append(_log_safe(f"{candidate}: {exc.strerror or exc}"))


def invocation_sources(plugin_root) -> Sources:
    """The plugin files whose `requivo` calls are *executed*, which is not the same set as the files
    that mention one -- and the paths this walk could not decide about.

    Every `SKILL.md` body, plus `REASONING.md`. That second one is not a reader's document: it
    holds the shared preflight every skill is required to run before its first `requivo` call (#93),
    so a command named there runs on every skill path and is the most-executed invocation in the
    plugin. It happens to introduce no verb the skills do not already name, which is exactly why it
    needs to be in the walked set rather than left to keep happening to be redundant. (#542)

    Three outcomes and not two, because the predicate reaches a filesystem that can refuse. A walk
    that silently grades the subset it managed to read tells the caller `resolved` about a plugin it
    never opened -- pinned by
    `test_main_reports_could_not_look_when_it_could_only_walk_part_of_the_plugin` (#139).

    `plugins/claude-code/README.md` is deliberately NOT in this set, and that is now a decided
    question rather than an open one (#138). It stays out because it is a page of English prose
    rather than an instruction Claude follows -- the same line `tests/test_plugin.py` draws -- and
    `INVOCATION_RE` cannot tell `requivo requires an API key` from a command. What covers it instead
    is `test_the_plugin_readme_names_only_verbs_this_checkout_has`, which reads only the README's
    code spans and fenced blocks: narrower than this walk, and it cannot false-positive on a
    sentence. So the silence here is a decision with a guard behind it, not coverage nobody wrote.
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
    """What a finding calls the file it came from. `skills/brief/SKILL.md` is "brief"; anything else
    is its own name, so `REASONING.md` reads as itself rather than as the directory above it.

    Sanitised, because this returns a *directory name* into every finding the script prints, and a
    name is untrusted text -- see `_log_safe`."""
    return _log_safe(path.parent.name if path.name == "SKILL.md" else path.name)


def referenced_invocations(paths) -> dict[tuple[str, str | None], list[str]]:
    """Every `requivo ...` the given files mention, mapped to the files that mention it.

    The sources are carried because a finding nobody can locate is a finding nobody acts on.
    """
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
    """The in-tree half, which is a different question from drift and needs a different rule.

    `compare()` drops a second word the tree does not call a subcommand, because it cannot tell a
    positional argument from prose -- correct for the drift question, and it means a skill that
    misspells `requivo model rebase` is flagged by nothing at all.

    A verb that *has* a subcommand group is the case where the ambiguity disappears: argparse gives
    that group the next positional, so `model`, `session` and `artifact` accept no bare word of their
    own and any bare word after them is a subcommand claim. That is checkable against the tree with
    no false positives, and it is what `tests/test_plugin_cli_drift.py` asserts. Verbs with no group
    are left alone here, deliberately -- that is where the prose lives.
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
    """Three states. See the module docstring for why the tree classifies and the release answers.

    A second word the tree does not call a subcommand is dropped rather than flagged; `tree_typos()`
    above is the check that covers that case, against the checkout where it can be answered."""
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

    A plugin path, a directory name a fork controls, or a released version string can hold a
    character the console's codepage cannot encode -- on Windows that is typically cp1252 -- and an
    unhandled `UnicodeEncodeError` would kill this process at a `print`, after the comparison it was
    reporting had already been made. `backslashreplace` and never `replace`, because a reader cannot
    tell a substituted character from one that was never there.

    Not `golden_lib.configure_output()`, on purpose. That function reaches `requivo.streams`, and
    `golden_lib` itself imports `requivo.core.analysis` and friends -- so importing either would give
    this module a dependency on `requivo` being installed. The module docstring's "stdlib only" is
    load-bearing for exactly this call site: the workflow's tree-side `pip install -e .` step is
    `continue-on-error`, and when it fails this script still has to run far enough to report
    could-not-look in those words. An import-time `ModuleNotFoundError` would crash before `main()`
    ever got there, which is worse than the bug this function exists to fix.
    `test_the_module_stays_stdlib_only` pins that.

    The third state -- a stream this cannot reach -- is named on stderr rather than swallowed, the
    same rule `requivo.streams.configure_stream` follows for the product: a stream that could not be
    hardened is exactly the one that can still crash later, so staying silent about it would be this
    function's own instance of the class it exists to fix.
    `test_harden_streams_names_a_stream_it_could_not_reach` is the guard, with
    `test_a_strict_console_reports_a_real_finding_as_could_not_look_when_streams_are_not_hardened` as
    the positive control showing what this call site prevents: without it, a real drift finding is
    overwritten by the crash that happens while printing it, and the exit code describes the crash
    rather than the walk (see the module docstring's "Advisory by design" section).
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
    """`_harden_streams`'s third state, written defensively: `reason` is composed from an exception's
    own `str()`, which can itself hold a character stderr cannot encode -- so the FIRST write can
    raise `UnicodeEncodeError`, the exact class this whole module exists to survive, one function
    inward. A bare `except: pass` there would make the note go missing with nothing to tell a reader
    "both streams hardened fine" from "hardening AND the report of its own failure both failed
    silently" -- so the fallback re-encodes with `backslashreplace` rather than giving up on the
    first attempt, the same shape `requivo.streams.safe_write` uses for the product (not imported,
    for the same stdlib-only reason `_harden_streams` gives).
    `test_note_could_not_harden_survives_a_console_that_cannot_encode_its_own_reason` is the guard.
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
    """A GitHub Actions annotation is one line; a newline inside it truncates the message.

    `_log_safe` rather than a bare squash, so this function's guarantee is self-contained instead of
    leaning on every caller having sanitised what it composed. It is a backstop and not the defence:
    the values inside `message` were already sanitised where they entered, and the reason this line
    was never the hole is that the runner takes the whole line as the command we open here.
    """
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

    # Every unhandled exception below would otherwise exit 1, which is EXIT_DRIFT -- so an
    # unreadable `SKILL.md` (a permission error, a bad byte) would be reported by the caller as
    # "drift, annotated above" for a run that annotated nothing and reached no verdict at all. That
    # is this file's own defect class turned on itself. A crash is precisely could-not-look: the
    # question was not answered and we know it was not.
    try:
        return _run(args)
    except Exception as exc:                       # noqa: BLE001 - the point is that it is total
        # `_log_safe` on the exception text: it routinely carries a path, and this one is printed to
        # stderr, which the runner parses exactly as it parses stdout.
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

    # The other half of the question, and it has to be asked HERE rather than only in the tests.
    # `compare()` drops a bare word the tree does not call a subcommand, so a plugin whose only
    # defect is `requivo model rebase` -- a subcommand in neither the release nor the checkout --
    # made this function print `resolved` and exit 0. `tests/test_plugin_cli_drift.py` calls
    # `tree_typos()` on the real plugin and is a required check, so such a defect could not have
    # shipped; but a run of this script reported a clean bill for a plugin that has one, and a
    # verdict that is clean for the wrong reason is the thing this whole file exists to refuse.
    # Both groups are invocations that do not resolve, so both count toward the same exit code --
    # and the state line below has to be computed after this, or it prints `resolved` above a list
    # of things that did not resolve.
    typos = tree_typos(referenced, tree) if report.state != COULD_NOT_LOOK else []
    state = DRIFT if (report.state == RESOLVED and typos) else report.state

    # The third state for the part of the plugin this process could not read (#139). It downgrades a
    # `resolved` and never a firm negative: a complete answer outranks a partial one, which is the
    # rule invariant 15 states for `session verify`. Pinned from both sides by
    # `test_drift_in_the_part_it_could_read_outranks_the_part_it_could_not`.
    if state == RESOLVED and sources.unreadable:
        state = COULD_NOT_LOOK

    # Sanitised for the same reason as everything else printed here, even though this one arrives on
    # argv rather than off disk: the value is still text this function did not author.
    print(f"plugin root   : {_log_safe(str(args.plugin))}")
    print(f"files walked  : {len(sources.paths)}")
    # Printed even when it is zero, because a count that appears only when it is interesting cannot
    # be told from a check that stopped running.
    print(f"unreadable    : {len(sources.unreadable)}")
    print("tree CLI      : {}".format(tree.version if tree else "COULD NOT INTROSPECT"))
    print("released CLI  : {}".format(released.version if released else "COULD NOT INTROSPECT"))
    print(f"invocations   : {len(referenced)}")
    print(f"state         : {state}")
    print("")

    # Named before any verdict detail, on every path out of this function, and that ordering is the
    # whole of the fix rather than a presentation choice. `compare()` is never handed the unreadable
    # set -- it is a fact about how `referenced` was built, not about the surfaces it compares -- so
    # when the only invocation-bearing file is the one that could not be opened it reports `no
    # requivo invocations were found in the plugin's files`, which blames the plugin for an emptiness
    # this process created. Returning on that arm first meant the path was never named at all.
    # `test_the_reason_names_the_unreadable_path_when_nothing_could_be_extracted`.
    if sources.unreadable:
        detail = ("{} plugin path(s) could not be read, so this run walked a subset of the plugin and "
                  "everything below is a verdict over that subset rather than over the plugin: {}. That "
                  "is not a clean result and it is not evidence of drift either.").format(
                      len(sources.unreadable), "; ".join(sources.unreadable))
        # A path is untrusted text reaching an annotation AND a bare print. Each path in the list was
        # `_log_safe`d where it entered, which is what makes both safe: a squash alone would leave
        # `##[` intact, and `TryParse` needs no line start to read it (#176). It is not enough that
        # `_annotate` puts our own `::` first -- this `print` puts nothing first.
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
            # The two lines #176 was reported about. Nothing is sanitised here on purpose: the
            # sources came through `_label`, the reason's verb names came through `parse_surface`,
            # and `invocation` is built from `INVOCATION_RE` captures. Sanitising at the point of
            # entry is what lets this stay a plain `print` -- but it also means a NEW value reaching
            # this line has to have gone through `_log_safe` first.
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
