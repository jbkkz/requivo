"""The shared refusal rule for a caller-supplied selector token, used by `resolve_cards`,
`load_context` and `resolve_slots`: empty/whitespace is refused, not matched-or-dropped (invariant
3); a control character is refused, not echoed, checked on the **stripped** token so a selector
must echo `raw.strip()` (#40). Pinned by `test_every_selector_refuses_an_empty_token` and
`test_a_control_character_in_a_selector_token_is_refused_not_echoed`. `display_token`/`display_text`/
`display_document` are the render-side companions where no selector runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from requivo.core.errors import EmptySelectorTokenError, UnsafeSelectorTokenError

# C0/DEL/C1 -- what can move a cursor or end a line (not bidi/confusables, #11/#29).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# display_document's class (#430): the same range minus tab/newline, a document's own layout.
_DOCUMENT_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def display_token(value: str) -> str:
    """One caller-supplied token as one line -- unchanged if safe, quoted (`!r`) if not (#40)."""
    return value if not _CONTROL_CHARS.search(value) else repr(value)


def display_document(value: str) -> str:
    """A document's prose, escaped per character without collapsing its own layout (#430, #460)."""
    return _DOCUMENT_CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], value.replace("\r\n", "\n"))


def display_text(value: str) -> str:
    """One line of untrusted prose, escaped per character rather than `repr()`-ed whole (#213)."""
    return _CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], value)


def normalize_tokens(tokens: Iterable[str], *, what: str) -> list[str]:
    """Strip/lower-case selector tokens; refuse an empty one or an interior control character
    (module docstring). `what` names the vocabulary for the message."""
    out: list[str] = []
    for position, token in enumerate(tokens):
        raw = token.strip()
        key = raw.lower()
        if not key:
            raise EmptySelectorTokenError(
                f"empty {what} selector at position {position} — an empty token matches everything, "
                f"so it would widen the selection instead of narrowing it. Remove it (a stray comma "
                f"is the usual cause), or pass no selector at all to select everything deliberately.",
                details={"selector": what, "position": position})
        if _CONTROL_CHARS.search(raw):
            raise UnsafeSelectorTokenError(
                f"the {what} selector at position {position} contains a control character: {raw!r}. "
                "A name carrying a newline, a tab or an escape sequence can write its own lines into "
                "a diagnostic that reports it, so it is refused rather than displayed.",
                details={"selector": what, "position": position})
        out.append(key)
    return out
