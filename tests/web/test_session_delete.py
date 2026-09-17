"""Requivo Web: deleting a session (#238)."""

from __future__ import annotations

from requivo.services.sessions import SessionService
from requivo.web.security import CSRF_FIELD, csrf_token
from tests.web.conftest import HIGH_EXPLICIT, _make_session


def test_the_delete_confirm_page_renders_and_names_the_session(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/sessions/leave-approval/delete")
    assert r.status_code == 200
    assert "leave-approval" in r.text
    # The undo story the issue's own proposed change names explicitly.
    assert "export" in r.text.lower()
    # The form on this page has to carry the token like every other write in this app.
    assert csrf_token() in r.text


def test_the_delete_confirm_page_for_a_missing_session_is_404(client):
    assert client.get("/sessions/does-not-exist/delete").status_code == 404


def test_deleting_a_session_removes_it_and_redirects_home(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.post("/sessions/leave-approval/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    sessions = SessionService()
    assert sessions.exists("leave-approval") is False
    assert client.get("/").status_code == 200
    # Not a bare substring check: "leave-approval" also appears as the create-form's placeholder text.
    assert 'href="/sessions/leave-approval"' not in client.get("/").text


def test_deleting_without_the_request_token_is_refused(raw_client):
    """The delete POST is a write like any other on this app (#238)."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = raw_client.post("/sessions/leave-approval/delete")
    assert r.status_code == 403
    assert SessionService().exists("leave-approval") is True


def test_the_token_works_as_a_form_field_for_delete(raw_client):
    """The browser path: a hidden input, not a header."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = raw_client.post("/sessions/leave-approval/delete",
                        data={CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    assert SessionService().exists("leave-approval") is False


def test_deleting_a_nonexistent_session_is_404(client):
    r = client.post("/sessions/does-not-exist/delete")
    assert r.status_code == 404


def test_the_session_detail_page_links_to_the_delete_confirm_page(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = client.get("/sessions/leave-approval").text
    assert "/sessions/leave-approval/delete" in page
