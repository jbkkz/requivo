"""Does a session on disk tell the truth about itself (#210)?"""
from __future__ import annotations

import json
import shutil
import threading

import pytest

from conftest import blind_to_dangling_links as _blind_to_dangling_links
from conftest import full_model as _full_model
from conftest import healthy_session as _healthy
from conftest import slot as _slot
from conftest import symlink_or_skip as _symlink_or_skip
from requivo.core import persistence as store
from requivo.core.context import check_selection
from requivo.core.errors import InvalidSlugError, RequivoError
from requivo.core.integrity import check_session, inspect_session, newest_readable_revision, readable_revision
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

# ── forward compatibility of the session file ─────────────────────────────────


def test_a_field_from_a_future_requivo_survives_a_round_trip(workspace):
    """`docs/compatibility.md` promises that adding a field is a compatible change."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["future_field"] = {"added_by": "requivo 1.4", "keep": True}
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())        # a real mutation: reads, then rewrites session.json
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["future_field"] == {"added_by": "requivo 1.4", "keep": True}
    assert after["current_revision"] == 1       # and the known fields still moved


def test_a_retired_key_is_dropped_rather_than_carried_forever(workspace):
    """The mirror image: `extra="allow"` must not resurrect keys a past Requivo retired."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["prompt_versions"] = {"engine.md": "sha256:dead"}   # declared once, never written, now retired
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())
    assert "prompt_versions" not in json.loads(p.read_text(encoding="utf-8"))


# ── slug bounds ───────────────────────────────────────────────────────────────


def test_a_long_request_yields_a_slug_the_filesystem_accepts(workspace):
    """A request is arbitrary user text. One 300-character word made a 300-character directory name and the
    write failed deep inside with a bare `OSError: File name too long`."""
    long_word = "a" * 300
    slug = store.derive_slug(f"{long_word} system")
    assert len(slug) <= store.MAX_SLUG_LENGTH
    store.validate_slug(slug)                    # still a well-formed kebab-case token

    # Truncation must not merge two different requests into one session directory.
    other = store.derive_slug(f"{long_word} platform")
    assert slug != other

    svc = SessionService()
    meta = svc.create_session(f"{long_word} system")   # and the whole path is writable
    assert store.canonical_dir(meta.slug).is_dir()


def test_an_explicit_over_long_slug_is_refused_at_the_boundary(workspace):
    with pytest.raises(InvalidSlugError) as e:
        store.validate_slug("x" * (store.MAX_SLUG_LENGTH + 1))
    assert e.value.code == "invalid_slug"
    assert e.value.details["max_length"] == store.MAX_SLUG_LENGTH


# ── artifact freshness ────────────────────────────────────────────────────────


def test_an_artifact_saved_against_an_older_revision_is_recorded_stale(workspace):
    """Saving is not the same moment as reasoning."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                        # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new")}))  # 2

    st = art.save("s", "prd", "# PRD\n", source_revision=1)   # reasoned from 1, saved at 2
    assert st.revision == 1
    assert st.stale is True
    assert art.list("s")["prd"]["stale"] is True


def test_an_older_revision_that_missed_the_artifact_leaves_it_fresh(workspace):
    """The control. Staleness is the dependency graph, not revision drift."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    # `current_process` is in no artifact's slot set except the assessment's `*`.
    svc.update_model("s", _full_model(**{"current_process": _slot(80, "explicit", "high", "email")}))

    assert art.save("s", "prd", "# PRD\n", source_revision=1).stale is False
    assert art.save("s", "brief", "# Assessment\n", source_revision=1).stale is True


# ── the reasoning layer as a dependency ───────────────────────────────────────


def _with_decision(model: dict, why: str) -> dict:
    model["decisions"] = [{"decision": "Draft-first", "why": why, "derived_from": ["permissions"]}]
    return model


def test_reasoning_that_changes_without_a_slot_moving_still_invalidates(workspace):
    """Every generator is prompted with the whole model, reasoning included, so a rewritten design decision
    can change the PRD with no slot touched."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)
    assert art.list("s")["prd"]["stale"] is False

    result = svc.update_model("s", _with_decision(_full_model(), "reviewers were the bottleneck"))
    assert result.changed_slots == []                       # the facts did not move
    assert len(result.changed_decisions) == 1               # the judgment over them did
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True
    assert "changed_decisions" in result.to_dict()


def test_reasoning_merely_omitted_by_a_turn_is_preserved(workspace):
    """A refinement turn answers a question; it does not re-derive the brief."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", _full_model())           # same slots, reasoning simply absent
    assert [d.why for d in svc.load_model("s").decisions] == ["drafts are cheap"]
    assert result.changed_decisions == []
    assert result.stale_artifacts == []
    assert art.list("s")["prd"]["stale"] is False


