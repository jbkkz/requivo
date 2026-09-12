"""The bundled example session -- Web's keyless activation path (#226), through the ordinary validated path
(`create_session` + `update_model`), nothing reasoned. `is_example` answers from the request text, not the slug,
which can be pushed to a derived name (invariant 11, test_the_example_is_recognised_by_what_it_asks_not_by_the_name_it_landed_under)."""

from __future__ import annotations

import json

from requivo.core.errors import RevisionConflictError
from requivo.paths import DEMO
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

# Absent a collision (see `is_example`); not `cli.DEMO_SLUG` (the browsable copy under `examples/`).
EXAMPLE_SLUG = "example-event-check-in"

# provider/model_name stay absent (invariant 6) -- test_the_revision_claims_no_provider_it_did_not_use.
EXAMPLE_SURFACE = "web-example"


def _read(name: str) -> str:
    """One bundled asset, UTF-8 (invariant 16)."""
    return (DEMO / name).read_text(encoding="utf-8")


def _unquote(markdown: str) -> str:
    """The client email out of `request.md`'s narrated blockquote wrapper; falls back to the whole text."""
    quoted = [line.lstrip()[1:].strip() for line in markdown.splitlines()
              if line.lstrip().startswith(">")]
    return "\n".join(quoted).strip() or markdown.strip()


def example_request() -> str:
    """The request the example session captures."""
    return _unquote(_read("request.md"))


def example_proposal() -> dict:
    """The bundled model, parsed fresh each call."""
    return json.loads(_read("model.json"))


def example_brief() -> str:
    """The bundled decision brief, read as a file rather than reconstructed (#429) -- test_the_bundled_brief_is_read_rather_than_restated."""
    return _read("brief.md")


def _normalised(text: str) -> str:
    """Whitespace-insensitive, so platform line endings don't cost the example its recognition."""
    return " ".join(text.split())


def is_example(request_text: str) -> bool:
    """Decided by what a session asks (module docstring), not by its slug."""
    return bool(request_text) and _normalised(request_text) == _normalised(example_request())


def seed_example(sessions: SessionService, artifacts: ArtifactService | None = None) -> str:
    """Materialise the bundled example as a real local session; returns the slug it landed under. A second click
    navigates rather than refusing (idempotent identity); the model applies only at revision 0, and a racing
    click's `RevisionConflictError` is swallowed (invariant 9) since someone else already seeded this session
    (test_a_second_click_returns_to_the_same_session_rather_than_making_another). The brief is seeded the same
    way (#429), gated on presence rather than revision 0, under the same lock, so a concurrent real generation cannot be overwritten."""
    artifacts = artifacts if artifacts is not None else ArtifactService(repo=sessions.repo)
    meta = sessions.create_session(example_request(), slug=EXAMPLE_SLUG)
    if meta.current_revision == 0:
        try:
            sessions.update_model(meta.slug, example_proposal(), expected_revision=0,
                                  provenance={"surface": EXAMPLE_SURFACE})
        except RevisionConflictError:
            pass  # a concurrent click seeded it first
    with sessions.repo.lock(meta.slug):
        if "brief" not in artifacts.list(meta.slug):
            artifacts.save(meta.slug, "brief", example_brief(), source_revision=1)
    return meta.slug
