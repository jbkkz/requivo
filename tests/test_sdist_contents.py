"""The sdist ships exactly what it means to ship -- never a half-collectable `tests/` tree (#431).

Filed from the 2026-09 readiness audit (packaging pass, verified against the actual PyPI artifact):
`requivo-3.0.0.tar.gz` shipped `tests/` (66 .py files) but not the underscore helpers
(`_fakes.py`, `_cli_harness.py`, `_scan.py`, `_credentials.py`, `conftest.py`), not `tests/web/`, not
the root `conftest.py`, not `scripts/`, not `fixtures/` -- so `pytest --co` against the extracted
sdist died at collection with `ModuleNotFoundError: No module named '_fakes'`, and even past that the
self-scanning guard tests read `workflows/`, plugin dirs and `fixtures/golden` the sdist never
carried at all.

The decision (see the direction section of #431, and `docs/compatibility.md`): exclude `tests/`
entirely rather than ship a complete second copy of the suite's fixtures/scripts/harnesses. The
guards' subjects are the repo, not the package -- distro packagers verify the wheel via the
wheel-install CI leg and the publish gates, not by re-running this suite inside the extracted sdist.
`MANIFEST.in`'s `prune tests` is the mechanism; this file is the proof it actually does that, built
the same way `python -m build --sdist` would (`setuptools.build_meta.build_sdist`, in-process, no
subprocess, no network -- `setuptools` is a `dev`-extra-only dependency purely for this test).

Would this pass if #431 did nothing? No: before `MANIFEST.in` existed, a real sdist build carried 70
files under `tests/` and this file's own assertion (`test_the_sdist_ships_no_tests_directory_at_all`)
was red against it.
"""
from __future__ import annotations

import importlib.metadata
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# The floor this project's own `dev` extra declares for setuptools (pyproject.toml's own comment
# there has the full story): below it, `import pkg_resources` itself crashes on Python 3.12 with
# `AttributeError: module 'pkgutil' has no attribute 'ImpImporter'` -- bisected directly against
# Python 3.12.13/3.12.14, not guessed.
#
# **This is checked at import time here too (#453), and the first version of this check (reviewer
# finding) was itself broken by the exact failure it existed to catch.** It read the version via
# `import setuptools; setuptools.__version__`, reached only after a `pytest.importorskip("setuptools",
# ...)` that performs the identical import -- so on setuptools 64.0.0-66.0.0, the plain `import
# setuptools` crashes with the same `AttributeError` before either the importorskip's own
# `except ImportError` (which does not catch `AttributeError`) or the version comparison ever ran.
# Verified directly: reinstalling setuptools==64.0.0 into a fresh Python 3.12 venv and rerunning this
# file reproduced the identical raw traceback in fixture setup on every test, including the one whose
# entire job was proving the guard fires.
#
# `importlib.metadata.version("setuptools")` is the fix: it reads the installed distribution's own
# METADATA file, never importing the package's code, so it cannot trigger `pkg_resources`'s crash --
# verified the same way, in the same broken venv: `importlib.metadata.version("setuptools")` returns
# `"64.0.0"` cleanly where `import setuptools` raises. `_build_sdist` below reaches
# `from setuptools.build_meta import build_sdist` only after this check has already confirmed the
# version is new enough to import safely.
# Raised from 66.1.0 to the PEP 639 parse floor (#337): since the licence moved to
# `[project] license = "Apache-2.0"`, a setuptools below 77 cannot read this pyproject.toml at all --
# it fails in `get_requires_for_build_wheel` with `project.license must be valid exactly by one
# definition`, which is a build *error*, not the clean skip this guard exists to produce. 77.0.0 was
# never published, so 77.0.1 is the lowest pinnable value. The `66.1.0` history is in pyproject.toml
# beside the floor itself: it is still what would decide this if the licence form were reverted.
_MIN_SETUPTOOLS_FOR_SDIST = "77.0.1"


