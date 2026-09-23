"""The renderers: data → string, no side effects (#72); Markdown → HTML for the web's artifact page (#235)."""
from __future__ import annotations

import re

import pytest
from _fakes import out, printed, slot

from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    DesignDecision,
    Epic,
    Exclusion,
    Leverage,
    Opportunity,
    ReleaseNotes,
    Threshold,
)
from requivo.render.html import _BULLET, _INLINE_MARKUP, _INLINE_TAGS, _ORDERED, _inline, _list_items, markdown_to_html
from requivo.render.markdown import brief_markdown, criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.render.terminal import render_brief

_MODEL = {"problem": slot(80, "explicit", "high")}


# ── the Markdown writers ─────────────────────────────────────────────────────────


def test_prd_markdown_renders_title_and_requirement_table_with_pipes_escaped():
    prd = PRD(title="Leave approval", problem="Approvals are lost in email.", requirements=[
        {"id": "FR-1", "requirement": "Submit a request", "priority": "must"},
        {"id": "FR-2", "requirement": "Export as CSV | XLSX | PDF", "priority": "must"}])
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval") and "| FR-1 | Submit a request | Must |" in md
    assert "| FR-2 | Export as CSV \\| XLSX \\| PDF | Must |" in md, "a literal pipe would split the row"


def test_prd_markdown_shows_each_envelope_element_s_own_provenance():
    """#603: a slot-sourced element names the slot's label, an assumed one says so, an empty envelope renders nothing."""
    md = prd_markdown(PRD(title="X", problem="P", envelope=[
        {"kind": "Budget", "value": "50k", "origin": "slot", "source_slot": "constraints"},
        {"kind": "Team size", "value": "3 developers", "origin": "assumption"}]))
    assert "## Resource envelope" in md
    assert "**Budget** — 50k _(stated in Constraints)_" in md
    assert "**Team size** — 3 developers _(assumption — not stated in the model)_" in md
    assert "Resource envelope" not in prd_markdown(PRD(title="X", problem="P"))


def test_criteria_markdown_renders_gherkin_checklist():
    ac = AcceptanceCriteria(title="Leave approval", features=[{"name": "Submitting a request", "scenarios": [
        {"id": "AC-1", "title": "Valid request is accepted", "kind": "happy_path",
         "given": ["the employee is logged in", "they have enough balance"],
         "when": "they submit a 3-day request", "then": ["the request is created", "the manager is notified"]}]}],
        open_questions=["Can a manager approve their own request?"])
    md = criteria_markdown(ac)
    assert md.startswith("# Leave approval") and "### [ ] AC-1 — Valid request is accepted  _Happy path_" in md
    # First given is "Given", subsequent ones fold to "And"; likewise Then → And.
    for line in ("- **Given** the employee is logged in", "- **And** they have enough balance",
                 "- **When** they submit a 3-day request", "- **Then** the request is created",
                 "- **And** the manager is notified", "## Open questions"):
        assert line in md


def test_epic_markdown_renders_issues_with_labels_and_deps():
    epic = Epic(title="Leave approval", milestone="Pilot", goal="Let employees request leave.", issues=[
        {"id": "#1", "title": "Model the leave object", "description": "Fields and states.", "labels": ["backend"]},
        {"id": "#2", "title": "Build approval circuit", "description": "Route to manager.",
         "labels": ["feature", "backend"], "depends_on": ["#1"]}], open_questions=["Half-day support?"])
    md = epic_markdown(epic)
    for expected in ("# Epic: Leave approval", "**Milestone:** Pilot", "### [ ] #1 — Model the leave object",
                     "**Labels:** `feature`, `backend` · **Depends on:** #1", "## Open questions"):
        assert expected in md
    assert md.startswith("# Epic: Leave approval")


def test_release_markdown_stamps_version_and_sections():
    rn = ReleaseNotes(title="Leave approval", version="v1.0", summary="Your team can now request leave online.",
                      highlights=["Submit a request in a few clicks"], known_limitations=["Payroll export is not included yet"],
                      notes=["An administrator sets the approval circuit first"])
    md = release_markdown(rn)
    assert md.startswith("# Leave approval — v1.0")
    for expected in ("Your team can now request leave online.", "## What's new", "## Not included yet", "## Before you start"):
        assert expected in md
    unversioned = release_markdown(ReleaseNotes(title="Leave approval", highlights=["A"]))
    assert unversioned.startswith("# Leave approval\n") and "—" not in unversioned.splitlines()[0]


# ── the decision brief ───────────────────────────────────────────────────────────


