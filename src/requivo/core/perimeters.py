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
from typing import NamedTuple

from requivo.core.errors import UnknownPerimeterError
from requivo.paths import PERIMETERS

SOFTWARE = "software"
GO_TO_MARKET = "go-to-market"

# A session written before perimeters existed carries none, and reads as this one -- a default,
# never a guess, because there was only ever one (#608's migration note).
DEFAULT_PERIMETER = SOFTWARE

# Which artifact types each perimeter may produce, per #607's cost rule ("each new perimeter ships
# with exactly one artifact... a second is added when a user asks"). Software carries every generator
# that exists today; go-to-market ships exactly its one (#609: `gtm_plan`), registered the same way
# in every table `test_the_real_artifact_registries_agree_on_their_key_sets` cross-checks.
_ARTIFACT_TYPES: dict[str, frozenset[str]] = {
    SOFTWARE: frozenset(
        {"brief", "prd", "stories", "estimate", "criteria", "epic", "release"}),
    # #609: go-to-market's one artifact, per #607's cost rule -- its equivalent of the decision
    # brief, over its own twelve slots. A distinct key from "brief" on purpose: the two are
    # different contracts (`GoToMarketPlan` vs `Brief`) reasoned from different schemas, and the
    # registries below (`_GENERATORS`, `_WRITERS`, `ARTIFACT_FILENAMES`, ...) are keyed globally,
    # not per perimeter -- reusing "brief" here would collide with software's own entry.
    GO_TO_MARKET: frozenset({"gtm_plan"}),
}

# The one type each perimeter's page/next-step hint leads with -- everything else is available, one
# click or one command further (a Web "More documents" disclosure, `requivo docs`'s menu). A second,
# genuinely central fact, not a caption: `web/viewmodels/sessions.py`'s `session_detail()` and
# `render/terminal.py`'s `next_command()` each used to read it off a software-only local default
# (`PRIMARY_ARTIFACT = "brief"`, and a bare `"brief"` literal) regardless of which perimeter they
# were actually serving, so a go-to-market session -- whose only artifact is never `"brief"` -- had
# no primary on either surface: the Web buried it under "More documents" and posted its generate
# form to a route that does not exist, and `requivo status` on a converged, plan-less go-to-market
# session suggested nothing at all (#609's follow-up review, Codex + a deliberate sweep after it).
# One table, read by both surfaces, so the two cannot drift the way that duplication did.
_PRIMARY_ARTIFACT: dict[str, str] = {
    SOFTWARE: "brief",
    GO_TO_MARKET: "gtm_plan",
}


@dataclass(frozen=True)
class Perimeter:
    """One installed perimeter: its id, the directory its assets live under, the artifact types it
    may produce, and which of those leads a reader to it first. `schema_path`/`elicitation_path`/
    `engine_guidance_path` are its three owned files -- a directory holding model_schema.json,
    elicitation.md and engine_guidance.md."""

    id: str
    dir: Path
    artifact_types: frozenset[str]
    # `None` for a perimeter this install has not been told a primary for -- every caller must read
    # that as *nothing leads*, never fall back to another perimeter's primary (#609's follow-up).
    primary_artifact: str | None = None

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

    @property
    def router_hint_path(self) -> Path:
        """One short paragraph naming the kind of request this perimeter fits -- read by the router
        (#601) so it can judge which installed perimeter a request belongs to without paying to send
        its whole schema or elicitation spec."""
        return self.dir / "router_hint.md"


@functools.lru_cache(maxsize=1)
def _registry() -> dict[str, Perimeter]:
    return {pid: Perimeter(id=pid, dir=PERIMETERS / pid, artifact_types=types,
                           primary_artifact=_PRIMARY_ARTIFACT.get(pid))
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


class PerimeterSummary(NamedTuple):
    """One installed perimeter, reduced to what a routing judgment (#601) needs to decide whether it
    fits a request: its id and the one paragraph naming the kind of request it is for. `unreadable`
    mirrors `core.context.CardSummary`'s own third state -- the asset is there and could not be
    read, which is not the same as a perimeter whose hint is empty."""

    id: str
    hint: str
    unreadable: bool = False


def perimeter_summaries() -> list[PerimeterSummary]:
    """Every installed perimeter as one line, for a routing judgment that must not pay to send every
    schema and elicitation spec in full. A per-perimeter read failure degrades that row and never the
    listing (invariant 15), the same discipline `core.context.card_summaries()` applies to cards."""
    out = []
    for pid in known_perimeter_ids():
        try:
            hint = get_perimeter(pid).router_hint_path.read_text(encoding="utf-8").strip()
        except OSError:
            out.append(PerimeterSummary(id=pid, hint="", unreadable=True))
            continue
        out.append(PerimeterSummary(id=pid, hint=hint))
    return out
