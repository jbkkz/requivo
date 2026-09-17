"""The process's standard streams: the one place their encoding is decided (#29).

A `print` that raises `UnicodeEncodeError` on a console codepage kills the process after the work it
was reporting has landed, so every stream gets `errors="backslashreplace"` (never `replace`, which
hides the substitution) and `encoding="utf-8"` unless `PYTHONIOENCODING` names a codec. A stream that
refuses to be reconfigured is reported per stream rather than raised or ignored; `doctor` prints it.
"""

from __future__ import annotations

import os
import sys

# What every stream gets, whatever its codec: `backslashreplace`, never `replace`.
ERRORS = "backslashreplace"

# What a stream gets when the operator has not named a codec.
PREFERRED_ENCODING = "utf-8"


def _target_encoding(stream) -> str | None:
    """The codec to move `stream` to, or None when `PYTHONIOENCODING` already named one."""
    if os.environ.get("PYTHONIOENCODING"):
        return None
    current = (getattr(stream, "encoding", "") or "").lower().replace("_", "-")
    if current in ("utf-8", "utf8"):
        return None  # already there; reconfiguring would be a no-op with a failure mode
    return PREFERRED_ENCODING


def configure_stream(stream, name: str) -> dict:
    """Make one stream unable to kill the process on a character it cannot encode. Returns a report
    in three states: `configured`, `unchanged`, `could-not` (with the reason). `could-not` keeps its
    hyphen because this report is not a `--json` output; if it ever is, respell it in the same change
    (`test_the_published_stream_states_are_all_underscore_spelled`)."""
    report = {
        "stream": name,
        "state": "could-not",
        "encoding_before": getattr(stream, "encoding", None),
        "encoding_after": None,
        "errors": getattr(stream, "errors", None),
        "reason": None,
    }
    if stream is None:
        report["reason"] = "the stream is None (pythonw, or a detached process)"
        return report
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        report["reason"] = (
            f"{type(stream).__name__} has no reconfigure(); it is not a TextIOWrapper, so its "
            f"codec is whatever substituted it"
        )
        return report
    encoding = _target_encoding(stream)
    try:
        reconfigure(encoding=encoding, errors=ERRORS)
    except (ValueError, OSError, LookupError) as e:
        # ValueError: closed or detached. OSError: unseekable. LookupError: an unknown codec name.
        report["reason"] = f"{type(e).__name__}: {e}"
        return report
    report["state"] = "configured" if encoding else "unchanged"
    report["encoding_after"] = getattr(stream, "encoding", None)
    report["errors"] = getattr(stream, "errors", None)
    if not encoding:
        report["reason"] = (
            "PYTHONIOENCODING names the codec" if os.environ.get("PYTHONIOENCODING")
            else "already UTF-8"
        )
    return report


def configure_streams() -> list:
    """Configure stdout and stderr, once, first thing in an entry point that prints; never at import.
    The callers are `cli.app()` and `golden_lib.configure_output()` (#164)."""
    return [configure_stream(sys.stdout, "stdout"), configure_stream(sys.stderr, "stderr")]


# Handlers that substitute something a reader can *see*.
_VISIBLE_ERROR_HANDLERS = frozenset({"backslashreplace", "xmlcharrefreplace", "namereplace"})

# Handlers that cannot crash but lose a character silently; `doctor` must not report them as `safe`.
_LOSSY_ERROR_HANDLERS = frozenset({"replace", "ignore"})


def describe_stream(stream, name: str) -> dict:
    """What `stream` is set to now, no side effect, in four states: `safe`, `lossy` (cannot crash,
    drops the character silently), `will_crash` (a strict handler, the #29 shape), `unknown` (no codec exposed)."""
    encoding = getattr(stream, "encoding", None)
    errors = getattr(stream, "errors", None)
    if stream is None or encoding is None:
        return {"stream": name, "state": "unknown", "encoding": None, "errors": errors,
                "detail": f"{type(stream).__name__} does not expose an encoding; this check cannot look"}
    handler = errors or "strict"
    if handler in _VISIBLE_ERROR_HANDLERS:
        return {"stream": name, "state": "safe", "encoding": encoding, "errors": errors, "detail": None}
    if handler in _LOSSY_ERROR_HANDLERS:
        return {
            "stream": name, "state": "lossy", "encoding": encoding, "errors": errors,
            "detail": f"{encoding} with errors={errors!r}: a character it cannot encode is dropped or "
                      f"blanked with no mark, so a reader cannot tell it from one that was never there",
        }
    return {
        "stream": name, "state": "will_crash", "encoding": encoding, "errors": errors,
        "detail": f"{encoding} with errors={errors!r}: a character it cannot encode raises "
                  f"UnicodeEncodeError and kills the process at the print",
    }


def describe_streams() -> list:
    return [describe_stream(sys.stdout, "stdout"), describe_stream(sys.stderr, "stderr")]


def safe_write(stream, text: str) -> None:
    """Write `text` to `stream`, never raising on a character it cannot encode: the belt for a stream
    that reported `could-not`."""
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = (getattr(stream, "encoding", None) or "ascii")
        try:
            stream.write(text.encode(encoding, ERRORS).decode(encoding, "replace"))
        except (ValueError, OSError, UnicodeError):
            # The stream can close between the first attempt and the retry.
            return
    except (ValueError, OSError):
        return  # a closed or broken stream: there is nowhere left to report to
    try:
        stream.flush()
    except (ValueError, OSError):
        pass
