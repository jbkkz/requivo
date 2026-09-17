"""Artifact routes (#425): the freshness listing, one artifact, generation through
`DiscoveryService.generate`, and the external-reasoner save through `ArtifactService.save`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from requivo.api.dependencies import get_artifacts, get_discovery, safe_slug
from requivo.api.schemas import ArtifactSaveRequest
from requivo.api.usage import track_api_usage, usage_view
from requivo.services.artifacts import ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import GENERATABLE, DiscoveryService

router = APIRouter()


@router.get("/sessions/{slug}/artifacts")
def list_artifacts(slug: str = Depends(safe_slug),
                    artifacts: ArtifactService = Depends(get_artifacts)) -> dict:
    """Every recorded artifact and its freshness: the explicit stale flag (invariant 1)."""
    return artifacts.list(slug)


def _wants_markdown(request: Request) -> bool:
    return "text/markdown" in request.headers.get("accept", "")


@router.get("/sessions/{slug}/artifacts/{artifact_type}")
def show_artifact(request: Request, artifact_type: str, slug: str = Depends(safe_slug),
                   artifacts: ArtifactService = Depends(get_artifacts)):
    """One artifact: `Accept: text/markdown` returns the saved bytes; anything else the JSON envelope
    `{type, filename, source_revision, updated_at, stale, content}`, read as one snapshot (invariant 12)."""
    content, row = artifacts.show_with_status(slug, artifact_type)
    if _wants_markdown(request):
        return PlainTextResponse(content, media_type="text/markdown")
    return {"type": artifact_type, "filename": row.get("filename"),
            "source_revision": row.get("revision"), "updated_at": row.get("updated_at"),
            "stale": row.get("stale"), "content": content}


@router.post("/sessions/{slug}/artifacts/{artifact_type}")
def generate_artifact(artifact_type: str, slug: str = Depends(safe_slug),
                      discovery: DiscoveryService = Depends(get_discovery)) -> dict:
    """Generate an artifact through the provider and save it; the vocabulary is `GENERATABLE`. Not
    idempotent: each call pays and overwrites. The response carries the status, the contract's dump and the spend."""
    if artifact_type not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"{artifact_type!r} is not a generated artifact; supported: {', '.join(GENERATABLE)}",
            details={"type": artifact_type})
    with track_api_usage(f"api-{artifact_type}") as ledger:
        result = discovery.generate(slug, artifact_type, surface=f"api-{artifact_type}")
        usage = usage_view(ledger)
    # A runtime `artifact_type` resolves the `str` overload, `Generated[object]` (`decision: typed-generation-seam`);
    # every contract is a pydantic model, so narrow by the fact rather than a cast.
    artifact = result.artifact
    if not isinstance(artifact, BaseModel):  # pragma: no cover - every registered contract is one
        raise TypeError(f"generated {artifact_type!r} is not a pydantic contract: {type(artifact)!r}")
    return {"type": artifact_type, "status": result.status.model_dump(),
            "artifact": artifact.model_dump(), "usage": usage}


@router.put("/sessions/{slug}/artifacts/{artifact_type}")
def save_artifact(body: ArtifactSaveRequest, artifact_type: str, slug: str = Depends(safe_slug),
                  artifacts: ArtifactService = Depends(get_artifacts)) -> dict:
    """The external-reasoner save: persist content produced elsewhere against its source revision,
    no provider call. Omitting `source_revision` is the service's own 400."""
    status = artifacts.save(slug, artifact_type, body.content, source_revision=body.source_revision)
    return status.model_dump()
