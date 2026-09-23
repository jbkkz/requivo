"""Does a session on disk tell the truth about itself (#210, #263, #14), and the containment checks #3's first
Windows leg found (invariant 17: `_child_of`, `artifact_path`, dangling symlinks)."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from _fakes import full_model, simulate_py314_denied_path, slot

from conftest import blind_to_dangling_links, healthy_session, symlink_or_skip
from requivo.core import persistence as store
from requivo.core.context import check_selection
from requivo.core.errors import InvalidSessionError, InvalidSlugError, RequivoError
from requivo.core.integrity import check_session, inspect_session, newest_readable_revision, readable_revision
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")


def _session(slug: str = "s") -> tuple[SessionService, ArtifactService]:
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    return svc, ArtifactService()


def _rewrite_meta(slug: str, **fields) -> None:
    p = store.canonical_dir(slug) / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.update(fields)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _codes(slug: str = "s") -> set[str]:
    return {p.code for p in check_session(slug)}


# ── the session file: forward compatibility, slug bounds, the schema version ──


def test_a_field_from_a_future_requivo_survives_a_round_trip():
    """`docs/compatibility.md`: adding a field is compatible; a key a past Requivo retired is not resurrected."""
    svc, _ = _session()
    _rewrite_meta("s", future_field={"added_by": "requivo 1.4", "keep": True},
                  prompt_versions={"engine.md": "sha256:dead"})   # declared once, never written, now retired
    svc.update_model("s", full_model())        # a real mutation: reads, then rewrites session.json
    after = json.loads((store.canonical_dir("s") / "session.json").read_text(encoding="utf-8"))
    assert after["future_field"] == {"added_by": "requivo 1.4", "keep": True} and after["current_revision"] == 1
    assert "prompt_versions" not in after


def test_a_long_request_yields_a_slug_the_filesystem_accepts():
    """One 300-character word used to make a 300-character directory name; truncation must not merge two requests."""
    long_word = "a" * 300
    slug = store.derive_slug(f"{long_word} system")
    assert len(slug) <= store.MAX_SLUG_LENGTH and store.validate_slug(slug) == slug
    assert slug != store.derive_slug(f"{long_word} platform")
    assert store.canonical_dir(SessionService().create_session(f"{long_word} system").slug).is_dir()


def test_an_explicit_over_long_slug_is_refused_at_the_boundary():
    with pytest.raises(InvalidSlugError) as e:
        store.validate_slug("x" * (store.MAX_SLUG_LENGTH + 1))
    assert e.value.code == "invalid_slug" and e.value.details["max_length"] == store.MAX_SLUG_LENGTH


def test_a_session_from_a_newer_slot_schema_is_refused_clearly():
    """`schema_version` is read; a newer one is refused with the number, an older one is ordinary compatibility."""
    _session()
    _rewrite_meta("s", schema_version=store.SCHEMA_VERSION + 1)
    with pytest.raises(InvalidSessionError) as e:
        store.read_meta("s")
    assert e.value.details["schema_version"] == store.SCHEMA_VERSION + 1
    _rewrite_meta("s", schema_version=0)
    assert store.read_meta("s").slug == "s"


# ── artifact freshness and the reasoning layer as a dependency (invariants 1, 10) ──


def test_an_artifact_saved_against_an_older_revision_is_stale_only_if_something_it_rests_on_moved():
    """Saving is not the moment of reasoning; staleness is the graph, not revision drift (invariant 1)."""
    svc, art = _session()
    svc.update_model("s", full_model())                                                     # revision 1
    svc.update_model("s", full_model(workflow=slot(80, "explicit", "high", "new")))         # 2: prd consumes workflow
    st = art.save("s", "prd", "# PRD\n", source_revision=1)
    assert (st.revision, st.stale, art.list("s")["prd"]["stale"]) == (1, True, True)

    svc, art = _session("t")
    svc.update_model("t", full_model())
    svc.update_model("t", full_model(current_process=slot(80, "explicit", "high", "email")))   # no artifact's slot but `*`
    assert art.save("t", "prd", "# PRD\n", source_revision=1).stale is False
    assert art.save("t", "brief", "# Assessment\n", source_revision=1).stale is True


def _with_decision(model: dict, why: str) -> dict:
    return {**model, "decisions": [{"decision": "Draft-first", "why": why, "derived_from": ["permissions"]}]}


def test_reasoning_that_changes_without_a_slot_moving_still_invalidates():
    """Every generator is prompted with the whole model, so a rewritten decision changes the PRD with no slot touched."""
    svc, art = _session()
    svc.update_model("s", _with_decision(full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)
    assert art.list("s")["prd"]["stale"] is False

    result = svc.update_model("s", _with_decision(full_model(), "reviewers were the bottleneck"))
    assert (result.changed_slots, len(result.changed_decisions)) == ([], 1)
    assert "prd" in result.stale_artifacts and art.list("s")["prd"]["stale"] is True
    assert "changed_decisions" in result.to_dict()


def test_reasoning_explicitly_replaced_is_a_change_that_invalidates():
    """A proposal that *states* its reasoning replaces what was there: one dropped, one added."""
    svc, art = _session()
    svc.update_model("s", _with_decision(full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n", source_revision=1)
    result = svc.update_model("s", {**full_model(), "decisions": [{"decision": "Approve-first", "derived_from": ["permissions"]}]})
    assert [d.decision for d in svc.load_model("s").decisions] == ["Approve-first"]
    assert len(result.changed_decisions) == 2 and "prd" in result.stale_artifacts


# The three collections invariant 10 treats as tri-state (#599 exclusions, #604 thresholds): key, one item, its text.
_REASONING = {
    "decisions": ({"decision": "Draft-first", "why": "drafts are cheap", "derived_from": ["permissions"]}, "why"),
    "exclusions": ({"option": "Bulk import", "reason": "Out of scope for v1", "rests_on": ["permissions"]}, "reason"),
    "thresholds": ({"condition": "CAC exceeds the stated budget ceiling", "measure": "cost per paid signup",
                    "action": "stop the paid channel", "rests_on": ["permissions"]}, "action"),
}


@pytest.mark.parametrize("key", sorted(_REASONING))
def test_reasoning_merely_omitted_by_a_turn_is_preserved(key):
    """Invariant 10: a refinement turn that says nothing about a collection leaves the established item standing."""
    item, text = _REASONING[key]
    svc, art = _session()
    svc.update_model("s", {**full_model(), key: [item]})
    art.save("s", "prd", "# PRD\n", source_revision=1)
    result = svc.update_model("s", full_model())            # same slots, the collection simply absent
    assert [getattr(x, text) for x in getattr(svc.load_model("s"), key)] == [item[text]]
    assert getattr(result, f"changed_{key}") == [] and result.stale_artifacts == []
    assert art.list("s")["prd"]["stale"] is False


@pytest.mark.parametrize("key", sorted(_REASONING))
def test_reasoning_explicitly_emptied_is_a_deletion_that_invalidates(key):
    """`"<collection>": []` is a statement, not a silence: reported, and it invalidates what rests on it."""
    item, _ = _REASONING[key]
    svc, art = _session()
    svc.update_model("s", {**full_model(), key: [item]})
    art.save("s", "prd", "# PRD\n", source_revision=1)
    result = svc.update_model("s", {**full_model(), key: []})
    assert getattr(svc.load_model("s"), key) == [] and len(getattr(result, f"changed_{key}")) == 1
    assert "prd" in result.stale_artifacts and art.list("s")["prd"]["stale"] is True


def test_a_repeated_reasoning_item_is_refused_rather_than_deduplicated():
    """Ids are content-derived, so two identical decisions collide on one id (invariant 5)."""
    svc, _ = _session()
    twice = [{"decision": "Draft-first", "derived_from": ["permissions"]}, {"decision": "Draft-first", "derived_from": ["workflow"]}]
    with pytest.raises(RequivoError) as e:
        svc.update_model("s", {**full_model(), "decisions": twice})
    assert "repeated" in str(e.value).lower()


def test_a_snapshot_cannot_report_one_revision_and_another_revisions_model():
    """Invariant 12: the revision and the model are read under one lock, so a racing apply cannot split them."""
    svc, _ = _session()
    svc.update_model("s", full_model(workflow=slot(50, "inferred", "medium", "first")))
    reading, wrote = threading.Event(), threading.Event()

    def concurrent_apply() -> None:
        reading.wait(timeout=10)
        try:
            SessionService().update_model("s", full_model(workflow=slot(90, "explicit", "high", "second")))
        finally:
            wrote.set()

    writer = threading.Thread(target=concurrent_apply)
    writer.start()
    real_read_meta = svc.repo.read_meta

    def slow_read_meta(slug):                       # widen the window between the two reads
        meta = real_read_meta(slug)
        reading.set()
        wrote.wait(timeout=0.5)
        return meta

    svc.repo.read_meta = slow_read_meta
    snap = svc.snapshot("s")
    writer.join(timeout=10)
    assert (snap.revision, snap.model.model["workflow"].value) == (1, "first")


# ── check_session: does a session tell the truth about itself? ────────────────


def _point_artifact_at(slug: str, filename: str) -> None:
    p = store.canonical_dir(slug) / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["prd"]["filename"] = filename
    p.write_text(json.dumps(raw), encoding="utf-8")


def _tamper(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace('"completeness": 0', '"completeness": 5', 1), encoding="utf-8")


def _drop_history(d):
    for f in (d / "revisions").glob("*.json"):
        f.unlink()


def _shorten_log(d):
    raw = json.loads((d / "session.json").read_text(encoding="utf-8"))
    (d / "session.json").write_text(json.dumps({**raw, "revisions": raw["revisions"][:1]}), encoding="utf-8")


_LIES = {
    "coherent": (lambda d: None, set()),
    "missing_revision_file": (_drop_history, {"missing_revision_file"}),
    "model_is_not_the_last_revision": (lambda d: (d / "model.json").write_text(
        json.dumps(full_model(problem=slot(90, "explicit", "high", "other"))), encoding="utf-8"), {"model_is_not_the_last_revision"}),
    "revision_hash_mismatch": (lambda d: _tamper(d / "revisions" / "0001-model.json"), {"revision_hash_mismatch"}),
    "missing_artifact_file": (lambda d: (d / "artifacts" / "prd.md").unlink(), {"missing_artifact_file"}),
    "invalid_session_json": (lambda d: (d / "session.json").write_text('{"slug": "s"}', encoding="utf-8"), {"invalid_session_json"}),
    "revision_count_mismatch": (_shorten_log, {"revision_count_mismatch"}),
}


@pytest.mark.parametrize("lie", list(_LIES))
def test_each_way_a_session_can_lie_about_itself_is_caught(lie):
    """One mutation per problem code, and a coherent session reports none; a safe-but-missing file is `missing`, not `unsafe`."""
    mutate, expected = _LIES[lie]
    healthy_session()
    mutate(store.canonical_dir("s"))
    codes = _codes()
    assert expected <= codes and "unsafe_artifact_filename" not in codes, codes
    if lie in ("coherent", "missing_revision_file", "invalid_session_json"):
        assert codes == expected


def test_check_session_waits_for_a_concurrent_writer_instead_of_reporting_a_tear(monkeypatch):
    """#263: the checker takes the lock, so it cannot observe `save_revision`'s compound write torn."""
    svc = healthy_session()
    real_write_meta = store.Store.write_meta                  # patched on the class, not the module function (#272)
    at_the_gate, release = threading.Event(), threading.Event()

    def paused(self, slug, meta):
        if slug == "s" and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, slug, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)
    write_failures, check_results, checker_done = [], [], threading.Event()

    def _write():
        try:
            svc.update_model("s", full_model(problem=slot(90, "explicit", "high", "moved again")))
        except BaseException as e:  # noqa: BLE001 - reported, not swallowed
            write_failures.append(e)

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"
    checker = threading.Thread(target=lambda: (check_results.append(check_session("s")), checker_done.set()), daemon=True)
    checker.start()
    assert not checker_done.wait(1.0), "check_session read past a writer still mid-save instead of waiting for its lock"
    release.set()
    writer.join(10)
    checker.join(10)
    assert not write_failures and checker_done.is_set()
    assert check_results == [[]]


