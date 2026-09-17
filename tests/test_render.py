"""The renderers: data → string, no side effects (#72)."""
import io
from contextlib import redirect_stdout

from _fakes import out, slot

from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    DesignDecision,
    Epic,
    Leverage,
    Opportunity,
    ReleaseNotes,
)
from requivo.render.markdown import brief_markdown, criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.render.terminal import render_brief


def test_prd_markdown_renders_title_and_requirement_table():
    prd = PRD(
        title="Leave approval",
        problem="Approvals are lost in email.",
        requirements=[{"id": "FR-1", "requirement": "Submit a request", "priority": "must"}],
    )
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "| FR-1 | Submit a request | Must |" in md


def test_prd_markdown_escapes_pipes_in_table_cells():
    # A requirement containing a literal | would otherwise split the Markdown table row.
    prd = PRD(title="X", problem="P", requirements=[
        {"id": "FR-1", "requirement": "Export as CSV | XLSX | PDF", "priority": "must"}])
    md = prd_markdown(prd)
    assert "| FR-1 | Export as CSV \\| XLSX \\| PDF | Must |" in md


def test_prd_markdown_shows_each_envelope_element_s_own_provenance():
    """#603: a slot-sourced element names the slot's human label; an assumed one says so plainly."""
    prd = PRD(title="X", problem="P", envelope=[
        {"kind": "Budget", "value": "50k", "origin": "slot", "source_slot": "constraints"},
        {"kind": "Team size", "value": "3 developers", "origin": "assumption"},
    ])
    md = prd_markdown(prd)
    assert "## Resource envelope" in md
    assert "**Budget** — 50k _(stated in Constraints)_" in md
    assert "**Team size** — 3 developers _(assumption — not stated in the model)_" in md


def test_prd_markdown_omits_the_envelope_section_when_it_is_empty():
    # Acceptance criterion (#603): nothing invented means nothing rendered, not an empty heading.
    md = prd_markdown(PRD(title="X", problem="P"))
    assert "Resource envelope" not in md


def test_criteria_markdown_renders_gherkin_checklist():
    ac = AcceptanceCriteria(
        title="Leave approval",
        features=[
            {
                "name": "Submitting a request",
                "scenarios": [
                    {
                        "id": "AC-1",
                        "title": "Valid request is accepted",
                        "kind": "happy_path",
                        "given": ["the employee is logged in", "they have enough balance"],
                        "when": "they submit a 3-day request",
                        "then": ["the request is created", "the manager is notified"],
                    }
                ],
            }
        ],
        open_questions=["Can a manager approve their own request?"],
    )
    md = criteria_markdown(ac)
    assert md.startswith("# Leave approval")
    assert "### [ ] AC-1 — Valid request is accepted  _Happy path_" in md
    # First given is "Given", subsequent ones fold to "And"; likewise Then → And.
    assert "- **Given** the employee is logged in" in md
    assert "- **And** they have enough balance" in md
    assert "- **When** they submit a 3-day request" in md
    assert "- **Then** the request is created" in md
    assert "- **And** the manager is notified" in md
    assert "## Open questions" in md


def test_epic_markdown_renders_issues_with_labels_and_deps():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Let employees request leave and managers approve it.",
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields and states.",
             "labels": ["backend"]},
            {"id": "#2", "title": "Build approval circuit", "description": "Route to manager.",
             "labels": ["feature", "backend"], "depends_on": ["#1"]},
        ],
        open_questions=["Half-day support?"],
    )
    md = epic_markdown(epic)
    assert md.startswith("# Epic: Leave approval")
    assert "**Milestone:** Pilot" in md
    assert "### [ ] #1 — Model the leave object" in md
    assert "**Labels:** `feature`, `backend` · **Depends on:** #1" in md
    assert "## Open questions" in md


def test_release_markdown_stamps_version_and_sections():
    rn = ReleaseNotes(
        title="Leave approval",
        version="v1.0",
        summary="Your team can now request and approve leave online.",
        highlights=["Submit a request in a few clicks"],
        known_limitations=["Payroll export is not included yet"],
        notes=["An administrator sets the approval circuit first"],
    )
    md = release_markdown(rn)
    assert md.startswith("# Leave approval — v1.0")
    assert "Your team can now request and approve leave online." in md
    assert "## What's new" in md
    assert "## Not included yet" in md
    assert "## Before you start" in md


def test_release_markdown_omits_version_when_empty():
    md = release_markdown(ReleaseNotes(title="Leave approval", highlights=["A"]))
    assert md.startswith("# Leave approval\n")
    assert "—" not in md.splitlines()[0]