def test_reasoning_explicitly_replaced_is_a_change_that_invalidates(workspace):
    """The other side of the tri-state: a proposal that *states* its reasoning replaces what was there."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    replacement = {**_full_model(),
                   "decisions": [{"decision": "Approve-first", "derived_from": ["permissions"]}]}
    result = svc.update_model("s", replacement)
    assert [d.decision for d in svc.load_model("s").decisions] == ["Approve-first"]
    assert len(result.changed_decisions) == 2               # the one dropped, the one added
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def test_reasoning_explicitly_emptied_is_a_deletion_that_invalidates(workspace):
    """`"decisions": []` is a statement, not a silence."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", {**_full_model(), "decisions": []})
    assert svc.load_model("s").decisions == []
    assert len(result.changed_decisions) == 1               # the deletion is reported, not absorbed
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def _with_exclusion(model: dict, reason: str) -> dict:
    model["exclusions"] = [{"option": "Bulk import", "reason": reason, "rests_on": ["permissions"]}]
    return model


def test_an_exclusion_merely_omitted_by_a_turn_is_preserved(workspace):
    """#599, invariant 10's fourth collection: a refinement turn that says nothing about `exclusions` must
    leave the established one standing, exactly like decisions/challenges."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_exclusion(_full_model(), "Out of scope for v1"))

    result = svc.update_model("s", _full_model())            # same slots, exclusions simply absent
    assert [e.reason for e in svc.load_model("s").exclusions] == ["Out of scope for v1"]
    assert result.changed_exclusions == []


def test_an_exclusion_explicitly_emptied_invalidates_what_rests_on_it(workspace):
    """#599: `"exclusions": []` deletes, like `"decisions": []`."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_exclusion(_full_model(), "Out of scope for v1"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", {**_full_model(), "exclusions": []})
    assert svc.load_model("s").exclusions == []
    assert len(result.changed_exclusions) == 1
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def _with_threshold(model: dict, action: str) -> dict:
    model["thresholds"] = [{"condition": "CAC exceeds the stated budget ceiling",
                            "measure": "cost per paid signup", "action": action,
                            "rests_on": ["permissions"]}]
    return model


def test_a_threshold_merely_omitted_by_a_turn_is_preserved(workspace):
    """#604, invariant 10's fifth collection: a refinement turn that says nothing about `thresholds` must
    leave the established one standing, exactly like decisions/challenges/ exclusions."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_threshold(_full_model(), "stop the paid channel"))

    result = svc.update_model("s", _full_model())            # same slots, thresholds simply absent
    assert [t.action for t in svc.load_model("s").thresholds] == ["stop the paid channel"]
    assert result.changed_thresholds == []


def test_a_threshold_explicitly_emptied_invalidates_what_rests_on_it(workspace):
    """#604: `"thresholds": []` deletes, like `"exclusions": []`."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_threshold(_full_model(), "stop the paid channel"))
    art.save("s", "prd", "# PRD\n", source_revision=1)

    result = svc.update_model("s", {**_full_model(), "thresholds": []})
    assert svc.load_model("s").thresholds == []
    assert len(result.changed_thresholds) == 1
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


# ── the second version contract: the slot vocabulary ──────────────────────────


def test_a_session_from_a_newer_slot_schema_is_refused_clearly(workspace):
    """`schema_version` was recorded on every session and read by nothing."""
    from requivo.core.errors import InvalidSessionError

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["schema_version"] = store.SCHEMA_VERSION + 1
    p.write_text(json.dumps(raw))

    with pytest.raises(InvalidSessionError) as e:
        store.read_meta("s")
    assert e.value.details["schema_version"] == store.SCHEMA_VERSION + 1
    # An older schema is ordinary backward compatibility, not an error.
    raw["schema_version"] = 0
    p.write_text(json.dumps(raw))
    assert store.read_meta("s").slug == "s"


# ── reasoning identity, and one-snapshot reads (invariant 12) ─────────────────


