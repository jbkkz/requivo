"""Markdown → HTML for the web's artifact page: what it renders, and what it refuses to (#235).

The decision brief is this product's stated primary deliverable, and the web served it as literal
`# Decision Brief` and `**Objective:**` inside a monospace code block — Markdown source handed to the
one audience the web vocabulary exists for, at the exact moment the product delivers its value.

Its own file rather than a section of `test_render.py`, because half of what is asserted here is a
*security* property and the other half is formatting, and among eight tests about Markdown output an
injection test that stops being collected looks exactly like one that passes.

**The dialect is closed on purpose.** These documents are written by `render/markdown.py`, in this
repository, so the constructs are known: three heading levels, a blockquote, bullets one level deep,
ordered items, a pipe table, and `**bold**` / `_italic_` / `` `code` `` inline. A general Markdown
library would parse a superset — and would parse it over text a language model wrote and a user can
edit on disk. Anything outside the dialect is rendered as escaped text, which is the same thing the
`<pre>` block did and no worse than where this started.

A checkbox marker is deliberately outside that list even though the writers emit one; `render/html.py`
says why, and `test_a_checkbox_marker_renders_as_text_and_not_as_an_input` is where that decision is
held. This file used to list "checkbox items" as part of the dialect while the module it tests did
not and never implemented one — the two-docstrings-disagreeing form of the same defect class
everything else here is about, written into the very commit that added both.

**Escaping is structural, not a scrub.** Every tag in the output is one this module constructed; every
byte that came from the document went through `html.escape` first. That is why there is no separate
sanitizer to keep up to date, and why the injection tests below are about a property rather than
about a blocklist.
"""

from __future__ import annotations

import re

import pytest

from requivo.render.html import _BULLET, _INLINE_MARKUP, _INLINE_TAGS, _ORDERED, _inline, _list_items, markdown_to_html

# Anything a browser would run, fetch or lay out from bytes it did not choose.
_LIVE_MARKUP = re.compile(r"<\s*(script|iframe|object|embed|style|img|svg)\b", re.I)


def md(*lines: str) -> str:
    """A document as its lines, so the fixtures read like the Markdown they stand for."""
    return "\n".join(lines) + "\n"


# ── the dialect the generators emit ───────────────────────────────────────────

def test_headings_become_headings():
    html = markdown_to_html(md("# Decision Brief", "", "## Main risks", "", "### A challenge"))
    assert "<h1>Decision Brief</h1>" in html
    assert "<h2>Main risks</h2>" in html
    assert "<h3>A challenge</h3>" in html
    assert "#" not in html, "a heading marker reached the reader"


def test_inline_emphasis_becomes_emphasis():
    html = markdown_to_html(md("**Objective:** ship it, _eventually_, via `requivo prd`"))
    assert "<strong>Objective:</strong>" in html
    assert "<em>eventually</em>" in html
    assert "<code>requivo prd</code>" in html
    assert "**" not in html and "`" not in html


def test_a_marker_inside_a_code_span_is_shown_rather_than_obeyed():
    """A backtick span is the one place these markers are meant to be seen, so the passes must not run
    over each other's output: an earlier version let the code pass wrap the span and the bold pass
    then mark up what was inside it, showing emphasis where the author had written characters. One
    left-to-right pass over an alternation consumes each match whole."""
    html = markdown_to_html(md("Write `**not bold**` and `_not italic_` literally."))
    assert "<code>**not bold**</code>" in html
    assert "<code>_not italic_</code>" in html
    assert "<strong>" not in html and "<em>" not in html


def test_an_underscore_inside_a_word_is_not_emphasis():
    """`business_rules` and `config_vs_custom` appear in this product's own prose. Reading that
    underscore as emphasis would swallow the rest of the paragraph into an `<em>` and silently drop
    two characters the reader wrote."""
    html = markdown_to_html(md("The slot business_rules feeds config_vs_custom."))
    assert "business_rules" in html and "config_vs_custom" in html
    assert "<em>" not in html


def test_bullets_nest_one_level():
    html = markdown_to_html(md("- **A decision** — because",
                               "  - _Alternative weighed:_ the other one"))
    assert html.count("<ul>") == 2 and html.count("</ul>") == 2
    assert "<em>Alternative weighed:</em>" in html