def test_a_model_from_a_newer_requivo_is_not_reported_as_a_defect():
    """#14: the checker reads through the permissive contract, so an unknown key is not `invalid_model`."""
    healthy_session()
    d = store.canonical_dir("s")
    for f in (d / "model.json", d / "revisions" / "0001-model.json"):
        f.write_text(json.dumps({**json.loads(f.read_text(encoding="utf-8")), "risk_register": []}), encoding="utf-8")
    assert not {"invalid_model", "invalid_revision_model"} & _codes()
    for f in (d / "model.json", d / "revisions" / "0001-model.json"):   # the positive control
        f.write_text('{"model": {"workflow": ', encoding="utf-8")
    assert {"invalid_model", "invalid_revision_model"} <= _codes()


def test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session(tmp_path):
    """An existence oracle: the verdict must not depend on whether the path outside exists (invariant 14)."""
    present, absent = tmp_path / "outside-present.md", tmp_path / "outside-absent.md"
    present.write_text("secret\n", encoding="utf-8")
    verdicts = []
    for probe in (present, absent):
        healthy_session("probe")
        _point_artifact_at("probe", str(probe))
        verdicts.append(_codes("probe"))
        shutil.rmtree(store.canonical_dir("probe"))
    assert verdicts[0] == verdicts[1], f"the verdict leaks whether {present.as_posix()} exists: {verdicts}"
    assert "unsafe_artifact_filename" in verdicts[0] and "missing_artifact_file" not in verdicts[0]


