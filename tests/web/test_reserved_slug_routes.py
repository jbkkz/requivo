"""#396: a session already on disk under a Windows reserved device name is reachable through the Web."""

from __future__ import annotations

import json

import pytest

from requivo.core import persistence as store
from tests.web.conftest import engine_reply


def _reserved_session_on_disk(slug: str = "con") -> None:
    """A session directory at a reserved name, written directly."""
    d = store.session_root() / slug
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": slug, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                    "'con' already on disk, which Windows refuses to create at the OS level "
                    "independent of anything Requivo does -- so a session at a reserved slug is a "
                    "state only a platform that never enforced the restriction can reach. "
                    "REASONED, NOT OBSERVED: the same platform limit the sibling #372 fixtures "
                    "carry.")
def test_a_reserved_slug_already_on_disk_is_reachable_through_the_web_read_routes(client,
                                                                                  with_provider):
    """The read/create asymmetry, both halves in one fixture (#396)."""
    _reserved_session_on_disk()

    # A read route -- the half nobody had to be told was affected.
    assert client.get("/sessions/con").status_code == 200

    # A write route reaches past the slug guard too.
    with_provider(engine_reply(converged=True))
    r = client.post("/sessions/con/discover", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/sessions/con"
    assert store.read_meta("con").current_revision == 1

    # The second read route, asserted only now there is a model to export.
    assert client.get("/sessions/con/export").status_code == 200

    # Must-not-fire control, same fixture: `POST /sessions` is the one route that can bring a slug into existence, it takes its name from a form field rather than the path (so it never reaches `safe_slug`), and it stays strict (#372).
    created = client.post("/sessions", data={"request_text": "A leave approval system.",
                                             "slug": "nul", "provider": "create_only"})
    assert created.status_code == 400
    assert "reserved Windows device name" in created.text
    assert not (store.session_root() / "nul").exists()
