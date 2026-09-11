"""What the runtime dependency floor *is*, and whether the environment is actually at it.

`pyproject.toml`'s lower bounds are a promise to whoever runs `pip install requivo`. This script is
what makes that promise testable: it owns the two halves a resolver cannot supply on its own --
*which* requirements the promise covers, and whether the environment that came out is the one that
was asked for. Why verification means installing the floor rather than inspecting the bound, and why
a generated constraints file was rejected: `decision: dependency-floor-verified-by-install`.

Scope is the *runtime* promise: `[project] dependencies` plus the `anthropic`, `web` and `api`
extras, each of which a user installs by name. The `dev` extra is deliberately excluded -- pytest and ruff are
this project's tooling, not something a user's resolver has to satisfy, and `tomli`/`packaging` are
how the floor is measured, so flooring them is a leg checking itself.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Hand-classified rather than "every extra except dev", so a new extra needs a person's decision
# instead of a silent default -- `api` (#425) escaped it for one review round.
# Guarded by test_every_extra_in_the_manifest_is_either_floored_or_excluded_on_record.
RUNTIME_EXTRAS = ("anthropic", "web", "api")

# `name>=1.2.3` with optional trailing specifiers: `pydantic>=2.0,<3` -> ("pydantic", "2.0").
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<rest>.*)$")
_LOWER_BOUND = re.compile(r">=\s*(?P<version>[0-9][0-9A-Za-z.*+!-]*)")


class UndeclaredFloor(Exception):
    """A runtime requirement with no `>=` bound.

    Raised rather than skipped, and that is the whole point of this script existing. A requirement
    that quietly drops out of the constraints file leaves a leg that installs *the newest* of it and
    still reports having tested the floor -- the silent absence this file was written to close,
    reappearing inside the check for it.
    """


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
    """The `name==floor` lines, deduplicated on name and sorted.

    A name appearing in two extras with two different floors is a contradiction pip would resolve by
    picking one, so it is refused here instead: the manifest is saying two things about one package.
    """
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
    """Parse TOML with the standard library, or with `tomli` below 3.11.

    Not a fallback parser: `tomli` is the library that *became* `tomllib`, same code and same
    author, so this is one implementation reached by two names rather than two answers that can
    drift. It is in the `dev` extra and nowhere near the runtime promise this script measures.

    The alternative was to require 3.11 and let the one CI leg pick its interpreter -- rejected
    because the supported floor is 3.9, and a check that cannot run on the developer's own
    interpreter is verified only by the leg it is meant to feed.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - taken on 3.9/3.10, not on the version CI lints
        import tomli as tomllib
    return tomllib.loads(text)


def verify(pyproject: dict) -> list[str]:
    """Every runtime requirement whose *installed* version is not in its declared floor series.

    The half that makes the leg mean anything. Asking a resolver for the oldest releases is a
    request, not an outcome: drop `--resolution lowest-direct` from the command, or install an extra
    in a second call without it, and the environment comes back at the newest of everything with
    this leg still green over it.

    **The check is exactly as precise as the declaration, and that is the design rather than a
    shortfall.** `python-multipart>=0.0.9` names one release, so 0.0.20 fails. `jinja2>=3.1` promises
    the 3.1 series and nothing narrower, so any 3.1.x satisfies it and 3.2 does not. Demanding an
    exact match against the literal bound was tried first and is wrong twice over: `jinja2==3.1` names
    no release that exists — the oldest is 3.1.0 — and `pydantic>=2.0` resolves to 2.0.2, because the
    releases below it are not installable on every supported interpreter. A floor is the oldest
    release a user can actually get, not the string in the manifest.
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

# The only flags this script understands. Anything else starting with "-" is refused rather than
# treated as an output path: a typo of --verify used to write a file of that name and exit 0, so a
# reader believed a verification had run that never did (#494). Pinned by
# test_an_unrecognised_flag_is_refused_not_written_as_a_path.
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
    # Explicit codec (#11): `read_text()` with no encoding decodes with the *locale* codepage, and
    # this file carries em dashes.
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