def test_render_brief_titles_decision_brief_and_shows_challenges_decisions_and_opportunities():
    brief = Brief(problem="P", solution="S", complexity="high",
                  decisions=[DesignDecision(decision="Draft-first invoices reviewed before issuance", why="Finance sign-off.",
                                            alternative="Immediate issuance.", tradeoff="Extra step, lower risk."),
                             DesignDecision(decision="Amount sourced from the Contract")],  # bare fact, no fork
                  challenges=[Challenge(headline="Invoice at signature", premise="Invoices follow the signature.",
                                        alternative="Invoice on a billing schedule.", consequence="More credit notes.",
                                        recommendation="Validate the trigger with Finance first.")],
                  opportunities=[Opportunity(text="Generalize the approval circuit.", leverage=Leverage.high,
                                             modules=["Absence", "Contracts", "Missions"]),
                                 Opportunity(text="Add a dashboard later.", leverage=Leverage.future)])
    text = printed(render_brief, out(_MODEL), brief)
    # One vocabulary for one artifact (#166); the forked decision shows its reasoning, the bare fact stays one line;
    # a grounded opportunity names the modules it reaches and an ungrounded one shows no ↳ line.
    for expected in ("DECISION BRIEF", "CHALLENGES", "Challenge Invoice at signature", "⚑ Invoice at signature", "Premise",
                     "Alternative", "Recommend", "DESIGN DECISIONS", "✓ Draft-first invoices reviewed before issuance", "Why",
                     "Tradeoff", "✓ Amount sourced from the Contract", "↳ reaches: Absence, Contracts, Missions", "Add a dashboard later."):
        assert expected in text, expected
    assert "SOLUTION ASSESSMENT" not in text and "DECISION LOG" not in text and text.count("↳ reaches:") == 1


def test_a_newline_in_a_reasoning_item_cannot_open_a_forged_heading_in_the_brief():
    """Self-review finding on #599."""
    model = out(_MODEL)
    model.exclusions = [Exclusion(option="X\n# INJECTED", reason="r\n# INJECTED")]
    model.thresholds = [Threshold(condition="C\n# INJECTED", measure="m", action="a\n# INJECTED", rests_on=["problem"])]
    brief = Brief(problem="P", solution="S", complexity="low", decisions=[DesignDecision(decision="D\n# INJECTED")],
                  challenges=[Challenge(headline="H\n# INJECTED", premise="p", alternative="a", consequence="c", recommendation="r")],
                  opportunities=[Opportunity(text="O\n# INJECTED", leverage="high")])
    md = brief_markdown(model, brief)
    assert "\n# INJECTED" not in md
    # 7, not 5: the exclusion and the threshold each carry it in two fields, the other three once each.
    assert md.count("INJECTED") == 7


@pytest.mark.parametrize("field, item, heading, line", [
    ("exclusions", Exclusion(option="Bulk import", reason="Out of scope for v1", rests_on=["problem"]),
     "## Out of scope", "**Bulk import** — Out of scope for v1"),
    ("thresholds", Threshold(condition="CAC exceeds the stated budget ceiling", measure="cost per paid signup",
                             action="stop the paid channel", rests_on=["problem"]),
     "## Decision thresholds", "**CAC exceeds the stated budget ceiling** → stop the paid channel"),
], ids=["exclusions-599", "thresholds-604"])
def test_the_decision_brief_projects_reasoning_items_rather_than_writing_them(field, item, heading, line):
    """#599, #604: the brief's "Out of scope" and "Decision thresholds" are projections of the model, not provider prose."""
    model = out(_MODEL)
    setattr(model, field, [item])
    md = brief_markdown(model, Brief(problem="P", solution="S", complexity="low"))
    assert heading in md and line in md and "rests on: Real problem" in md


def _bilingual_brief() -> str:
    """The brief is bilingual by construction (#277): four sections project the model's own words."""
    model = out({"problem": slot(90, "explicit", "high", "Les congés sont validés par courriel.")})
    model.summary.objective = "Gérer les congés des employés."
    model.summary.scope = "Un portail où chaque salarié dépose sa demande."
    model.summary.assumptions = ["Les managers valident sous 48 heures."]
    return brief_markdown(model, Brief(complexity="low", problem="Leave approvals are inconsistent.", solution="A single approval workflow."))


def test_the_decision_brief_projects_the_models_own_words_rather_than_restating_them():
    md = _bilingual_brief()
    for verbatim in ("**Objective:** Gérer les congés des employés.", "Un portail où chaque salarié dépose sa demande.",
                     "Les congés sont validés par courriel.", "Les managers valident sous 48 heures."):
        assert verbatim in md