def test_a_line_break_inside_a_paragraph_is_kept():
    """These documents never wrap, so consecutive lines are consecutive facts. A general renderer
    folds a soft break into a space, right for flowed prose -- but `render/markdown.py` emits one
    source line per value, so folding turned the decision brief's opening block into one run-on
    paragraph, on the most-read part of the primary deliverable."""
    html = markdown_to_html(md("**Objective:** ship it",
                               "**Problem:** it is not shipped",
                               "**Complexity:** medium"))
    assert html.count("<p>") == 1, "the lines are one paragraph, not three"
    assert html.count("<br>") == 2
    assert "ship it<br><strong>Problem:</strong>" in html, (
        "the break has to sit between the facts, not be dropped: " + html)


def test_a_blank_line_still_starts_a_new_paragraph():
    """Must fire for the test above. Turning every break into a `<br>` would also be satisfied by a
    renderer that emitted one giant paragraph for the whole document."""
    html = markdown_to_html(md("First fact.", "", "Second fact."))
    assert html.count("<p>") == 2 and "<br>" not in html


def test_a_blockquote_and_a_paragraph_are_told_apart():
    html = markdown_to_html(md("> generated by Requivo", "", "A plain paragraph."))
    assert "<blockquote>" in html and "generated by Requivo" in html
    assert "<p>A plain paragraph.</p>" in html
    assert "&gt;" not in html, "the blockquote marker was escaped instead of understood"


def test_a_requirements_table_becomes_a_table():
    html = markdown_to_html(md("| ID | Requirement | Priority |",
                               "|----|-------------|----------|",
                               "| R-1 | Managers approve leave | Must |"))
    assert "<table>" in html and "<th>Requirement</th>" in html
    assert "<td>Managers approve leave</td>" in html
    assert "|" not in html, "a table delimiter reached the reader"


def test_an_escaped_pipe_comes_back_as_a_pipe():
    """`_cell()` writes a literal pipe as a backslash-pipe so it cannot close the cell. Rendering that
    verbatim would show the reader an escape they never wrote."""
    html = markdown_to_html(md("| ID | Requirement |", "|----|----|",
                               r"| R-1 | approve \| reject |"))
    assert "approve | reject" in html


def test_a_pipe_block_with_no_header_degrades_instead_of_crashing():
    """The dialect's floor is *escaped text*, never an exception. A lone rule-shaped line like
    `|---|---|` -- which a hand-edited artifact file can easily carry -- used to leave the table
    renderer with nothing to unpack, and the `ValueError` went straight past every handler
    `create_app` registers (it is not a `RequivoError`): a bare 500, worse than the `<pre>` block."""
    html = markdown_to_html(md("Some notes.", "", "|---|---|", "", "More notes."))

    assert "<table>" not in html, "a block with no header is not a table"
    assert "|---|---|" in _unescape(html), (
        "the block has to survive as text — dropping it is the same absence one step quieter")
    # must fire: the surrounding document is still rendered, so this is not asserting about a
    # renderer that gave up on the whole file.
    assert "<p>Some notes.</p>" in html and "<p>More notes.</p>" in html


def test_a_body_row_that_looks_like_a_rule_is_still_a_row():
    """A row is dropped only for being in the rule's *position*, never for its contents. The
    separator used to be recognised by pattern anywhere in the block, so a genuine data row whose
    cells held only dashes -- a placeholder, an "n/a" written as `--` -- matched it and silently
    vanished from the rendered table."""
    html = markdown_to_html(md("| ID | Priority |", "|----|----------|",
                               "| R-1 | Must |", "|--|--|", "| R-3 | Should |"))

    assert html.count("<tr>") == 4, (
        "header plus three body rows; a row went missing: " + html)
    assert "R-1" in html and "R-3" in html
    assert "<td>--</td>" in html, "the placeholder row has to render as data: " + html


def _unescape(html: str) -> str:
    """Entities back to characters, so a test can ask what the reader sees rather than how it is
    spelled on the wire."""
    return html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


def test_a_checkbox_marker_renders_as_text_and_not_as_an_input():
    """`render/markdown.py` emits `- [ ] …` and `### [ ] …`, and both stay text on purpose: a real
    checkbox would mean an `<input>` carrying at least two attributes, and "this renderer emits no
    attribute anywhere" is the property `test_an_attribute_break_out_cannot_reach_an_attribute` pins.
    A prettier checkbox is not worth trading that for."""
    html = markdown_to_html(md("### [ ] SC-1 — Manager approves", "", "- [ ] the request is approved"))

    assert "<input" not in html and "=" not in html.split("<h3>")[1], (
        "a checkbox brought an attribute into a renderer that has none: " + html)
    assert "[ ] SC-1 — Manager approves" in html
    assert "<li>[ ] the request is approved</li>" in html


