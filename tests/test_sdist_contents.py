"""The sdist ships exactly what it means to ship, never a half-collectable `tests/` tree (#431), and the
setuptools-floor skip that gates the build never imports the package it vets (#453)."""
from __future__ import annotations

import contextlib
import importlib.metadata
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Below the `dev` extra's floor, `import pkg_resources` itself crashes on Python 3.12 (pyproject.toml has the story).
_MIN_SETUPTOOLS_FOR_SDIST = "77.0.1"


def _setuptools_build_backend_reason(min_version: str) -> str | None:
    """None if the installed setuptools is present, parseable and new enough; otherwise the skip reason (#337, #453)."""
    try:
        installed_str = importlib.metadata.version("setuptools")  # never `import setuptools`: that is the crash
    except importlib.metadata.PackageNotFoundError:
        return "setuptools is not installed -- it is a dev-only addition for this test"
    from packaging.version import InvalidVersion, Version

    try:
        installed = Version(installed_str)
    except InvalidVersion:
        return f"setuptools reports a version string ({installed_str!r}) this check cannot parse -- treated as unvetted"
    if installed >= Version(min_version):
        return None
    return (f"setuptools {installed_str} is older than {min_version}, the floor pyproject.toml documents as the first release "
            f"that can `import pkg_resources` on Python {sys.version_info.major}.{sys.version_info.minor}. UNTESTED HERE: "
            f"the sdist's member list; every other CI leg and a real release build cover it.")


def _build_sdist(dest: Path) -> Path:
    """A real sdist in-process, exactly as `python -m build --sdist --no-isolation` would."""
    too_old = _setuptools_build_backend_reason(_MIN_SETUPTOOLS_FOR_SDIST)
    if too_old:
        pytest.skip(too_old)
    from setuptools.build_meta import build_sdist

    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            name = build_sdist(str(dest))
    finally:
        os.chdir(cwd)
    return dest / name


@pytest.fixture(scope="module")
def sdist_members() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(_build_sdist(Path(td))) as tf:
            return tf.getnames()


def test_the_sdist_ships_no_tests_directory_at_all(sdist_members):
    offenders = [m for m in sdist_members if "/tests/" in m or m.rstrip("/").endswith("/tests")]
    assert offenders == [], f"the sdist still carries tests/ content: {offenders[:10]}"


def test_the_sdist_still_ships_the_package_and_its_assets(sdist_members):
    """The positive control for the exclusion above."""
    for tail in ("src/requivo/__init__.py", "src/requivo/py.typed", "src/requivo/assets/prompts/engine.md"):
        assert any(m.endswith(tail) for m in sdist_members), tail


def test_manifest_in_declares_the_prune():
    assert "prune tests" in (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8"), "MANIFEST.in no longer excludes tests/ (#431)"


@pytest.mark.parametrize("installed, expected", [
    ("64.0.0", "64.0.0"), ("66.1.0", None), ("84.0.0", None), ("not-a-version", "not-a-version"), (None, "not installed"),
], ids=["below-the-floor", "at-the-floor", "above-the-floor", "unparseable-fails-closed", "not-installed"])
def test_the_floor_check_answers_without_importing_setuptools(monkeypatch, installed, expected):
    """Fires below the floor and on nonsense, not at or above it, and never imports the package to find out (#453)."""
    def _version(name):
        if installed is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setitem(sys.modules, "setuptools", None)  # any `import setuptools` now raises
    monkeypatch.setattr(importlib.metadata, "version", _version)
    reason = _setuptools_build_backend_reason("66.1.0")
    assert reason is None if expected is None else expected in reason, reason


def test_the_skip_path_actually_skips_rather_than_crashing(monkeypatch):
    monkeypatch.setitem(sys.modules, "setuptools", None)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0.0")
    with pytest.raises(pytest.skip.Exception, match="1.0.0"):
        _build_sdist(REPO_ROOT)  # dest is never used: the skip fires before any build call
