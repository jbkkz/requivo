"""Requivo Web: deleting a session (#238)."""

from __future__ import annotations

import pytest

from requivo.services.sessions import SessionService
from requivo.web.security import CSRF_FIELD, csrf_token
from tests.web.conftest import HIGH_EXPLICIT, _make_session


def test_the_delete_confirm_page_renders_and_names_the_session(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    assert "/sessions/leave-approval/delete" in client.get("/sessions/leave-approval").text   # linked from the detail page
    r = client.get("/sessions/leave-approval/delete")
    assert r.status_code == 200 and "leave-approval" in r.text
    assert "export" in r.text.lower()                # the undo story the issue names explicitly
    assert csrf_token() in r.text                    # the form carries the token like every other write


def test_a_missing_session_is_404_on_both_delete_routes(client):
    assert client.get("/sessions/does-not-exist/delete").status_code == 404
    assert client.post("/sessions/does-not-exist/delete").status_code == 404


@pytest.mark.parametrize("token_as", ["header", "form-field"])
def test_deleting_a_session_removes_it_and_redirects_home(client, raw_client, token_as):
    """The header is the everyday client; the hidden input is the browser path."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    if token_as == "header":
        r = client.post("/sessions/leave-approval/delete", follow_redirects=False)
    else:
        r = raw_client.post("/sessions/leave-approval/delete", data={CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert SessionService().exists("leave-approval") is False
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/sessions/leave-approval"' not in home.text   # not a bare substring: the placeholder text names it too


def test_deleting_without_the_request_token_is_refused(raw_client):
    """The delete POST is a write like any other on this app (#238)."""
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    assert raw_client.post("/sessions/leave-approval/delete").status_code == 403
    assert SessionService().exists("leave-approval") is True
