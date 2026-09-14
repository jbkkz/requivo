"""#6 — an artifact's provenance is stated by the caller or the save is refused.

Invariant 2 ends "Never record `stale=False` because the caller didn't say otherwise", and
`ArtifactService.save` was doing exactly that: an omitted `source_revision` was read as *the current
revision*, which made `_stale_since` return False without consulting the dependency graph at all. The
recorded number was the session's real current revision, so the fabrication was undetectable
downstream — `artifact list`, `session show` and the Web all reported a stale document fresh.

Every assertion here is paired. The "must not happen" half of a guard passes just as happily when the
fixture is broken and nothing happens at all, so each refusal test sits beside a save that must still
be accepted and must still record the flag it always recorded.
"""

from __future__ import annotations

import json

import pytest

from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core import persistence as store
from requivo.core.errors import InvalidSessionError, RequivoError
from requivo.services.artifacts import ArtifactService, UnstatedSourceRevisionError
from requivo.services.sessions import SessionService


@pytest.fixture
def moved(tmp_path, monkeypatch):
    """A session at revision 2 whose `workflow` slot — which `prd` and `criteria` both rest on —
    materially changed between revision 1 and revision 2.

    So: an artifact reasoned from revision 1 IS stale, and one reasoned from revision 2 is NOT. The
    two answers are different, which is what makes an assertion about either of them mean something.
    """
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    slug = "prov-moved"
    store.create_session(slug, "a leave approval system")
    svc = SessionService()
    svc.update_model(slug, json.dumps(_full_model(workflow=_slot(50, "inferred", "high", "draft"))))
    svc.update_model(slug, json.dumps(_full_model(
        workflow=_slot(90, "explicit", "high", "draft -> issued -> archived"))))
    assert store.read_meta(slug).current_revision == 2, "fixture did not reach revision 2"
    return slug


# ── F1: an omitted source revision is unknown provenance, not "now" ───────────────


def test_a_stated_source_revision_still_records_the_flag_it_always_did(moved):
    """MUST FIRE. The positive control for every refusal below: with the revision stated, the two
    answers are still computed from the dependency graph and they still differ from each other."""
    svc = ArtifactService()
    stale = svc.save(moved, "prd", "# PRD reasoned from revision 1", source_revision=1)
    assert stale.revision == 1 and stale.stale is True

    fresh = svc.save(moved, "criteria", "# criteria reasoned from revision 2", source_revision=2)
    assert fresh.revision == 2 and fresh.stale is False


def test_an_omitted_source_revision_is_refused_rather_than_read_as_now(moved):
    """#6 F1. The save that used to be recorded `revision: 2, stale: false` about content the caller
    never said anything about."""
    with pytest.raises(UnstatedSourceRevisionError) as e:
        ArtifactService().save(moved, "prd", "# PRD reasoned from revision 1")
    assert isinstance(e.value, RequivoError), "must reach a surface as a structured failure"
    assert e.value.details["source_revision"] is None
    assert e.value.details["current_revision"] == 2


def test_the_refusal_says_what_to_pass(moved):
    """A refusal a caller cannot act on is a worse outcome than the wrong answer it replaced: the
    message has to name the flag and the range of revisions that exist."""
    with pytest.raises(UnstatedSourceRevisionError) as e:
        ArtifactService().save(moved, "prd", "# PRD")
    msg = e.value.message
    assert "--revision" in msg and "source_revision" in msg
    assert "1" in msg and "2" in msg, f"the revision range is not in the message: {msg!r}"


def test_a_refused_save_writes_nothing_at_all(moved):
    """The refusal happens before the artifact file and before `session.json` is touched, so a
    rejected save cannot leave content on disk that no status row describes."""
    svc = ArtifactService()
    with pytest.raises(UnstatedSourceRevisionError):
        svc.save(moved, "prd", "# PRD")
    assert not (store.canonical_dir(moved) / "artifacts" / "prd.md").exists()
    assert "prd" not in store.read_meta(moved).artifact_status

    # must fire: the same call with the revision stated does write both.
    svc.save(moved, "prd", "# PRD", source_revision=1)
    assert (store.canonical_dir(moved) / "artifacts" / "prd.md").exists()
    assert "prd" in store.read_meta(moved).artifact_status


