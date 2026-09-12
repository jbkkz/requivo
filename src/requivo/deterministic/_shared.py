"""The primitives every deterministic verb shares, and deliberately nothing else.

The issue that split this package (#73) asked where the shared helpers should go, and warned that
the obvious answer, a `_shared` module, is also the one that quietly becomes a second
`deterministic.py`. So the membership rule is written down here rather than left to accretion, and
it has two halves that both have to hold:

1. **Two or more of the verb modules use it.** A helper used by exactly one of `doctor`, `sessions`,
   `model` or `artifacts` belongs in that module, however generic it looks.
2. **It is about the surface, not about a domain.** Reading input the user named, printing JSON, and
   the exit code for an answer that is neither complete nor absent are surface concerns. Card health
   is used by `doctor` and by `sessions` and is deliberately *not* here, because it is a domain fact
   rather than a surface one: it lives in `doctor`, and `sessions` imports it from there, which is
   what makes the reuse visible instead of anonymous.

A helper that fails either half goes back to the module that owns it. Nothing here imports from a
sibling module, which is what keeps this package's import graph a DAG.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from requivo.core.context import resolve_cards
from requivo.core.errors import InvalidModelError
from requivo.core.selectors import display_token

# The work was done and part of the answer was unreachable. Neither 0 nor 1 is true of that: 0 says
# nothing is wrong, 1 says there is no answer at all, and a script that does not parse stdout reads
# the exit code alone — so collapsing it into either neighbour is invariant 15's own defect in the
# one channel left. It is safe to make non-zero *because* stdout is complete: unlike the error path,
# nothing is withheld, so a caller that only wants what was produced still gets all of it.
#
# It was `EXIT_DEGRADED_LISTING` and it is `EXIT_DEGRADED` (#86): an exit code describes a shape of
# answer, not a verb. `session list` rendering every row it could was the first instance;
# `session verify` unable to read a session's product context is the second, and minting a number
# per verb would rebuild the problem this one was introduced to solve.
#
# 0/1/2 are success, `RequivoError` and argparse; 3 is `cli.EXIT_RENDER_FAILED`. It cannot be
# imported from there — `cli` imports this module, so the dependency runs one way only — and
# `test_the_degraded_code_collides_with_nothing` is what stops the two numbers drifting into each
# other.
EXIT_DEGRADED = 4


def print_json(obj) -> None:
    """Print `obj` as indented JSON, with `ensure_ascii`'s protective default preserved (#70, below).

    Public since #301: `cli.py`'s `--json` output (`status`, and `app()`'s own error envelope) used
    to call `json.dumps` directly, its own second copy of the same call with no way to inherit this
    function's guarantee -- or the #70 fix, had it landed here first instead."""
    # `ensure_ascii` is left at its default and that is load-bearing (#70) — for a narrower set of
    # characters than `session list` and `session show` claim between them. JSON's own grammar
    # forbids a literal control character below U+0020 inside a string, so a newline is escaped
    # whatever this flag says. What the flag decides is everything *non-ASCII*: U+007F–U+009F, where
    # NEL and CSI live, and also U+2028/U+2029, which the terminal-side guard deliberately does not
    # cover. So this path is the *stricter* of the two and stays that way only while the default
    # does; turning it off to make accented output readable would reopen the forgery by that route,
    # and `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line` is what
    # objects.
    print(json.dumps(obj, indent=2))