def test_a_repeated_reasoning_item_is_refused_rather_than_deduplicated(workspace):
    """Ids are content-derived, so two identical decisions collide on one id."""

    model = _full_model()
    model["decisions"] = [
        {"decision": "Draft-first", "derived_from": ["permissions"]},
        {"decision": "Draft-first", "derived_from": ["workflow"]},   # same text → same id
    ]
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    with pytest.raises(RequivoError) as e:
        svc.update_model("s", model)
    assert "repeated" in str(e.value).lower()


def test_a_snapshot_cannot_report_one_revision_and_another_revisions_model(workspace):
    """Every provider-backed operation reads a revision and a model before it reasons."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"workflow": _slot(50, "inferred", "medium", "first")}))

    reading, wrote = threading.Event(), threading.Event()

    def concurrent_apply() -> None:
        reading.wait(timeout=10)
        try:
            SessionService().update_model(
                "s", _full_model(**{"workflow": _slot(90, "explicit", "high", "second")}))
        finally:
            wrote.set()

    writer = threading.Thread(target=concurrent_apply)
    writer.start()

    # Widen the window between the two reads to whatever the writer needs.
    real_read_meta = svc.repo.read_meta

    def slow_read_meta(slug):
        meta = real_read_meta(slug)
        reading.set()
        wrote.wait(timeout=0.5)
        return meta

    svc.repo.read_meta = slow_read_meta
    snap = svc.snapshot("s")
    writer.join(timeout=10)

    assert snap.revision == 1
    assert snap.model.model["workflow"].value == "first"   # the model *of* revision 1, not a later one


# ── session integrity: does a session tell the truth about itself? ────────────


def _point_artifact_at(slug: str, filename: str) -> None:
    """Rewrite the recorded artifact's `filename` in `session.json`."""
    p = store.canonical_dir(slug) / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["prd"]["filename"] = filename
    p.write_text(json.dumps(raw))


def test_a_coherent_session_reports_no_problems(workspace):
    _healthy()
    assert check_session("s") == []


def test_check_session_waits_for_a_concurrent_writer_instead_of_reporting_a_tear(workspace,
                                                                                  monkeypatch):
    """#263. `check_session` used to read session.json, the revision files and model.json with no lock, so it
    could observe `save_revision`'s compound write torn."""
    svc = _healthy()
    # Patched on the `Store` class, not the module function (#272).
    real_write_meta = store.Store.write_meta
    at_the_gate, release = threading.Event(), threading.Event()

    def paused(self, slug, meta):
        if slug == "s" and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, slug, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)

    write_failures: list[BaseException] = []

    def _write():
        try:
            svc.update_model("s", _full_model(**{"problem": _slot(90, "explicit", "high",
                                                                    "moved again")}))
        except BaseException as e:  # noqa: BLE001 - reported, not swallowed
            write_failures.append(e)

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"

    check_results: list[list] = []
    checker_done = threading.Event()

    def _check():
        check_results.append(check_session("s"))
        checker_done.set()

    checker = threading.Thread(target=_check, daemon=True)
    checker.start()

    # Must fire: a checker racing the writer must not read past the lock while the writer still holds it.
    assert not checker_done.wait(1.0), (
        "check_session read past a writer still mid-save instead of waiting for its lock")

    release.set()
    writer.join(10)
    checker.join(10)
    assert not write_failures, f"the writer failed: {write_failures}"
    assert checker_done.is_set(), "check_session never returned once the writer released the lock"
    assert check_results == [[]], (
        f"a healthy session mid-save must report clean once the writer finishes, got {check_results}")


def test_a_session_whose_history_is_gone_is_caught(workspace):
    """The reviewer's repro, and the one shape that used to pass every check."""
    _healthy()
    for f in (store.canonical_dir("s") / "revisions").glob("*.json"):
        f.unlink()
    codes = {p.code for p in check_session("s")}
    assert codes == {"missing_revision_file"}


def test_a_model_swapped_out_from_under_its_hash_is_caught(workspace):
    """Every revision records the hash of what was written."""
    _healthy()
    d = store.canonical_dir("s")
    (d / "model.json").write_text(json.dumps(_full_model(**{"problem": _slot(90, "explicit", "high", "other")})))
    codes = {p.code for p in check_session("s")}
    assert "model_is_not_the_last_revision" in codes


def test_a_hand_edited_revision_file_is_caught(workspace):
    _healthy()
    f = store.canonical_dir("s") / "revisions" / "0001-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert "revision_hash_mismatch" in {p.code for p in check_session("s")}


def test_a_recorded_artifact_with_no_file_is_caught(workspace):
    _healthy()
    (store.canonical_dir("s") / "artifacts" / "prd.md").unlink()
    assert "missing_artifact_file" in {p.code for p in check_session("s")}


