"""What `requivo web` and `requivo api serve` do at bind time, and what every served surface answers (#425, #503)."""
from __future__ import annotations

import argparse
import builtins
import logging
import os
import sys
import types

import pytest
from _surfaces import SURFACES

from requivo.api.auth import API_TOKEN_ENV, ApiTokenRequiredError
from requivo.cli import _build_parser, _cmd_api_serve, _cmd_web, app
from requivo.core.errors import RequivoError
from requivo.providers.errors import EngineError
from requivo.web.config import provider_status

HOSTS_ENV = "REQUIVO_WEB_ALLOWED_HOSTS"


@pytest.fixture(autouse=True)
def _clean_bind_environment(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(HOSTS_ENV, raising=False)


# ── requivo web ──────────────────────────────────────────────────────────────────


def _web(host: str, monkeypatch, capsys) -> str:
    """`_cmd_web` with no uvicorn installed: the bind policy runs, the import then refuses; stderr is returned."""
    monkeypatch.delenv(HOSTS_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(RequivoError):
        _cmd_web(argparse.Namespace(host=host, port=8000, no_open=True, reload=False), None)
    return capsys.readouterr().err


def test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var(monkeypatch, capsys):
    """#217: no Host header will ever equal `0.0.0.0`; a LAN address still is one; loopback says nothing."""
    warning = _web("0.0.0.0", monkeypatch, capsys)
    assert HOSTS_ENV not in os.environ and HOSTS_ENV in warning and "0.0.0.0" in warning, warning
    _web("192.168.1.50", monkeypatch, capsys)
    assert os.environ[HOSTS_ENV] == "192.168.1.50"
    assert _web("127.0.0.1", monkeypatch, capsys) == "" and HOSTS_ENV not in os.environ


@pytest.mark.parametrize("spelling", ["::0", "0000:0000:0000:0000:0000:0000:0000:0000", "0:0:0:0:0:0:0:0"])
def test_an_equivalent_spelling_of_the_wildcard_address_is_caught_too(monkeypatch, capsys, spelling):
    """A literal check for `"::"` alone recognises one spelling of the IPv6 unspecified address."""
    warning = _web(spelling, monkeypatch, capsys)
    assert HOSTS_ENV not in os.environ, f"{spelling!r} is the same address as '::'"
    assert HOSTS_ENV in warning


def test_the_missing_web_extra_keeps_its_published_error_code(monkeypatch):
    """A missing `[web]` extra reports `provider_unavailable`, and the remedy names the extra (#135)."""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(RequivoError) as e:
        _cmd_web(argparse.Namespace(host="127.0.0.1", port=8000, no_open=True, reload=False), None)
    assert e.value.code == "provider_unavailable" == e.value.to_dict()["code"]
    assert "requivo[web]" in str(e.value)


# ── requivo api serve ───────────────────────────────────────────────────────────


class _RunCalled(Exception):
    """Raised by the uvicorn stub in place of binding."""


def _stub_uvicorn(monkeypatch) -> list:
    calls = []

    def run(app, **kwargs):
        calls.append((app, kwargs))
        raise _RunCalled()

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=run))
    return calls


def _serve(monkeypatch, host="127.0.0.1", port=8767) -> list:
    """`api serve` on the stub; the recorded `uvicorn.run` calls."""
    calls = _stub_uvicorn(monkeypatch)
    with pytest.raises(_RunCalled):
        _cmd_api_serve(argparse.Namespace(host=host, port=port), None)
    return calls


def test_the_verb_is_registered_under_api_with_the_three_flags():
    parser = _build_parser()
    ns = parser.parse_args(["api", "serve", "--host", "127.0.0.1", "--port", "9000", "--workspace", "/w"])
    assert ns.func is _cmd_api_serve and (ns.host, ns.port, ns.workspace) == ("127.0.0.1", 9000, "/w")
    defaults = parser.parse_args(["api", "serve"])
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8767)
    assert parser.parse_args(["--workspace", "/w", "api", "serve"]).workspace == "/w"   # SUPPRESS on the verb's copy
    with pytest.raises(SystemExit):
        parser.parse_args(["api"])


def test_the_serve_verb_refuses_a_non_loopback_bind_with_no_token_before_binding(monkeypatch, capsys):
    """Must fire: a clean `api_token_required`, uvicorn never handed an app, and nothing else printed."""
    calls = _stub_uvicorn(monkeypatch)
    with pytest.raises(ApiTokenRequiredError):
        _cmd_api_serve(argparse.Namespace(host="192.168.1.50", port=8767), None)
    assert calls == [] and HOSTS_ENV not in os.environ
    assert capsys.readouterr() == ("", "")


def test_the_serve_verb_binds_beyond_loopback_once_the_token_is_set(monkeypatch, capsys):
    monkeypatch.setenv(API_TOKEN_ENV, "t")
    calls = _serve(monkeypatch, host="192.168.1.50", port=9000)
    assert [c[1] for c in calls] == [{"host": "192.168.1.50", "port": 9000}]
    assert os.environ[HOSTS_ENV] == "192.168.1.50"
    err = capsys.readouterr().err
    assert "REQUIVO_API_TOKEN" in err and "NO authentication" not in err