def read_user_text(path: Path) -> str:
    """A file the *user* named, decoded as UTF-8, with a structured refusal when it is not UTF-8.

    Every file Requivo writes is UTF-8 (`_atomic_write`), so UTF-8 is the right codec to read one
    back with — that is #11. But these particular reads take a path the user typed, and that file was
    written by something else: a French client brief saved out of a Windows editor is genuinely
    cp1252 often enough to matter.

    The old behaviour decoded it with the locale's codec, which on Windows silently produced mojibake
    that then validated, persisted, and shipped in the PRD. Refusing is right — invariant 3, refuse
    rather than truncate, because half a request reads exactly like a whole one, and mojibake reads
    exactly like prose. What is *not* right is refusing with a bare `UnicodeDecodeError` traceback:
    that would trade a silently wrong answer for an unexplained crash, which is the same trade one
    step along. So the refusal names the file, the codec and the way out.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # `display_token`, not the bare path: the path is untrusted input, and a filename carrying a
        # newline would otherwise write what reads as a second, authoritative line of Requivo's own
        # output at column 0 — the shape #40 found in `doctor`, in a message added by the fix for
        # #11. Found by this file's own guard test rather than in review, which is the argument for
        # the guard: the class reaches code written today, not only legacy paths.
        raise InvalidModelError(
            f"{display_token(str(path))} is not valid UTF-8 "
            f"(byte 0x{e.object[e.start]:02x} at position {e.start}). "
            f"Requivo reads and writes UTF-8 throughout, so a file in another encoding is refused "
            f"rather than decoded into text that would look like prose and be wrong. Re-save it as "
            f"UTF-8 and try again.",
            details={"path": str(path), "expected_encoding": "utf-8", "position": e.start},
        ) from None


def is_file_argument(arg: str) -> bool:
    """True if `arg` names an existing *file*, safe against the three pathlib traps a naive
    `Path(arg).is_file()` falls into on this kind of argument -- one that is legitimately either a
    path or the content itself.

    Note what this does **not** answer: `is_file_argument("-")` is False, because `-` is not a file.
    It is not literal text either -- it is stdin. A caller that has to tell all three apart wants
    `read_source` below, which is what `discover` was missing (#360).

    The three traps, each of which must read as 'not a file' so the caller falls back to literal
    text: a blank string, which `Path("")` resolves to the current directory
    (`test_discover_file_check_rejects_blank_arg`); a bare directory name, which `.exists()` accepts
    and `read_text()` then raises on (`test_discover_file_check_rejects_a_directory`); and an
    argument past the OS filename limit, on which the check itself *raises* rather than returning
    False (`test_discover_file_check_survives_a_real_length_request`). Public since #301, when
    `cli.py`'s `discover` was found re-implementing all three under its own name; `read_source`
    below is this function's other caller.
    """
    if not arg.strip():
        return False
    try:
        return Path(arg).is_file()
    except OSError:
        return False


def read_source(arg: str) -> str:
    """A request/answers argument that may be an inline string, a path to a file, or `-` for stdin.

    Public since #360, for the same reason `is_file_argument` above became public in #301: `cli.py`'s
    `discover` needed the middle of this function and reached for the one public piece of it, so
    `discover -` never met the `-` branch at all and discovered on the two-character request `-`,
    at full price. A verb that documents its argument as "the client request, or a path to a file
    containing it" wants this whole function, not one third of it.
    """
    if arg == "-":
        return read_stdin()
    return read_user_text(Path(arg)) if is_file_argument(arg) else arg


# The old private spelling, kept alive for `deterministic/sessions/lifecycle.py`'s import while that
# module is held by another change in flight (#360). It is one binding to one function object, so
# both names behave identically; drop it, and switch that import to `read_source`, once that change
# lands.
_read_source = read_source


def read_stdin() -> str:
    """Everything on stdin, as text. Refused when stdin is a terminal, which would otherwise hang
    waiting for input the caller never meant to type."""
    if sys.stdin is None or sys.stdin.isatty():
        raise InvalidModelError(
            "'-' means read from stdin, but stdin is a terminal — pipe the content in, "
            "or pass a file path instead")
    return sys.stdin.read()


def _read_document(arg: str) -> str:
    """A *document* argument: a path, or `-` for stdin. Unlike `read_source`, the text is never
    itself the content — `model apply <session> proposal.json` takes a path, so a non-existent path is
    a mistake to report, not a proposal whose body happens to be a filename.

    Stdin exists so a caller with content in hand does not have to invent a temp file for it. The
    Claude Code skills used to write `/tmp/requivo:prd.md`: a path that is not writable on Windows
    (`:` is illegal in a filename there), that needed `rm` to clean up — a command the plugin does not
    grant itself — and that two concurrent sessions would have shared."""
    if arg == "-":
        return read_stdin()
    p = Path(arg)
    if not p.is_file():
        raise InvalidModelError(f"no such file: {display_token(arg)} (use '-' to read from stdin)",
                                details={"path": arg})
    return read_user_text(p)


def _resolve_cards(spec: str | None) -> list[str] | None:
    """A comma-separated --context spec → validated card stems (None == all cards). The resolution and
    the unknown-card error live in Core (`resolve_cards`), shared with the Web, so a typo can never
    silently widen the context on one surface and fail on another."""
    return resolve_cards(spec.split(",")) if spec else None


# Said once rather than at each of the sites that need it, because they have to agree: an entry with
# no error text still has to read as *we could not look*, and an empty string there would render as a
# row that failed for no reason.
_NO_DETAIL = "no further detail"
