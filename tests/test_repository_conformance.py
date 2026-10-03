"""`SessionRepository` is a backing-agnostic seam (#424): a dict-backed repository runs the service unchanged,
and the conformance suite is a public, wheel-shipped artifact."""
from __future__ import annotations

import contextlib
import zipfile
from pathlib import Path

import pytest
from _fakes import InMemorySessionRepository, out, slot

from requivo.core.errors import RevisionConflictError
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository, SessionRepository, accepts_stale
from requivo.services.sessions import SessionService
from requivo.testing.repository_conformance import SessionRepositoryConformance

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestInMemoryRepositoryConformance(SessionRepositoryConformance):
    """The non-file backing, `_fakes.InMemorySessionRepository`, proven against the shared suite."""

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
    assert not accepts_stale(repo)                      # predates #648: staleness below is the fallback write
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
