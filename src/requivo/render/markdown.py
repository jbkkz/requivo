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
    ClaimSource,
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
from requivo.core.language import DEFAULT_DOCS_LANGUAGE
from requivo.core.perimeters import DEFAULT_PERIMETER, GO_TO_MARKET
from requivo.render.phrases import enum_phrase, phrase

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


def _defaults(out: EngineOutput, perimeter: str, language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """The settled values the client did not state, in schema order, each with its source (#809)."""
    slots = [out.model[sid] for sid in slot_meta(perimeter)[1] if sid in out.model]
    return [f"- {_line(text)} _({phrase(language, _DEFAULT_TAG[source])})_"
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


def _known(out: EngineOutput, perimeter: str, seen: set[str], of: str,
           language: str = DEFAULT_DOCS_LANGUAGE) -> list[tuple[str, list[str]]]:
    """What the client stated, the defaults chosen on their behalf, then what was inferred: the three
    projected sections, each item once against `seen` (#809), their notes in `language` (#790)."""
    confirmed = _once(_stated(out, Confidence.explicit, perimeter), seen)
    defaults = _once(_defaults(out, perimeter, language), seen)
    if defaults:
        defaults += ["", phrase(language, "_You did not state these. Each stands unless you change it._")]
    assumed = _once(_stated(out, Confidence.inferred, perimeter) + [f"- {_line(a)}" for a in out.summary.assumptions], seen)
    if assumed:
        assumed += ["", phrase(language, f"_Each of these was inferred, not stated. Confirm the ones that would change the {of}._")]
    return [("What is confirmed", confirmed), ("Defaults chosen on your behalf", defaults),
            ("Important assumptions", assumed)]


def _unresolved(out: EngineOutput, open_decisions: list[str], perimeter: str,
                language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """The open decisions, then each readiness blocker in plain words on its own line (#783), then the
    least explored area: the one list the readiness verdict points at."""
    items = [f"- {_line(d)}" for d in open_decisions]
    items += [f"- {_line(_in_words(out, sid, perimeter))}" for sid in readiness_blockers(out, perimeter)]
    if out.summary.blind_spot:
        items.append(f"- {phrase(language, 'Least explored')}: {_line(out.summary.blind_spot)}")
    return items


# The verdict's three sentences, the keys `render/phrases.py` translates them under.
_NOT_READY = ("**Not ready.** Points that can still change the solution are open; they are listed under "
              "*Unresolved questions*. Settle them {settle}.")
_DECIDE_FIRST = ("**Decisions to make first.** Everything that could change the solution is known, but the "
                 "decisions listed under *Unresolved questions* remain to be settled {settle}.")
_READY = "**Ready.** Nothing that could change the solution is still open."


def _verdict(blocked: bool, open_decisions: bool, settle: str, language: str = DEFAULT_DOCS_LANGUAGE) -> str:
    """Not a second readiness question (#165): the blockers alone decide ready or not, and open decisions
    only word the ready side, so it never says Ready beside one (#783).
    `test_the_brief_never_says_ready_beside_an_open_decision`."""
    sentence = _NOT_READY if blocked else _DECIDE_FIRST if open_decisions else _READY
    return phrase(language, sentence).replace("{settle}", phrase(language, settle))


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


def _rests_on(slots: list[str], perimeter: str, language: str) -> str:
    """The slots a reasoning item rests on, by label; the labels are the schema's own (#790's open edge)."""
    labels = ", ".join(slot_label(sid, perimeter) for sid in slots)
    return f" _({phrase(language, 'rests on')}: {labels})_" if slots else ""


def _excluded(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER, language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """Excluded options projected off the model (#599), by label (Voice rule), `_line()`-flattened."""
    return [f"- **{_line(ex.option)}** — {_line(ex.reason)}" + _rests_on(ex.rests_on, perimeter, language)
            for ex in out.exclusions]


def _thresholds(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER, language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """Decision thresholds projected off the model (#604), by label, `_line()`-flattened."""
    return [f"- **{_line(th.condition)}** → {_line(th.action)}" + _rests_on(th.rests_on, perimeter, language)
            for th in out.thresholds]


def _challenges(challenges: list[Challenge], language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """Premises worth contesting, one sub-section each, `_line()`-flattened; the brief's and the plan's (#728)."""
    lines: list[str] = []
    for c in challenges:
        lines.append(f"### {_line(c.headline)}")
        for label, text in (("Premise", c.premise), ("Alternative", c.alternative),
                            ("Consequence", c.consequence), ("Recommendation", c.recommendation)):
            lines.append(f"- **{phrase(language, label)}:** {_line(text)}")
        lines.append("")
    return lines


_SOURCE_PHRASE = {   # where an assumption on the client page came from (#751), in the client's terms
    ClaimSource.requester: "as you stated", ClaimSource.artifact: "from your documents",
    ClaimSource.evidence: "from outside evidence", ClaimSource.proposed: "our proposal, for you to confirm",
    ClaimSource.domain: "from domain knowledge", ClaimSource.assumed: "inferred, not stated: to confirm",
}


def _still_to_settle(out: EngineOutput, sid: str, language: str) -> str:
    """A blocker the client page left out (#814): the session's own question on it, else its topic inside a
    sentence in `language`, never the schema's English probe. `test_a_french_page_that_leaves_a_blocker_out_carries_no_english_schema_probe`."""
    asked = next((q.q for q in out.questions if q.slot == sid), "")
    return asked or phrase(language, "Still to settle with you: {topic}.").replace("{topic}", slot_label(sid, DEFAULT_PERIMETER))


def brief_markdown(out: EngineOutput, brief: Brief, language: str = DEFAULT_DOCS_LANGUAGE) -> str:
    """The decision brief the client reads (#814): one page of prose, each element once, in `language`.
    No readiness label, boilerplate or slot id, and a blocker the page leaves out is added in the client's
    words: `test_the_client_brief_carries_no_readiness_label_slot_id_or_repeat`."""
    t = functools.partial(phrase, language)
    page = brief.page
    if page is None:   # a reply with no page: the brief's own fields, said once each
        costs = " ".join(_line(x).rstrip(".") + "." for x in [brief.cost_driver, *brief.risks] if x.strip())
        title, said = "", [("", brief.solution or out.summary.objective), ("Why.", brief.problem), ("Cost and risk.", costs)]
        open_points = [f"- {_line(d)}" for d in brief.open_decisions]
        rests_on = [f"- {_line(a)} _({t(_SOURCE_PHRASE[ClaimSource.assumed])})_" for a in out.summary.assumptions]
    else:
        title, said = page.title, [("", page.recommendation), ("Why.", page.why), ("Cost and risk.", page.cost_and_risk)]
        open_points = [f"- {_line(p.question)} — {_line(p.would_change)}" for p in page.open]
        rests_on = [f"- {_line(a.text)} _({t(_SOURCE_PHRASE[a.source])})_" for a in page.assumptions]
    covered = {sid for p in (page.open if page else []) for sid in p.slots}
    open_points += [f"- {_line(_still_to_settle(out, sid, language))}" for sid in readiness_blockers(out) if sid not in covered]
    seen: set[str] = set()
    md = [f"# {t('Decision Brief')}" + (f": {_line(title)}" if title.strip() else ""), ""]
    md += [line for lead, text in said if text.strip()
           for line in ((f"**{t(lead)}** {_line(text)}" if lead else _block_line(text)), "")]
    for lead, lines in (("Still open, and would change this:", _once(open_points, seen)),
                        ("This recommendation assumes:", _once(rests_on, seen))):
        md += [f"**{t(lead)}**", "", *lines, ""] if lines else []
    return _unjargoned("\n".join(md).rstrip() + "\n", DEFAULT_PERIMETER)


def consultant_view_markdown(out: EngineOutput, brief: Brief, language: str = DEFAULT_DOCS_LANGUAGE) -> str:
    """The consultant's internal view of the brief (#814): what is settled and what is assumed first, then
    the judgment, section by section, readiness verdict included. The confirmed, default, assumed, excluded,
    threshold and blocking sections are projected off the model, because a restatement can drift and a
    projection cannot. No slot ids or confidence labels (Voice rule), nor slot labels framing a value
    (#783). Its own words are in the documents `language` where `render/phrases.py` has them (#790)."""
    t = functools.partial(phrase, language)
    blockers = readiness_blockers(out)
    draft = t(" — Draft: unresolved topics remain") if blockers else ""
    md: list[str] = [f"# {t('Decision Brief')} — {t('consultant view')}{draft}", "",
                     t("> Internal: the analysis behind the decision brief. Not the page sent to the client."), ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            md.extend([f"## {t(heading)}", "", *lines, ""])

    summary = []
    if out.summary.objective:
        summary.append(f"**{t('Objective')}:** {_line(out.summary.objective)}")
    if brief.problem:
        summary.append(f"**{t('Problem')}:** {_line(brief.problem)}")
    if brief.solution:
        summary.append(f"**{t('Solution')}:** {_line(brief.solution)}")
    reasons = "; ".join(_line(r) for r in brief.complexity_reasons)
    summary.append(f"**{t('Complexity')}:** {enum_phrase(language, 'complexity', brief.complexity.value)}"
                   + (f" — {reasons}" if reasons else ""))
    if brief.cost_driver:
        summary.append(f"**{t('Main cost driver')}:** {_line(brief.cost_driver)}")
    section("Request and objective", summary)

    section("Current understanding", [_block_line(out.summary.scope)] if out.summary.scope else [])
    # An open point stays where the verdict points; any earlier copy of it goes (#809).
    seen: set[str] = set()
    open_items = _once(_unresolved(out, brief.open_decisions, DEFAULT_PERIMETER, language), seen)
    for heading, lines in _known(out, DEFAULT_PERIMETER, seen, "build", language):
        section(heading, lines)

    # `_line()`-flattened, or a newline in a provider decision opens a forged heading (#599).
    decisions: list[str] = []
    for d in brief.decisions:
        line = f"- **{_line(d.decision)}**"
        if d.why:
            line += f" — {_line(d.why)}"
        decisions.append(line)
        if d.alternative:
            decisions.append(f"  - _{t('Alternative weighed')}:_ {_line(d.alternative)}")
        if d.tradeoff:
            decisions.append(f"  - _{t('Trade-off accepted')}:_ {_line(d.tradeoff)}")
    section("Decisions made", decisions)

    section("Scope implications", _once([f"- {_line(i)}" for i in brief.introduces], seen))

    section("Out of scope", _excluded(out, DEFAULT_PERIMETER, language))

    section("Decision thresholds", _thresholds(out, DEFAULT_PERIMETER, language))

    section("Assumptions worth contesting", _challenges(brief.challenges, language))

    section("Main risks", _once([f"- {_line(r)}" for r in brief.risks], seen))

    section("Unresolved questions", open_items)

    opportunities = []
    for o in brief.opportunities:
        modules = ", ".join(_line(m) for m in o.modules)
        opportunities.append(f"- **{_line(o.text)}** ({t('leverage')}: {enum_phrase(language, 'leverage', o.leverage.value)})"
                             + (f" — {t('reaches')} {modules}" if modules else ""))
    section("Opportunities", opportunities)

    # Not a second readiness question (#165): `test_every_surface_asks_the_same_readiness_question`.
    section("Are we ready?", [_verdict(bool(blockers), bool(brief.open_decisions), "before estimating", language)])
    section("Recommended next steps", _once([f"- {_line(s)}" for s in brief.next_steps], seen))

    return _unjargoned("\n".join(md).rstrip() + "\n", DEFAULT_PERIMETER)


def gtm_plan_markdown(out: EngineOutput, brief: GoToMarketPlan, language: str = DEFAULT_DOCS_LANGUAGE) -> str:
    """Render the go-to-market perimeter's one artifact (#609): `brief_markdown`'s split, over its own
    slots, every projection called with `GO_TO_MARKET` explicitly, its own words in `language` (#790)."""
    t = functools.partial(phrase, language)
    blockers = readiness_blockers(out, GO_TO_MARKET)
    draft = t(" — Draft: unresolved topics remain") if blockers else ""
    md: list[str] = [f"# {t('Go-to-Market Plan')}{draft}", "",
                     t("> What to review before committing to this push — generated by Requivo"), ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            md.extend([f"## {t(heading)}", "", *lines, ""])

    summary = []
    if out.summary.objective:
        summary.append(f"**{t('Objective')}:** {_line(out.summary.objective)}")
    section("Objective", summary)

    section("Current understanding", [_block_line(out.summary.scope)] if out.summary.scope else [])
    seen: set[str] = set()
    open_items = _once(_unresolved(out, brief.open_decisions, GO_TO_MARKET, language), seen)
    for heading, lines in _known(out, GO_TO_MARKET, seen, "plan", language):
        section(heading, lines)

    section("Resource envelope", _envelope_lines(brief.envelope, GO_TO_MARKET, language))

    plan: list[str] = [f"- {_line(p)}" for p in brief.plan]
    if brief.rationale:
        plan += ["", _block_line(brief.rationale)]
    section("The plan", plan)

    section("Out of scope", _excluded(out, GO_TO_MARKET, language))
    section("Decision thresholds", _thresholds(out, GO_TO_MARKET, language))
    section("Assumptions worth contesting", _challenges(brief.challenges, language))
    section("Main risks", _once([f"- {_line(r)}" for r in brief.risks], seen))

    section("Unresolved questions", open_items)
    # Not a second readiness question, as in `brief_markdown`.
    section("Are we ready?", [_verdict(bool(blockers), bool(brief.open_decisions), "before committing to the plan",
                                       language)])

    return _unjargoned("\n".join(md).rstrip() + "\n", GO_TO_MARKET)


def _envelope_lines(elements: list[EnvelopeElement], perimeter: str = DEFAULT_PERIMETER,
                    language: str = DEFAULT_DOCS_LANGUAGE) -> list[str]:
    """Render the resource envelope an artifact was planned within (#603): a slot-sourced element by
    its label, an assumed one said plainly. `perimeter` (#609) selects the labels."""
    lines = []
    for e in elements:
        # The enum decides, never the field's presence (#603).
        slot = e.source_slot if e.origin is EnvelopeOrigin.slot else None
        origin = (f"{phrase(language, 'stated in')} {slot_label(slot, perimeter)}" if slot
                  else phrase(language, "assumption — not stated in the model"))
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
    section("Data model", [line for e in prd.data_model for line in (
        f"- **{_line(e.name)}**" + (f" — {_line(', '.join(e.fields))}" if e.fields else ""),
        *[f"  - {_line(r)}" for r in e.rules])])
    section("Permissions", bullets(prd.permissions))
    section("Integrations & notifications", bullets(prd.integrations))
    section("Proposed technical approach", bullets(prd.technical_approach))
    section("Non-functional requirements", bullets(prd.non_functional))
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
        if st.dev_notes:
            out += ["**For the developer**", "", *[f"- {_line(n)}" for n in st.dev_notes], ""]
        if st.slots:
            out += [f"_Traces to: {', '.join(slot_label(sid) for sid in st.slots)}_", ""]

    return "\n".join(out).rstrip() + "\n"


def estimate_totals(draft: EstimateDraft) -> tuple[float, float]:
    """The estimate's total day range: every story plus the delivery work beyond them (#821)."""
    parts = [*draft.items, *draft.delivery]
    return sum(p.days_low for p in parts), sum(p.days_high for p in parts)


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.0f} {_cell(currency)}"


def estimate_money_lines(draft: EstimateDraft) -> list[str]:
    """What the day rate makes of the total and what a stated budget buys of it (#821), computed here,
    never by the provider: `test_the_estimate_prices_delivery_work_and_confronts_the_budget`."""
    low, high = estimate_totals(draft)
    rate, budget = draft.day_rate, draft.budget
    lines: list[str] = []
    if rate is not None:
        said = "stated" if rate.origin is EnvelopeOrigin.slot else "assumed"
        lines.append(f"**Cost:** about {_money(low * rate.amount, rate.currency)}–{_money(high * rate.amount, rate.currency)} "
                     f"at {_money(rate.amount, rate.currency)} a day ({said}).")
    if budget is None:
        return lines
    stated = f"The stated budget of {_money(budget.amount, budget.currency)}"
    if rate is None or rate.currency.strip().casefold() != budget.currency.strip().casefold():
        lines.append(f"**Budget:** {stated} is not compared with these days: no day rate in its currency.")
        return lines
    days = budget.amount / rate.amount
    verdict = ("covers the whole range" if days >= high
               else f"buys about {days:,.0f} days at this rate, short of even the low end" if days < low
               else f"buys about {days:,.0f} days at this rate: the low end fits, the high end does not")
    lines.append(f"**Budget:** {stated} {verdict}.")
    return lines


def estimate_alternative_line(draft: EstimateDraft) -> str:
    """The path the total does not price, its days turned into money at the same rate (#821), or ''."""
    alt, rate = draft.alternative, draft.day_rate
    if alt is None:
        return ""
    money = (f" (about {_money(alt.days_low * rate.amount, rate.currency)}–{_money(alt.days_high * rate.amount, rate.currency)})"
             if rate is not None else "")
    recurring = f"; {_line(alt.recurring)}" if alt.recurring else ""
    return f"**Alternative — {_line(alt.path)}:** {alt.days_low:g}–{alt.days_high:g} days{money}{recurring}."


def estimate_markdown(draft: EstimateDraft, soft: list[str], confidence: str,
                      uncovered: Sequence[str] = ()) -> str:
    """Render the uncertainty-aware estimate as Markdown from the provider's draft and the facts
    computed in core (soft and uncovered slots, confidence); totals and money are computed here. Saveable
    since #519 (`decision: the-estimate-graduates`). The spread section is never silently absent."""
    total_low, total_high = estimate_totals(draft)
    n = len(draft.items)
    split = (f" — stories {sum(i.days_low for i in draft.items):g}–{sum(i.days_high for i in draft.items):g}, "
             f"delivery work {sum(d.days_low for d in draft.delivery):g}–{sum(d.days_high for d in draft.delivery):g}"
             if draft.delivery else f" across {n} stor{'y' if n == 1 else 'ies'}")
    out: list[str] = ["# Estimate", "", "> Uncertainty-aware estimate — generated by Requivo", "",
                      f"**Total:** {total_low:g}–{total_high:g} days{split} · **Confidence:** {confidence}", ""]
    out += [line for m in estimate_money_lines(draft) for line in (m, "")]
    if draft.prices:
        out += [f"**What this prices.** {_line(draft.prices)}", ""]
    if draft.alternative:
        out += [estimate_alternative_line(draft), ""]

    basis = draft.basis
    if basis and (basis.unit or basis.team or basis.excludes):
        out += ["## How to read these figures", ""]
        out += [f"- **One day:** {_line(basis.unit)}"] if basis.unit else []
        out += [f"- **Team assumed:** {_line(basis.team)}"] if basis.team else []
        out += ["- **Not included:**", *[f"  - {_line(x)}" for x in basis.excludes]] if basis.excludes else []
        out += [""]

    if draft.items:
        rows = ["| Story | Size | Days | Basis |", "|---|---|---|---|"]
        rows += [f"| {_cell(i.story_id)} — {_cell(i.title)} | {i.complexity.value} "
                 f"| {i.days_low:g}–{i.days_high:g} | {_cell(i.note)} |" for i in draft.items]
        out += ["## Per story", "", *rows, ""]

    if draft.delivery:
        rows = ["| Work | Days | Basis |", "|---|---|---|"]
        rows += [f"| {_cell(d.title)} | {d.days_low:g}–{d.days_high:g} | {_cell(d.note)} |" for d in draft.delivery]
        out += ["## Delivery work beyond the stories", "", *rows, ""]

    priced = {sid for s in draft.swings for sid in s.slots}
    rest = [sid for sid in soft if sid not in priced]
    if draft.swings:
        out += ["## What would move this number", "",
                *[f"- {_line(s.question)} — {s.days_low:+g} to {s.days_high:+g} days" for s in draft.swings], ""]
        if rest:
            out += [f"Also still open, not priced above: {', '.join(slot_label(sid) for sid in rest)}.", ""]
    elif soft:
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
