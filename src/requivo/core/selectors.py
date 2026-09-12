"""The shared rule for a caller-supplied selector — the one place a token list becomes a filter.

Three selectors in this package turn caller-typed tokens into a subset of a vocabulary:
`resolve_cards` and `load_context` (context cards) and `resolve_slots` (slot ids). Two rules are
stated here, once, so a fourth selector inherits them instead of re-deriving them:

  * an **empty or whitespace token** is a refusal (invariant 3, *refuse, don't filter*). Matched, it
    widens to everything a substring test runs against; dropped, it can leave an empty selection,
    which every reader downstream spells "all of them" — and a widened selection arrives well-formed
    and calm. Each selector reached that on its own before the rule was shared:
    `test_empty_slot_token_is_refused_rather_than_matching_every_label`,
    `test_resolve_cards_refuses_an_empty_token_instead_of_returning_all_cards`, and the shared rule
    itself, `test_every_selector_refuses_an_empty_token`.
  * a token carrying a **control character** is a refusal too (#40). Every selector echoes an
    offending token into an error message, and a card name persists into `session.json` and is
    rendered by `doctor` and `session verify`, where a newline ends the line rather than looking
    odd. Refusing here means a render site cannot be handed one — the same choice `validate_slug`
    and `validate_filename` make — where escaping at the print sites would have closed the ones that
    existed and said nothing about the next. Pinned by
    `test_a_control_character_in_a_selector_token_is_refused_not_echoed`.

What a selector does with a token that is well-formed but matches nothing is its own business and
stays at the call site — the vocabularies differ (a card name is exact, a slot token is an id *or* a
label substring) and folding those into one helper would mean a flag per caller, which is three
local rules again with an import in front.

Stated precisely: the guard inspects the **stripped** token, so what a selector may echo is a token
with no *interior* control character — a leading or trailing one is normalised away by
`str.strip()`, not refused. That only adds up while each selector echoes `raw.strip()` rather than
the original, a discipline at the call site this module cannot enforce; `resolve_slots` once did
not, and `test_impact_cannot_be_made_to_print_a_line_by_an_unmatched_slot_token` is the guard.
`normalize_tokens`' own docstring states the rule so the next selector meets it.

`display_token` is the companion for the one shape that guard cannot cover: a site that *shows* a
stored token without selecting anything with it, where no refusal can run
(`test_display_token_is_the_render_side_companion_where_no_selector_runs`). Nothing makes an
arbitrary future f-string safe, and this module does not pretend otherwise — the guarantee it
offers is about the data, and the display helper is what the exceptions call.

Pure and IO-free: it reads no file and knows no vocabulary, so it stays inside core's boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from requivo.core.errors import EmptySelectorTokenError, UnsafeSelectorTokenError

# C0 (including NUL, tab, CR, LF and the ANSI escape introducer), DEL, and C1 — which carries CSI at
# U+009B, an escape introducer in its own right on terminals that decode it. Nothing wider: this is
# the class that can *move the cursor or end the line*, which is the property being guarded. Bidi
# overrides and confusable glyphs are a different question with its own issues (#11, #29) and a
# different remedy; folding them in here would make one guard answer two questions and be argued
# about as a whole.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# `display_document`'s own class (#430): the same range, minus tab (\x09) and newline (\x0a) -- the
# two control characters a document's own layout legitimately uses. CR (\x0d) stays in the guarded
# range on purpose, and `display_document` folds a CRLF *pair* to LF before this regex ever sees it,
# so what stays guarded is the lone CR that can only move a cursor (#460). Pinned by
# `test_the_same_document_renders_identically_through_generation_and_read_back`.
_DOCUMENT_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def display_token(value: str) -> str:
    """One caller-supplied token, rendered as **one line** of terminal output.

    The render-side companion to the guard in `normalize_tokens`, for the sites that show a stored
    token without selecting anything with it — `session show` prints a session's `context_cards`
    straight out of its metadata, so no selector ever runs and no refusal can reach it. There is no
    mechanism that makes an arbitrary future `print(f"…{x}…")` safe, so this is not offered as one:
    it is the thing such a site calls, named so that the reason travels with it.

    A value that is already one safe line comes back byte-for-byte, so ordinary output is unchanged
    and no reader learns a new shape for the normal case. Only a value that could break the line is
    quoted and escaped, which is the same `!r` treatment `core/integrity.py` gives the recorded
    artifact filename — its sibling untrusted field, read out of the same file.
    """
    return value if not _CONTROL_CHARS.search(value) else repr(value)


def display_document(value: str) -> str:
    """Untrusted **prose that is a document**, rendered so it cannot write a line of its own (#430),
    without collapsing the document's own layout.

    The third sibling in this family, and the reason it exists rather than reusing `display_text`:
    a saved artifact is a full markdown document -- headings, paragraphs, lists -- whose newlines and
    tabs are its layout, not incidental whitespace. `display_text` escapes every control character
    including `\\n` and `\\t`, which is exactly right for one line of prose (a question, a challenge
    headline) and exactly wrong here -- it would turn an honest multi-paragraph brief into one long
    line of visible `\\n` escapes, which is a worse outcome than the injection it guards against.

    So this neutralizes the same class `display_token`/`display_text` neutralize -- everything that
    can move a cursor or end a line on its own -- except the characters a document legitimately
    uses to lay itself out: `\\n`, `\\t`, and the `\\r\\n` that spells the first of those on Windows.
    A raw ESC, a *lone* CR, a raw form-feed and every other C0/C1 control character are still
    escaped, per character, exactly as `display_text` escapes them.

    This is `artifact show`'s own guard (#430). `artifact show` was not, in fact, the class's last
    unguarded member -- `prd`/`criteria`/`epic`/`release` printed their generator's markdown the same
    way, unguarded, on every ordinary generation rather than only a later read-back; #449 is that
    fix, at the same four print sites `_cmd_artifact_show`'s own docstring in
    `deterministic/artifacts.py` names. The saved-file bytes on disk and the web download route stay
    untouched on purpose here too (both are the byte-identical promise `core/integrity.py`'s hashing
    rests on) -- only the terminal print site calls this, at print time, so what changes is what
    reaches the operator's screen, never what is stored or downloaded.

    **A CRLF pair is layout and is folded to LF here, so this function's five callers agree** (#460).
    It used to hold CR guarded on the premise that "the read path normalises a raw CR to LF", which
    was true of the one caller that reads a file back and false of the four #449 added, which hand a
    generator's string straight in. A lone CR is still escaped: it can only move a cursor, which is
    what #430 exists to stop. Pinned by
    `test_the_same_document_renders_identically_through_generation_and_read_back`.

    Pure, like everything else here.
    """
    return _DOCUMENT_CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], value.replace("\r\n", "\n"))


def display_text(value: str) -> str:
    """Untrusted **prose**, rendered so it cannot write a line of its own (#213).

    The sibling of `display_token`, and they differ because their subjects do. A token is one word
    and is shown as one line, so quoting the whole of it when anything is wrong costs nothing and
    keeps the reader's model simple. Prose is a paragraph a user has to *read*: `repr()`-ing a
    two-hundred-character challenge because it carries one stray character would be a worse outcome
    than the injection, and would ship green, because nobody re-reads output that looks busy. So
    this escapes **per character** and leaves everything else exactly as written.

    Same class as `display_token` and `normalize_tokens`, from the same regex, and deliberately no
    wider: what is neutralized is what can move a cursor or end a line. Same escape vocabulary too --
    `repr()` of the single character, so a newline reads as a backslash-n and an escape introducer as
    a backslash-x-1-b, exactly as the token sites already spell them.

    **What it does not do, said here rather than assumed at the call site.** It makes a *value* safe;
    it does not make a renderer safe. There is no mechanism that reaches an f-string somebody writes
    next, which is why `tests/test_render_untrusted_output.py` sweeps every renderer with every field
    forged at once rather than trusting the fifteen call sites to stay complete.

    The threat this exists for is not a hostile model. A client request is untrusted business data by
    SECURITY.md's own framing, the engine turns it into prose, and the prose is printed. `streams.py`
    is no help: `backslashreplace` acts on what a console cannot *encode*, and ESC encodes fine in
    UTF-8.

    Pure, like everything else here.
    """
    return _CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], value)


def normalize_tokens(tokens: Iterable[str], *, what: str) -> list[str]:
    """Strip and lower-case caller-supplied selector tokens; refuse an empty one, and refuse one
    carrying a control character.

    Returns the tokens in the order given, stripped and lower-cased (matching is case-insensitive on
    every selector). Duplicates are kept — de-duplication is each selector's own business. An empty
    *list* is not an empty token: it means no selection was made at all, which is a legitimate state
    each caller reads for itself.

    `what` names the vocabulary being selected from, so the refusal can say what the caller was
    choosing between; the position is carried in `details` because a comma-split list is usually long
    enough that "one of these is empty" is not an actionable sentence.

    **The control-character guard inspects the token *after* stripping, and that is the whole of its
    scope — say it here rather than let a caller infer more.** `str.strip()` removes the control
    characters Python classifies as whitespace (tab, newline, vertical tab, form feed, carriage
    return, U+001C–U+001F, and NEL), so a token whose control character is leading or trailing is
    normalised away rather than refused; only an *interior* one is a refusal. That is safe **only
    because every selector echoes the stripped token**, so what a caller renders is what this
    function checked. A selector that echoed the unstripped original would leak a leading newline
    into its own error line — `resolve_slots` did exactly that, and it is why this paragraph exists.
    A new selector inherits the guard; it does not inherit that discipline, so echo `raw.strip()`.
    """
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
        # The *stripped* token is what is checked, because the stripped token is what every caller
        # echoes back into its error message — `_selection_keys` and `resolve_cards` both name the
        # unknown card as `raw.strip()`. Checking the unstripped token instead would guard a string
        # nobody renders while leaving the rendered one unexamined. `.strip()` removes surrounding
        # whitespace; an *interior* newline it does not, and that is the one #40 is about.
        #
        # Named as typed rather than as lower-cased, matching the convention the two card selectors
        # already state in a comment of their own: a reader should see the name they wrote, not the
        # key it was matched by. `.lower()` neither adds nor removes a control character, so the two
        # forms are the same guard and only the message differs.
        if _CONTROL_CHARS.search(raw):
            raise UnsafeSelectorTokenError(
                f"the {what} selector at position {position} contains a control character: {raw!r}. "
                "A name carrying a newline, a tab or an escape sequence can write its own lines into "
                "a diagnostic that reports it, so it is refused rather than displayed.",
                details={"selector": what, "position": position})
        out.append(key)
    return out