# ── F2: the honesty guard is as wide as the failure set it was written for ────────


def _revision_file(slug: str, revision: int):
    return store.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"


def test_a_corrupt_revision_file_is_refused_as_a_structured_error(moved):
    """#6 F2. `except RequivoError` only caught a *missing* revision. A file that is present but
    truncated — an interrupted sync — reaches `PersistedEngineOutput.model_validate_json`, which raises
    pydantic's `ValidationError`: a `ValueError`, so the guard never fired and a raw traceback came
    out of a service call, from inside the session lock.
    """
    _revision_file(moved, 1).write_text('{"model": {"workflow": ', encoding="utf-8")
    with pytest.raises(InvalidSessionError) as e:
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert e.value.code == "unreadable_source_revision"
    assert e.value.details["source_revision"] == 1
    assert "ValidationError" in json.dumps(e.value.details), "the cause is not recorded"


def test_an_unreadable_revision_file_is_refused_as_a_structured_error(moved):
    """The `OSError` half of the same widening. A directory where the revision file belongs is the
    portable way to make the read fail rather than the parse: POSIX raises `IsADirectoryError` and
    Windows raises `PermissionError`, and both are `OSError`, so this asserts the same refusal on
    every leg instead of branching on the platform.
    """
    p = _revision_file(moved, 1)
    p.unlink()
    p.mkdir()
    with pytest.raises(InvalidSessionError) as e:
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert e.value.code == "unreadable_source_revision"


def test_a_missing_revision_file_is_still_refused_the_way_it_always_was(moved):
    """The case the original guard did catch, kept as a control: widening it must not have moved
    the answer for the failure it already handled."""
    _revision_file(moved, 1).unlink()
    with pytest.raises(InvalidSessionError):
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)


def test_the_two_provenance_refusals_carry_two_codes_and_one_details_shape(moved):
    """Two refusals share one `details` shape but ride two codes now — split from one shared
    `invalid_session` by #57 (a new code needs a row in `requivo/http.py::STATUS_BY_CODE`, moved by
    #422; the file it lived in was held by another lane in the round #6 landed). The `details` keys
    are asserted as sets because a key on one payload and not the other was the failure #35 measured;
    #52 is the reverse precedent — two codes can share a shape without sharing a meaning."""
    svc = ArtifactService()
    with pytest.raises(UnstatedSourceRevisionError) as unstated:
        svc.save(moved, "prd", "# PRD")

    _revision_file(moved, 1).write_text("{", encoding="utf-8")
    with pytest.raises(InvalidSessionError) as unreadable:
        svc.save(moved, "prd", "# PRD", source_revision=1)

    # The two codes differ, and each is named rather than only compared: a test that asserted
    # inequality alone would pass just as well if both arms moved to some third code together.
    assert unstated.value.code == "unstated_source_revision"
    assert unreadable.value.code == "unreadable_source_revision"
    # The split moved the code, not the hierarchy — `except InvalidSessionError` still catches both,
    # which is what keeps this from breaking a caller that catches the class.
    assert isinstance(unstated.value, InvalidSessionError)
    assert isinstance(unreadable.value, InvalidSessionError)
    # must fire: neither arm may answer the family base. #57 gave one of them a code and left the
    # other on `invalid_session`, which made the pair distinguishable in exactly one direction — a
    # consumer branching on the unstated arm worked, and one branching on the unreadable arm caught
    # every other malformed-session fact with it. #82 closed the other direction.
    assert "invalid_session" not in {unstated.value.code, unreadable.value.code}

    assert set(unstated.value.details) == set(unreadable.value.details)
    # must fire: the shared shape is the real one, not two empty dicts agreeing with each other.
    assert {"slug", "type", "source_revision", "current_revision", "cause"} == set(unstated.value.details)
    # and the two payloads still say different things — one shape is not one fact.
    assert unstated.value.details["cause"] is None
    assert unreadable.value.details["cause"] is not None


def test_an_intact_history_is_still_diffed_rather_than_refused(moved):
    """MUST FIRE for the whole F2 block. Every test above breaks a revision file and asserts a
    refusal; if the fixture stopped producing a readable history, they would all still pass. This
    one fails instead."""
    st = ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert st.stale is True
