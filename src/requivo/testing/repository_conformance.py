"""A pytest suite any `SessionRepository` implementation can run against itself (#424): subclass
it under a `Test*` name, implement `make_repository()`, and pytest collects the rest.

    from requivo.testing.repository_conformance import SessionRepositoryConformance

    class TestMyPostgresRepository(SessionRepositoryConformance):
        def make_repository(self):
            return MyPostgresSessionRepository(dsn=TEST_DSN)

It asserts the protocol-level semantics the services assume (create claims the slug once,
`expected_revision` is a real precondition, the lock is exclusive and re-entrant, known and
unexaminable never overlap, `load_artifact` returns `None` for an absence, an unknown model key
survives a round trip, `delete` releases the slug) and nothing about where or how a backing stores.
"""
from __future__ import annotations

import threading
import time

import pytest

from requivo.core.contracts import schema_slot_ids, schema_slots
from requivo.core.errors import RevisionConflictError, SessionExistsError
from requivo.core.persistence import PersistedEngineOutput
from requivo.services.repository import SessionRepository

__all__ = ["SessionRepositoryConformance", "full_model"]


def full_model(**overrides) -> PersistedEngineOutput:
    """A minimal, schema-complete model, built from the public schema surface so an out-of-repo subclass can call it."""
    _, required = schema_slot_ids()
    order = [s["id"] for s in schema_slots()]
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low", "value": ""}
             for sid in order if sid in required}
    model.update(overrides.pop("model", {}))
    payload = {"model": model, "questions": [], "summary": {"objective": "A conformance-suite model"}}
    payload.update(overrides)
    return PersistedEngineOutput(**payload)


class SessionRepositoryConformance:
    """Subclass and implement `make_repository`; not collected on its own (`python_classes = Test*`)."""

    def make_repository(self) -> SessionRepository:
        raise NotImplementedError("subclasses must return a fresh, empty SessionRepository")

    @pytest.fixture
    def repo(self) -> SessionRepository:
        return self.make_repository()

    # -- invariant 11: create claims the slug once ------------------------------------------------

    def test_create_claims_the_slug_once(self, repo: SessionRepository):
        repo.create("s", "a request")
        with pytest.raises(SessionExistsError):
            repo.create("s", "a different request")

    def test_create_does_not_refuse_a_different_slug(self, repo: SessionRepository):
        """Positive control: `create` still succeeds in the ordinary case."""
        repo.create("a", "req")
        repo.create("b", "req")
        assert {"a", "b"} <= set(repo.list_slugs())

    # -- invariant 2: expected_revision is a real precondition -------------------------------------

    def test_expected_revision_refuses_a_stale_write(self, repo: SessionRepository):
        repo.create("s", "req")
        repo.save_revision("s", full_model(), expected_revision=0)  # -> revision 1
        with pytest.raises(RevisionConflictError):
            repo.save_revision("s", full_model(), expected_revision=0)  # stale: session is at 1 now

    def test_expected_revision_accepts_a_correct_precondition(self, repo: SessionRepository):
        """Positive control: a correct precondition must not be refused."""
        repo.create("s", "req")
        rev, meta = repo.save_revision("s", full_model(), expected_revision=0)
        assert rev == 1
        assert meta.current_revision == 1

    # -- invariant 9: the lock is mutually exclusive and re-entrant --------------------------------

    def test_lock_serialises_two_concurrent_holders(self, repo: SessionRepository):
        """Each holder records the interval it held the lock; the two must not overlap."""
        repo.create("s", "req")
        start = threading.Barrier(2)
        intervals: list[tuple[float, float]] = []
        guard = threading.Lock()

        def hold() -> None:
            start.wait()
            with repo.lock("s"):
                t0 = time.monotonic()
                time.sleep(0.05)
                t1 = time.monotonic()
            with guard:
                intervals.append((t0, t1))

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(intervals) == 2, "a holder did not finish -- the lock may have deadlocked"
        (a0, a1), (b0, b1) = intervals
        assert a1 <= b0 or b1 <= a0, f"the two holders overlapped: {intervals} -- lock is not exclusive"

    def test_lock_is_reentrant_within_one_thread(self, repo: SessionRepository):
        """The nested acquire runs on a daemon thread with a `join(timeout=...)`, so a deadlock fails rather than hangs."""
        repo.create("s", "req")
        finished = threading.Event()

        def nested() -> None:
            with repo.lock("s"):
                with repo.lock("s"):
                    finished.set()

        t = threading.Thread(target=nested, daemon=True)
        t.start()
        t.join(timeout=10)
        assert finished.is_set(), "lock() is not re-entrant within one thread (invariant 9)"

    # -- invariant 15's shape at the repository layer: known vs. unexaminable never overlap --------

    def test_known_slugs_and_unexaminable_entries_do_not_overlap(self, repo: SessionRepository):
        repo.create("a", "req")
        repo.create("b", "req")
        slugs = set(repo.list_slugs())
        assert {"a", "b"} <= slugs
        unexaminable_names = {e.name for e in repo.list_unexaminable()}
        assert unexaminable_names.isdisjoint(slugs), (
            "a name reported as a known session must never also be reported as unexaminable"
        )

    # -- load_artifact: None means absent, and only that -------------------------------------------

    def test_load_artifact_is_none_for_a_real_absence(self, repo: SessionRepository):
        repo.create("s", "req")
        assert repo.load_artifact("s", "prd.md") is None

    def test_load_artifact_returns_what_was_saved(self, repo: SessionRepository):
        """Positive control: the None above is about absence. Saved against revision 1, since the file
        backing refuses a `source_revision` the session has not reached."""
        repo.create("s", "req")
        repo.save_revision("s", full_model(), expected_revision=0)
        repo.save_artifact("s", "prd", "prd.md", "# Hello", source_revision=1)
        assert repo.load_artifact("s", "prd.md") == "# Hello"

    # -- invariants 8/10: an unrecognised top-level key survives the round trip ---------------------

    def test_an_unknown_top_level_key_survives_a_save_and_load_round_trip(self, repo: SessionRepository):
        """A field a newer Requivo wrote must not be dropped by the backing's own (de)serialisation (invariants 8/10)."""
        repo.create("s", "req")
        model = full_model(a_field_from_a_newer_requivo="kept")
        repo.save_revision("s", model, expected_revision=0)
        loaded = repo.load_model("s")
        assert getattr(loaded, "a_field_from_a_newer_requivo", None) == "kept"

    # -- #238: delete is on the protocol, not only the file backing ---------------------------------

    def test_delete_removes_the_session_from_the_backing(self, repo: SessionRepository):
        """`delete` belongs on the protocol, not only on the file backing (#238)."""
        repo.create("s", "req")
        repo.delete("s")
        assert repo.exists("s") is False
        assert "s" not in repo.list_slugs()

    def test_deleting_then_recreating_the_same_slug_succeeds(self, repo: SessionRepository):
        """A delete genuinely releases the name (invariant 11)."""
        repo.create("s", "the first occupant")
        repo.delete("s")
        repo.create("s", "a completely different request")
        assert repo.request_text("s") == "a completely different request"
