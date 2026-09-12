"""`requivo api serve` (#425 slice 4): the verb that binds the `[api]` extra, driven at `_cmd_api_serve`
the same way `tests/test_cli.py` drives `_cmd_web` -- `uvicorn` stubbed out of `sys.modules` so the
function raises before it would ever bind a port, and every decision under test made before that.

The refuse-to-start rule is pinned here at the verb as well as at the factory (`tests/api/`): the
factory is the implementation, the verb is the path an operator actually takes, and #133's lesson is
that a gate the documenting path takes and the used path does not is not enforced.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import pytest

from requivo.api.auth import API_TOKEN_ENV, ApiTokenRequiredError
from requivo.cli import _build_parser, _cmd_api_serve, app
from requivo.core.errors import RequivoError
from requivo.providers.errors import EngineError


@pytest.fixture(autouse=True)
def _clean_bind_environment(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)


def _args(host="127.0.0.1", port=8767):
    return argparse.Namespace(host=host, port=port)


class _RunCalled(Exception):
    """Raised by the uvicorn stub in place of binding, carrying what it was handed."""


def _stub_uvicorn(monkeypatch):
    """A `uvicorn` whose `run` records its arguments and raises instead of serving."""
    import types

    calls = []

    def run(app, **kwargs):
        calls.append((app, kwargs))
        raise _RunCalled()

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=run))
    return calls


def test_the_verb_is_registered_under_api_with_the_three_flags():
    parser = _build_parser()
    ns = parser.parse_args(["api", "serve", "--host", "127.0.0.1", "--port", "9000",
                            "--workspace", "/w"])
    assert ns.func is _cmd_api_serve
    assert (ns.host, ns.port, ns.workspace) == ("127.0.0.1", 9000, "/w")
    defaults = parser.parse_args(["api", "serve"])
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8767)
    # SUPPRESS on the verb's own copy: an absent `api serve --workspace` never clobbers the global one
    assert parser.parse_args(["--workspace", "/w", "api", "serve"]).workspace == "/w"
    with pytest.raises(SystemExit):
        parser.parse_args(["api"])                  # the group needs a verb


def test_the_serve_verb_refuses_a_non_loopback_bind_with_no_token_before_binding(monkeypatch, capsys):
    """Must-fire, at the verb: `--host 192.168.1.50` with no `REQUIVO_API_TOKEN` is a clean
    `api_token_required`, uvicorn is never handed an app, and the refusal is the only output --
    no bind warning, no banner, no allowlist written for a bind that never happened."""
    calls = _stub_uvicorn(monkeypatch)
    with pytest.raises(ApiTokenRequiredError):
        _cmd_api_serve(_args(host="192.168.1.50"), None)
    assert calls == []
    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_the_serve_verb_binds_beyond_loopback_once_the_token_is_set(monkeypatch, capsys):
    """The must-not-fire control: same bind, token set -> uvicorn is handed the app on that host,
    the bind warning names this surface's real exposure (a token, not "NO authentication") and the
    address is auto-allowlisted exactly as `requivo web` does."""
    monkeypatch.setenv(API_TOKEN_ENV, "t")
    calls = _stub_uvicorn(monkeypatch)
    with pytest.raises(_RunCalled):
        _cmd_api_serve(_args(host="192.168.1.50", port=9000), None)
    assert len(calls) == 1
    assert calls[0][1] == {"host": "192.168.1.50", "port": 9000}
    assert os.environ["REQUIVO_WEB_ALLOWED_HOSTS"] == "192.168.1.50"
    err = capsys.readouterr().err
    assert "REQUIVO_API_TOKEN" in err
    assert "NO authentication" not in err


def test_a_wildcard_bind_warns_and_is_not_allowlisted_on_this_surface_either(monkeypatch, capsys):
    """#217's rule, inherited through `_announce_bind`: the literal wildcard is never a `Host`
    value, and the warning's copy-pasteable example names this verb, not `web`."""
    monkeypatch.setenv(API_TOKEN_ENV, "t")
    _stub_uvicorn(monkeypatch)
    with pytest.raises(_RunCalled):
        _cmd_api_serve(_args(host="0.0.0.0"), None)
    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ
    err = capsys.readouterr().err
    assert "REQUIVO_WEB_ALLOWED_HOSTS" in err
    assert "requivo api serve --host 0.0.0.0" in err


def test_the_loopback_default_serves_with_no_token_and_no_warning(monkeypatch, capsys):
    """§5 step 1 at the verb: the default bind needs nothing set and says nothing on stderr; the
    banner on stdout names the URL and the docs."""
    calls = _stub_uvicorn(monkeypatch)
    with pytest.raises(_RunCalled):
        _cmd_api_serve(_args(), None)
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8767}
    out, err = capsys.readouterr()
    assert err == ""
    assert "http://127.0.0.1:8767" in out and "/docs" in out


def test_the_api_serve_verb_configures_the_logger_before_it_serves(monkeypatch):
    """#291 one surface along: `api/usage.py` writes the cost line to `requivo.api` at INFO from a
    `finally`, which `lastResort` (WARNING, unformatted) would drop. The verb attaches a handler
    before handing off to uvicorn; the must-fire half is that the logger had none before."""
    logger = logging.getLogger("requivo.api")
    monkeypatch.setattr(logger, "handlers", [])
    _stub_uvicorn(monkeypatch)
    assert not logger.handlers
    with pytest.raises(_RunCalled):
        _cmd_api_serve(_args(), None)
    assert logger.handlers, "no handler attached: the spend line would be dropped"
    assert logger.level == logging.INFO
    assert logger.propagate is False


def test_the_missing_api_extra_keeps_its_published_error_code(monkeypatch):
    """Mirrors `test_the_missing_web_extra_keeps_its_published_error_code`: a missing `uvicorn` is
    `provider_unavailable`, the vocabulary's existing answer for an absent optional install, with the
    install hint -- never a bare `ImportError`."""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(EngineError) as exc:
        _cmd_api_serve(_args(), None)
    assert exc.value.code == "provider_unavailable"
    assert "requivo[api]" in str(exc.value)


def test_the_refusal_reaches_the_terminal_as_one_clean_line(monkeypatch, capsys):
    """End to end through `app()`: the start refusal is a `RequivoError`, so the CLI prints it and
    exits 1 rather than tracing back -- the same arm every other clean failure takes."""
    _stub_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        app(["api", "serve", "--host", "192.168.1.50"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "REQUIVO_API_TOKEN" in err
    assert "Traceback" not in err
    # must-fire control for the assertion above: a RequivoError is what the arm catches
    assert issubclass(ApiTokenRequiredError, RequivoError)
