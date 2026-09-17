"""Invariant 8: `.requivo/sessions/` is the interface between every surface, at `format_version` 1. A session
an older Requivo wrote still loads (session.json); a model a *newer* Requivo wrote loads, survives a
refinement turn, and is still refused at the provider boundary (model.json, #14)."""
from __future__ import annotations

import json
import typing

import pytest
from pydantic import BaseModel, ValidationError

from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, ModelProposal, PersistedEngineOutput
from requivo.core.errors import RequivoError
from requivo.services.sessions import SessionService

# ── the session format is public ──────────────────────────────────────────────


# Verbatim shape of a session.json as 0.8.2 wrote it.
SESSION_JSON_0_8_2 = """{
  "format_version": 1,
  "requivo_version": "0.8.2",
  "session_id": "d4f1a0c2e5b74d0e9a3c8b1f2e6d7a45",
  "slug": "leave-approval",
  "created_at": "2026-07-30T09:12:00Z",
  "updated_at": "2026-07-30T09:41:00Z",
  "provider": "anthropic",
  "model_name": "claude-sonnet-5",
  "context_cards": null,
  "request_hash": "sha256:6b2f1c",
  "schema_version": 1,
  "prompt_versions": {},
  "current_revision": 2,
  "revisions": [
    {"revision": 1, "created_at": "2026-07-30T09:12:00Z", "previous_revision": null,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-discover",
     "prompt_version": null, "model_hash": "sha256:aaa"},
    {"revision": 2, "created_at": "2026-07-30T09:41:00Z", "previous_revision": 1,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-answer",
     "prompt_version": null, "model_hash": "sha256:bbb"}
  ],
  "artifact_status": {
    "prd": {"revision": 2, "filename": "prd.md", "updated_at": "2026-07-30T09:42:00Z", "stale": false}
  }
}"""


def test_a_session_written_by_an_older_requivo_still_loads(workspace):
    """Invariant 8, the backward half. `.requivo/sessions/` is the interface between every surface, at
    `format_version` 1. Adding a field is free (#286)."""
    d = store.canonical_dir("leave-approval")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2)

    meta = store.read_meta("leave-approval")
    assert meta.current_revision == 2 and meta.provider == "anthropic"
    assert [r.surface for r in meta.revisions] == ["cli-discover", "cli-answer"]
    assert meta.artifact_status["prd"].filename == "prd.md"
    assert meta.artifact_status["prd"].stale is False
    # A field this version dropped is ignored, not fatal.
    assert not hasattr(meta, "prompt_versions")
    # Fields added since simply take their defaults.
    assert meta.revisions[0].prompt_version is None


# ── the malformed-session family (#82) ───────────────────────────────────────


def test_nothing_raises_the_malformed_session_family_base():
    """The base is a family, and a family with a raise site is not one (#82)."""
    import re
    from pathlib import Path

    import requivo

    root = Path(requivo.__file__).parent
    files = sorted(root.rglob("*.py"))
    # must fire: a glob over a moved package returns [], and `assert not []` is an all-clear nobody earned.
    assert len(files) >= 10, f"scan set looks blind: {files}"

    pattern = re.compile(r"\braise\s+InvalidSessionError\s*\(")
    offenders = {}
    for p in files:
        hits = [i for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if pattern.search(line)]
        if hits:
            offenders[str(p.relative_to(root))] = hits
    assert not offenders, (
        "these sites raise the family base instead of an arm that names the fact; give the condition "
        f"its own subclass and a row in requivo/http.py::STATUS_BY_CODE: {offenders}")


