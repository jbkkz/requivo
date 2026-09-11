"""Artifact routes -- the freshness listing, one artifact's content, generation, and the
external-reasoner save (#425, slices 1 and 2).

Generation goes through `DiscoveryService.generate`, which calls the provider and saves via
`ArtifactService` with the source revision it was actually read at -- so staleness is tracked
identically to every other surface. The save route is the wire path for content produced elsewhere
(the Claude Code shape, given an HTTP body instead of the filesystem): `ArtifactService.save`
directly, no provider call, no `usage` object."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from requivo.api.dependencies import get_artifacts, get_discovery, safe_slug
from requivo.api.schemas import ArtifactSaveRequest
from requivo.api.usage import usage_view
from requivo.services.artifacts import ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.usage import track_usage

router = APIRouter()


@router.get("/sessions/{slug}/artifacts")
def list_artifacts(slug: str = Depends(safe_slug),
                    artifacts: ArtifactService = Depends(get_artifacts)) -> dict:
    """Every recorded artifact and its freshness relative to the current model revision -- the
    explicit stale flag, never revision drift (invariant 1). `ArtifactService.list`."""
    return artifacts.list(slug)


def _wants_markdown(request: Request) -> bool:
    return "text/markdown" in request.headers.get("accept", "")


@router.get("/sessions/{slug}/artifacts/{artifact_type}")
def show_artifact(request: Request, artifact_type: str, slug: str = Depends(safe_slug),
                   artifacts: ArtifactService = Depends(get_artifacts)):
    """One artifact: `Accept: text/markdown` returns the saved document byte-for-byte (the same
    contract Requivo Web's download route keeps); anything else returns the JSON envelope
    `{type, filename, source_revision, updated_at, stale, content}`.

    Backed by `ArtifactService.show_with_status` -- content and freshness read as one coherent
    snapshot rather than two separate calls, so a regeneration landing in between cannot report
    content from one revision beside metadata describing another (invariant 12)."""
    content, row = artifacts.show_with_status(slug, artifact_type)
    if _wants_markdown(request):
        return PlainTextResponse(content, media_type="text/markdown")
    return {"type": artifact_type, "filename": row.get("filename"),
            "source_revision": row.get("revision"), "updated_at": row.get("updated_at"),
            "stale": row.get("stale"), "content": content}


@router.post("/sessions/{slug}/artifacts/{artifact_type}")
def generate_artifact(artifact_type: str, slug: str = Depends(safe_slug),
                      discovery: DiscoveryService = Depends(get_discovery)) -> dict:
    """Generate an artifact through the provider and save it against the session
    (`DiscoveryService.generate`). The vocabulary is the service's `GENERATABLE`, not a list kept
    here, so this surface offers exactly what the shared orchestration can produce.

    Not idempotent -- each call pays and overwrites, documented as such (§3 of the decision record).
    Paid, so scoped in a `track_usage()` ledger; the response carries the saved artifact's
    provenance (`ArtifactStatus`), the typed contract's own dump, and what this call spent."""
    if artifact_type not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"{artifact_type!r} is not a generated artifact; supported: {', '.join(GENERATABLE)}",
            details={"type": artifact_type})
    with track_usage() as ledger:
        result = discovery.generate(slug, artifact_type, surface=f"api-{artifact_type}")
        usage = usage_view(ledger)
    return {"type": artifact_type, "status": result.status.model_dump(),
            "artifact": result.artifact.model_dump(), "usage": usage}


@router.put("/sessions/{slug}/artifacts/{artifact_type}")
def save_artifact(body: ArtifactSaveRequest, artifact_type: str, slug: str = Depends(safe_slug),
                  artifacts: ArtifactService = Depends(get_artifacts)) -> dict:
    """The external-reasoner save -- persist content produced elsewhere and tie it to the model
    revision it was reasoned from (`ArtifactService.save`). No provider call, no `usage` object.

    `source_revision` keeps the service's own optional default in `ArtifactSaveRequest`: omitting it
    is refused as `unstated_source_revision` (400) by `ArtifactService.save` itself, unchanged --
    this route adds no requiredness of its own."""
    status = artifacts.save(slug, artifact_type, body.content, source_revision=body.source_revision)
    return status.model_dump()