def test_an_unknown_artifact_type_does_not_fall_through_to_the_filesystem(tmp_path):
    """The fall-through #23's auditor named: an unknown type is reported and its filename refused, not followed."""
    outside = tmp_path / "outside.md"
    outside.write_text("x\n", encoding="utf-8")
    healthy_session("probe")
    p = store.canonical_dir("probe") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["not-an-artifact-type"] = dict(raw["artifact_status"]["prd"], filename=str(outside))
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert "unknown_artifact_type" in {pr.code for pr in inspect_session("probe")}
    codes = _codes("probe")
    assert "unsafe_artifact_filename" in codes and "missing_artifact_file" not in codes


def test_an_artifact_that_is_a_symlink_out_of_the_session_is_still_refused(tmp_path):
    """#3: the artifact path is resolved only when something is there; a live and a dangling link both count."""
    healthy_session()
    artifacts = store.canonical_dir("s") / "artifacts"
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not part of this session", encoding="utf-8")
    (artifacts / "prd.md").unlink()
    symlink_or_skip(artifacts / "prd.md", outside)
    assert "unsafe_artifact_filename" in _codes()
    (artifacts / "prd.md").unlink()
    symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")   # dangling, still outside
    assert "unsafe_artifact_filename" in _codes()


def test_an_artifact_symlink_is_reported_unsafe_where_the_platform_cannot_resolve_it(tmp_path, monkeypatch):
    """`check_session` and `artifact_path` share the decision, blinded the way CPython 3.9 is blinded on Windows."""
    healthy_session()
    artifacts = store.canonical_dir("s") / "artifacts"
    (artifacts / "prd.md").unlink()
    symlink_or_skip(artifacts / "prd.md", tmp_path / "never-created.md")
    blind_to_dangling_links(monkeypatch)
    codes = _codes()
    assert "unsafe_artifact_filename" in codes and "missing_artifact_file" not in codes
    with pytest.raises(RequivoError) as ei:
        store.artifact_path("s", "prd.md")
    assert ei.value.code == "invalid_filename"


