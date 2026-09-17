"""The saved artifact Markdown, rendered as a document for the web's artifact page (#235).

A closed dialect, not a Markdown parser: the constructs `render/markdown.py` emits, enumerated below;
anything else degrades to escaped text. A checkbox marker renders as text
(`test_a_checkbox_marker_renders_as_text_and_not_as_an_input`). Escaping is structural: every text run
goes through `_inline`, which escapes before marking up
(`test_markup_smuggled_inside_every_construct_is_still_escaped`), and nothing emits a `style` attribute
or lands document text in any attribute (`test_no_inline_style_is_emitted`,
`test_an_attribute_break_out_cannot_reach_an_attribute`). Standard library only.
"""

from __future__ import annotations

import re
from html import escape

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+\.\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# The inline markers as one alternation in a single pass, code first, so a code span's contents are
# shown rather than obeyed (`test_a_marker_inside_a_code_span_is_shown_rather_than_obeyed`). Applied
# to already-escaped text. The underscore arm opens emphasis only at a word boundary, since slot ids
# carry underscores (`test_an_underscore_inside_a_word_is_not_emphasis`).
_INLINE_MARKUP = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>\S(?:[^*]*\S)?)\*\*"
    r"|(?<![\w`])_(?P<italic>\S(?:[^_]*\S)?)_(?![\w`])"
)
_INLINE_TAGS = {"code": "code", "bold": "strong", "italic": "em"}

# A pipe that is not backslash-escaped ends a cell; `_cell()` in the writer escapes a literal one.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _inline(text: str) -> str:
    """One run of document text, escaped and then marked up; the order is the security property."""
    def tag(match: re.Match) -> str:
        name = match.lastgroup
        # Every branch of `_INLINE_MARKUP` is a named group, so `lastgroup` is never None; the assert is
        # the narrowing pyright needs (#393). `test_every_inline_markup_branch_is_a_named_group_with_a_tag`.
        assert name is not None, f"an unnamed _INLINE_MARKUP branch matched {match.group(0)!r}"
        return f"<{_INLINE_TAGS[name]}>{match.group(name)}</{_INLINE_TAGS[name]}>"

    return _INLINE_MARKUP.sub(tag, escape(text, quote=True))


def _cells(line: str) -> list[str]:
    """One table row's cells, with the writer's pipe escape undone: `test_an_escaped_pipe_comes_back_as_a_pipe`."""
    body = line.strip().strip("|")
    return [part.strip().replace("\\|", "|") for part in _UNESCAPED_PIPE.split(body)]


def _table(rows: list[str]) -> list[str]:
    """A pipe table: header, rule row, body, read by position rather than content-matched, so an
    all-rule block cannot raise and a dash-only body row is not dropped.
    `test_a_pipe_block_with_no_header_degrades_instead_of_crashing`,
    `test_a_body_row_that_looks_like_a_rule_is_still_a_row`."""
    head, _rule, *body = rows
    out = ["<table>", "<thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in _cells(head)]
    out += ["</tr></thead>", "<tbody>"]
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(row)) + "</tr>")
    out += ["</tbody>", "</table>"]
    return out


def _list_items(lines: list[str], ordered: bool) -> list[str]:
    """A bullet or ordered list, nested at most one level deep; deeper indentation folds into the second."""
    tag = "ol" if ordered else "ul"
    out = [f"<{tag}>"]
    nested = False
    for line in lines:
        bullet = _BULLET.match(line)
        if bullet:
            indent, text = len(bullet.group(1)), bullet.group(2)
        else:
            # The caller collects only bullet or ordered lines, so a non-bullet is ordered; asserted by
            # name rather than an `AttributeError` (#393). `test_a_list_line_that_matches_neither_marker_is_refused_by_name`.
            item = _ORDERED.match(line)
            assert item is not None, f"a list item matched neither marker: {line!r}"
            indent, text = 0, item.group(1)
        if indent >= 2 and not nested:
            out.append(f"<{tag}>")
            nested = True
        elif indent < 2 and nested:
            out.append(f"</{tag}>")
            nested = False
        out.append(f"<li>{_inline(text)}</li>")
    if nested:
        out.append(f"</{tag}>")
    out.append(f"</{tag}>")
    return out


def markdown_to_html(text: str) -> str:
    """One saved artifact as a document: a block scanner over the closed dialect, every branch
    wrapping `_inline` output in literal tags. A fragment the template marks `safe`:
    `test_hostile_markup_in_a_saved_artifact_is_shown_not_executed`."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        if line.lstrip().startswith("> "):
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith("> "):
                quoted.append(lines[i].lstrip()[2:])
                i += 1
            out.append("<blockquote>" + _inline(" ".join(quoted)) + "</blockquote>")
            continue

        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            # A table is a header line then a rule line, in that order: `rows[1]`, not "a rule row anywhere".
            if len(rows) > 1 and _TABLE_RULE.match(rows[1]):
                out += _table(rows)
            else:
                out += [f"<p>{_inline(r)}</p>" for r in rows]
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            ordered = _ORDERED.match(line) is not None
            items = []
            while i < len(lines) and (
                    (_ORDERED.match(lines[i]) is not None) == ordered
                    and (_BULLET.match(lines[i]) or _ORDERED.match(lines[i]))):
                items.append(lines[i])
                i += 1
            out += _list_items(items, ordered)
            continue

        paragraph = []
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            paragraph.append(lines[i].strip())
            i += 1
        # A line break inside a paragraph is kept: the writer never wraps, so consecutive lines are
        # consecutive facts. `test_a_line_break_inside_a_paragraph_is_kept`.
        out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
    return "\n".join(out)


def _is_block_start(line: str) -> bool:
    """Whether a line opens a construct, so a paragraph stops before it.
    `test_an_unclosed_fence_does_not_swallow_the_document`."""
    stripped = line.lstrip()
    return bool(_HEADING.match(line) or _BULLET.match(line) or _ORDERED.match(line)
                or stripped.startswith(("> ", "|")))
