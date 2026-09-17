"""The primitives every deterministic verb shares, and nothing else (#73). Membership needs both:
two or more verb modules use it, and it is about the surface (input, JSON, an exit code), not a
domain; card health lives in `remedies.py`. Nothing here imports from a sibling module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from requivo.core.context import resolve_cards
from requivo.core.errors import InvalidModelError
from requivo.core.selectors import display_token

# The work was done and part of the answer was unreachable: neither 0 (nothing wrong) nor 1 (no
# answer), and stdout is complete (invariant 15). A shape of answer, not a verb (#86). 3 is
# `cli.EXIT_RENDER_FAILED`, which cannot be imported from here: `test_the_degraded_code_collides_with_nothing`.
EXIT_DEGRADED = 4


def print_json(obj) -> None:
    """Print `obj` as indented JSON with `ensure_ascii`'s protective default (#70); the one copy of
    the call (#301)."""
    # `ensure_ascii` at its default is load-bearing (#70): it escapes U+007F–U+009F and U+2028/9,
    # which the terminal-side guard does not. `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`.
    print(json.dumps(obj, indent=2))


def read_user_text(path: Path) -> str:
    """A file the user named, decoded as UTF-8, refused by name (file, codec, the way out) when it is
    not: a cp1252 brief decoded with the locale codec is mojibake that validates (#11, invariant 3)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # `display_token`: the path is untrusted input (#40).
        raise InvalidModelError(
            f"{display_token(str(path))} is not valid UTF-8 "
            f"(byte 0x{e.object[e.start]:02x} at position {e.start}). "
            f"Requivo reads and writes UTF-8 throughout, so a file in another encoding is refused "
            f"rather than decoded into text that would look like prose and be wrong. Re-save it as "
            f"UTF-8 and try again.",
            details={"path": str(path), "expected_encoding": "utf-8", "position": e.start},
        ) from None


def is_file_argument(arg: str) -> bool:
    """True if `arg` names an existing *file*: a blank string (`Path("")` is the cwd), a bare
    directory and an argument past the OS filename limit (where the check itself raises) all read as
    not a file. `is_file_argument("-")` is False; `read_source` tells the three shapes apart (#360).
    `test_discover_file_check_survives_a_real_length_request`."""
    if not arg.strip():
        return False
    try:
        return Path(arg).is_file()
    except OSError:
        return False


def read_source(arg: str) -> str:
    """A request/answers argument: an inline string, a path to a file, or `-` for stdin (#360)."""
    if arg == "-":
        return read_stdin()
    return read_user_text(Path(arg)) if is_file_argument(arg) else arg


# The old private spelling, kept for `lifecycle.py`'s import while that module was in flight (#360).
_read_source = read_source


def read_stdin() -> str:
    """Everything on stdin, as text; refused when stdin is a terminal, which would hang."""
    if sys.stdin is None or sys.stdin.isatty():
        raise InvalidModelError(
            "'-' means read from stdin, but stdin is a terminal — pipe the content in, "
            "or pass a file path instead")
    return sys.stdin.read()


def _read_document(arg: str) -> str:
    """A *document* argument: a path, or `-` for stdin, never the content itself, so a non-existent
    path is a mistake to report. Stdin spares the Claude Code skills a temp file."""
    if arg == "-":
        return read_stdin()
    p = Path(arg)
    if not p.is_file():
        raise InvalidModelError(f"no such file: {display_token(arg)} (use '-' to read from stdin)",
                                details={"path": arg})
    return read_user_text(p)


def _resolve_cards(spec: str | None) -> list[str] | None:
    """A comma-separated `--context` spec → validated card stems (None means all), through Core's `resolve_cards`."""
    return resolve_cards(spec.split(",")) if spec else None


# Said once, because the sites that need it have to agree: an entry with no error text still reads as *could not look*.
_NO_DETAIL = "no further detail"