def test_the_decision_briefs_english_anchor_covers_the_judgment_the_provider_wrote():
    md = _bilingual_brief()
    assert "**Problem:** Leave approvals are inconsistent." in md and "**Solution:** A single approval workflow." in md


# ── Markdown → HTML: the dialect the generators emit (#235) ──────────────────────

# Anything a browser would run, fetch or lay out from bytes it did not choose.
_LIVE_MARKUP = re.compile(r"<\s*(script|iframe|object|embed|style|img|svg)\b", re.I)


def md(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _unescape(html: str) -> str:
    return html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


@pytest.mark.parametrize("document, present, absent", [
    (md("# Decision Brief", "", "## Main risks", "", "### A challenge"),
     ["<h1>Decision Brief</h1>", "<h2>Main risks</h2>", "<h3>A challenge</h3>"], ["#"]),
    (md("**Objective:** ship it, _eventually_, via `requivo prd`"),
     ["<strong>Objective:</strong>", "<em>eventually</em>", "<code>requivo prd</code>"], ["**", "`"]),
    (md("- **A decision** — because", "  - _Alternative weighed:_ the other one"),
     ["<ul>", "</ul>", "<em>Alternative weighed:</em>"], []),
    (md("First fact.", "", "Second fact."), ["<p>First fact.</p>", "<p>Second fact.</p>"], ["<br>"]),
    (md("> generated by Requivo", "", "A plain paragraph."), ["<blockquote>", "generated by Requivo", "<p>A plain paragraph.</p>"], ["&gt;"]),
    (md("| ID | Requirement | Priority |", "|----|-------------|----------|", "| R-1 | Managers approve leave | Must |"),
     ["<table>", "<th>Requirement</th>", "<td>Managers approve leave</td>"], ["|"]),
    (md("1. Request leave", "2. Manager approves"), ["<ol>", "<li>Manager approves</li>"], []),
    (md("Some paragraph text.", "## A heading right after", "- and a bullet"),
     ["<p>Some paragraph text.</p>", "<h2>A heading right after</h2>", "<li>and a bullet</li>"], []),
], ids=["headings", "inline-emphasis", "nested-bullets", "blank-line-paragraphs", "blockquote", "table", "ordered-list",
        "paragraph-stops-at-heading"])
def test_each_construct_of_the_dialect_renders_as_its_element(document, present, absent):
    html = markdown_to_html(document)
    for tag in present:
        assert tag in html, html
    for marker in absent:
        assert marker not in html, f"{marker!r} reached the reader: {html}"


def test_a_marker_inside_a_code_span_is_shown_rather_than_obeyed():
    html = markdown_to_html(md("Write `**not bold**` and `_not italic_` literally."))
    assert "<code>**not bold**</code>" in html and "<code>_not italic_</code>" in html and "<strong>" not in html and "<em>" not in html


def test_an_underscore_inside_a_word_is_not_emphasis():
    html = markdown_to_html(md("The slot business_rules feeds config_vs_custom."))
    assert "business_rules" in html and "config_vs_custom" in html and "<em>" not in html


def test_a_line_break_inside_a_paragraph_is_kept():
    """These documents never wrap, so consecutive lines are consecutive facts; a blank line still splits (must-fire above)."""
    html = markdown_to_html(md("**Objective:** ship it", "**Problem:** it is not shipped", "**Complexity:** medium"))
    assert html.count("<p>") == 1 and html.count("<br>") == 2
    assert "ship it<br><strong>Problem:</strong>" in html, html


def test_an_escaped_pipe_comes_back_as_a_pipe():
    assert "approve | reject" in markdown_to_html(md("| ID | Requirement |", "|----|----|", r"| R-1 | approve \| reject |"))


def test_a_pipe_block_with_no_header_degrades_instead_of_crashing():
    """The dialect's floor is escaped text, never an exception; the surrounding document still renders."""
    html = markdown_to_html(md("Some notes.", "", "|---|---|", "", "More notes."))
    assert "<table>" not in html and "|---|---|" in _unescape(html) and "<p>Some notes.</p>" in html and "<p>More notes.</p>" in html


def test_a_body_row_that_looks_like_a_rule_is_still_a_row():
    """A row is dropped only for being in the rule's position, never for its contents."""
    html = markdown_to_html(md("| ID | Priority |", "|----|----------|", "| R-1 | Must |", "|--|--|", "| R-3 | Should |"))
    assert html.count("<tr>") == 4 and "R-1" in html and "R-3" in html, html
    assert "<td>--</td>" in html, html


def test_a_checkbox_marker_renders_as_text_and_not_as_an_input():
    html = markdown_to_html(md("### [ ] SC-1 — Manager approves", "", "- [ ] the request is approved"))
    assert "<input" not in html and "=" not in html.split("<h3>")[1], html
    assert "[ ] SC-1 — Manager approves" in html and "<li>[ ] the request is approved</li>" in html


# ── what it refuses to render: the content is model-written and user-editable ────


def test_markup_smuggled_inside_every_construct_is_still_escaped():
    """One arm per block type, because escaping is applied per text run and a construct that builds its own tags is where a run gets forgotten."""
    hostile = "<img src=x onerror=alert(1)>"
    document = md("# " + hostile, "", "> " + hostile, "", "- " + hostile, "  - " + hostile, "", "1. " + hostile, "",
                  "| ID | " + hostile + " |", "|----|----|", "| R-1 | " + hostile + " |", "",
                  "**" + hostile + "** and `" + hostile + "` and _" + hostile + "_")
    html = markdown_to_html(document)
    assert not _LIVE_MARKUP.search(html), "a construct rendered attacker-chosen markup live: " + html
    # Heading, blockquote, bullet, nested bullet, ordered item, table header cell, table body cell, bold, code, italic.
    assert html.count("&lt;img") == 10, "a count short means one construct dropped its run: " + html
    script = markdown_to_html(md("## Risks", "", "<script>alert(1)</script>"))
    assert not _LIVE_MARKUP.search(script) and "&lt;script&gt;" in script, "dropped instead of shown loses evidence"


def test_an_attribute_break_out_cannot_reach_an_attribute():
    html = markdown_to_html(md('# " onmouseover="alert(1)'))
    assert '="' not in html and "&quot;" in html, "no attribute anywhere, and the quotes escaped rather than dropped: " + html


def test_no_inline_style_is_emitted():
    # The app's CSP is `style-src 'self'`, so an inline style is not merely untidy.
    assert "style=" not in markdown_to_html(md("# Title", "", "| a | b |", "|---|---|", "| 1 | 2 |", "- one", "", "> quoted"))


def test_an_unclosed_fence_does_not_swallow_the_document():
    html = markdown_to_html(md("# Title", "", "```", "some code", "## Still rendered"))
    assert "<h1>Title</h1>" in html and "<h2>Still rendered</h2>" in html, html


# ── the two narrowing invariants pyright could not see (#393) ───────────────────


def test_every_inline_markup_branch_is_a_named_group_with_a_tag():
    """`_inline`'s callback indexes `_INLINE_TAGS` with `match.lastgroup` and never checks it for `None`."""
    assert _INLINE_MARKUP.groups == len(_INLINE_MARKUP.groupindex), sorted(_INLINE_MARKUP.groupindex)
    assert set(_INLINE_MARKUP.groupindex) == set(_INLINE_TAGS), sorted(set(_INLINE_MARKUP.groupindex) ^ set(_INLINE_TAGS))
    assert _INLINE_MARKUP.groups == 3, sorted(_INLINE_MARKUP.groupindex)  # must fire: the three-branch pattern
    text = "plain `code` and **bold** and _italic_ and `**not bold**` and a_b_c"
    matched = [m.lastgroup for m in _INLINE_MARKUP.finditer(text)]
    assert None not in matched and set(matched) == {"code", "bold", "italic"}, matched
    rendered = _inline(text)
    for expected in ("<code>code</code>", "<strong>bold</strong>", "<em>italic</em>", "<code>**not bold**</code>", "a_b_c"):
        assert expected in rendered, rendered
    assert "None>" not in rendered, rendered


def test_a_list_line_that_matches_neither_marker_is_refused_by_name():
    """`_list_items` reads `_ORDERED.match(line).group(1)` with no `None` check, and the two markers cover every gathered line."""
    with pytest.raises(AssertionError, match="matched neither marker"):
        _list_items(["not a list item at all"], ordered=True)
    for line in ("- a", "* b", "  - nested", "1. one", "10. ten", "   2. nested ordered"):
        assert _BULLET.match(line) or _ORDERED.match(line), line
    assert markdown_to_html(md("- a", "* b", "  - nested")).count("<li>") == 3
    ordered = markdown_to_html(md("1. one", "10. ten"))
    assert ordered.startswith("<ol>") and ordered.count("<li>") == 2, ordered