def test_a_wildcard_bind_warns_and_is_not_allowlisted_on_this_surface_either(monkeypatch, capsys):
    """#217's rule, inherited through `_announce_bind`."""
    monkeypatch.setenv(API_TOKEN_ENV, "t")
    _serve(monkeypatch, host="0.0.0.0")
    err = capsys.readouterr().err
    assert HOSTS_ENV not in os.environ and HOSTS_ENV in err and "requivo api serve --host 0.0.0.0" in err


def test_the_loopback_default_serves_with_no_token_and_no_warning(monkeypatch, capsys):
    calls = _serve(monkeypatch)
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8767}
    out, err = capsys.readouterr()
    assert err == "" and "http://127.0.0.1:8767" in out and "/docs" in out


def test_the_api_serve_verb_configures_the_logger_before_it_serves(monkeypatch):
    """#291: the cost line goes to `requivo.api` at INFO, which `lastResort` would drop."""
    logger = logging.getLogger("requivo.api")
    monkeypatch.setattr(logger, "handlers", [])
    _serve(monkeypatch)
    assert logger.handlers and logger.level == logging.INFO and logger.propagate is False


def test_the_missing_api_extra_keeps_its_published_error_code(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(EngineError) as exc:
        _cmd_api_serve(argparse.Namespace(host="127.0.0.1", port=8767), None)
    assert exc.value.code == "provider_unavailable" and "requivo[api]" in str(exc.value)


def test_the_refusal_reaches_the_terminal_as_one_clean_line(monkeypatch, capsys):
    """End to end through `app()`: the start refusal is a `RequivoError`, so no traceback."""
    _stub_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        app(["api", "serve", "--host", "192.168.1.50"])
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "REQUIVO_API_TOKEN" in err and "Traceback" not in err
    assert issubclass(ApiTokenRequiredError, RequivoError)


# ── the security-header policy is one definition consumed by every surface (#503) ───────


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_security_headers_present(surface):
    """Every response, success and 404 alike, carries the full header set."""
    client = surface.client()
    for path, expected_status in ((surface.ok_path, 200), (surface.ok_path + "-does-not-exist", 404)):
        r = client.get(path)
        assert r.status_code == expected_status, f"{surface}: {path} answered {r.status_code}"
        assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]
        assert "Referrer-Policy" in r.headers


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_the_500_page_carries_every_header_an_ordinary_page_carries(surface):
    """An unhandled exception is answered by `ServerErrorMiddleware`, outside the app's own middleware stack."""
    app = surface.factory()

    @app.get("/_boom")
    def _boom():
        raise ValueError("a bug, not a refusal")

    client = surface.client(app)
    ordinary, unhandled = client.get(surface.ok_path), client.get("/_boom")
    assert (ordinary.status_code, unhandled.status_code) == (200, 500)
    missing = set(ordinary.headers) - set(unhandled.headers)
    assert not missing, f"{surface}: the 500 page is missing {sorted(missing)}"


@pytest.mark.parametrize("surface", SURFACES, ids=repr)
def test_a_bundled_asset_stays_cacheable(surface):
    """The other half of the `Cache-Control` boundary."""
    client = surface.client()
    for path in surface.bundled_asset_paths:
        response = client.get(path)
        assert response.status_code == 200, f"{path} was not served, so this row asserts nothing"
        assert "Content-Security-Policy" in response.headers, f"the header middleware never ran on {path}"
        assert "no-store" not in response.headers.get("Cache-Control", ""), f"{path} must stay cacheable"


# ── the Web's provider probe has three answers, not two (#339) ───────────────────────────

_BROKEN = "cannot import name 'NotGiven' from 'httpx' (a broken transitive dependency)"


@pytest.fixture
def broken_provider_import(monkeypatch):
    """Importing `requivo.providers.anthropic` fails for a reason that is not absence."""
    real = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "requivo.providers.anthropic":
            raise ImportError(_BROKEN)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)


def test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed(broken_provider_import):
    """Neither half is claimed when the probe could not look (#332); the actual cause reaches the reader."""
    status = provider_status()
    assert status.sdk_installed is None and status.key_present is None and status.available is False
    assert "pip install" not in status.reason, "the reader has already installed it"
    assert _BROKEN in status.reason and "ImportError" in status.reason


def test_a_genuinely_absent_sdk_still_says_so_and_still_names_the_install(monkeypatch):
    """The must-fire half; the third arm keeps the other two honest."""
    assert provider_status().sdk_installed is True
    monkeypatch.setattr("requivo.providers.anthropic.Anthropic", None)
    status = provider_status()
    assert status.sdk_installed is False and status.available is False and "pip install" in status.reason
