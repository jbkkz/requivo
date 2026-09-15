"""The driver: information_value = uncertainty × impact, and the readiness it feeds.

Split out of `test_engine.py` (#72). `core/analysis.py` is pure logic over a filled model — no
provider, no session, no filesystem — and it is the central design idea: the engine does not ask
because a slot is empty, it asks where information value is high.
"""
from _fakes import out, slot

from requivo.core.analysis import estimate_confidence, readiness_blockers, soft_slots, state_of, understanding_view
from requivo.core.contracts import EngineOutput, Slot


def test_soft_slots_are_medium_or_high_and_unresolved():
    # Real slot ids: the model must speak the schema's vocabulary (padded slots stay empty/low → not
    # soft). business_objects precedes business_rules in schema order, so soft comes back in that order.
    model = out({
        "problem": slot(90, "explicit", "high"),           # solid → not soft
        "business_rules": slot(30, "inferred", "high"),    # uncertain + high → soft
        "business_objects": slot(50, "inferred", "medium"),# uncertain + medium → soft
        "reporting": slot(10, "empty", "low"),             # low impact → never soft
    })
    assert soft_slots(model) == ["business_objects", "business_rules"]


def test_estimate_confidence_tiers():
    assert estimate_confidence(0) == "high"
    assert estimate_confidence(1) == "high"
    assert estimate_confidence(3) == "medium"
    assert estimate_confidence(5) == "low"


def test_readiness_blockers_are_high_impact_unconfirmed():
    # Padded slots are low-impact → never blockers; only the high-impact-unconfirmed override is.
    model = out({
        "problem": slot(90, "explicit", "high"),         # confirmed → not blocking
        "business_rules": slot(80, "inferred", "high"),  # high but inferred → blocker
        "success_metrics": slot(0, "empty", "medium"),   # medium → not blocking
    })
    assert readiness_blockers(model) == ["business_rules"]


def test_readiness_flags_a_missing_high_impact_slot_as_blocker():
    # The north-star guard: a required high-impact slot the model omitted entirely must NOT vanish
    # from readiness. Build a model with everything explicit EXCEPT business_rules, then drop it.
    from requivo.core.contracts import schema_slot_ids

    _, required = schema_slot_ids()
    model_dict = {sid: slot(90, "explicit", "high") for sid in required}
    del model_dict["business_rules"]  # a high-impact dimension goes missing
    model = EngineOutput.model_validate({"model": model_dict, "questions": [], "summary": {}})
    # business_rules is absent, high-impact by default → it is still a blocker, not invisible.
    assert "business_rules" in readiness_blockers(model)


def test_readiness_blocks_a_thin_high_impact_slot_even_when_explicit():
    # Provenance is not coverage: a high-impact slot stated in one word (completeness below the soft
    # boundary) is NOT resolved, even if its confidence is explicit — it must still block, not read as
    # confirmed. Guards the readiness fix that gates on completeness, not confidence alone.
    model = out({
        "business_rules": slot(5, "explicit", "high"),   # explicit but thin → still a blocker
        "problem": slot(90, "explicit", "high"),         # explicit AND covered → not blocking
    })
    blockers = readiness_blockers(model)
    assert "business_rules" in blockers
    assert "problem" not in blockers


def test_state_of_maps_confidence():
    assert state_of(Slot(completeness=90, confidence="explicit", impact="high")) == "confirmed"
    assert state_of(Slot(completeness=50, confidence="inferred", impact="high")) == "inferred"
    assert state_of(Slot(completeness=0, confidence="empty", impact="low")) == "unknown"


def test_state_of_gives_testable_its_own_bucket():
    # #610: a reader must never mistake a named, deferred-to-test gap for a confirmed fact or a
    # plain open question -- it is neither, so it is a fourth bucket, not folded into "unknown".
    s = Slot(completeness=0, confidence="testable", impact="high", test_plan="Run a landing-page test.")
    assert state_of(s) not in ("confirmed", "unknown")
    assert state_of(s) == "to_test"


def test_readiness_does_not_block_on_a_slot_deferred_to_a_test():
    # #610: the state's whole point is that a known unknown deliberately deferred to a test does not
    # keep the target persona (a builder) from ever reaching "ready".
    model = out({
        "problem": slot(90, "explicit", "high"),
        "business_rules": {"completeness": 0, "confidence": "testable", "impact": "high",
                           "test_plan": "Ship a waitlist and see if 20 people sign up."},
    })
    assert "business_rules" not in readiness_blockers(model)


def test_readiness_is_not_reached_on_self_asserted_beliefs_about_the_world_alone():
    """#611: with no client, `explicit` means the requester is the authority for the fact -- true of
    their own intent, false of their guess about the world (model_schema.json's `driver.confidence`
    and engine.md state the rule the engine grades by). A world-belief graded honestly lands
    `inferred`, which still blocks high impact, so a model built entirely that way cannot read
    "ready" on how much the user typed. Pins the *effect* of the rule; the classification itself is
    the engine's own judgment, per CLAUDE.md's single-LLM-call architecture, not something this
    test -- or any code in `core/` -- can carry out itself."""
    model = out({
        "success_metrics": slot(90, "inferred", "high"),   # "I think people will pay for this"
        "problem": slot(90, "inferred", "high"),            # "I think this is a real problem"
    })
    assert readiness_blockers(model)


def test_a_solo_builders_own_intent_still_reaches_explicit_confidence():
    # #611's other half: the fix must not make a solo builder's session permanently second-class.
    # A fact about what THEY decided (not about the world) is still gradeable at full confidence.
    model = out({
        "constraints": slot(90, "explicit", "high"),   # "I'm giving myself a two-week budget"
        "workflow": slot(90, "explicit", "high"),       # "I've decided the approval has one step"
    })
    assert "constraints" not in readiness_blockers(model)
    assert "workflow" not in readiness_blockers(model)


def test_understanding_view_carries_a_to_test_group():
    # The groups dict every JSON/Web reader iterates -- a new bucket that stays absent from it would
    # silently drop every testable slot from `requivo status --json` and the Web's understanding row.
    model = out({
        "business_rules": {"completeness": 0, "confidence": "testable", "impact": "high",
                           "test_plan": "Run a pricing survey."},
    })
    groups = understanding_view(model)
    assert "to_test" in groups
    assert [e["slot"] for e in groups["to_test"]] == ["business_rules"]


def test_the_four_slot_projections_all_read_from_one_schema_parse(monkeypatch):
    """#301. `schema_slot_ids`/`_schema_order` and `slot_meta`/`_default_impacts` used to each parse
    `model_schema.json` independently; all four now project from `schema_slots()`, the one cached
    parse they share. Caches are cleared explicitly (not assumed cold) and `contracts.json.loads`
    is spied on, so what is measured is the parse count itself, not a proxy for it.
    """
    from requivo.core import analysis, contracts

    for fn in (contracts.schema_slots, contracts.schema_slot_ids, contracts._schema_order,
               analysis.slot_meta, analysis._default_impacts):
        fn.cache_clear()

    calls = []
    original = contracts.json.loads

    def spy(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    monkeypatch.setattr(contracts.json, "loads", spy)

    contracts.schema_slot_ids()
    contracts._schema_order()
    analysis.slot_meta()
    analysis._default_impacts()

    assert len(calls) == 1, (
        f"the schema file was parsed {len(calls)} time(s) across four projections, not the one "
        f"`schema_slots()` is meant to unify them onto"
    )
