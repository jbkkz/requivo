# The dependency floor is verified by installing it, not by inspecting the manifest

**Slug:** `dependency-floor-verified-by-install`

## Context

`scripts/dependency_floor.py` exists because no CI leg ever installed Requivo at the floor its own
`pyproject.toml` promises (#91): every job ran `pip install -e ".[dev]"`, which resolves to the
newest satisfying release, so a declared lower bound could be false and every leg would still be
green. That was not a hypothetical gap. `pydantic>=2.0` was false by eleven minor versions when this
script was written: the package does not import at all on 2.0.x, and up to 2.10 the two guards
pinning invariant 8's permissive-mirror graph (`SerializeAsAny`) fail on it. The v0.11.0 audit had
already cleared that exact bound -- by confirming `SerializeAsAny` is *exported* by pydantic 2.0.0 --
and that was the wrong question: a symbol existing is not the symbol working, and only installing the
thing tells the two apart.

## Decision

The floor is verified by asking a real resolver for the oldest release that satisfies each bound and
then checking what actually landed (`dependency_floor.py --verify`, fed by `uv pip install
--resolution lowest-direct`), never by simulating what a resolver would do. Nothing in this script
installs anything; installing is uv's job, and `--verify` only compares `importlib.metadata`
against the declared floor after the resolver has already run.

## What breaking it cost

The concrete instance above: a bound that read as satisfied and was not, for eleven minor versions,
with no CI leg saying so. Checking "does the symbol this project needs exist in the oldest allowed
release" instead of installing that release would have repeated the exact mistake this script exists
to close.

## Alternatives rejected

- **Generate a `name==floor` constraints file and install with it.** Tried first, and wrong twice
  over: `jinja2==3.1` names no release that exists at all (the oldest published `3.1.*` is `3.1.0`),
  and a floor is the oldest release a user can *actually get*, not the literal string in the
  manifest -- `pydantic>=2.0` resolves to `2.0.2` in practice, because nothing below it installs on
  every interpreter this project supports. `uv --resolution lowest-direct` is the one mechanism that
  answers the question the bound is actually asking.
- **Check that a required symbol imports, rather than installing the floor release.** Rejected for
  the reason in Context: it was tried on `pydantic>=2.0` by the v0.11.0 audit and passed while the
  bound was still false by eleven minor versions.