def test_every_arm_of_the_family_names_a_distinct_fact():
    """Eleven arms, eleven codes, and none of them the base (#101)."""
    from requivo.core.errors import InvalidSessionError
    from requivo.services import artifacts  # noqa: F401  - registers the two service-layer arms

    def arms(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from arms(sub)

    codes = {a.code for a in arms(InvalidSessionError)}
    assert codes == {
        "unsupported_format_version", "unsupported_schema_version", "session_unreadable",
        "model_unreadable", "artifact_revision_out_of_range", "unstated_source_revision",
        "unreadable_source_revision", "inconsistent_archive", "unreadable_archive",
        "invalid_archive", "import_move_failed", "unknown_perimeter",
    }
    assert "invalid_session" not in codes, "the base is the family, not an arm"


def test_a_session_from_a_newer_requivo_is_refused_not_guessed(workspace):
    d = store.canonical_dir("from-the-future")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2.replace('"format_version": 1', '"format_version": 2'))
    with pytest.raises(RequivoError) as ei:
        store.read_meta("from-the-future")
    assert ei.value.code == "unsupported_format_version"
    assert "upgrade requivo" in str(ei.value).lower()
    # `newer than what` is half the fact, and #82 made the payload carry it.
    assert ei.value.details == {"format_version": 2, "supported_format_version": 1}


# ── the other half of the format promise: model.json (#14) ────────────────────


def _model_from_the_future() -> dict:
    """A model as a Requivo one minor version ahead might write it."""
    m = _full_model()
    m["risk_register"] = [{"id": "R1", "text": "adoption"}]        # a new top-level collection
    m["model"]["workflow"]["provenance"] = "interview-3"           # a new field inside a slot
    m["summary"]["horizon"] = "two quarters"                       # a new field inside the summary
    m["questions"] = [{"q": "Who approves?", "slot": "workflow", "why": "unclear", "asked_at": "r2"}]
    m["decisions"] = [{"decision": "Approve-first", "derived_from": ["workflow"], "settled_at": "r1"}]
    return m


def test_a_model_written_by_a_newer_requivo_loads_and_survives_a_round_trip(workspace):
    """The forward half of invariant 8, for `model.json` (#14)."""
    store.create_session("mixed", "A leave approval system.")
    d = store.canonical_dir("mixed")
    (d / "model.json").write_text(json.dumps(_model_from_the_future(), indent=2), encoding="utf-8")

    loaded = store.load_session_model("mixed")           # used to raise ValidationError
    assert loaded.summary.objective == "A leave approval system"
    assert loaded.model["workflow"].confidence.value == "empty"

    # Loading is only half of it.
    store.save_revision("mixed", loaded)
    for path in (d / "model.json", d / "revisions" / "0001-model.json"):
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["risk_register"] == [{"id": "R1", "text": "adoption"}], path.name
        assert written["model"]["workflow"]["provenance"] == "interview-3", path.name
        assert written["summary"]["horizon"] == "two quarters", path.name
        assert written["questions"][0]["asked_at"] == "r2", path.name
        assert written["decisions"][0]["settled_at"] == "r1", path.name


def test_a_slot_confidence_this_version_does_not_know_survives_a_round_trip_unread_as_explicit(workspace):
    """Invariant 8, extended to a closed enum, not only unknown keys (#610)."""
    store.create_session("future-confidence", "A leave approval system.")
    d = store.canonical_dir("future-confidence")
    m = _full_model(workflow=_slot(90, "measured", "high"))   # a confidence value this build refuses
    (d / "model.json").write_text(json.dumps(m, indent=2), encoding="utf-8")

    loaded = store.load_session_model("future-confidence")   # must not raise
    assert loaded.model["workflow"].confidence == "measured"
    from requivo.core.analysis import readiness_blockers
    assert "workflow" in readiness_blockers(loaded)   # tolerated, never promoted to "confirmed"