def test_an_ordered_step_list_becomes_an_ordered_list():
    html = markdown_to_html(md("1. Request leave", "2. Manager approves"))
    assert "<ol>" in html and "<li>Manager approves</li>" in html


# ── what it refuses to render ─────────────────────────────────────────────────
# The content is written by a language model and lives in a file the user can edit, so it is
# untrusted input by both routes this repo already recognises (invariant 14). Autoescape is off for
# this string in the template — it has to be, or the tags below would be shown rather than applied —
# so the escaping has to be complete *here*, and these are the tests that say it is.

def test_a_script_tag_in_the_content_is_shown_rather_than_run():
    html = markdown_to_html(md("## Risks", "", "<script>alert(1)</script>"))
    assert not _LIVE_MARKUP.search(html), "live markup survived into the rendered document"
    assert "&lt;script&gt;" in html, "the tag was dropped instead of shown — that loses evidence"


def test_markup_smuggled_inside_every_construct_is_still_escaped():
    """One arm per block type, because escaping is applied per text run and a construct that builds
    its own tags is exactly where a run gets forgotten. A single-arm version of this test passed
    against an implementation that escaped paragraphs and nothing else."""
    hostile = "<img src=x onerror=alert(1)>"
    document = md(
        "# " + hostile,
        "",
        "> " + hostile,
        "",
        "- " + hostile,
        "  - " + hostile,
        "",
        "1. " + hostile,
        "",
        "| ID | " + hostile + " |",
        "|----|----|",
        "| R-1 | " + hostile + " |",
        "",
        "**" + hostile + "** and `" + hostile + "` and _" + hostile + "_",
    )
    html = markdown_to_html(document)
    assert not _LIVE_MARKUP.search(html), (
        "a construct rendered attacker-chosen markup live: " + html)
    # Heading, blockquote, bullet, nested bullet, ordered item, table header cell, table body cell,
    # bold, code, italic.
    assert html.count("&lt;img") == 10, (
        "every one of the ten hostile runs has to survive as visible text — a count short means one "
        "construct dropped its run, which hides the tampering instead of showing it: " + html)


def test_an_attribute_break_out_cannot_reach_an_attribute():
    """There is no place in this output where document text lands inside an attribute, and this is
    what says so. A renderer that grew one — a heading anchor, a table `title` — would have to argue
    with this test rather than quietly open the hole."""
    html = markdown_to_html(md('# " onmouseover="alert(1)'))
    assert '="' not in html, (
        "this renderer emits no attribute anywhere, so document text has nowhere to break out of — "
        "a construct that grew one has to argue with this line: " + html)
    assert "&quot;" in html, "the quotes were dropped rather than escaped"


def test_no_inline_style_is_emitted():
    """The app's CSP is `style-src 'self'`, so an inline style is not merely untidy — it is blocked,
    and a page that logs a violation on every artifact view is a CSP nobody reads (the argument
    `base.html` already makes about htmx's indicator styles)."""
    html = markdown_to_html(md("# Title", "", "| a | b |", "|---|---|", "| 1 | 2 |",
                               "- one", "", "> quoted"))
    assert "style=" not in html


def test_an_unclosed_fence_does_not_swallow_the_document():
    """A construct outside the dialect degrades to escaped text, the same as the `<pre>` block it
    replaced, and never worse -- it must not consume everything after it. No blank line before the
    heading, deliberately: a blank line already ends a paragraph on its own, so a fixture with one
    passed identically with `_is_block_start` stubbed to `False`, guarding nothing (CLAUDE.md's
    invariant 13)."""
    html = markdown_to_html(md("# Title", "", "```", "some code", "## Still rendered"))
    assert "<h1>Title</h1>" in html
    assert "<h2>Still rendered</h2>" in html, (
        "the heading was swallowed by the paragraph that ran up to it: " + html)


def test_a_paragraph_stops_at_the_heading_that_follows_it():
    """The hazard `_is_block_start` is actually written for: no blank line between prose and the
    block after it. Without the guard the paragraph loop runs on and the heading is rendered as words
    inside it, which is the failure mode nothing else in this file can see."""
    html = markdown_to_html(md("Some paragraph text.", "## A heading right after", "- and a bullet"))
    assert "<p>Some paragraph text.</p>" in html
    assert "<h2>A heading right after</h2>" in html
    assert "<li>and a bullet</li>" in html


