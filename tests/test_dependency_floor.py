"""`scripts/dependency_floor.py` — the generator behind the Dependency floor CI leg (#91).

The leg installs Requivo at the oldest release every runtime dependency declares, and runs the suite
against it. That is only worth a job if the floor set is *complete*: a requirement that quietly drops
out leaves pip resolving it to the newest release while the leg still reports having tested the
floor, which is the silent absence the whole leg exists to close, reappearing inside the check for
it. So every way of losing a requirement raises here rather than returning a shorter list.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dependency_floor  # noqa: E402
from dependency_floor import (  # noqa: E402
    RUNTIME_EXTRAS,
    UndeclaredFloor,
    _floor,
    _load_toml,
    constraints,
    runtime_requirements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_pyproject() -> dict:
    return _load_toml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ── reading one requirement ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("requirement, expected", [
    ("pydantic>=2.0,<3", ("pydantic", "2.0")),
    ("python-dotenv>=1.0.0,<2", ("python-dotenv", "1.0.0")),
    ("anthropic >= 0.40.0, <1", ("anthropic", "0.40.0")),
    ("tomli>=1.1.0; python_version < '3.11'", ("tomli", "1.1.0")),
])
def test_the_floor_is_read_off_the_lower_bound(requirement, expected):
    assert _floor(requirement) == expected


@pytest.mark.parametrize("requirement", ["httpx", "ruff", "some-package<2", "  "])
def test_a_requirement_with_no_lower_bound_is_refused(requirement):
    """Refused, not skipped. Skipping is how a dependency ends up outside the constraints file with
    the leg still green — the newest release installed, and a report that the floor was tested."""
    with pytest.raises(UndeclaredFloor):
        _floor(requirement)


# ── assembling the set ─────────────────────────────────────────────────────────────────────────

def _extras(**declared) -> dict:
    """Every extra `RUNTIME_EXTRAS` names, empty unless this test declares one.

    Derived rather than hand-listed. `runtime_requirements` refuses a manifest missing an extra the
    script names -- correctly, that is the check above -- so a synthetic manifest that hardcodes the
    set raises `declares no <extra>` before the test's own assertion can run. Three of these tests
    did exactly that and went red the moment `api` joined `RUNTIME_EXTRAS` (#425), each with a
    message about a missing extra rather than about the thing it was testing.
    """
    return {extra: list(declared.get(extra, ())) for extra in RUNTIME_EXTRAS}

def test_a_named_runtime_extra_that_is_gone_is_refused():
    """`RUNTIME_EXTRAS` names the extras a user installs. If one is renamed or removed, the script
    must say so: silently covering one fewer extra is a narrower promise reported as the same one."""
    pyproject = {"project": {"dependencies": ["pydantic>=2.0"], "optional-dependencies": {}}}
    with pytest.raises(UndeclaredFloor, match="declares no"):
        runtime_requirements(pyproject)


def test_one_name_with_two_different_floors_is_refused():
    """pip would resolve the contradiction by picking one, and the leg would report a floor nobody
    declared. The manifest is saying two things about one package; that is for a person to settle."""
    pyproject = {"project": {
        "dependencies": ["jinja2>=3.1,<4"],
        "optional-dependencies": _extras(anthropic=["anthropic>=0.40.0"], web=["jinja2>=3.0,<4"]),
    }}
    with pytest.raises(UndeclaredFloor, match="two different floors"):
        constraints(pyproject)


def test_the_same_floor_declared_twice_is_not_a_contradiction():
    """The must-not-fire half: `web` and `dev` legitimately restate the same requirement, and the
    duplicate is deduplicated rather than treated as a disagreement."""
    pyproject = {"project": {
        "dependencies": ["jinja2>=3.1,<4"],
        "optional-dependencies": _extras(anthropic=["anthropic>=0.40.0"], web=["jinja2>=3.1,<4"]),
    }}
    assert constraints(pyproject) == ["anthropic==0.40.0", "jinja2==3.1"]


# ── against the real manifest ──────────────────────────────────────────────────────────────────

def test_every_external_requirement_declares_a_lower_bound():
    manifest = _real_pyproject()
    project = manifest["project"]
    groups = [manifest["build-system"]["requires"], project["dependencies"],
              *project["optional-dependencies"].values()]
    for requirements in groups:
        for requirement in requirements:
            parsed = Requirement(requirement)
            if canonicalize_name(parsed.name) == canonicalize_name(project["name"]):
                # Selecting this checkout's own extras is not an external version promise.
                continue
            _floor(requirement)


def test_the_real_manifest_yields_every_runtime_dependency():
    """Named against the manifest rather than a count, so adding a dependency does not fail this
    test while *dropping* one from the floor set still does."""
    pins = dict(line.split("==") for line in constraints(_real_pyproject()))
    for name in ("pydantic", "python-dotenv", "anthropic", "fastapi", "uvicorn", "jinja2",
                 "python-multipart"):
        assert name in pins, f"{name} is a runtime dependency and is missing from the floor set"


def test_the_dev_toolchain_is_not_floored():
    """Scope is the promise a *user's* resolver has to satisfy. Pinning pytest and ruff to their
    floors would test this project's harness, and `tomli`/`packaging` are how the floor is measured
    — flooring the measuring instrument is a leg checking itself."""
    pins = dict(line.split("==") for line in constraints(_real_pyproject()))
    for name in ("pytest", "ruff", "httpx", "tomli", "packaging"):
        assert name not in pins, f"{name} is dev tooling and must not be in the floor set"


def test_every_runtime_extra_named_here_exists_in_the_manifest():
    """The other direction of the same rule: an extra this script claims to cover but the manifest
    no longer declares is a promise about something that is not there."""
    extras = _real_pyproject()["project"]["optional-dependencies"]
    for extra in RUNTIME_EXTRAS:
        assert extra in extras, f"RUNTIME_EXTRAS names '{extra}', which pyproject.toml does not declare"


# Extras the runtime promise deliberately does not cover, each with the reason. An entry here is a
# classification on record; the test below is what makes the classification compulsory.
_NOT_A_RUNTIME_PROMISE = {
    "dev": "this project's own toolchain -- pytest, ruff, and the tomli/packaging that measure the "
           "floor. Flooring the measuring instrument is a leg checking itself.",
    "testing": "the repository conformance suite (#424), installed by a consumer writing their own "
               "pytest suite against a non-file backing -- test scaffolding, not a runtime path.",
}


def test_every_extra_in_the_manifest_is_either_floored_or_excluded_on_record():
    """The direction `test_every_runtime_extra_named_here_exists_in_the_manifest` does not check,
    and the one that actually leaked. `RUNTIME_EXTRAS` is hand-maintained precisely so a new extra
    is classified by a person -- but nothing failed when a person did not, so `[api]` shipped
    user-installable by name and outside the floor set, which is the silent-narrower-promise this
    whole script exists to prevent, reached through the script's own configuration (#425).

    Adding an extra now forces the choice: floor it, or say here why it is not a runtime promise."""
    extras = set(_real_pyproject()["project"]["optional-dependencies"])
    unclassified = extras - set(RUNTIME_EXTRAS) - set(_NOT_A_RUNTIME_PROMISE)
    assert not unclassified, (
        f"pyproject.toml declares {sorted(unclassified)}, which is neither in RUNTIME_EXTRAS nor "
        f"excluded on record in _NOT_A_RUNTIME_PROMISE. A user installing it by name gets a "
        f"promise the floor leg does not test. Classify it in one place or the other."
    )
    assert not (set(_NOT_A_RUNTIME_PROMISE) - extras), (
        "_NOT_A_RUNTIME_PROMISE excuses an extra pyproject.toml no longer declares -- prose that "
        "no longer describes anything, which is how an exclusion list stops being read."
    )


# ── the verify half ────────────────────────────────────────────────────────────────────────────

def test_verify_compares_versions_and_not_strings():
    """`fastapi==0.110` installs and reports itself as `0.110.0`. A string comparison would fail the
    leg over a difference that is not one, and the first person to hit it would delete the check
    rather than the comparison. `3.1` against `3.1.6` is a real difference and must still fail."""
    from packaging.version import Version
    assert Version("0.110") == Version("0.110.0")
    assert Version("2.0") == Version("2.0.0")
    assert Version("3.1") != Version("3.1.6")


def test_verify_reports_a_dependency_that_is_not_installed_at_all():
    """Absent is not satisfied. A name pip never had to resolve — because nothing imported it, or
    because an extra was installed by a later command without the constraints file — is exactly the
    case that leaves the leg green over nothing."""
    from dependency_floor import verify
    pyproject = {"project": {
        "dependencies": ["requivo-nonexistent-package>=9.9.9"],
        "optional-dependencies": _extras(),
    }}
    wrong = verify(pyproject)
    assert len(wrong) == 1
    assert "not installed at all" in wrong[0]


# -- argument handling (#494) -----------------------------------------------------------------
#
# The bare positional argument used to be "whatever args[0] is, treat it as an output path" with
# exactly one carve-out (--verify). That means --help writes a file named --help and exits 0,
# and any typo of --verify does the same -- silently, with a success exit code, so the reader
# believes a check ran that never did. These five tests pin the shapes the fix must produce: a
# real --help that writes nothing, a refused unrecognised flag that writes nothing and exits
# non-zero, and the existing behaviours (--verify, a bare output path, no args at all) left alone.

def test_help_prints_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    exit_code = dependency_floor.main(["dependency_floor.py", "--help"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip(), "must print something to stdout"
    assert not (tmp_path / "--help").exists(), "must not write a file called --help"


def test_an_unrecognised_flag_is_refused_not_written_as_a_path(tmp_path, monkeypatch, capsys):
    """A typo of --verify is the worse case named in the issue: it must not exit 0, because a
    reader would believe the verification ran and passed."""
    monkeypatch.chdir(tmp_path)
    exit_code = dependency_floor.main(["dependency_floor.py", "--verfiy"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err.strip(), "must say something on stderr"
    assert not (tmp_path / "--verfiy").exists(), "must not write a file called --verfiy"


def test_verify_still_runs_when_named_explicitly(monkeypatch, capsys):
    monkeypatch.setattr(dependency_floor, "verify", lambda pyproject: [])
    exit_code = dependency_floor.main(["dependency_floor.py", "--verify"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "declared floor" in captured.out


def test_a_bare_output_path_still_writes_the_constraints_file(tmp_path):
    out = tmp_path / "floor-constraints.txt"
    exit_code = dependency_floor.main(["dependency_floor.py", str(out)])
    assert exit_code == 0
    assert out.exists()
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines == constraints(_real_pyproject())


def test_no_args_still_writes_to_stdout(capsys):
    exit_code = dependency_floor.main(["dependency_floor.py"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip().splitlines() == constraints(_real_pyproject())
