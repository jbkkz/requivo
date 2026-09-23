"""What the runtime dependency floor *is*, and whether the environment is actually at it.

It owns what a resolver cannot: *which* requirements the `pyproject.toml` floor promises, and whether
the environment that came out is the one asked for (`decision: dependency-floor-verified-by-install`).
Scope: `[project] dependencies` plus the `anthropic`, `web` and `api` extras; `dev` is tooling, and
`tomli`/`packaging` measure the floor, so flooring them would check the leg against itself."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Hand-classified, so a new extra needs a decision rather than a default (`api`, #425, slipped once).
# Guarded by test_every_extra_in_the_manifest_is_either_floored_or_excluded_on_record.
RUNTIME_EXTRAS = ("anthropic", "web", "api")

# `name>=1.2.3` with optional trailing specifiers: `pydantic>=2.0,<3` -> ("pydantic", "2.0").
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<rest>.*)$")
_LOWER_BOUND = re.compile(r">=\s*(?P<version>[0-9][0-9A-Za-z.*+!-]*)")


class UndeclaredFloor(Exception):
    """A runtime requirement with no `>=` bound: raised, or its newest release is tested as the floor."""


def _floor(requirement: str) -> tuple[str, str]:
    """`("pydantic", "2.0")` for `pydantic>=2.0,<3`. Raises `UndeclaredFloor` if there is no floor."""
    match = _REQUIREMENT.match(requirement.strip())
    if not match:
        raise UndeclaredFloor(f"could not read a requirement out of {requirement!r}")
    name = match.group("name")
    bound = _LOWER_BOUND.search(match.group("rest"))
    if not bound:
        raise UndeclaredFloor(
            f"{name} declares no lower bound in pyproject.toml. Either give it one, or this leg is "
            f"reporting that it tested a floor that does not exist."
        )
    return name, bound.group("version")


def runtime_requirements(pyproject: dict) -> list[str]:
    """Every requirement string the runtime promise covers, in declaration order."""
    project = pyproject["project"]
    requirements = list(project.get("dependencies", []))
    extras = project.get("optional-dependencies", {})
    for extra in RUNTIME_EXTRAS:
        if extra not in extras:
            raise UndeclaredFloor(
                f"pyproject.toml declares no '{extra}' extra, but this script names it as part of "
                f"the runtime promise. One of the two is out of date -- do not drop it silently."
            )
        requirements.extend(extras[extra])
    return requirements


def constraints(pyproject: dict) -> list[str]:
    """The `name==floor` lines, deduplicated and sorted; two different floors for one name are refused."""
    pins: dict[str, str] = {}
    for requirement in runtime_requirements(pyproject):
        name, version = _floor(requirement)
        key = name.lower().replace("_", "-")
        if key in pins and pins[key] != version:
            raise UndeclaredFloor(
                f"{name} is declared with two different floors ({pins[key]} and {version}). pip "
                f"would silently pick one; say which is meant."
            )
        pins[key] = version
    return [f"{name}=={version}" for name, version in sorted(pins.items())]


def _load_toml(text: str) -> dict:
    """Parse TOML with the standard library, or with `tomli` (the same code under its pre-3.11 name)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - taken on 3.9/3.10, not on the version CI lints
        import tomli as tomllib
    return tomllib.loads(text)


def verify(pyproject: dict) -> list[str]:
    """Every runtime requirement whose *installed* version is not in its declared floor series.

        A resolver request is not an outcome: without this, dropping `--resolution lowest-direct` leaves
        the leg green over the newest of everything. The check is as precise as the declaration:
        `jinja2>=3.1` accepts any 3.1.x (there is no `3.1` release), `python-multipart>=0.0.9` exactly one.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed_version

    from packaging.version import Version

    wrong: list[str] = []
    for line in constraints(pyproject):
        name, _, floor = line.partition("==")
        try:
            found = installed_version(name)
        except PackageNotFoundError:
            wrong.append(f"{name}: declared floor {floor}, not installed at all")
            continue
        declared = Version(floor).release
        if Version(found).release[: len(declared)] != declared:
            wrong.append(f"{name}: declared floor {floor}, installed {found} — not in that series")
    return wrong


_HELP = """usage: dependency_floor.py [--verify | -h | --help | OUTPUT_PATH]

Generate, or verify, the pinned constraints file for this project's runtime dependency floors --
the lower bounds pyproject.toml promises to whoever runs `pip install requivo`.

With no arguments, write the `name==floor` constraints (one per runtime dependency) to stdout.

  OUTPUT_PATH   write the constraints to this file instead of stdout.
  --verify      check that the *installed* environment is actually at those floors, and exit
                non-zero (with a reason on stderr) if it is not.
  -h, --help    show this message and exit.

Any other leading-dash argument is refused rather than treated as an output path."""

# The only flags understood; any other "-..." is refused, not written as an output path (#494).
# Pinned by test_an_unrecognised_flag_is_refused_not_written_as_a_path.
_RECOGNISED_FLAGS = ("--verify", "-h", "--help")


def main(argv: list[str]) -> int:
    args = argv[1:]

    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0

    unrecognised = [a for a in args if a.startswith("-") and a not in _RECOGNISED_FLAGS]
    if unrecognised:
        print(
            f"dependency_floor.py: unrecognised option {unrecognised[0]!r} -- "
            f"run with --help for usage",
            file=sys.stderr,
        )
        return 2

    root = Path(__file__).resolve().parents[1]
    # Explicit codec (#11): this file carries em dashes.
    pyproject = _load_toml((root / "pyproject.toml").read_text(encoding="utf-8"))
    if args and args[0] == "--verify":
        wrong = verify(pyproject)
        for line in wrong:
            print(f"floor not installed -- {line}", file=sys.stderr)
        if wrong:
            print(f"{len(wrong)} runtime dependency/dependencies are not at their declared floor, so "
                  f"this leg did not test what it says it tested.", file=sys.stderr)
            return 1
        print(f"all {len(constraints(pyproject))} runtime dependencies are installed at their "
              f"declared floor")
        return 0

    lines = constraints(pyproject)
    if not lines:
        print("dependency_floor.py produced no constraints -- an empty floor file would install "
              "the newest of everything and report that it tested the floor", file=sys.stderr)
        return 2
    out = Path(args[0]) if args else None
    body = "\n".join(lines) + "\n"
    if out:
        out.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