def test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save(workspace):
    """The half the first version of this fix got wrong, and the reason it is worth a second test (#14)."""
    store.create_session("refined", "A leave approval system.")
    d = store.canonical_dir("refined")
    (d / "model.json").write_text(json.dumps(_model_from_the_future(), indent=2), encoding="utf-8")
    store.save_revision("refined", store.load_session_model("refined"))   # now at revision 1

    # An ordinary refinement turn: the full slot set and a new objective.
    SessionService().update_model("refined", {**_full_model(), "summary": {"objective": "Refined"}})

    written = json.loads((d / "model.json").read_text(encoding="utf-8"))
    assert written["summary"]["objective"] == "Refined", "the turn did not land"
    assert written["decisions"][0]["settled_at"] == "r1"
    assert written["risk_register"] == [{"id": "R1", "text": "adoption"}]
    # And the narrowing that is real, pinned so it is a stated limit rather than a belief.
    assert "provenance" not in written["model"]["workflow"]
    assert "horizon" not in written["summary"]


def test_the_provider_boundary_still_refuses_the_same_payload():
    """The positive control for the test above, and the reason the fix is a sibling contract rather than a
    relaxed flag on `StrictModel`."""
    for contract in (EngineOutput, ModelProposal):
        with pytest.raises(ValidationError) as ei:
            contract.model_validate(_model_from_the_future())
        assert "risk_register" in str(ei.value)
    # And permissive is not the same as credulous: the persisted contract still enforces the slot vocabulary.
    with pytest.raises(ValidationError):
        PersistedEngineOutput.model_validate(
            {**_full_model(), "model": {**_full_model()["model"], "real_problem": _slot()}})


def _contracts_reachable_from(cls: type[BaseModel]) -> set[type[BaseModel]]:
    """Every pydantic contract reachable from `cls` through its own fields, including `cls`."""
    found: set[type[BaseModel]] = set()

    def walk_type(ann) -> None:
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            walk_model(ann)
        for arg in typing.get_args(ann):
            walk_type(arg)

    def walk_model(model: type[BaseModel]) -> None:
        if model in found:
            return
        found.add(model)
        for field in model.model_fields.values():
            walk_type(field.annotation)

    walk_model(cls)
    return found


def test_the_persisted_contract_is_permissive_all_the_way_down():
    """The guard on the shape of the fix. `PersistedEngineOutput` mirrors the model tree class by class."""
    persisted = _contracts_reachable_from(PersistedEngineOutput)
    strict = _contracts_reachable_from(EngineOutput)
    # A walk that finds nothing is an all-clear nobody earned (#604).
    assert len(persisted) == len(strict) >= 9
    assert [c.__name__ for c in persisted if c.model_config.get("extra") != "allow"] == []
    assert [c.__name__ for c in strict if c.model_config.get("extra") != "forbid"] == []


def test_the_persisted_mirror_copies_every_constraint_it_restates():
    """The sibling of the walk above, for the other half of a field's contract (#14)."""
    pairs = [(p, p.__mro__[1]) for p in _contracts_reachable_from(PersistedEngineOutput)]
    for permissive, strict in pairs:
        assert issubclass(strict, BaseModel) and strict is not BaseModel, permissive.__name__
    # Not vacuous, on both counts: nine twins exist (#604 added Threshold/PersistedThreshold), and the mirror really does re-point eight fields at permissive types — which is exactly why it has to restate their constraints.
    assert len(pairs) == 9
    redeclared = {name for p, s in pairs for name in p.model_fields
                  if p.model_fields[name].annotation != s.model_fields[name].annotation}
    # `confidence` joined this set with #610: `PersistedSlot` widens it to `Confidence | str` so a value this build does not define survives a round-trip instead of raising (invariant 8).
    assert redeclared == {"model", "questions", "summary", "decisions", "challenges", "opportunities",
                          "exclusions", "thresholds", "confidence"}

    drift = []
    for permissive, strict in pairs:
        assert set(permissive.model_fields) == set(strict.model_fields), permissive.__name__
        for name, pf in permissive.model_fields.items():
            sf = strict.model_fields[name]
            if (pf.metadata != sf.metadata                                     # min/max length, ge/le…
                    or pf.is_required() != sf.is_required()
                    or (pf.default_factory is None) != (sf.default_factory is None)
                    or pf.default != sf.default):
                drift.append(f"{permissive.__name__}.{name}: {sf.metadata} vs {pf.metadata}")
    assert drift == []