def test_render_brief_titles_decision_brief_and_shows_challenges():
    model = {"problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        decisions=[
            DesignDecision(
                decision="Draft-first invoices reviewed before issuance",
                why="Finance sign-off is required.",
                alternative="Immediate issuance.",
                tradeoff="Extra step, lower compliance risk.",
            ),
            DesignDecision(decision="Amount sourced from the Contract"),  # bare fact, no fork
        ],
        challenges=[
            Challenge(
                headline="Invoice at signature",
                premise="Invoices are generated the moment a contract is signed.",
                alternative="Many teams invoice at the contract start date or on a billing schedule.",
                consequence="Signature-triggered invoicing multiplies credit-note handling.",
                recommendation="Validate the billing trigger with Finance first.",
            )
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    # One vocabulary for one artifact (#166): the caption is "decision brief" wherever a person reads it.
    assert "DECISION BRIEF" in text and "SOLUTION ASSESSMENT" not in text
    assert "CHALLENGES" in text
    # the top challenge surfaces in the executive summary, detail in the full analysis
    assert "Challenge Invoice at signature" in text
    assert "⚑ Invoice at signature" in text  # full-analysis section
    assert "Premise" in text and "Alternative" in text and "Recommend" in text
    # Design decisions: the forked one shows its reasoning, the bare fact stays a single line.
    assert "DESIGN DECISIONS" in text and "DECISION LOG" not in text
    assert "✓ Draft-first invoices reviewed before issuance" in text
    assert "Why" in text and "Tradeoff" in text
    assert "✓ Amount sourced from the Contract" in text


def test_a_newline_in_a_reasoning_item_cannot_open_a_forged_heading_in_the_brief():
    """Self-review finding on #599."""
    from requivo.core.contracts import Exclusion, Threshold

    model = out({"problem": slot(80, "explicit", "high")})
    model.exclusions = [Exclusion(option="X\n# INJECTED", reason="r\n# INJECTED")]
    model.thresholds = [Threshold(condition="C\n# INJECTED", measure="m",
                                  action="a\n# INJECTED", rests_on=["problem"])]
    brief = Brief(problem="P", solution="S", complexity="low",
                 decisions=[DesignDecision(decision="D\n# INJECTED")],
                 challenges=[Challenge(headline="H\n# INJECTED", premise="p", alternative="a",
                                       consequence="c", recommendation="r")],
                 opportunities=[Opportunity(text="O\n# INJECTED", leverage="high")])
    md = brief_markdown(model, brief)
    assert "\n# INJECTED" not in md
    # 7, not 5: the exclusion and the threshold each carry it in two fields, the other three once each.
    assert md.count("INJECTED") == 7


def test_the_decision_brief_projects_excluded_options_rather_than_writing_them():
    """#599 acceptance criterion: the brief's "Out of scope" content is a projection of `out.exclusions`."""
    from requivo.core.contracts import Exclusion

    model = out({"problem": slot(80, "explicit", "high")})
    model.exclusions = [Exclusion(option="Bulk import", reason="Out of scope for v1",
                                  rests_on=["problem"])]
    brief = Brief(problem="P", solution="S", complexity="low")
    md = brief_markdown(model, brief)
    assert "## Out of scope" in md
    assert "**Bulk import** — Out of scope for v1" in md
    assert "rests on: Real problem" in md


def test_the_decision_brief_projects_decision_thresholds_rather_than_writing_them():
    """#604 acceptance criterion, mirroring #599: the brief's "Decision thresholds" content is a projection of
    `out.thresholds`, not prose the provider wrote into `Brief`."""
    from requivo.core.contracts import Threshold

    model = out({"problem": slot(80, "explicit", "high")})
    model.thresholds = [Threshold(condition="CAC exceeds the stated budget ceiling",
                                  measure="cost per paid signup", action="stop the paid channel",
                                  rests_on=["problem"])]
    brief = Brief(problem="P", solution="S", complexity="low")
    md = brief_markdown(model, brief)
    assert "## Decision thresholds" in md
    assert "**CAC exceeds the stated budget ceiling** → stop the paid channel" in md
    assert "rests on: Real problem" in md


def test_render_brief_opportunity_names_reached_modules():
    model = {"problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        opportunities=[
            Opportunity(
                text="Generalize the approval circuit.",
                leverage=Leverage.high,
                modules=["Absence", "Contracts", "Missions"],
            ),
            Opportunity(text="Add a dashboard later.", leverage=Leverage.future),  # no modules
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    # a grounded opportunity names the modules it reaches; an ungrounded one shows no ↳ line
    assert "↳ reaches: Absence, Contracts, Missions" in text
    assert "Add a dashboard later." in text
    assert text.count("↳ reaches:") == 1


# ── The decision brief is bilingual by construction (#277) ────────────────────
# `docs/requirements-model.md` puts the six saved artifacts on the English side of the output-language policy.
#
# `brief_markdown` is the exception, and the exception was documented as an anchor for a whole release candidate: it takes an `EngineOutput` as well as its `Brief`, and four of its sections are a *projection* of that model — the objective, the current understanding, each slot's stated value under "What is confirmed", and the first half of "Important assumptions" (#277).
#
# The two tests below hold the shape the page now describes rather than the shape it wished for.

def _bilingual_brief() -> str:
    model = out({"problem": {"completeness": 90, "confidence": "explicit", "impact": "high",
                             "value": "Les congés sont validés par courriel."}})
    model.summary.objective = "Gérer les congés des employés."
    model.summary.scope = "Un portail où chaque salarié dépose sa demande."
    model.summary.assumptions = ["Les managers valident sous 48 heures."]
    return brief_markdown(model, Brief(complexity="low", problem="Leave approvals are inconsistent.",
                                       solution="A single approval workflow."))


def test_the_decision_brief_projects_the_models_own_words_rather_than_restating_them():
    md = _bilingual_brief()
    # Copied through verbatim, from all four projected sections.
    assert "**Objective:** Gérer les congés des employés." in md
    assert "Un portail où chaque salarié dépose sa demande." in md
    assert "Les congés sont validés par courriel." in md
    assert "Les managers valident sous 48 heures." in md


def test_the_decision_briefs_english_anchor_covers_the_judgment_the_provider_wrote():
    md = _bilingual_brief()
    assert "**Problem:** Leave approvals are inconsistent." in md
    assert "**Solution:** A single approval workflow." in md
