"""The perimeter registry (#608): one vocabulary per session. A perimeter owns a slot schema, an
elicitation spec, discovery guidance and the artifact types it produces
(`decision: the-job-not-the-artifact-type`); the Core reasons over slot ids and never cares which.
Data, not code: adding a perimeter is a directory under `assets/perimeters/` plus one row here.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from requivo.core.errors import UnknownPerimeterError
from requivo.paths import PERIMETERS

SOFTWARE = "software"
GO_TO_MARKET = "go-to-market"

# A pre-perimeter session carries none and reads as this one: a default, never a guess (#608).
DEFAULT_PERIMETER = SOFTWARE

# Which artifact types each perimeter may produce (#607's cost rule: one per new perimeter).
# `test_the_real_artifact_registries_agree_on_their_key_sets` cross-checks every table.
_ARTIFACT_TYPES: dict[str, frozenset[str]] = {
    SOFTWARE: frozenset(
        {"brief", "prd", "stories", "estimate", "criteria", "epic", "release"}),
    # Go-to-market's one artifact (#609), a distinct key from "brief" since the registries are keyed globally.
    GO_TO_MARKET: frozenset({"gtm_plan"}),
}

# The one type each perimeter's page and next-step hint lead with, read by both surfaces so the Web
# and the CLI cannot drift the way a software-only local default did (#609).
_PRIMARY_ARTIFACT: dict[str, str] = {
    SOFTWARE: "brief",
    GO_TO_MARKET: "gtm_plan",
}


@dataclass(frozen=True)
class Perimeter:
    """One installed perimeter: its id, its asset directory, the artifact types it may produce, and the primary one."""

    id: str
    dir: Path
    artifact_types: frozenset[str]
    # `None` means *nothing leads*; never fall back to another perimeter's primary (#609).
    primary_artifact: str | None = None

    @property
    def schema_path(self) -> Path:
        return self.dir / "model_schema.json"

    @property
    def elicitation_path(self) -> Path:
        return self.dir / "elicitation.md"

    @property
    def engine_guidance_path(self) -> Path:
        """The perimeter-specific fragment substituted into `engine.md`'s `{{PERIMETER_GUIDANCE}}`."""
        return self.dir / "engine_guidance.md"

    @property
    def router_hint_path(self) -> Path:
        """One short paragraph naming the kind of request this perimeter fits, read by the router (#601)."""
        return self.dir / "router_hint.md"


@functools.lru_cache(maxsize=1)
def _registry() -> dict[str, Perimeter]:
    return {pid: Perimeter(id=pid, dir=PERIMETERS / pid, artifact_types=types,
                           primary_artifact=_PRIMARY_ARTIFACT.get(pid))
            for pid, types in _ARTIFACT_TYPES.items()}


def known_perimeter_ids() -> tuple[str, ...]:
    """Every installed perimeter's id, sorted."""
    return tuple(sorted(_registry()))


def get_perimeter(perimeter_id: str) -> Perimeter:
    """The installed perimeter named `perimeter_id`, or `UnknownPerimeterError`: refused by name, never defaulted (#608)."""
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
    """A session's recorded perimeter id, or the software default for `None` (a pre-perimeter session);
    a named unknown raises `UnknownPerimeterError`."""
    if name is None:
        return DEFAULT_PERIMETER
    get_perimeter(name)  # raises by name if unknown
    return name


class PerimeterSummary(NamedTuple):
    """One installed perimeter reduced to what a routing judgment (#601) needs; `unreadable` is the third state."""

    id: str
    hint: str
    unreadable: bool = False


def perimeter_summaries() -> list[PerimeterSummary]:
    """Every installed perimeter as one line for the router; a per-perimeter read failure degrades its row (invariant 15)."""
    out = []
    for pid in known_perimeter_ids():
        try:
            hint = get_perimeter(pid).router_hint_path.read_text(encoding="utf-8").strip()
        except OSError:
            out.append(PerimeterSummary(id=pid, hint="", unreadable=True))
            continue
        out.append(PerimeterSummary(id=pid, hint=hint))
    return out
