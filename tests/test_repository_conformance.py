"""The repository conformance suite is a public, wheel-shipped, out-of-repo-runnable artifact (#424) -- not
merely a class under `src/`."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_suite_is_importable_and_not_collected_as_a_test_on_its_own():
    from requivo.testing import SessionRepositoryConformance
    from requivo.testing.repository_conformance import SessionRepositoryConformance as direct

    assert SessionRepositoryConformance is direct
    # pytest's default `python_classes = Test*` -- a bare import of this module must add no tests.
    assert not SessionRepositoryConformance.__name__.startswith("Test")
    with pytest.raises(NotImplementedError):
        SessionRepositoryConformance().make_repository()


def test_full_model_is_re_exported_and_documented():
    """A reviewer finding (#424)."""
    from requivo.testing import SessionRepositoryConformance, full_model
    from requivo.testing.repository_conformance import full_model as direct

    assert full_model is direct
    model = full_model()
    assert model.summary.objective
    assert SessionRepositoryConformance  # both names exercised in one test, deliberately

    text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "full_model" in text, "full_model is exported but not named in the declared seam"


def test_both_shipped_implementations_are_wired_to_the_suite():
    # Bare module name, not `tests.test_sessions` -- there is no `tests/__init__.py`, so pytest's own rootdir import mode puts `tests/` directly on `sys.path` (the same reason `_fakes` is imported by bare name throughout this suite rather than as `tests._fakes`).
    from test_discovery_provider_seam import TestFileRepositoryConformance, TestInMemoryRepositoryConformance

    from requivo.testing.repository_conformance import SessionRepositoryConformance

    assert issubclass(TestInMemoryRepositoryConformance, SessionRepositoryConformance)
    assert issubclass(TestFileRepositoryConformance, SessionRepositoryConformance)


def test_the_suite_ships_in_the_built_wheel():
    # `build_wheel`'s own floor is one release higher than `build_sdist`'s (#453).
    #
    # No `pytest.importorskip("setuptools", ...)` here, deliberately (#453, reviewer finding).
    from test_sdist_contents import _setuptools_build_backend_reason

    # 77.0.1, not 70.1.0, since #337: the licence is a PEP 639 SPDX string now, and a setuptools below 77 fails to *parse* pyproject.toml before `bdist_wheel`'s own availability is reachable.
    too_old = _setuptools_build_backend_reason("77.0.1")
    if too_old:
        pytest.skip(too_old)

    import contextlib
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
    assert "SessionRepositoryConformance" in text
    assert "requivo[testing]" in text


def test_the_testing_extra_is_declared():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'testing = ["pytest' in text, (
        "no [project.optional-dependencies] testing extra -- requivo.testing needs pytest to be "
        "usable, and the base install deliberately does not carry it"
    )
