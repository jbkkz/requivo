from __future__ import annotations

import textwrap
from typing import NamedTuple

from requivo.core.analysis import readiness_blockers, slot_label, state_of
from requivo.core.contracts import (
    Brief,
    Confidence,
    ContextDecision,
    EngineOutput,
    EstimateDraft,
    Impact,
    Leverage,
    PerimeterDecision,
    Stories,
)
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.perimeters import DEFAULT_PERIMETER, get_perimeter
from requivo.core.persistence import ArtifactStatus
from requivo.core.selectors import display_text, display_token
from requivo.usage import CallRecord, UsageLedger
from requivo.web.viewmodels.labels import ARTIFACT_LABELS

STATE_ROWS = [
    ("confirmed", "✅ Confirmed"),
    ("inferred", "🟡 Inferred"),
    ("to_test", "🧪 To test"),
    ("unknown", "⚪ Unknown"),
]

# A constant so `test_the_browsable_examples_deterministic_half_matches_the_renderer` can import it (#172).
DRAFT_NOTE = "(blocking decisions remain — see Unknowns below)"


# Everything below renders LLM-authored prose over an untrusted request, so `display_text` neutralizes
# it (#40): the two helpers escape their `text`, the bare f-strings call it explicitly, and
# `test_every_llm_authored_string_the_terminal_renders_is_neutralized` forges every field at once.
# The label, marker and indent arguments are this module's own literals.


def _bullet(text: str, marker: str = "•", indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(
        display_text(text), width=width, initial_indent=f"{indent}{marker} ",
        subsequent_indent=f"{indent}  "
    )


def _labeled(label: str, text: str, lw: int = 9, width: int = 80, indent: str = "  ") -> str:
    prefix = f"{indent}{label:<{lw}} "
    return textwrap.fill(display_text(text), width=width, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix))