def test_a_model_from_a_newer_requivo_is_not_reported_as_a_defect(workspace):
    """#14. The checker read model.json through the strict boundary contract, so an unknown key."""
    _healthy()
    d = store.canonical_dir("s")
    for f in (d / "model.json", d / "revisions" / "0001-model.json"):
        payload = json.loads(f.read_text(encoding="utf-8"))
        f.write_text(json.dumps({**payload, "risk_register": []}), encoding="utf-8")
    codes = {p.code for p in check_session("s")}
    assert "invalid_model" not in codes
    assert "invalid_revision_model" not in codes

    # The positive control.
    (d / "model.json").write_text('{"model": {"workflow": ', encoding="utf-8")
    (d / "revisions" / "0001-model.json").write_text('{"model": {"workflow": ', encoding="utf-8")
    broken = {p.code for p in check_session("s")}
    assert "invalid_model" in broken
    assert "invalid_revision_model" in broken


def test_a_structurally_invalid_session_json_is_a_problem_not_a_traceback(workspace):
    """A session.json that is valid JSON but not valid metadata raised a bare Pydantic `ValidationError`
    through the CLI."""
    _healthy()
    (store.canonical_dir("s") / "session.json").write_text('{"slug": "s"}')  # no session_id, no dates
    codes = {p.code for p in check_session("s")}
    assert codes == {"invalid_session_json"}


def test_a_revision_log_that_does_not_match_the_revision_count_is_caught(workspace):
    _healthy()
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["revisions"] = raw["revisions"][:1]          # claims revision 2, logs one
    p.write_text(json.dumps(raw))
    assert "revision_count_mismatch" in {p_.code for p_ in check_session("s")}


def test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session(
        workspace, tmp_path):
    """An existence oracle: `st.filename` was read out of `session.json` and joined straight into the
    artifacts directory, so `.is_file()` was called on whatever it named."""
    present = tmp_path / "outside-present.md"
    present.write_text("secret\n")
    absent = tmp_path / "outside-absent.md"
    assert present.is_file() and not absent.exists(), "fixture is blind: the two paths must differ"

    verdicts = []
    for probe in (present, absent):
        _healthy("probe")
        _point_artifact_at("probe", str(probe))
        verdicts.append({p.code for p in check_session("probe")})
        shutil.rmtree(store.canonical_dir("probe"))

    assert verdicts[0] == verdicts[1], (
        f"the verdict leaks whether {present} exists: {verdicts[0]} vs {verdicts[1]}")
    assert "unsafe_artifact_filename" in verdicts[0]
    assert "missing_artifact_file" not in verdicts[0], (
        "reporting the artifact as merely missing means the path outside the session was stat-ed")


def test_an_unknown_artifact_type_does_not_fall_through_to_the_filesystem(workspace, tmp_path):
    """The fall-through the #23 lane's auditor named."""
    outside = tmp_path / "outside.md"
    outside.write_text("x\n")

    _healthy("probe")
    p = store.canonical_dir("probe") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["not-an-artifact-type"] = dict(raw["artifact_status"]["prd"],
                                                          filename=str(outside))
    p.write_text(json.dumps(raw))

    assert "unknown_artifact_type" in {pr.code for pr in inspect_session("probe")}   # must-fire
    codes = {pr.code for pr in check_session("probe")}
    assert "unsafe_artifact_filename" in codes       # and its filename was refused, not followed
    assert "missing_artifact_file" not in codes


def test_a_safe_but_missing_artifact_file_is_still_reported(workspace):
    """The must-fire control for the two tests above."""
    _healthy()
    (store.canonical_dir("s") / "artifacts" / "prd.md").unlink()
    codes = {p.code for p in check_session("s")}
    assert "missing_artifact_file" in codes
    assert "unsafe_artifact_filename" not in codes


def test_an_artifact_that_is_a_symlink_out_of_the_session_is_still_refused(workspace, tmp_path):
    """The branch the #3 fix leans on. `check_session_dir` now resolves the artifact path only when something
    is there."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not part of this session", encoding="utf-8")

    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", outside)
    assert "unsafe_artifact_filename" in {p.code for p in check_session("s")}

    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")   # dangling, still outside
    assert not (artifacts / "prd.md").exists() and (artifacts / "prd.md").is_symlink()
    assert "unsafe_artifact_filename" in {p.code for p in check_session("s")}


def test_an_artifact_symlink_is_reported_unsafe_where_the_platform_cannot_resolve_it(workspace,
                                                                                     tmp_path,
                                                                                     monkeypatch):
    """`session verify`'s copy of the decision, blinded the same way."""
    _healthy()
    artifacts = store.canonical_dir("s") / "artifacts"
    (artifacts / "prd.md").unlink()
    _symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")
    _blind_to_dangling_links(monkeypatch)
    codes = {p.code for p in check_session("s")}
    assert "unsafe_artifact_filename" in codes
    assert "missing_artifact_file" not in codes


