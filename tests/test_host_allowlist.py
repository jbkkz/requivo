"""The host allowlist is one definition consumed by every FastAPI surface this project ships (#508)."""

from __future__ import annotations

import pytest

from requivo.host_policy import ALLOWED_HOSTS_ENV
from tests._surfaces import SURFACES


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_loopback_host_is_accepted_by_every_surface(surface):
    """The must-fire control: a guard that refused everything would pass every other row here."""
    assert surface.client().get(surface.ok_path).status_code == 200


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_an_unrecognised_host_is_refused_by_every_surface(surface):
    """The measured defect: this is the row that was 200 on `api` and 403 on `web`."""
    response = surface.client().get(surface.ok_path, headers={"Host": "evil.example.com"})
    assert response.status_code == 403, (
        f"{surface}: an unrecognised Host answered {response.status_code} -- this is the DNS-"
        f"rebinding guard, and it applies to reads")


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_host_nobody_could_read_is_refused_by_every_surface(surface):
    """The third state (#45): *could not determine the host* is a refusal, not a skip."""
    response = surface.client().get(surface.ok_path, headers={"Host": ""})
    assert response.status_code == 403, f"{surface}: an unreadable Host answered {response.status_code}"


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_refused_host_names_its_code_rather_than_only_its_status(surface):
    """403 is the status; the code is what a caller is told to match on (#52)."""
    client = surface.client()
    unrecognised = client.get(surface.ok_path, headers={"Host": "evil.example.com"})
    unreadable = client.get(surface.ok_path, headers={"Host": ""})
    if surface.name == "api":
        assert unrecognised.json()["code"] == "host_not_allowed"
        assert unreadable.json()["code"] == "undetermined_host"
    else:
        assert "host_not_allowed" in unrecognised.text
        assert "undetermined_host" in unreadable.text


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_refusal_still_carries_the_security_headers(surface):
    """The guard is installed *inside* the header middleware on both surfaces (#340)."""
    response = surface.client().get(surface.ok_path, headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_the_operator_opt_in_governs_every_surface(surface, monkeypatch):
    """One env var, one policy. `REQUIVO_WEB_ALLOWED_HOSTS` keeps its name after #508 widened its scope."""
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "app.internal")
    client = surface.client()
    assert client.get(surface.ok_path, headers={"Host": "app.internal"}).status_code == 200
    assert client.get(surface.ok_path, headers={"Host": "other.internal"}).status_code == 403
