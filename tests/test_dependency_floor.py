"""`scripts/dependency_floor.py`, the generator behind the Dependency floor CI leg (#91), and the dependabot
commit-message rule a grouped runtime bump must not break (#347)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dependency_floor  # noqa: E402
from dependency_floor import (  # noqa: E402
    RUNTIME_EXTRAS,
    UndeclaredFloor,
    _floor,
    _load_toml,
    constraints,
    runtime_requirements,
    verify,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"


def _real_pyproject() -> dict:
    return _load_toml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _extras(**declared) -> dict:
    """Every extra `RUNTIME_EXTRAS` names, empty unless this test declares one (#425)."""
    return {extra: list(declared.get(extra, ())) for extra in RUNTIME_EXTRAS}


def _pyproject(dependencies, **extras) -> dict:
    return {"project": {"dependencies": dependencies, "optional-dependencies": _extras(**extras)}}


# ── reading requirements and assembling the set ────────────────────────────────────


@pytest.mark.parametrize("requirement, expected", [
    ("pydantic>=2.0,<3", ("pydantic", "2.0")), ("python-dotenv>=1.0.0,<2", ("python-dotenv", "1.0.0")),
    ("anthropic >= 0.40.0, <1", ("anthropic", "0.40.0")), ("tomli>=1.1.0; python_version < '3.11'", ("tomli", "1.1.0")),
])
def test_the_floor_is_read_off_the_lower_bound(requirement, expected):
    assert _floor(requirement) == expected


@pytest.mark.parametrize("requirement", ["httpx", "ruff", "some-package<2", "  "])
def test_a_requirement_with_no_lower_bound_is_refused(requirement):
    """Refused, not skipped: a skipped dependency installs at its newest and the leg still reports a tested floor."""
    with pytest.raises(UndeclaredFloor):
        _floor(requirement)


def test_a_named_runtime_extra_that_is_gone_is_refused():
    with pytest.raises(UndeclaredFloor, match="declares no"):
        runtime_requirements({"project": {"dependencies": ["pydantic>=2.0"], "optional-dependencies": {}}})


def test_one_name_with_two_different_floors_is_refused():
    """pip would pick one, and the leg would report a floor nobody declared."""
    with pytest.raises(UndeclaredFloor, match="two different floors"):
        constraints(_pyproject(["jinja2>=3.1,<4"], anthropic=["anthropic>=0.40.0"], web=["jinja2>=3.0,<4"]))


def test_the_same_floor_declared_twice_is_not_a_contradiction():
    """The must-not-fire half: `web` and `dev` legitimately restate one requirement."""
    pins = constraints(_pyproject(["jinja2>=3.1,<4"], anthropic=["anthropic>=0.40.0"], web=["jinja2>=3.1,<4"]))
    assert pins == ["anthropic==0.40.0", "jinja2==3.1"]


# ── against the real manifest ───────────────────────────────────────────────────────


def test_every_external_requirement_declares_a_lower_bound():
    manifest = _real_pyproject()
    project = manifest["project"]
    for requirements in (manifest["build-system"]["requires"], project["dependencies"], *project["optional-dependencies"].values()):
        for requirement in requirements:
            if canonicalize_name(Requirement(requirement).name) != canonicalize_name(project["name"]):  # a self-extra is no promise
                _floor(requirement)


def test_the_real_manifest_yields_every_runtime_dependency_and_no_dev_tooling():
    """Named against the manifest, so adding a dependency passes while dropping one from the floor set still fails."""
    pins = dict(line.split("==") for line in constraints(_real_pyproject()))
    for name in ("pydantic", "python-dotenv", "anthropic", "fastapi", "uvicorn", "jinja2", "python-multipart"):
        assert name in pins, f"{name} is a runtime dependency and is missing from the floor set"
    for name in ("pytest", "ruff", "httpx", "tomli", "packaging"):
        assert name not in pins, f"{name} is dev tooling and must not be in the floor set"


# Extras the runtime promise deliberately does not cover, each with the reason.
_NOT_A_RUNTIME_PROMISE = {
    "dev": "this project's own toolchain; flooring the measuring instrument is a leg checking itself",
    "testing": "the repository conformance suite (#424), test scaffolding rather than a runtime path",
}


def test_every_extra_in_the_manifest_is_either_floored_or_excluded_on_record():
    """Both directions: every extra is classified in one place, and no exclusion names an extra that is gone (#425)."""
    extras = set(_real_pyproject()["project"]["optional-dependencies"])
    assert set(RUNTIME_EXTRAS) <= extras, "RUNTIME_EXTRAS names an extra pyproject.toml does not declare"
    unclassified = extras - set(RUNTIME_EXTRAS) - set(_NOT_A_RUNTIME_PROMISE)
    assert not unclassified, f"{sorted(unclassified)} is neither in RUNTIME_EXTRAS nor excluded on record"
    assert not (set(_NOT_A_RUNTIME_PROMISE) - extras), "_NOT_A_RUNTIME_PROMISE excuses an extra that is gone"


# ── the verify half, and argument handling (#494) ───────────────────────────────────


def test_verify_compares_versions_and_not_strings():
    """`fastapi==0.110` installs and reports itself as `0.110.0`."""
    assert Version("0.110") == Version("0.110.0") and Version("3.1") != Version("3.1.6")


def test_verify_reports_a_dependency_that_is_not_installed_at_all():
    wrong = verify(_pyproject(["requivo-nonexistent-package>=9.9.9"]))
    assert len(wrong) == 1 and "not installed at all" in wrong[0]


@pytest.mark.parametrize("flag, code, stream", [("--help", 0, "out"), ("--verfiy", 1, "err")])
def test_an_unrecognised_flag_is_refused_not_written_as_a_path(tmp_path, monkeypatch, capsys, flag, code, stream):
    """A flag, typo'd or not, is never taken as the output path (#494)."""
    monkeypatch.chdir(tmp_path)
    exit_code = dependency_floor.main(["dependency_floor.py", flag])
    captured = capsys.readouterr()
    assert (exit_code == 0) is (code == 0) and getattr(captured, stream).strip()
    assert not (tmp_path / flag).exists(), f"must not write a file called {flag}"


def test_verify_still_runs_when_named_explicitly(monkeypatch, capsys):
    monkeypatch.setattr(dependency_floor, "verify", lambda pyproject: [])
    assert dependency_floor.main(["dependency_floor.py", "--verify"]) == 0
    assert "declared floor" in capsys.readouterr().out


def test_a_bare_output_path_writes_the_constraints_file_and_no_args_writes_stdout(tmp_path, capsys):
    out = tmp_path / "floor-constraints.txt"
    assert dependency_floor.main(["dependency_floor.py", str(out)]) == 0
    assert out.read_text(encoding="utf-8").strip().splitlines() == constraints(_real_pyproject())
    assert dependency_floor.main(["dependency_floor.py"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == constraints(_real_pyproject())


# ── #347: a grouped runtime bump must not be titled `chore(deps-dev)` ─────────────────

# Anchored at a line's key position and never inside a comment, so the file's own prose cannot trip it.
_PREFIX_DEV_RE = re.compile(r'^[ \t]*prefix-development[ \t]*:', re.MULTILINE)
_PREFIX_RE = re.compile(r'^[ \t]*prefix[ \t]*:[ \t]*["\']chore\(deps\)["\']', re.MULTILINE)


def _ecosystem_block(ecosystem: str) -> str:
    """The YAML text of one `- package-ecosystem: <ecosystem>` entry in the real dependabot.yml."""
    text = DEPENDABOT.read_text(encoding="utf-8")
    rest = text[text.index(f"- package-ecosystem: {ecosystem}") + len(f"- package-ecosystem: {ecosystem}"):]
    next_at = rest.find("\n  - package-ecosystem:")
    return rest if next_at == -1 else rest[:next_at]


def _prefix_offence(block: str) -> str | None:
    """Why a pip-ecosystem block reintroduces #347, or None."""
    code = "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))
    if "commit-message:" not in code:
        return "no `commit-message:` block under the pip ecosystem -- dependabot falls back to its production/development split"
    if _PREFIX_DEV_RE.search(code):
        return "`prefix-development` is set under the pip ecosystem -- the classification reaches the title again"
    if not _PREFIX_RE.search(code):
        return "the pip ecosystem's commit-message prefix is not `chore(deps)` -- update this guard deliberately"
    return None


def test_the_pip_block_sets_one_prefix_and_no_development_split():
    assert _prefix_offence(_ecosystem_block("pip")) is None


_PIP = "  - package-ecosystem: pip\n"


@pytest.mark.parametrize("block, flagged", [
    (_PIP + "    commit-message:\n      prefix: \"chore(deps)\"\n      prefix-development: \"chore(deps-dev)\"\n    groups:\n", True),
    (_PIP + "    groups:\n", True),
    (_PIP + "    commit-message:\n      prefix: \"chore(deps)\"\n    groups:\n", False),
], ids=["reintroduced-split", "no-commit-message-block", "clean-block"])
def test_the_guard_classifies_a_pip_block_correctly(block, flagged):
    assert (_prefix_offence(block) is not None) == flagged


def test_the_github_actions_block_is_untouched():
    """#347 is scoped to pip: `github-actions` has no production/development classification to mislabel."""
    assert "commit-message" not in _ecosystem_block("github-actions")