def test_a_context_card_that_no_longer_resolves_is_not_an_integrity_problem(tmp_path, monkeypatch):
    """Evidence is the directory and only the directory; a lost card is an environment finding."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "lost-domain.md").write_text("# Lost domain\n", encoding="utf-8")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))
    slug = SessionService().create_session("Something.", context_cards=["lost-domain"], slug="carded").slug
    assert store.read_meta(slug).context_cards == ["lost-domain"] and check_session(slug) == []
    (cards / "lost-domain.md").unlink()
    assert check_session(slug) == []
    assert check_selection(store.read_meta(slug).context_cards) is not None


# ── newest_readable_revision: the repair-target search (#210) ─────────────────


@pytest.mark.parametrize(("break_two", "found"), [
    ("keep", 2), ("corrupt", 1), ("delete", 1), ("corrupt-both", None),
], ids=["latest", "skips-a-broken-file", "missing-is-unreadable", "none-readable"])
def test_newest_readable_revision_answers_from_what_parses(break_two, found):
    """The latest that parses, an older one past a broken or missing file, or plainly `None`; the payload is byte for byte."""
    healthy_session()
    d = store.canonical_dir("s")
    if break_two == "corrupt-both":
        for f in (d / "revisions").glob("*.json"):
            f.write_text("{not json", encoding="utf-8")
    elif break_two == "corrupt":
        (d / "revisions" / "0002-model.json").write_text("{not json", encoding="utf-8")
    elif break_two == "delete":
        (d / "revisions" / "0002-model.json").unlink()
    got = newest_readable_revision(d, 2)
    assert (got.revision if got else None) == found
    if got is not None:
        assert got.payload == (d / "revisions" / f"000{found}-model.json").read_text(encoding="utf-8")


def test_newest_readable_revision_skips_a_revision_whose_hash_no_longer_matches():
    healthy_session()
    d = store.canonical_dir("s")
    hashes = {r.revision: r.model_hash for r in store.read_meta("s").revisions}
    assert newest_readable_revision(d, 2, expected_hashes=hashes).revision == 2
    _tamper(d / "revisions" / "0002-model.json")           # still parses, no longer matches
    assert newest_readable_revision(d, 2, expected_hashes=hashes).revision == 1


def test_newest_readable_revision_with_no_expected_hashes_trusts_anything_that_parses():
    healthy_session()
    d = store.canonical_dir("s")
    _tamper(d / "revisions" / "0002-model.json")
    assert newest_readable_revision(d, 2).revision == 2


def test_readable_revision_checks_one_specific_number():
    healthy_session()
    d = store.canonical_dir("s")
    hashes = {r.revision: r.model_hash for r in store.read_meta("s").revisions}
    assert readable_revision(d, 2, expected_hashes=hashes).revision == 2
    assert readable_revision(d, 1, expected_hashes=hashes).revision == 1
    assert readable_revision(d, 3, expected_hashes=hashes) is None
    _tamper(d / "revisions" / "0002-model.json")
    assert readable_revision(d, 2, expected_hashes=hashes) is None


# ── containment (#3, invariant 17): a verdict never depends on transient filesystem state ──


def _counting_resolve(monkeypatch) -> list:
    resolved: list = []
    real_resolve = store._resolve
    monkeypatch.setattr(store, "_resolve", lambda path: (resolved.append(str(path)), real_resolve(path))[1])
    return resolved


def test_a_session_path_is_not_resolved_before_it_exists(tmp_path, monkeypatch):
    """Invariant 17: `_child_of` reaches no resolution at all for a child that is not there."""
    root = tmp_path / "sessions"
    root.mkdir()
    resolved = _counting_resolve(monkeypatch)
    assert store._child_of(root, "s") == root / "s"
    assert resolved == [], resolved


def test_an_unreadable_child_is_not_accepted_as_contained_on_py314(tmp_path, monkeypatch):
    """#636: a denied path is not the same as a missing child at the containment boundary."""
    root = tmp_path / "sessions"
    root.mkdir()
    child = root / "blocked"
    child.touch()
    simulate_py314_denied_path(monkeypatch, child)
    assert store.is_contained(child, root) is False
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "blocked")


