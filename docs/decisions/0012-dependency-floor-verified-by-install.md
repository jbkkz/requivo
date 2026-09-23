# The dependency floor is verified by installing it, not by inspecting the manifest

**Slug:** `dependency-floor-verified-by-install`

## Context

No CI leg ever installed Requivo at the floor its `pyproject.toml` promises (#91): `pip install -e`
resolves to the newest release, so a false lower bound stays green. `pydantic>=2.0` was false by
eleven minor versions — no import on 2.0.x, and up to 2.10 the `SerializeAsAny` guards on invariant
8's permissive mirror fail. The v0.11.0 audit had cleared that bound by checking the symbol was
*exported*; a symbol existing is not the symbol working.

## Decision

Ask a real resolver for the oldest satisfying release and check what landed:
`uv pip install --resolution lowest-direct`, then `scripts/dependency_floor.py --verify` compares
`importlib.metadata` against the declared floor. The script installs nothing and simulates no
resolver.

## What breaking it cost

A bound that read as satisfied and was not, for eleven minor versions, with no leg saying so.

## Alternatives rejected

- **A `name==floor` constraints file** — tried first: `jinja2==3.1` names no release (the oldest is
  `3.1.0`), and a floor is the oldest release a user can actually get (`pydantic>=2.0` resolves to
  `2.0.2`), not the literal in the manifest.
- **Check that a required symbol imports** — the v0.11.0 audit did, and passed a false bound.
