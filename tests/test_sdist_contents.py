"""The sdist ships exactly what it means to ship -- never a half-collectable `tests/` tree (#431)."""
from __future__ import annotations

import importlib.metadata
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# The floor this project's own `dev` extra declares for setuptools (pyproject.toml's own comment there has the full story): below it, `import pkg_resources` itself crashes on Python 3.12 with `AttributeError: module 'pkgutil' has no attribute 'ImpImporter'` -- bisected directly against Python 3.12.13/3.12.14, not guessed.
#
# **This is checked at import time here too (#453), and the first version of this check (reviewer finding) was itself broken by the exact failure it existed to catch.** It read the version via `import setuptools; setuptools.__version__`, reached only after a `pytest.importorskip("setuptools", ...)` that performs the identical import -- so on setuptools 64.0.0-66.0.0, the plain `import setuptools` crashes with the same `AttributeError` before either the importorskip's own `except ImportError` (which does not catch `AttributeError`) or the version comparison ever ran.
#
# `importlib.metadata.version("setuptools")` is the fix (#337).
_MIN_SETUPTOOLS_FOR_SDIST = "77.0.1"


def _setuptools_build_backend_reason(min_version: str) -> str | None:
    """None if the installed setuptools is present, parseable, and new enough to import and build with here;
    otherwise the skip reason."""
    try:
        installed_str = importlib.metadata.version("setuptools")
    except importlib.metadata.PackageNotFoundError:
        return "setuptools is not installed -- it is a dev-only addition for this test"

    from packaging.version import InvalidVersion, Version

    try:
        installed = Version(installed_str)
    except InvalidVersion:
        return (
            f"setuptools reports a version string ({installed_str!r}) this check cannot parse -- "
            f"treated as unvetted rather than assumed safe to import and build with"
        )
    if installed >= Version(min_version):
        return None
    return (
        f"setuptools {installed_str} is older than {min_version}, the floor pyproject.toml documents "
        f"as the first release that can even `import pkg_resources` on this interpreter (Python "
        f"{sys.version_info.major}.{sys.version_info.minor}) without crashing. UNTESTED HERE: the "
        f"sdist's actual member list. Every other CI leg installs the newest satisfying setuptools "
        f"and does cover it; so does a real release build."
    )


def _build_sdist(dest: Path) -> Path:
    """Build a real sdist in-process, exactly as `python -m build --sdist --no-isolation` would."""
    too_old = _setuptools_build_backend_reason(_MIN_SETUPTOOLS_FOR_SDIST)
    if too_old:
        pytest.skip(too_old)
    import contextlib
    import io
    import os

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
        archive = _build_sdist(Path(td))
        with tarfile.open(archive) as tf:
            return tf.getnames()


def test_the_sdist_ships_no_tests_directory_at_all(sdist_members):
    """The chosen story is (a): exclude `tests/` entirely (#431)."""
    offenders = [m for m in sdist_members if "/tests/" in m or m.rstrip("/").endswith("/tests")]
    assert offenders == [], f"the sdist still carries tests/ content: {offenders[:10]}"


def test_the_sdist_still_ships_the_package_and_its_assets(sdist_members):
    """A positive control for the assertion above (CLAUDE.md's own rule: a 'must not fire' case needs a paired
    'must fire' case)."""
    assert any(m.endswith("src/requivo/__init__.py") for m in sdist_members)
    assert any(m.endswith("src/requivo/py.typed") for m in sdist_members)
    assert any("src/requivo/assets/prompts/engine.md" in m for m in sdist_members)


def test_manifest_in_declares_the_prune():
    text = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tests" in text, "MANIFEST.in no longer excludes tests/ -- #431 regressed"


# -- the version-floor skip itself (#453): must fire below the floor, must not fire above it,
# must never import the broken package to find out, and must fail closed on nonsense -----------


def test_the_check_fires_below_the_floor(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "64.0.0")
    reason = _setuptools_build_backend_reason("66.1.0")
    assert reason is not None
    assert "64.0.0" in reason
    assert "UNTESTED HERE" in reason


def test_the_check_does_not_fire_at_or_above_the_floor(monkeypatch):
    """The must-not-fire half, paired with the test above."""
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "66.1.0")
    assert _setuptools_build_backend_reason("66.1.0") is None
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "84.0.0")
    assert _setuptools_build_backend_reason("66.1.0") is None


def test_the_check_fails_closed_on_an_unparseable_version(monkeypatch):
    """Reviewer finding: the version this check replaced failed OPEN on an unparseable string (returned None,
    meaning 'proceed to build')."""
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "not-a-version")
    reason = _setuptools_build_backend_reason("66.1.0")
    assert reason is not None
    assert "not-a-version" in reason


def test_the_check_fires_when_setuptools_is_not_installed_at_all(monkeypatch):
    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    reason = _setuptools_build_backend_reason("66.1.0")
    assert reason is not None
    assert "not installed" in reason


def test_the_check_never_imports_setuptools_itself_to_read_the_version(monkeypatch):
    """The reviewer's central finding: the version this check replaced read `setuptools.__version__`."""
    monkeypatch.setitem(sys.modules, "setuptools", None)  # any `import setuptools` now raises
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "64.0.0")
    reason = _setuptools_build_backend_reason("66.1.0")  # must not raise ImportError
    assert reason is not None
    assert "64.0.0" in reason


def test_the_skip_path_actually_skips_rather_than_crashing(monkeypatch):
    """Not just that the check returns a string."""
    monkeypatch.setitem(sys.modules, "setuptools", None)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0.0")
    with pytest.raises(pytest.skip.Exception) as ei:
        _build_sdist(REPO_ROOT)  # dest is never used -- the skip fires before any build call
    assert "1.0.0" in str(ei.value)
