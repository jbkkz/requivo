"""The perimeter registry (#608) — one vocabulary per session, not one per install.

A perimeter owns a slot schema, an elicitation spec, discovery guidance specific to it, and the
artifact types it can produce (`decision: the-job-not-the-artifact-type`). The Core stays perimeter-
free: `Slot`, `Confidence`, `Impact`, the dependency graph, the session store, all reason over slot
ids and never care which vocabulary they came from. This module is the one place that resolves a
perimeter id to the assets it owns.

Deliberately data, not code: adding a perimeter is a directory under `assets/perimeters/` plus one
row below naming which artifact types it may produce -- never a new code path through `core/`,
`services/` or a provider.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from requivo.core.errors import UnknownPerimeterError
from requivo.paths import PERIMETERS

SOFTWARE = "software"
GO_TO_MARKET = "go-to-market"

# A session written before perimeters existed carries none, and reads as this one -- a default,
# never a guess, because there was only ever one (#608's migration note).
DEFAULT_PERIMETER = SOFTWARE

# Which artifact types each perimeter may produce, per #607's cost rule ("each new perimeter ships
# with exactly one artifact... a second is added when a user asks"). Software carries every generator
# that exists today; go-to-market ships none yet -- its one artifact is #609's own scope, and an
# empty set here is what keeps its registries trivially agreeing with every other table until #609
# adds a row to all of them together (`test_the_real_artifact_registries_agree_on_their_key_sets`).
_ARTIFACT_TYPES: dict[str, frozenset[str]] = {
    SOFTWARE: frozenset(
        {"brief", "prd", "stories", "estimate", "criteria", "epic", "release"}),
    GO_TO_MARKET: frozenset(),
}


@dataclass(frozen=True)
class Perimeter:
    """One installed perimeter: its id, the directory its assets live under, and the artifact types
    it may produce. `schema_path`/`elicitation_path`/`engine_guidance_path` are its three owned
    files -- a directory holding model_schema.json, elicitation.md and engine_guidance.md."""

    id: str
    dir: Path
    artifact_types: frozenset[str]

    @property
    def schema_path(self) -> Path:
        return self.dir / "model_schema.json"

    @property
    def elicitation_path(self) -> Path:
        return self.dir / "elicitation.md"

    @property
    def engine_guidance_path(self) -> Path:
        """The perimeter-specific fragment substituted into `engine.md`'s `{{PERIMETER_GUIDANCE}}` --
        the one piece of discovery guidance the issue names explicitly: a software heuristic like
        "primary objects first" must never reach a session running a different perimeter."""
        return self.dir / "engine_guidance.md"


@functools.lru_cache(maxsize=1)
def _registry() -> dict[str, Perimeter]:
    return {pid: Perimeter(id=pid, dir=PERIMETERS / pid, artifact_types=types)
            for pid, types in _ARTIFACT_TYPES.items()}


def known_perimeter_ids() -> tuple[str, ...]:
    """Every installed perimeter's id, sorted -- the vocabulary `doctor` and a `--perimeter` flag
    report against."""
    return tuple(sorted(_registry()))


def get_perimeter(perimeter_id: str) -> Perimeter:
    """The installed perimeter named `perimeter_id`, or `UnknownPerimeterError` -- refused by name,
    never tolerated or defaulted (#608's deliberate inversion of invariant 8)."""
    try:
        return _registry()[perimeter_id]
    except KeyError:
        raise UnknownPerimeterError(
            f"unknown perimeter {perimeter_id!r} -- this install has: "
            f"{', '.join(known_perimeter_ids())}. Upgrade requivo, or check that the session was not "
            "written by a newer install.",
            details={"perimeter": perimeter_id, "known": list(known_perimeter_ids())},
        ) from None


def resolve_perimeter(name: str | None) -> str:
    """A session's recorded perimeter id, or the software default for one recorded as `None` (a
    pre-perimeter session -- there was only ever one, so this is the migration, not a guess). Raises
    `UnknownPerimeterError` for a *named* perimeter this install does not have."""
    if name is None:
        return DEFAULT_PERIMETER
    get_perimeter(name)  # raises by name if unknown
    return name