def test_a_context_card_that_no_longer_resolves_is_not_an_integrity_problem(workspace, tmp_path,
                                                                            monkeypatch):
    """The boundary of what this module answers, pinned as behaviour rather than left in a docstring."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))

    svc = SessionService()
    slug = svc.create_session("Something.", context_cards=["lost-domain"], slug="carded").slug
    assert store.read_meta(slug).context_cards == ["lost-domain"]
    assert check_session(slug) == []                 # must-fire control: the session is coherent

    (cards / "lost-domain.md").unlink()
    assert check_session(slug) == [], "integrity must not depend on what this machine has installed"
    # …and the environment check, which is where it belongs, does see it.
    assert check_selection(store.read_meta(slug).context_cards) is not None


# ── newest_readable_revision: the repair-target search (#210) ─────────────────


def test_newest_readable_revision_returns_the_latest_when_everything_parses(workspace):
    _healthy()  # revision 1, then 2 (see _healthy's own docstring)
    d = store.canonical_dir("s")
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 2
    # byte-for-byte, not a re-serialized model -- restoring from this payload must reproduce the revision file's own content_hash (see ReadableRevision's docstring).
    assert found.payload == (d / "revisions" / "0002-model.json").read_text(encoding="utf-8")


def test_newest_readable_revision_skips_a_broken_file_and_returns_an_older_one(workspace):
    _healthy()
    d = store.canonical_dir("s")
    (d / "revisions" / "0002-model.json").write_text("{not json", encoding="utf-8")
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 1


def test_newest_readable_revision_treats_a_missing_file_the_same_as_an_unreadable_one(workspace):
    _healthy()
    d = store.canonical_dir("s")
    (d / "revisions" / "0002-model.json").unlink()
    found = newest_readable_revision(d, 2)
    assert found is not None and found.revision == 1


def test_newest_readable_revision_returns_none_when_nothing_in_range_is_readable(workspace):
    """The honest third answer -- must be able to say plainly that there is nothing to restore from."""
    _healthy()
    d = store.canonical_dir("s")
    for f in (d / "revisions").glob("*.json"):
        f.write_text("{not json", encoding="utf-8")
    assert newest_readable_revision(d, 2) is None


def test_newest_readable_revision_skips_a_revision_whose_hash_no_longer_matches(workspace):
    _healthy()
    d = store.canonical_dir("s")
    meta = store.read_meta("s")
    hashes = {r.revision: r.model_hash for r in meta.revisions}

    # must-fire control: with the real hashes, revision 2 (the latest) is trusted
    assert newest_readable_revision(d, 2, expected_hashes=hashes).revision == 2

    # tamper with revision 2's file after the fact -- it still parses, but no longer matches
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    found = newest_readable_revision(d, 2, expected_hashes=hashes)
    assert found is not None and found.revision == 1, (
        "a tampered-but-parseable revision must be skipped, not trusted")


def test_newest_readable_revision_with_no_expected_hashes_trusts_anything_that_parses(workspace):
    """The permissive default, stated as behaviour: when the caller has no hash log to check against (or
    passes none), a revision that merely parses is still returned."""
    _healthy()
    d = store.canonical_dir("s")
    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert newest_readable_revision(d, 2).revision == 2


def test_readable_revision_checks_one_specific_number(workspace):
    _healthy()
    d = store.canonical_dir("s")
    meta = store.read_meta("s")
    hashes = {r.revision: r.model_hash for r in meta.revisions}

    assert readable_revision(d, 2, expected_hashes=hashes).revision == 2
    assert readable_revision(d, 1, expected_hashes=hashes).revision == 1
    assert readable_revision(d, 3, expected_hashes=hashes) is None  # does not exist

    f = d / "revisions" / "0002-model.json"
    f.write_text(f.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1))
    assert readable_revision(d, 2, expected_hashes=hashes) is None, (
        "a named revision that no longer matches its recorded hash must be refused, not silently "
        "returned")