# -- the two narrowing invariants pyright could not see (#393) -------------------------------------
#
# `render/` was the one package directory outside `[tool.pyright]`'s scope, kept out by five
# diagnostics from two `None`-narrowing gaps that were argued to be false positives and never
# checked. Both arguments turned out to be right, and both rest on a fact stated *somewhere else* --
# one on the shape of `_INLINE_MARKUP`, one on what `markdown_to_html` collects before it calls
# `_list_items`. That is exactly the shape that goes stale silently, so each now has an `assert` at
# the site that depends on it and a test here that fires when the fact moves.


def test_every_inline_markup_branch_is_a_named_group_with_a_tag():
    """`_inline`'s callback indexes `_INLINE_TAGS` with `match.lastgroup` and never checks it for
    `None` -- sound only while every capturing group in `_INLINE_MARKUP` is *named*, since one
    unnamed branch gives `lastgroup` `None`, which before #393 meant a `KeyError`. Asserted on the
    pattern's own counts (`groups` vs `groupindex`) rather than by rendering samples, which only
    cover branches somebody thought to write."""
    assert _INLINE_MARKUP.groups == len(_INLINE_MARKUP.groupindex), (
        "an unnamed capturing group was added to _INLINE_MARKUP: `match.lastgroup` can now be None "
        f"in `_inline`'s callback (groups={_INLINE_MARKUP.groups}, "
        f"named={sorted(_INLINE_MARKUP.groupindex)})")
    assert set(_INLINE_MARKUP.groupindex) == set(_INLINE_TAGS), (
        "every named branch needs a tag to render it, and every tag needs a branch to fire it: "
        f"{sorted(set(_INLINE_MARKUP.groupindex) ^ set(_INLINE_TAGS))}")
    # must fire: the pattern really is the three-branch one the claim is about, so the equality
    # above is not two zeroes agreeing.
    assert _INLINE_MARKUP.groups == 3, sorted(_INLINE_MARKUP.groupindex)


def test_every_inline_marker_still_names_the_group_that_matched_it():
    """The runtime half of the claim above: `lastgroup` naming a group is what makes the
    `_INLINE_TAGS` lookup total -- a review finding on the change that added it, since asserting over
    `_INLINE_MARKUP.finditer` alone never enters the callback where `lastgroup` is actually read, so
    the test passed identically against the code before #393. In scope for the pyright leg since #393."""
    text = "plain `code` and **bold** and _italic_ and `**not bold**` and a_b_c"
    matched = [m.lastgroup for m in _INLINE_MARKUP.finditer(text)]
    assert None not in matched, matched
    # must fire: the fixture really exercised all three branches rather than matching nothing.
    assert set(matched) == {"code", "bold", "italic"}, matched

    rendered = _inline(text)
    assert "<code>code</code>" in rendered, rendered
    assert "<strong>bold</strong>" in rendered, rendered
    assert "<em>italic</em>" in rendered, rendered
    # The code-first ordering holds inside the span, and `a_b_c` keeps its underscores -- both are
    # the callback having chosen a branch rather than fallen through.
    assert "<code>**not bold**</code>" in rendered, rendered
    assert "a_b_c" in rendered, rendered
    # must fire: no tag anywhere carries a group that failed to name itself.
    assert "<None>" not in rendered and "None>" not in rendered, rendered


def test_a_list_line_that_matches_neither_marker_is_refused_by_name():
    """`_list_items` reads `_ORDERED.match(line).group(1)` with no `None` check, and it is right to:
    `markdown_to_html` collects a line only when `_BULLET` or `_ORDERED` matched it. The invariant
    belongs to the *caller*, which is why the assert is worth having -- a second caller handing it
    arbitrary lines used to get an `AttributeError` naming neither the line nor the rule it broke.
    Called directly on purpose, since no document reaches this state through `markdown_to_html`."""
    with pytest.raises(AssertionError, match="matched neither marker"):
        _list_items(["not a list item at all"], ordered=True)


def test_the_two_list_markers_between_them_cover_every_line_the_collector_gathers():
    """The must-fire half: the assert above must not be the only thing standing, and the ordinary
    shapes have to keep reaching `_list_items` cleanly. Both nesting levels and both marker
    characters, since `_BULLET` accepts `-` and `*` and `_ORDERED` accepts any run of digits."""
    for line in ("- a", "* b", "  - nested", "1. one", "10. ten", "   2. nested ordered"):
        assert _BULLET.match(line) or _ORDERED.match(line), line
    html = markdown_to_html(md("- a", "* b", "  - nested"))
    assert html.count("<li>") == 3, html
    ordered = markdown_to_html(md("1. one", "10. ten"))
    assert ordered.startswith("<ol>") and ordered.count("<li>") == 2, ordered