def render_understanding(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> None:
    print("UNDERSTANDING")
    for state, label in STATE_ROWS:
        names = [slot_label(sid, perimeter) for sid, s in out.model.items() if state_of(s) == state]
        if names:
            print(textwrap.fill(" · ".join(names), width=80, initial_indent=f"  {label}   ", subsequent_indent=" " * 15))


def render_readiness(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> None:
    # One boolean, never the length of the blocker list (#165): `test_readiness_renders_as_one_boolean_on_every_surface`.
    print("ARE WE READY?")
    blockers = [slot_label(b, perimeter) for b in readiness_blockers(out, perimeter)]
    status = "Not ready" if blockers else "Ready"
    print(f"  {'Status':<20} {status}")
    if blockers:
        print(_labeled("Blocking decision", "Confirm " + ", ".join(b.lower() for b in blockers), lw=20))
    gaps = [
        slot_label(sid, perimeter)
        for sid, s in out.model.items()
        if s.impact is not Impact.high and s.confidence is not Confidence.explicit
    ]
    if gaps:
        print(_labeled("Remaining gaps", ", ".join(gaps), lw=20))


def render_turn_state(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> None:
    """A turn's checkpoint without the questions; the interactive loops ask those one at a time
    (#592, `test_the_interactive_loop_asks_one_question_per_prompt`). `perimeter` (#608) is the
    session's own, or a go-to-market model shows software slots as blockers."""
    print()
    render_understanding(out, perimeter)
    blockers = [slot_label(b, perimeter) for b in readiness_blockers(out, perimeter)]
    # Same rule as `render_readiness`: the blockers are named on the line already.
    verdict = "⛔ Not ready" if blockers else "✅ Ready"
    print(f"\n  Ready?  {verdict}" + (f"  → {', '.join(blockers)}" if blockers else ""))


def render_turn(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> None:
    """The checkpoint plus the questions, for the verbs with nobody at a prompt (`discover`,
    `answer`, `status`, `demo`)."""
    render_turn_state(out, perimeter)
    if out.questions:
        print("\nPRIORITY QUESTIONS")
        for i, q in enumerate(out.questions, 1):
            print(f"  {i}. {display_text(q.q)}")
            print(f"     → {slot_label(q.slot, perimeter)}")   # a schema-validated slot id, not free text


def render_context_judgment(grounding, routing=None) -> None:
    """What the engine made of this request's grounding (#593) and, with `routing`, which perimeter
    it routed to (#601), both shown before either influences anything. Four outcomes, four sentences:
    *no card is needed* and *nobody asked* are different facts. `reason` goes through `display_text`."""
    if routing is not None:
        _render_perimeter_route(routing)
    judgment = grounding.judgment
    if judgment is None:
        # Not asked, said plainly: silence here reads as a clean bill.
        print(f"\nGrounding      not checked — {display_text(grounding.why_not)}")
        return
    reason = display_text(judgment.reason)
    if judgment.decision is ContextDecision.installed:
        print(_labeled("Grounding", f"{', '.join(display_token(c) for c in judgment.cards)} "
                                    f"describes this domain — {reason}", lw=14))
        print(_labeled("", f"Narrow to it with --context {','.join(judgment.cards)} on a fresh "
                           f"discovery; this session reasons against every card.", lw=14))
    elif judgment.decision is ContextDecision.uncovered:
        print(_labeled("Grounding", f"⚠ no installed card describes this domain — {reason}", lw=14))
        print(_labeled("", "Impact is being estimated against products this request has nothing to "
                           "do with, so the questions below are weaker than they look. Writing a "
                           "card for this domain is the lever (docs/context-cards.md).", lw=14))
    else:
        print(_labeled("Grounding", f"no special domain constraints — {reason}", lw=14))


def _render_perimeter_route(routing) -> None:
    """What the router (#601) made of this request's shape; `ambiguous` never reaches here, and
    `judgment is None` is the third state, never collapsed into `none`."""
    judgment = routing.judgment
    if judgment is None:
        print(f"\nPerimeter      not routed — {display_text(routing.why_not)}")
        return
    reason = display_text(judgment.reason)
    if judgment.decision is PerimeterDecision.fits:
        print(_labeled("Perimeter", f"routed to {display_token(judgment.perimeter)} — {reason}",
                       lw=14))
    else:
        print(_labeled("Perimeter", f"no clear fit, continuing under "
                                    f"{display_token(DEFAULT_PERIMETER)} — {reason}", lw=14))


def render_grounding(cards: list[str] | None) -> None:
    """The cards this session's impact estimates were scored against: a naming, never a verdict
    (#492; relevance is not decidable offline, so the human is the detector, until a third measured
    instance of card dilution funds routing; `decision: the-engine-writes-the-missing-card` reopens
    this for the first discovery only). `None` is *every card in the install*, re-resolved now. An
    unreadable card directory is its own third state and must not fail `status`:
    `test_an_unreadable_card_directory_degrades_the_grounding_line_rather_than_the_verb`."""
    from requivo.core.context import available_cards
    from requivo.core.errors import ContextUnreadableError
    print("\nGROUNDED ON")
    if cards:
        print(_labeled("Product context", ", ".join(display_token(c) for c in cards), lw=20))
        return
    try:
        names = available_cards()
    except ContextUnreadableError:
        print(_labeled("Product context",
                       "could not be read — this install's context cards are not enumerable, so "
                       "what the impact estimates were scored against cannot be stated here. "
                       "`requivo doctor` says which directory and why.", lw=20))
        return
    if not names:
        # The `empty` state's remedy lives in `doctor`; this states the fact and stops.
        print(_labeled("Product context",
                       "no cards in this install — impact is estimated from the request alone",
                       lw=20))
        return
    print(_labeled("Product context", f"all {len(names)} cards in this install "
                   f"({', '.join(display_token(n) for n in names)})", lw=20))
    print(_labeled("", "not narrowed at creation — `requivo session rescope` if this request is "
                       "about one product area", lw=20))


def next_command(payload: dict) -> str | None:
    """The single next step for a status view, or None when there is not one (#246): open questions
    outrank a stale artifact, which outranks the missing primary artifact
    (`test_open_questions_point_at_answer`). None for a converged session with a fresh brief and for
    a bare `model.json`. A projection over the payload, never a second computation."""
    slug = payload.get("slug")
    artifacts = payload.get("artifacts")
    if not slug or artifacts is None:
        return None                      # a bare model.json — no session behind it to point at
    if payload.get("questions"):
        return f'requivo answer {slug} "<your answers>"'
    # `stale` is the explicit flag (invariant 1); the first stale artifact is named, `impact` covers the rest.
    for artifact_type, status in artifacts.items():
        if status.get("stale"):
            return (f"requivo {artifact_type} {slug}   (regenerates {status['filename']}; "
                    f"requivo impact {slug} shows what else moved)")
    # Gated on ownership, read off `Perimeter.primary_artifact` rather than a `"brief"` literal (#609):
    # a go-to-market session's primary is `gtm_plan`.
    perimeter = payload.get("perimeter") or DEFAULT_PERIMETER
    primary = get_perimeter(perimeter).primary_artifact
    if primary and primary not in artifacts:
        return f"requivo {primary} {slug}"
    return None


def render_next_command(payload: dict) -> None:
    """Print `next_command`'s answer, once, or nothing; the arrow matches `discover`'s and `answer`'s."""
    line = next_command(payload)
    if line:
        print(f"\n→ {line}")


# ── `docs` menu (#544) ────────────────────────────────────────────────────────
# `DOC_TYPES` fixes the order, `ARTIFACT_LABELS` is the one name table, the state is `ArtifactStatus.stale`.
DOC_TYPES: tuple[str, ...] = tuple(ARTIFACT_FILENAMES)

DOC_BLURBS: dict[str, str] = {
    "brief": "The judgment call to review before estimating or committing to scope.",
    "gtm_plan": "The go-to-market plan to review before committing capacity to it.",
    "prd": "The requirements document a dev team builds from.",
    "stories": "The backlog, broken into shippable user stories.",
    "estimate": "Day-range estimates per story, reasoned from the stories above.",
    "criteria": "Given/When/Then acceptance criteria a client can sign off on.",
    "epic": "The delivery epic, ready for a tracker.",
    "release": "Client-facing release notes.",
}


class DocRow(NamedTuple):
    """One line of the `docs` menu, built by `docs_menu_rows` and read by `render_docs_menu`."""
    number: int
    doc_type: str
    label: str
    blurb: str
    state: str


def docs_menu_rows(artifact_status: dict[str, ArtifactStatus],
                   types: tuple[str, ...] = DOC_TYPES) -> list[DocRow]:
    """The menu rows in `DOC_TYPES` order, never `artifact_status`'s key order (a forged key cannot
    add a row); `filename` is escaped disk content. `types` (#609) narrows to the session's perimeter."""
    rows = []
    for i, doc_type in enumerate(types, 1):
        status = artifact_status.get(doc_type)
        if status is None:
            state = "not generated"
        else:
            filename = display_token(status.filename)
            state = (f"needs updating (from rev {status.revision}, {filename})" if status.stale
                     else f"up to date (rev {status.revision}, {filename})")
        rows.append(DocRow(i, doc_type, ARTIFACT_LABELS.get(doc_type, doc_type),
                           DOC_BLURBS.get(doc_type, ""), state))
    return rows


def render_docs_menu(rows: list[DocRow]) -> None:
    """Data -> str, no side effects beyond printing."""
    print("DOCUMENTS")
    for row in rows:
        print(f"  {row.number}. {row.label:<20} {row.state}")
        print(f"     {row.blurb}")


def render_usage(ledger: UsageLedger) -> None:
    """The run's API footprint: calls, tokens, latency and a labelled cost *estimate*; nothing when offline."""
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    if not ledger.calls or processed + ledger.output_tokens == 0:
        return  # no call, or usage absent (e.g. an offline test fake) — nothing worth printing
    print("\nAPI USAGE  (this run)")
    print(f"  {'Calls':<11} {len(ledger.calls)}")
    cached = f"  ({ledger.cache_read_tokens:,} served from cache)" if ledger.cache_read_tokens else ""
    print(f"  {'Input':<11} {processed:,} tokens{cached}")
    print(f"  {'Output':<11} {ledger.output_tokens:,} tokens")
    print(f"  {'Latency':<11} {ledger.latency_ms / 1000:.1f} s")
    cost = ledger.cost_usd()
    model = " · ".join(ledger.models)
    if cost is None:
        print(f"  {'Est. cost':<11} n/a — no price on file for {model} (tokens above are exact)")
        return
    # The rate date comes off the ledger, never a vendor constant (#167); a priced call with no date
    # prints without the clause rather than borrowing one.
    as_of = " · ".join(ledger.priced_as_of)
    stamp = f", rates as of {as_of}" if as_of else ""
    print(f"  {'Est. cost':<11} ~${cost:.3f}   ({model} — estimate{stamp})")


def render_session_cost(revisions: list) -> None:
    """The cumulative cost of every provider-backed apply in a session, from the usage provenance
    `RevisionRecord` carries (#292); silent, never `$0.00`, when none carries any (invariant 6). Partial
    by construction and the header says so: generators produce no `RevisionRecord`. The arithmetic is
    `UsageLedger`'s and nowhere else (#389,
    `test_render_session_cost_reads_its_arithmetic_from_usage_py_and_nowhere_else`);
    `usage_priced_as_of` is a persisted string, so it goes through `display_token` (#388)."""
    priced_revisions = [r for r in revisions if r.usage_input_tokens is not None]
    if not priced_revisions:
        return
    ledger = UsageLedger(calls=[
        CallRecord(
            model="",
            input_tokens=r.usage_input_tokens or 0,
            output_tokens=r.usage_output_tokens or 0,
            cache_read_tokens=r.usage_cache_read_tokens or 0,
            cache_write_tokens=r.usage_cache_write_tokens or 0,
            rate_per_mtok=r.usage_rate_per_mtok,
            # `or None`: `priced_as_of` filters on `is not None`, and a persisted empty string is untrusted anyway.
            priced_as_of=r.usage_priced_as_of or None,
        )
        for r in priced_revisions
    ])
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    plural = "s" if len(priced_revisions) != 1 else ""
    print(f"\nSESSION COST  (cumulative, {len(priced_revisions)} revision{plural} -- excludes prd/"
         "criteria/epic/release generation, which is not a revision)")
    cached = f"  ({ledger.cache_read_tokens:,} served from cache)" if ledger.cache_read_tokens else ""
    print(f"  {'Input':<11} {processed:,} tokens{cached}")
    print(f"  {'Output':<11} {ledger.output_tokens:,} tokens")
    cost = ledger.cost_usd()
    if cost is None:
        print(f"  {'Est. cost':<11} n/a — some revisions have no price on file (tokens above are exact)")
        return
    as_of = " · ".join(display_token(d) for d in ledger.priced_as_of)
    stamp = f", rates as of {as_of}" if as_of else ""
    print(f"  {'Est. cost':<11} ~${cost:.3f}   (estimate{stamp})")


def render_brief(out: EngineOutput, brief: Brief) -> None:
    """The two-tier decision brief: an executive summary, then the full analysis, in a PM's language.
    "Decision brief" is a caption; the type, verb and file stay `brief`/`solution-assessment.md` (#166)."""
    # A blocking decision unresolved means the brief is a draft, and the label says so.
    draft = bool(readiness_blockers(out))
    print("\n" + "═" * 64)
    print("DRAFT DECISION BRIEF" if draft else "DECISION BRIEF")
    if draft:
        print(DRAFT_NOTE)
    print("═" * 64)

    # ── Executive summary (what a PM reads first) ──
    print("\nEXECUTIVE SUMMARY")
    if brief.problem:
        print(_labeled("Problem", brief.problem))
    print(_labeled("Solution", brief.solution or out.summary.objective))
    if brief.challenges:
        more = f"   (+{len(brief.challenges) - 1} more below)" if len(brief.challenges) > 1 else ""
        print(_labeled("Challenge", brief.challenges[0].headline + more))
    if brief.risks:
        more = f"   (+{len(brief.risks) - 1} more below)" if len(brief.risks) > 1 else ""
        print(_labeled("Risks", brief.risks[0] + more))
    unknowns = [slot_label(b) for b in readiness_blockers(out)] + brief.open_decisions
    if unknowns:
        print(_labeled("Unknowns", " · ".join(unknowns)))
    if brief.next_steps:
        more = f"   (+{len(brief.next_steps) - 1} more below)" if len(brief.next_steps) > 1 else ""
        print(_labeled("Next", brief.next_steps[0] + more))

    print("\n  " + "─" * 22 + " full analysis " + "─" * 22 + "\n")

    # ── Full analysis ──
    render_understanding(out)

    if brief.decisions or brief.open_decisions:
        print("\nDESIGN DECISIONS")
        for d in brief.decisions:
            print(_bullet(d.decision, marker="✓", indent="  "))
            if d.why:
                print(_labeled("Why", d.why, lw=12, indent="      "))
            if d.alternative:
                print(_labeled("Alternative", d.alternative, lw=12, indent="      "))
            if d.tradeoff:
                print(_labeled("Tradeoff", d.tradeoff, lw=12, indent="      "))
        if brief.open_decisions:
            print("  Still to decide")
            for d in brief.open_decisions:
                print(_bullet(d, marker="•", indent="    "))

    if brief.challenges:
        print("\nCHALLENGES")
        for c in brief.challenges:
            print(_bullet(c.headline, marker="⚑", indent="  "))
            print(_labeled("Premise", c.premise, lw=12, indent="      "))
            print(_labeled("Alternative", c.alternative, lw=12, indent="      "))
            print(_labeled("Consequence", c.consequence, lw=12, indent="      "))
            print(_labeled("Recommend", c.recommendation, lw=12, indent="      "))

    print(f"\nCOMPLEXITY  {brief.complexity.value.upper()}")
    for r in brief.complexity_reasons:
        print(_bullet(r, marker="·", indent="    "))
    if brief.cost_driver:
        print(_labeled("Cost driver", brief.cost_driver, lw=13))

    if brief.risks:
        print("\nMAIN RISKS")
        for r in brief.risks:
            print(_bullet(r, marker="⚠"))

    if brief.opportunities:
        print("\nOPPORTUNITIES")
        for lev, label in [(Leverage.high, "High leverage"), (Leverage.medium, "Medium leverage"), (Leverage.future, "Future idea")]:
            group = [o for o in brief.opportunities if o.leverage is lev]
            if group:
                print(f"  {label}")
                for o in group:
                    print(_bullet(o.text, marker="◆", indent="    "))
                    if o.modules:
                        # A free `list[str]` the model fills, unlike a slot id.
                        print(f"        ↳ reaches: {', '.join(display_text(m) for m in o.modules)}")

    if brief.next_steps:
        print("\nRECOMMENDED NEXT STEPS")
        for i, step in enumerate(brief.next_steps, 1):
            print(_bullet(step, marker=f"{i}.", indent="  "))

    print()
    render_readiness(out)


def render_stories(s: Stories) -> None:
    # Every field is the model's own text except `slots`, validated against the schema (#213).
    print("\n=== USER STORIES ===")
    for st in s.stories:
        print(f"\n[{display_text(st.id)}] {display_text(st.title)}")
        if st.as_a or st.i_want or st.so_that:
            print(f"  As a {display_text(st.as_a)}, I want {display_text(st.i_want)}, "
                  f"so that {display_text(st.so_that)}.")
        for ac in st.acceptance:
            print(f"  ✓ {display_text(ac)}")
        if st.slots:
            print(f"  ↳ from: {', '.join(st.slots)}")


def render_estimate(draft: EstimateDraft, soft: list[str], confidence: str) -> None:
    total_low = sum(i.days_low for i in draft.items)
    total_high = sum(i.days_high for i in draft.items)
    print(f"\n=== ESTIMATE (from the model)   Confidence: {confidence.upper()} ===")
    print(f"{'Task':<44} {'Cplx':<5} {'Estimate':<11} Drives")
    for i in draft.items:
        est = f"{i.days_low:g}–{i.days_high:g} d"
        # Escaped *before* the 43-character cut, so the column stays 43 wide (#213).
        title = display_text(i.title)[:43]
        print(f"{title:<44} {i.complexity.value:<5} {est:<11} "
              f"{', '.join(display_text(d) for d in i.drives)}")
    print(f"{'─' * 43:<44} {'':<5} {'─' * 9:<11}")
    print(f"{'TOTAL':<44} {'':<5} {total_low:g}–{total_high:g} d")
    if soft:
        print(f"\nSpread driven by unresolved slots: {', '.join(slot_label(s) for s in soft)}")
    if draft.risks:
        print("Risks / unknowns:")
        for r in draft.risks:
            print(f"  - {display_text(r)}")


def render_impact(report) -> None:
    """Focused propagation view: name slots, see what rests on them go stale."""
    print("\n" + "═" * 64)
    print("IMPACT — what rests on: " + ", ".join(report.changed))
    print("═" * 64)
    if report.empty:
        print("\n  Nothing downstream depends on these — safe to revisit in isolation.")
        return

    if report.decisions:
        print("\nDECISIONS TO RE-VALIDATE")
        for d in report.decisions:
            print(_bullet(d.decision))
            print(f"    ↳ rests on: {', '.join(d.rests_on)}")

    if report.challenges:
        print("\nPREMISES TO RE-EXAMINE")
        for c in report.challenges:
            print(_bullet(c.headline))
            print(f"    ↳ contests: {', '.join(c.rests_on)}")

    if report.exclusions:
        print("\nEXCLUSIONS TO RECONSIDER")
        for e in report.exclusions:
            print(_bullet(e.option))
            print(f"    ↳ rests on: {', '.join(e.rests_on)}")

    if report.thresholds:
        print("\nTHRESHOLDS TO RECONSIDER")
        for t in report.thresholds:
            print(_bullet(t.condition))
            print(f"    ↳ rests on: {', '.join(t.rests_on)}")

    if report.artifacts:
        print("\nARTIFACTS THAT GO STALE")
        for name in report.artifacts:
            f = ARTIFACT_FILENAMES.get(name)
            where = f" ({f})" if f else " (regenerate on demand)"
            print(f"  • {name}{where}")
        print("\n  → Regenerate these after confirming the change.")


def render_evidence(report) -> None:
    """Decisions derived from thinner evidence than the session now holds (#493), in three states:
    `None` is not reviewed, `reviewed == 0` prints nothing, a reviewed report names the decisions
    *worth re-reading* (never "contradicted") and the ones it could not check."""
    if report is None:
        print("\n  Evidence since derivation: not reviewed — no revision history to compare "
              "against (a session has one; a bare model file does not).")
        return
    if report.reviewed == 0:
        return
    if report.flagged:
        print("\nDECISIONS DERIVED FROM THINNER EVIDENCE THAN EXISTS NOW — worth re-reading")
        for f in report.flagged:
            print(_bullet(f.decision))
            when = f"at revision {f.derived_at}" if f.derived_at is not None else "when recorded"
            print(f"    ↳ {', '.join(f.thickened)}: assumed or empty {when}, confirmed since")
    else:
        checked = report.reviewed - len(report.could_not_tell)
        print(f"\n  Evidence since derivation: {checked} of {report.reviewed} decision(s) checked, "
              "none rests on thinner evidence than it did when recorded.")
    if report.could_not_tell:
        print("\n  Could not check:")
        for u in report.could_not_tell:
            print(_bullet(u.decision))
            print(f"    ↳ {display_text(u.reason)}")


def render_dependency_map(out: EngineOutput, perimeter: str = DEFAULT_PERIMETER) -> None:
    """No-args overview: for every slot that can still move, what it would invalidate. The model's
    text goes through `display_text` (#213); `perimeter` (#608) is the session's own."""
    from requivo.core.dependencies import propagate
    print("\n" + "═" * 64)
    print("DEPENDENCY MAP — change a slot, see the blast radius")
    print("═" * 64)
    for sid in out.model:
        rep = propagate(out, [sid], perimeter)
        if rep.empty:
            continue
        print(f"\n{slot_label(sid, perimeter)}")
        if rep.decisions:
            print(f"  decisions: {'; '.join(display_text(d.decision) for d in rep.decisions)}")
        if rep.challenges:
            print(f"  challenges: {'; '.join(display_text(c.headline) for c in rep.challenges)}")
        if rep.exclusions:
            print(f"  exclusions: {'; '.join(display_text(e.option) for e in rep.exclusions)}")
        if rep.thresholds:
            print(f"  thresholds: {'; '.join(display_text(t.condition) for t in rep.thresholds)}")
        if rep.artifacts:
            print(f"  artifacts: {', '.join(rep.artifacts)}")


def render_stale(pairs, changed_labels) -> None:
    """After a discovery turn moved a slot, warn that already-generated artifacts are now stale."""
    if not pairs:
        return
    print("\n" + "─" * 64)
    print(f"⚠  STALE — you just changed: {', '.join(changed_labels)}")
    print("   These already-generated artifacts no longer match the model:")
    for _name, filename in pairs:
        print(f"     • {filename}")
    print("   → Regenerate them to pick up the change.")
