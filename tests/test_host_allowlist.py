"""The host allowlist is one definition consumed by every FastAPI surface this project ships (#508).

Carried forward from the v3.2.0 release audit, and the identical shape as #503 one policy along:
`web/app.py` installed a cross-site guard whose host check is, by that module's own account, *"the
only one that applies to reads"*; `api/app.py` installed nothing. Measured before the fix --
`GET /api/v1/sessions` with `Host: evil.example.com` answered **200** from the API and **403** from
Requivo Web -- so a page the reader visits could reach the client's verbatim request and the
understanding built from it, cross-origin, by DNS rebinding. Binding to loopback does not close
that: a rebound `evil.example.com` resolving to 127.0.0.1 is same-origin from the browser's point of
view, which is why this check is transport-level and has nothing to do with whether the listener
serves forms.

The fix is `requivo.host_policy.check_host`, one function both surfaces call; this file is the
guard, parameterised over `tests/_surfaces.py` so a *third* surface inherits the rows rather than
being a forgotten one. The acceptance criterion #508 was filed for is that sentence, not "the API
has a host check now".
"""

from __future__ import annotations

import pytest

from requivo.host_policy import ALLOWED_HOSTS_ENV
from tests._surfaces import SURFACES


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_loopback_host_is_accepted_by_every_surface(surface):
    """The must-fire control. Without this row a guard that refused *everything* would pass every
    other row in this file, and the two policies it would have broken -- the product working at all
    -- are exactly the ones a security test is most likely to be read as not caring about."""
    assert surface.client().get(surface.ok_path).status_code == 200


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_an_unrecognised_host_is_refused_by_every_surface(surface):
    """The measured defect: this is the row that was 200 on `api` and 403 on `web`.

    A **read**, deliberately -- every other arm of the web guard runs on unsafe methods only, and the
    API serves nothing but reads today, so a row driven through a POST would prove nothing about the
    surface this issue is about."""
    response = surface.client().get(surface.ok_path, headers={"Host": "evil.example.com"})
    assert response.status_code == 403, (
        f"{surface}: an unrecognised Host answered {response.status_code} -- this is the DNS-"
        f"rebinding guard, and it applies to reads")


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_host_nobody_could_read_is_refused_by_every_surface(surface):
    """The third state (#45): *could not determine the host* is a refusal, not a skip. An empty
    `Host` used to read as "no host check needed" and walk past the only check that runs on reads."""
    response = surface.client().get(surface.ok_path, headers={"Host": ""})
    assert response.status_code == 403, f"{surface}: an unreadable Host answered {response.status_code}"


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_refused_host_names_its_code_rather_than_only_its_status(surface):
    """403 is the status; the code is what a caller is told to match on (`docs/compatibility.md`:
    *assert on the code, never the message*). The two arms are distinct codes for distinct facts
    (#52), and a surface that collapsed them back into one would pass a status-only assertion.

    Read off the response where the surface serializes one (the API) and off the raised error where
    it renders HTML (the web), because the property under test is the guard's verdict, not the
    rendering -- `tests/web/test_security_parser.py` owns the web payload shapes."""
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
    """The guard is installed *inside* the header middleware on both surfaces, so a request it turns
    away leaves with the same CSP and nosniff a served page carries. Installing it outermost would
    have made the one response class this module produces the one served with no policy at all --
    which is the defect `web/app.py`'s `_unexpected` handler already carries a comment about (#340,
    #462), one middleware along."""
    response = surface.client().get(surface.ok_path, headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_the_operator_opt_in_governs_every_surface(surface, monkeypatch):
    """One env var, one policy. `REQUIVO_WEB_ALLOWED_HOSTS` keeps its name after #508 widened its
    scope -- it is documented in `docs/web.md`, promised in `docs/compatibility.md`, and printed by
    `cli.py`'s wildcard-bind warning, so renaming it would be a user-facing break for one word."""
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "app.internal")
    client = surface.client()
    assert client.get(surface.ok_path, headers={"Host": "app.internal"}).status_code == 200
    assert client.get(surface.ok_path, headers={"Host": "other.internal"}).status_code == 403