def _setuptools_build_backend_reason(min_version: str) -> str | None:
    """None if the installed setuptools is present, parseable, and new enough to import and build
    with here; otherwise the skip reason -- covering "not installed", "installed but its version
    string does not parse", and "installed and too old" as three distinct, named cases.

    Deliberately reads the version through `importlib.metadata`, never through `import setuptools`
    -- see the module-level comment above for why that distinction is the whole fix. Comparison uses
    `packaging.version.Version` (a `dev`-extra dependency already, for `scripts/dependency_floor.py`)
    rather than manual tuple parsing, and fails CLOSED on an unparseable string: a version this check
    cannot read is unvetted, and the entire point of the check is to keep an unvetted build backend
    from ever being imported (a second reviewer finding on the version this function replaces, which
    failed OPEN -- `return None`, meaning "proceed" -- on exactly the same case)."""
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
    """Build a real sdist in-process, exactly as `python -m build --sdist --no-isolation` would --
    no subprocess, no isolated venv, no network. Skips (not fails) when `setuptools` is missing, its
    version string is unparseable, or it is too old to import at all on this interpreter -- see
    `_setuptools_build_backend_reason` above, checked BEFORE the first `import setuptools` this
    function performs (via `setuptools.build_meta`, below)."""
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
    """The chosen story is (a): exclude `tests/` entirely. Half-shipped -- some files but not the
    helpers they import -- is the one wrong answer #431 was filed about, so the assertion is not
    "the helpers are present too" (option b) but "there is no tests/ tree to be half of"."""
    offenders = [m for m in sdist_members if "/tests/" in m or m.rstrip("/").endswith("/tests")]
    assert offenders == [], f"the sdist still carries tests/ content: {offenders[:10]}"


def test_the_sdist_still_ships_the_package_and_its_assets(sdist_members):
    """A positive control for the assertion above (CLAUDE.md's own rule: a 'must not fire' case
    needs a paired 'must fire' case) -- an empty member list would pass the exclusion check for the
    wrong reason (nothing built, not nothing-to-prune)."""
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
    """The must-not-fire half, paired with the test above -- a version check that always returns a
    reason would pass the first test for the wrong one."""
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "66.1.0")
    assert _setuptools_build_backend_reason("66.1.0") is None
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "84.0.0")
    assert _setuptools_build_backend_reason("66.1.0") is None


def test_the_check_fails_closed_on_an_unparseable_version(monkeypatch):
    """Reviewer finding: the version this check replaced failed OPEN on an unparseable string
    (returned None, meaning 'proceed to build'). This one must return a reason instead -- an
    unparseable version is unvetted, not presumed safe."""
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
    """The reviewer's central finding: the version this check replaced read `setuptools.__version__`,
    which needs `import setuptools` first -- exactly what crashes on the range (64.0.0-66.0.0 under
    Python 3.12) it was supposed to detect. Cannot install a genuinely broken setuptools into this
    process, so it poisons `sys.modules["setuptools"]` so any `import setuptools` raises, then
    confirms the check still reaches a correct verdict without ever importing it."""
    monkeypatch.setitem(sys.modules, "setuptools", None)  # any `import setuptools` now raises
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "64.0.0")
    reason = _setuptools_build_backend_reason("66.1.0")  # must not raise ImportError
    assert reason is not None
    assert "64.0.0" in reason


def test_the_skip_path_actually_skips_rather_than_crashing(monkeypatch):
    """Not just that the check returns a string -- that `_build_sdist` itself turns that string into
    a real `pytest.skip`, end to end, before its own `from setuptools.build_meta import build_sdist`
    line, which is where a genuinely broken setuptools would otherwise crash. Poisons `setuptools`
    the same way as the test above, so a regression that moved the check to *after* that import
    would fail here with a raw `ImportError` instead of the expected `Skipped`."""
    monkeypatch.setitem(sys.modules, "setuptools", None)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0.0")
    with pytest.raises(pytest.skip.Exception) as ei:
        _build_sdist(REPO_ROOT)  # dest is never used -- the skip fires before any build call
    assert "1.0.0" in str(ei.value)
