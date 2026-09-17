"""`SessionRepository` is a backing-agnostic seam (#424): a dict-backed repository runs the service unchanged,
and the conformance suite is a public, wheel-shipped artifact."""
from __future__ import annotations

import contextlib
import threading
import zipfile
from pathlib import Path

import pytest
from _fakes import out, slot

from requivo.core.errors import RevisionConflictError, SessionExistsError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus, RevisionRecord, SessionMeta
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository, SessionRepository
from requivo.services.sessions import SessionService
from requivo.testing.repository_conformance import SessionRepositoryConformance

REPO_ROOT = Path(__file__).resolve().parent.parent


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}
        self._locks: dict = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, slug):
        # threading.RLock is re-entrant *per thread* by construction (#424).
        with self._locks_guard:
            return self._locks.setdefault(slug, threading.RLock())

    @contextlib.contextmanager
    def lock(self, slug):
        with self._lock_for(slug):
            yield

    def _require(self, slug):
        if slug not in self._meta:
            raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    def exists(self, slug): return slug in self._meta
    def has_meta(self, slug): return slug in self._meta
    def ensure_writable(self, slug): self._require(slug)

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None, perimeter=None):
        # Invariant 11, at this backing's own layer (#424).
        if slug in self._meta:
            raise SessionExistsError(f"session '{slug}' already exists", details={"slug": slug})
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards,
                           perimeter=perimeter)
        self._meta[slug], self._req[slug], self._art[slug] = meta, request, {}
        return meta

    def read_meta(self, slug):
        self._require(slug)
        return self._meta[slug]

    def delete(self, slug):
        # The dict-backed analogue of the file backing's lock-then-remove (#238).
        self._require(slug)
        with self.lock(slug):
            for table in (self._meta, self._model, self._req, self._art):
                table.pop(slug, None)
            for key in [k for k in self._revs if k[0] == slug]:
                self._revs.pop(key, None)

    def write_meta(self, slug, meta): self._meta[slug] = meta
    def list_slugs(self): return sorted(self._meta)
    def list_unexaminable(self): return []  # a real answer rather than a stub (#80)

    def load_model(self, slug):
        if slug not in self._model:
            raise SessionNotFoundError(f"no model '{slug}'", details={"slug": slug})
        return self._model[slug]

    def load_revision(self, slug, revision):
        if (slug, revision) not in self._revs:
            raise SessionNotFoundError(f"no revision {revision}", details={"slug": slug, "revision": revision})
        return self._revs[(slug, revision)]

    def save_revision(self, slug, model, *, expected_revision=None, provenance=None):
        meta = self.read_meta(slug)
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise RevisionConflictError("conflict", details={"expected": expected_revision,
                                                             "actual": meta.current_revision})
        rev = meta.current_revision + 1
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev, created_at="t", previous_revision=meta.current_revision or None,
            model_hash="sha256:mem", provider=prov.get("provider"), model_name=prov.get("model_name"),
            surface=prov.get("surface"), prompt_version=prov.get("prompt_version")))
        meta.current_revision = rev
        self._model[slug] = self._revs[(slug, rev)] = model
        return rev, meta

    def request_text(self, slug): return self._req.get(slug, "")
    def context_cards(self, slug): return self._meta[slug].context_cards if slug in self._meta else None

    def save_artifact(self, slug, artifact_type, filename, content, *, source_revision, stale=False):
        self._art[slug][filename] = content
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at="t", stale=stale)
        self._meta[slug].artifact_status[artifact_type] = st
        return st

    def load_artifact(self, slug, filename): return self._art.get(slug, {}).get(filename)


class TestInMemoryRepositoryConformance(SessionRepositoryConformance):
    """The non-file backing above, proven against the shared suite."""

    def make_repository(self):
        return InMemorySessionRepository()


class TestFileRepositoryConformance(SessionRepositoryConformance):
    """The shipped file backing, against the same shared suite."""

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path):
        self._root = tmp_path

    def make_repository(self):
        return FileSessionRepository(root=self._root)


def test_session_service_runs_unchanged_on_a_non_file_repository():
    repo = InMemorySessionRepository()
    assert isinstance(repo, SessionRepository)          # satisfies the protocol (runtime-checkable)
    svc = SessionService(repo)

    svc.create_session("a leave request", slug="leave-mem")
    r1 = svc.update_model("leave-mem", out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                          provenance={"provider": "anthropic", "surface": "cli-discover"})
    assert r1.revision == 1

    # artifact tracking + dependency-graph staleness, entirely in memory (criteria consumes workflow)
    ArtifactService(repo).save("leave-mem", "criteria", "# c", source_revision=1)
    r2 = svc.update_model("leave-mem", out({"workflow": slot(95, "explicit", "high", "a → b")}).model_dump())
    assert r2.revision == 2
    assert ArtifactService(repo).list("leave-mem")["criteria"]["stale"] is True

    # optimistic locking is enforced by the backing, not the file layout
    with pytest.raises(RevisionConflictError):
        svc.update_model("leave-mem", out({"workflow": slot(95, "explicit", "high")}).model_dump(),
                         expected_revision=0)

    st = svc.status("leave-mem")
    assert st["revision"] == 2 and "understanding" in st
    assert [rr.surface for rr in svc.meta("leave-mem").revisions] == ["cli-discover", None]


def test_the_suite_is_importable_and_not_collected_as_a_test_on_its_own():
    from requivo.testing import SessionRepositoryConformance as exported

    assert exported is SessionRepositoryConformance
    # pytest's default `python_classes = Test*` -- a bare import of this module must add no tests.
    assert not SessionRepositoryConformance.__name__.startswith("Test")
    with pytest.raises(NotImplementedError):
        SessionRepositoryConformance().make_repository()
    assert issubclass(TestInMemoryRepositoryConformance, SessionRepositoryConformance)
    assert issubclass(TestFileRepositoryConformance, SessionRepositoryConformance)


def test_full_model_is_re_exported_and_documented():
    """A reviewer finding (#424)."""
    from requivo.testing import full_model
    from requivo.testing.repository_conformance import full_model as direct

    assert full_model is direct
    assert full_model().summary.objective
    text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "full_model" in text, "full_model is exported but not named in the declared seam"


def test_the_suite_ships_in_the_built_wheel():
    from test_sdist_contents import _setuptools_build_backend_reason

    # 77.0.1: the licence is a PEP 639 SPDX string since #337, and `build_wheel`'s floor is above `build_sdist`'s (#453).
    too_old = _setuptools_build_backend_reason("77.0.1")
    if too_old:
        pytest.skip(too_old)

    import io
    import os
    import tempfile

    from setuptools.build_meta import build_wheel

    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        with tempfile.TemporaryDirectory() as td:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                name = build_wheel(td)
            with zipfile.ZipFile(Path(td) / name) as zf:
                members = zf.namelist()
    finally:
        os.chdir(cwd)

    assert "requivo/testing/__init__.py" in members
    assert "requivo/testing/repository_conformance.py" in members


def test_compatibility_md_declares_the_suite():
    text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "SessionRepositoryConformance" in text and "requivo[testing]" in text
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'testing = ["pytest' in pyproject, "no [project.optional-dependencies] testing extra"
