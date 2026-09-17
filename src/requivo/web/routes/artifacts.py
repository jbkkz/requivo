"""Artifact routes: generate, view and download, always tied to a source revision through the services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.render.html import markdown_to_html
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import provider_status
from requivo.web.dependencies import get_artifacts, get_discovery, get_sessions, safe_slug
from requivo.web.spend import track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.labels import artifact_label
from requivo.web.viewmodels.sessions import session_detail
from requivo.web.viewmodels.usage import usage_view

router = APIRouter()


@router.post("/sessions/{slug}/artifacts/{artifact_type}")
def generate_artifact(
    request: Request,
    artifact_type: str,
    slug: str = Depends(safe_slug),
    discovery: DiscoveryService = Depends(get_discovery),
    sessions: SessionService = Depends(get_sessions),
):
    """Generate an artifact, save it against the session, and return the refreshed artifacts region
    for an HTMX swap; the vocabulary is the service's `GENERATABLE`."""
    if artifact_type not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"{artifact_type!r} is not a generated artifact; supported: {', '.join(GENERATABLE)}",
            details={"type": artifact_type})
    is_htmx = request.headers.get("HX-Request") == "true"
    # The paid step most likely to be repeated, so its cost is stated (#253): on the fragment for
    # htmx, stashed for the next GET on a no-JS redirect (#428), never both.
    with track_web_usage(f"web-{artifact_type}", carry_to=None if is_htmx else slug) as spend:
        discovery.generate(slug, artifact_type, surface=f"web-{artifact_type}")
        usage = usage_view(spend)
    if not is_htmx:
        # A plain form submit (#428): no fragment to swap, so a 303 to the session page.
        return RedirectResponse(url=f"/sessions/{slug}", status_code=303)
    return templates.TemplateResponse(request, "artifacts/list.html", {
        "s": session_detail(sessions, slug), "provider": provider_status(),
        "usage": usage,
    })


@router.get("/sessions/{slug}/artifacts/{artifact_type}")
def view_artifact(
    request: Request,
    artifact_type: str,
    download: bool = False,
    slug: str = Depends(safe_slug),
    artifacts: ArtifactService = Depends(get_artifacts),
):
    """View a saved artifact as a document, or download the raw Markdown byte-identical to what was
    saved (#235, `test_downloading_an_artifact_still_serves_the_bytes_that_were_saved`). The rendered
    half goes through `markdown_to_html`, which escapes before it builds any tag, since the template
    renders it with autoescape off."""
    # `artifacts.show()` refuses an unknown type first (400), so the branch below sees a real key.
    content = artifacts.show(slug, artifact_type)  # SessionNotFoundError → 404 if absent
    if download:
        # Plain indexing, no invented-filename fallback (#270, invariant 3).
        filename = ARTIFACT_FILENAMES[artifact_type]
        return PlainTextResponse(content, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})
    return templates.TemplateResponse(request, "artifacts/detail.html", {
        "slug": slug, "artifact_type": artifact_type,
        "label": artifact_label(artifact_type), "document": markdown_to_html(content),
        "provider": provider_status(),
    })
