from __future__ import annotations

import functools
import re
from collections.abc import Sequence

from requivo.core.analysis import readiness_blockers, slot_label, slot_meta, stated_and_defaults
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    Confidence,
    EngineOutput,
    EnvelopeElement,
    EnvelopeOrigin,
    Epic,
    EstimateDraft,
    GoToMarketPlan,
    ReleaseNotes,
    ScenarioKind,
    Stories,
    schema_slots,
)
from requivo.core.perimeters import DEFAULT_PERIMETER, GO_TO_MARKET

_KIND_TAG = {
    ScenarioKind.happy_path: "Happy path",
    ScenarioKind.edge_case: "Edge case",
    ScenarioKind.error: "Error",
    ScenarioKind.permission: "Permission",
}


def _cell(text: str) -> str:
    """Make text safe for a table cell: escape a literal `|`, flatten a newline."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _line(text: str) -> str:
    """Keep provider free text on one line: a newline inside a title would open a second heading when
    `markdown_to_html` re-parses the line. `test_a_newline_inside_a_provider_field_cannot_open_a_new_heading`."""
    return " ".join(text.split())


_BLOCK_MARKER = re.compile(
    r"^( {0,3})(#{1,6}(?:\s|$)|[>*+-](?:\s|$)|\d+[.)](?:\s|$)|\||`{3,}|~{3,}|<)",
    re.MULTILINE,
)


def _block_text(text: str) -> str:
    """Escape every provider-authored line that begins with a Markdown block marker (#686)."""
    return _BLOCK_MARKER.sub(r"\1\\\2", text)


def _block_line(text: str) -> str:
    """Flatten provider text at the start of a Markdown line and escape a block marker (#686)."""
    return _block_text(_line(text))


def _stated(out: EngineOutput, confidence: Confidence, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """The values stated at a given provenance, in `perimeter`'s schema order, as business statements:
    read off the model, never restated by the provider, never framed by a slot label (#783, Voice rule).
    A default the client did not state is never among them, claim by claim: `_defaults` lists it (#809)."""
    slots = [out.model[sid] for sid in slot_meta(perimeter)[1] if sid in out.model]
    said = [t for s in slots if s.confidence is confidence
            for t in (stated_and_defaults(s)[0] if confidence is Confidence.explicit else [s.value])]
    return [f"- {_line(t)}" for t in said if t.strip()]


_DEFAULT_TAG = {"delegated": "you left this decision to us", "let_stand": "proposed, and you let it stand"}


def _defaults(out: EngineOutput, perimeter: str) -> list[str]:
    """The settled values the client did not state, in schema order, each with its source (#809)."""
    slots = [out.model[sid] for sid in slot_meta(perimeter)[1] if sid in out.model]
    return [f"- {_line(text)} _({_DEFAULT_TAG[source]})_"
            for s in slots for text, source in stated_and_defaults(s)[1] if text.strip()]


def _in_words(out: EngineOutput, sid: str, perimeter: str) -> str:
    """What a readiness blocker leaves to decide, as a question in the client's terms (#783, #809): the
    session's own question on it, else the schema's probe; never its stored value under a tool prefix."""
    asked = next((q.q for q in out.questions if q.slot == sid), "")
    probe = next((p.get("probe", "") for p in schema_slots(perimeter) if p["id"] == sid), "")
    return asked or (probe.split("?", 1)[0] + "?" if "?" in probe else probe) or slot_label(sid, perimeter)


_SOURCE_TAG = re.compile(r"\s+_\([^)]*\)_$")


def _once(lines: list[str], seen: set[str]) -> list[str]:
    """`lines` less each bullet already in `seen`, which it extends: one item appears once (#809), whatever
    its case, spacing, end punctuation or source tag."""
    kept = []
    for line in lines:
        key = " ".join(_SOURCE_TAG.sub("", line[2:]).casefold().split()).rstrip(".;:!?")
        if line.startswith("- ") and key in seen:
            continue
        seen.update([key] if line.startswith("- ") else [])
        kept.append(line)
    return kept


@functools.cache
def _slot_ids(perimeter: str) -> re.Pattern[str]:
    ids = sorted(slot_meta(perimeter)[1], key=len, reverse=True)
    coded, bare = "|".join(map(re.escape, ids)), "|".join(re.escape(i) for i in ids if "_" in i)
    return re.compile(rf"`({coded})`|\b({bare})\b")


def _unjargoned(document: str, perimeter: str) -> str:
    """A slot id the provider let through, said by its label (#809): a code-quoted one, and a bare one only
    where it cannot be an English word, i.e. it carries an underscore."""
    return _slot_ids(perimeter).sub(lambda m: slot_label(m.group(1) or m.group(2), perimeter), document)


def _known(out: EngineOutput, perimeter: str, seen: set[str], of: str) -> list[tuple[str, list[str]]]:
    """What the client stated, the defaults chosen on their behalf, then what was inferred: the three
    projected sections, each item once against `seen` (#809)."""
    confirmed = _once(_stated(out, Confidence.explicit, perimeter), seen)
    defaults = _once(_defaults(out, perimeter), seen)
    if defaults:
        defaults += ["", "_You did not state these. Each stands unless you change it._"]
    assumed = _once(_stated(out, Confidence.inferred, perimeter) + [f"- {_line(a)}" for a in out.summary.assumptions], seen)
    if assumed:
        assumed += ["", f"_Each of these was inferred, not stated. Confirm the ones that would change the {of}._"]
    return [("What is confirmed", confirmed), ("Defaults chosen on your behalf", defaults),
            ("Important assumptions", assumed)]


def _unresolved(out: EngineOutput, open_decisions: list[str], perimeter: str) -> list[str]:
    """The open decisions, then each readiness blocker in plain words on its own line (#783), then the
    least explored area: the one list the readiness verdict points at."""
    items = [f"- {_line(d)}" for d in open_decisions]
    items += [f"- {_line(_in_words(out, sid, perimeter))}" for sid in readiness_blockers(out, perimeter)]
    if out.summary.blind_spot:
        items.append(f"- Least explored: {_line(out.summary.blind_spot)}")
    return items


def _verdict(blocked: bool, open_decisions: bool, settle: str) -> str:
    """Not a second readiness question (#165): the blockers alone decide ready or not, and open decisions
    only word the ready side, so it never says Ready beside one (#783).
    `test_the_brief_never_says_ready_beside_an_open_decision`."""
    if blocked:
        return ("**Not ready.** Points that can still change the solution are open; they are listed under "
                f"*Unresolved questions*. Settle them {settle}.")
    if open_decisions:
        return ("**Decisions to make first.** Everything that could change the solution is known, but the "
                f"decisions listed under *Unresolved questions* remain to be settled {settle}.")
    return "**Ready.** Nothing that could change the solution is still open."


def boundary_note(reason: str) -> str:
    """The note atop every document of a session the router found no clear perimeter for (#787): the
    boundary said where the document is read, its reason flattened (`_line`)."""
    said = _line(reason)
    said += "" if said.endswith((".", "!", "?")) else "."
    return ("> **Outside what Requivo scopes.** When this request arrived, Requivo found no clear fit for it "
            f"among the kinds of work it scopes: {said} It continued as software scoping anyway, so this "
            "document answers as if a software build were wanted. Check that before acting on it.")


def with_boundary_note(document: str, reason: str) -> str:
    """`document` with `boundary_note(reason)` under its title, or atop it with none; once, so a document
    read back and saved again does not carry it twice (#787)."""
    note = boundary_note(reason)
    if note in document:
        return document
    title, _, body = document.partition("\n")
    if not title.startswith("# "):
        return f"{note}\n\n{document}"
    return f"{title}\n\n{note}\n" + (body if body.startswith("\n") else f"\n{body}")


def _excluded(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Excluded options projected off the model (#599), by label (Voice rule), `_line()`-flattened."""
    lines = []
    for ex in out.exclusions:
        line = f"- **{_line(ex.option)}** — {_line(ex.reason)}"
        if ex.rests_on:
            line += f" _(rests on: {', '.join(slot_label(sid, perimeter) for sid in ex.rests_on)})_"
        lines.append(line)
    return lines


def _thresholds(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Decision thresholds projected off the model (#604), by label, `_line()`-flattened."""
    lines = []
    for th in out.thresholds:
        line = f"- **{_line(th.condition)}** → {_line(th.action)}"
        if th.rests_on:
            line += f" _(rests on: {', '.join(slot_label(sid, perimeter) for sid in th.rests_on)})_"
        lines.append(line)
    return lines


def _challenges(challenges: list[Challenge]) -> list[str]:
    """Premises worth contesting, one sub-section each, `_line()`-flattened; the brief's and the plan's (#728)."""
    lines: list[str] = []
    for c in challenges:
        lines += [f"### {_line(c.headline)}",
                  f"- **Premise:** {_line(c.premise)}",
                  f"- **Alternative:** {_line(c.alternative)}",
                  f"- **Consequence:** {_line(c.consequence)}",
                  f"- **Recommendation:** {_line(c.recommendation)}", ""]
    return lines


def brief_markdown(out: EngineOutput, brief: Brief) -> str:
    """Render the decision brief: what is settled and what is assumed first, then the judgment.
    Half of it is deterministic: the confirmed, default, assumed, excluded, threshold and blocking sections are
    projected off the model, because a restatement can drift and a projection cannot. No slot ids or
    confidence labels in either half (Voice rule), nor slot labels framing a value (#783)."""
    blockers = readiness_blockers(out)
    draft = " — Draft: unresolved topics remain" if blockers else ""
    md: list[str] = [f"# Decision Brief{draft}", "",
                     "> What to review before committing to the scope — generated by Requivo", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            md.extend([f"## {heading}", "", *lines, ""])

    summary = []
    if out.summary.objective:
        summary.append(f"**Objective:** {_line(out.summary.objective)}")
    if brief.problem:
        summary.append(f"**Problem:** {_line(brief.problem)}")
    if brief.solution:
        summary.append(f"**Solution:** {_line(brief.solution)}")
    summary.append(f"**Complexity:** {brief.complexity.value}"
                   + (f" — {'; '.join(_line(r) for r in brief.complexity_reasons)}" if brief.complexity_reasons else ""))
    if brief.cost_driver:
        summary.append(f"**Main cost driver:** {_line(brief.cost_driver)}")
    section("Request and objective", summary)

    section("Current understanding", [_block_line(out.summary.scope)] if out.summary.scope else [])
    # An open point stays where the verdict points; any earlier copy of it goes (#809).
    seen: set[str] = set()
    open_items = _once(_unresolved(out, brief.open_decisions, DEFAULT_PERIMETER), seen)
    for heading, lines in _known(out, DEFAULT_PERIMETER, seen, "build"):
        section(heading, lines)

    # `_line()`-flattened, or a newline in a provider decision opens a forged heading (#599).
    decisions: list[str] = []
    for d in brief.decisions:
        line = f"- **{_line(d.decision)}**"
        if d.why:
            line += f" — {_line(d.why)}"
        decisions.append(line)
        if d.alternative:
            decisions.append(f"  - _Alternative weighed:_ {_line(d.alternative)}")
        if d.tradeoff:
            decisions.append(f"  - _Trade-off accepted:_ {_line(d.tradeoff)}")
    section("Decisions made", decisions)

    section("Scope implications", _once([f"- {_line(i)}" for i in brief.introduces], seen))

    section("Out of scope", _excluded(out))

    section("Decision thresholds", _thresholds(out))

    section("Assumptions worth contesting", _challenges(brief.challenges))

    section("Main risks", _once([f"- {_line(r)}" for r in brief.risks], seen))

    section("Unresolved questions", open_items)

    section("Opportunities", [
        f"- **{_line(o.text)}** (leverage: {o.leverage.value})"
        + (f" — reaches {', '.join(_line(m) for m in o.modules)}" if o.modules else "")
        for o in brief.opportunities])

    # Not a second readiness question (#165): `test_every_surface_asks_the_same_readiness_question`.
    section("Are we ready?", [_verdict(bool(blockers), bool(brief.open_decisions), "before estimating")])
    section("Recommended next steps", _once([f"- {_line(s)}" for s in brief.next_steps], seen))

    return _unjargoned("\n".join(md).rstrip() + "\n", DEFAULT_PERIMETER)


def gtm_plan_markdown(out: EngineOutput, brief: GoToMarketPlan) -> str:
    """Render the go-to-market perimeter's one artifact (#609): `brief_markdown`'s split, over its own
    slots, every projection called with `GO_TO_MARKET` explicitly."""
    blockers = readiness_blockers(out, GO_TO_MARKET)
    draft = " — Draft: unresolved topics remain" if blockers else ""
    md: list[str] = [f"# Go-to-Market Plan{draft}", "",
                     "> What to review before committing to this push — generated by Requivo", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            md.extend([f"## {heading}", "", *lines, ""])

    summary = []
    if out.summary.objective:
        summary.append(f"**Objective:** {_line(out.summary.objective)}")
    section("Objective", summary)

    section("Current understanding", [_block_line(out.summary.scope)] if out.summary.scope else [])
    seen: set[str] = set()
    open_items = _once(_unresolved(out, brief.open_decisions, GO_TO_MARKET), seen)
    for heading, lines in _known(out, GO_TO_MARKET, seen, "plan"):
        section(heading, lines)

    section("Resource envelope", _envelope_lines(brief.envelope, GO_TO_MARKET))

    plan: list[str] = [f"- {_line(p)}" for p in brief.plan]
    if brief.rationale:
        plan += ["", _block_line(brief.rationale)]
    section("The plan", plan)

    section("Out of scope", _excluded(out, GO_TO_MARKET))
    section("Decision thresholds", _thresholds(out, GO_TO_MARKET))
    section("Assumptions worth contesting", _challenges(brief.challenges))
    section("Main risks", _once([f"- {_line(r)}" for r in brief.risks], seen))

    section("Unresolved questions", open_items)
    # Not a second readiness question, as in `brief_markdown`.
    section("Are we ready?", [_verdict(bool(blockers), bool(brief.open_decisions), "before committing to the plan")])

    return _unjargoned("\n".join(md).rstrip() + "\n", GO_TO_MARKET)


def _envelope_lines(elements: list[EnvelopeElement], perimeter: str = DEFAULT_PERIMETER) -> list[str]:
    """Render the resource envelope an artifact was planned within (#603): a slot-sourced element by
    its label, an assumed one said plainly. `perimeter` (#609) selects the labels."""
    lines = []
    for e in elements:
        # The enum decides, never the field's presence (#603).
        slot = e.source_slot if e.origin is EnvelopeOrigin.slot else None
        origin = f"stated in {slot_label(slot, perimeter)}" if slot else "assumption — not stated in the model"
        lines.append(f"- **{_line(e.kind)}** — {_line(e.value)} _({origin})_")
    return lines


def prd_markdown(prd: PRD) -> str:
    """Render a PRD as a clean, shareable Markdown document."""
    out: list[str] = [f"# {_block_text(prd.title)}", "", "> Product Requirements Document — generated by Requivo", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            out.extend([f"## {heading}", "", *lines, ""])

    def bullets(items: list[str]) -> list[str]:
        return [f"- {_line(i)}" for i in items]

    if prd.summary:
        section("Summary", [_block_text(prd.summary)])
    if prd.problem:
        section("Problem", [_block_text(prd.problem)])
    section("Resource envelope", _envelope_lines(prd.envelope))
    section("Goals", bullets(prd.goals))
    section("Users & roles", bullets(prd.users))

    scope: list[str] = []
    if prd.in_scope:
        scope += ["**In scope**", "", *bullets(prd.in_scope), ""]
    if prd.out_of_scope:
        scope += ["**Out of scope**", "", *bullets(prd.out_of_scope)]
    section("Scope", scope)

    if prd.requirements:
        rows = ["| ID | Requirement | Priority |", "|----|-------------|----------|"]
        rows += [f"| {_cell(r.id)} | {_cell(r.requirement)} | {r.priority.value.capitalize()} |"
                 for r in prd.requirements]
        section("Functional requirements", rows)

    section("Workflow", [f"{i}. {_line(step)}" for i, step in enumerate(prd.workflow, 1)])
    section("Business rules", bullets(prd.business_rules))
    section("Permissions", bullets(prd.permissions))
    section("Integrations & notifications", bullets(prd.integrations))
    section("Edge cases", bullets(prd.edge_cases))
    section("Acceptance criteria", [f"- [ ] {_line(a)}" for a in prd.acceptance_criteria])
    section("Assumptions", bullets(prd.assumptions))
    section("Open questions", bullets(prd.open_questions))
    section("Risks", bullets(prd.risks))

    return "\n".join(out).rstrip() + "\n"


def stories_markdown(s: Stories) -> str:
    """Render user stories as Markdown, one section per story; saveable since #519
    (`decision: the-estimate-graduates`). `Story.slots` are shown by label (Voice rule)."""
    out: list[str] = ["# User stories", "", "> User stories — generated by Requivo", ""]

    for st in s.stories:
        out += [f"## {_line(st.id)} — {_line(st.title)}", ""]
        if st.as_a or st.i_want or st.so_that:
            out += [f"As a {_line(st.as_a)}, I want {_line(st.i_want)}, so that {_line(st.so_that)}.",
                    ""]
        if st.acceptance:
            out += ["**Acceptance**", "", *[f"- [ ] {_line(a)}" for a in st.acceptance], ""]
        if st.slots:
            out += [f"_Traces to: {', '.join(slot_label(sid) for sid in st.slots)}_", ""]

    return "\n".join(out).rstrip() + "\n"


def estimate_markdown(draft: EstimateDraft, soft: list[str], confidence: str,
                      uncovered: Sequence[str] = ()) -> str:
    """Render the uncertainty-aware estimate as Markdown from the provider's draft and the facts
    computed in core (soft and uncovered slots, confidence); totals are summed here. Saveable since
    #519 (`decision: the-estimate-graduates`). The spread section is never silently absent."""
    total_low = sum(i.days_low for i in draft.items)
    total_high = sum(i.days_high for i in draft.items)
    out: list[str] = ["# Estimate", "", "> Uncertainty-aware estimate — generated by Requivo", "",
                      f"**Total:** {total_low:g}–{total_high:g} days across {len(draft.items)} "
                      f"stor{'y' if len(draft.items) == 1 else 'ies'} · **Confidence:** {confidence}",
                      ""]

    if draft.items:
        rows = ["| Story | Complexity | Days | Drives |", "|---|---|---|---|"]
        rows += [f"| {_cell(i.story_id)} — {_cell(i.title)} | {i.complexity.value} "
                 f"| {i.days_low:g}–{i.days_high:g} "
                 f"| {_cell(', '.join(slot_label(d) for d in i.drives))} |"
                 for i in draft.items]
        out += ["## Per story", "", *rows, ""]

    notes = [f"- **{_line(i.title)}** — {_line(i.note)}" for i in draft.items if i.note]
    if notes:
        out += ["## Notes", "", *notes, ""]

    if soft:
        out += ["## Spread driven by unresolved topics", "",
                *[f"- {slot_label(sid)}" for sid in soft], ""]
    else:
        out += ["No unresolved topic widens these ranges.", ""]

    if uncovered:  # #782: stated scope with no story, so absent from the total
        out += ["## Not covered by any story", "", "These areas are not in the total above:", "",
                *[f"- {slot_label(sid)}" for sid in uncovered], ""]

    if draft.risks:
        out += ["## Risks and unknowns", "", *[f"- {_line(r)}" for r in draft.risks], ""]

    return "\n".join(out).rstrip() + "\n"


def criteria_markdown(ac: AcceptanceCriteria) -> str:
    """Render acceptance criteria as a Given/When/Then Markdown checklist for recette."""
    out: list[str] = [f"# {_block_text(ac.title)}", "", "> Acceptance criteria — generated by Requivo", ""]

    for feature in ac.features:
        out += [f"## {_block_text(feature.name)}", ""]
        for s in feature.scenarios:
            out.append(f"### [ ] {_block_text(s.id)} — {_block_text(s.title)}  _{_KIND_TAG.get(s.kind, s.kind.value)}_")
            out.append("")
            for i, g in enumerate(s.given):
                out.append(f"- **{'Given' if i == 0 else 'And'}** {_line(g)}")
            if s.when:
                out.append(f"- **When** {_line(s.when)}")
            for i, t in enumerate(s.then):
                out.append(f"- **{'Then' if i == 0 else 'And'}** {_line(t)}")
            out.append("")

    if ac.open_questions:
        out += ["## Open questions", "", *[f"- {_line(q)}" for q in ac.open_questions], ""]

    return "\n".join(out).rstrip() + "\n"


def epic_markdown(epic: Epic) -> str:
    """Render a delivery epic as Markdown: goal, scope, then a checklist of trackable issues."""
    out: list[str] = [f"# Epic: {_block_text(epic.title)}", "", "> Delivery epic — generated by Requivo", ""]
    if epic.milestone:
        out += [f"**Milestone:** {_line(epic.milestone)}", ""]
    if epic.goal:
        out += [f"**Goal:** {_line(epic.goal)}", ""]
    if epic.business_value:
        out += [f"**Business value:** {_line(epic.business_value)}", ""]

    if epic.in_scope:
        out += ["## In scope", "", *[f"- {_line(i)}" for i in epic.in_scope], ""]
    if epic.out_of_scope:
        out += ["## Out of scope", "", *[f"- {_line(i)}" for i in epic.out_of_scope], ""]

    if epic.issues:
        out += ["## Issues", ""]
        for issue in epic.issues:
            out.append(f"### [ ] {_block_text(issue.id)} — {_block_text(issue.title)}")
            out.append("")
            meta: list[str] = []
            if issue.labels:
                meta.append("**Labels:** " + ", ".join(f"`{_line(lab)}`" for lab in issue.labels))
            if issue.depends_on:
                meta.append("**Depends on:** " + ", ".join(_line(dep) for dep in issue.depends_on))
            if meta:
                out += [" · ".join(meta), ""]
            if issue.description:
                out += [_block_text(issue.description), ""]

    if epic.open_questions:
        out += ["## Open questions", "", *[f"- {_line(q)}" for q in epic.open_questions], ""]

    return "\n".join(out).rstrip() + "\n"


def release_markdown(rn: ReleaseNotes) -> str:
    """Render client-facing release notes as a clean, shareable Markdown announcement."""
    heading = f"# {_block_text(rn.title)}" + (f" — {_block_text(rn.version)}" if rn.version else "")
    out: list[str] = [heading, "", "> Release notes — generated by Requivo", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            out.extend([f"## {heading}", "", *lines, ""])

    if rn.summary:
        out += [_block_text(rn.summary), ""]
    section("What's new", [f"- {_line(h)}" for h in rn.highlights])
    section("Not included yet", [f"- {_line(lim)}" for lim in rn.known_limitations])
    section("Before you start", [f"- {_line(n)}" for n in rn.notes])

    return "\n".join(out).rstrip() + "\n"