def test_an_artifact_path_is_not_resolved_before_it_exists(monkeypatch):
    """`artifact_path` is `_child_of`'s sibling and had the identical two-resolution shape."""
    healthy_session()
    resolved = _counting_resolve(monkeypatch)
    store.canonical_dir("s")                           # the baseline this test is not about
    baseline = len(resolved)
    resolved.clear()
    assert store.artifact_path("s", "epic.json").name == "epic.json"   # a valid name, no such file
    assert len(resolved) == baseline, resolved[baseline:]


def test_a_symlink_out_of_the_session_root_is_still_refused(tmp_path):
    """The must-fire half: live or dangling, a link pointing out is refused; inside, ordinary or dangling, is accepted."""
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert store._child_of(root, "s") == root / "s"
    symlink_or_skip(root / "live", outside, target_is_directory=True)
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "live")
    symlink_or_skip(root / "dangling", tmp_path / "not-yet", target_is_directory=True)   # exists() alone would wave it through
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "dangling")
    symlink_or_skip(root / "inside", root / "not-yet", target_is_directory=True)
    assert store._child_of(root, "inside") == root / "inside"


def test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it(tmp_path, monkeypatch):
    """The same assertion with the resolver blinded the way CPython 3.9 blinds it on Windows, plus its controls."""
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    (root / "target").mkdir()
    symlink_or_skip(root / "dangling", tmp_path / "not-yet", target_is_directory=True)
    blind_to_dangling_links(monkeypatch)
    assert store._resolve(root / "dangling") == store._resolve(root) / "dangling", "the simulation is not reproducing the defect"
    with pytest.raises(InvalidSlugError):
        store._child_of(root, "dangling")
    assert store._child_of(root, "s") == root / "s" and store._child_of(root, "absent") == root / "absent"
    symlink_or_skip(root / "live", root / "target", target_is_directory=True)
    assert store._child_of(root, "live") == root / "live"
