"""Artifact read routes -- the freshness listing and one artifact's content (#425, slice 1).
Generation (`POST`) is slice 2."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from requivo.api.dependencies import get_artifacts, safe_slug
from requivo.services.artifacts import ArtifactService

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
