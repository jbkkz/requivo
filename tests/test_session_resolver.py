"""`SessionService.resolve_default_session` — the resolver behind `run`/`status`/`impact` with no
explicit slug (#541). One test per branch: none, one, several (`updated_at` tie-break, never
directory mtime), and a degraded row among several (invariant 15: it must not hide the others).
"""
from __future__ import annotations

import json

import pytest

from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _seed(slug: str) -> None:
    SessionService().create_session(f"a request about {slug}", slug=slug)


def _set_updated_at(slug: str, iso: str) -> None:
    p = canonical_dir(slug) / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["updated_at"] = iso
    p.write_text(json.dumps(data), encoding="utf-8")


def _break_format(slug: str) -> None:
    """The same reproduction tests/test_cli_degraded_listing.py uses: a session.json written by
    a newer Requivo, which read_meta refuses -- a real degraded row, not a patched exception."""
    p = canonical_dir(slug) / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["format_version"] = SESSION_FORMAT_VERSION + 1
    p.write_text(json.dumps(data), encoding="utf-8")


def test_no_session_raises_and_names_run(workspace):
    """#541: none -> a structured error naming run."""
    with pytest.raises(SessionNotFoundError) as exc_info:
        SessionService().resolve_default_session()
    assert "run" in str(exc_info.value)


def test_exactly_one_session_is_the_default_with_nothing_to_list(workspace):
    """#541: exactly one -> that one, and no candidates to disambiguate."""
    _seed("only-one")
    resolution = SessionService().resolve_default_session()
    assert resolution.default == "only-one"
    assert resolution.candidates == []


def test_several_sessions_default_to_the_most_recently_written(workspace):
    """#541: several -> updated_at (never directory mtime) breaks the tie, and every candidate is
    still returned so the caller can list them before anything paid happens."""
    _seed("older")
    _seed("newer")
    _set_updated_at("older", "2020-01-01T00:00:00Z")
    _set_updated_at("newer", "2030-01-01T00:00:00Z")

    resolution = SessionService().resolve_default_session()

    assert resolution.default == "newer"
    assert {e.slug for e in resolution.candidates} == {"older", "newer"}


def test_a_degraded_row_among_several_does_not_hide_the_others(workspace):
    """Invariant 15, at the resolver: a session read_meta refuses is listed as a candidate rather
    than dropped or raised, and a readable sibling still wins the default."""
    _seed("healthy")
    _seed("broken")
    _break_format("broken")

    resolution = SessionService().resolve_default_session()

    assert resolution.default == "healthy"
    by_slug = {e.slug: e for e in resolution.candidates}
    assert set(by_slug) == {"healthy", "broken"}
    assert by_slug["broken"].readable is False
    assert by_slug["broken"].error
