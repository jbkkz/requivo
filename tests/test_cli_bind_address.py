"""What `requivo web` and `requivo api serve` say and do when asked to bind beyond loopback.

Split out of `test_cli.py` -- the `_cmd_web` bind-warning tests sat beside the rest of the journey-verb
surface, one subject among many others there. Split out of `test_cli_api_serve.py` too, folded in
whole rather than kept as a sibling file: both verbs answer through the identical shared helper,
`_announce_bind` (`cli_support.py`, #425 slice 4) -- one host-allowlist policy, one wildcard-detection
rule, one set of words about what binding wide exposes -- so the two verbs' tests living apart from
each other hid how parallel their guards already are. #133's lesson still applies at the seam between
them: the *verb* is the path an operator actually takes, not only the shared helper, so both entry
points keep their own tests rather than collapsing onto one call through `_announce_bind` directly.

`api serve` alone adds the token-gate tests (`ApiTokenRequiredError`) -- `web` has no such gate, so
those stay specific to the one verb that has it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import pytest

from requivo.api.auth import API_TOKEN_ENV, ApiTokenRequiredError
from requivo.cli import _build_parser, _cmd_api_serve, _cmd_web, app
from requivo.core.errors import RequivoError
from requivo.providers.errors import EngineError

# ── requivo web ──────────────────────────────────────────────────────────────────

def test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var(monkeypatch, capsys):
    """#217: `--host 0.0.0.0` used to auto-allowlist the literal string `"0.0.0.0"`, which no browser's
    `Host` header is ever going to equal -- so the flag appeared to bind wide and then 403'd every LAN
    request with no clue why. A wildcard bind is not a valid `Host` value and must not be
    auto-allowlisted; the warning has to name what to do instead, with a copy-pasteable example."""
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host="0.0.0.0", port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError):
        _cmd_web(args, None)

    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ, (
        "the literal wildcard address must not be allowlisted -- no Host header will ever equal it")
    warning = capsys.readouterr().err
    assert "REQUIVO_WEB_ALLOWED_HOSTS" in warning, "the warning has to name the env var, not just hint at it"
    assert "0.0.0.0" in warning              # the copy-pasteable example names the flag that was passed

    # must-fire control, same fixture: a real (non-wildcard) LAN address IS a legitimate Host value, so
    # it keeps being auto-allowlisted exactly as before -- this is not a tightening of that path.
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    args_lan = argparse.Namespace(host="192.168.1.50", port=8000, no_open=True, reload=False)
    with pytest.raises(RequivoError):
        _cmd_web(args_lan, None)
    assert os.environ["REQUIVO_WEB_ALLOWED_HOSTS"] == "192.168.1.50"
    capsys.readouterr()  # drain this leg's own warning before the next assertion reads stderr

    # and the loopback default is untouched: no warning, no env var written.
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    args_default = argparse.Namespace(host="127.0.0.1", port=8000, no_open=True, reload=False)
    with pytest.raises(RequivoError):
        _cmd_web(args_default, None)
    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("spelling", ["::0", "0000:0000:0000:0000:0000:0000:0000:0000", "0:0:0:0:0:0:0:0"])
def test_an_equivalent_spelling_of_the_wildcard_address_is_caught_too(monkeypatch, capsys, spelling):
    """A string-literal check for `"::"` alone recognises exactly one spelling of the IPv6 unspecified
    address and none of its equivalents -- `::0`, the fully-expanded all-zeros form, and so on all
    mean the identical bind address (`ipaddress.ip_address(...).is_unspecified` agrees). `--host ::0`
    would otherwise reproduce #217's exact symptom under a spelling the literal-string guard does not
    recognise."""
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host=spelling, port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError):
        _cmd_web(args, None)

    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ, (
        f"{spelling!r} is the same address as '::' and must not be allowlisted verbatim either")
    warning = capsys.readouterr().err
    assert "REQUIVO_WEB_ALLOWED_HOSTS" in warning


def test_the_missing_web_extra_keeps_its_published_error_code(monkeypatch):
    """A missing `[web]` extra reports `provider_unavailable`, and that is a decision, not an oversight
    (#135)."""
    # `None` in sys.modules is what makes `import uvicorn` raise without uninstalling anything —
    # the extra really is installed in the dev environment this runs in.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host="127.0.0.1", port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError) as e:
        _cmd_web(args, None)

    assert e.value.code == "provider_unavailable"
    assert "requivo[web]" in str(e.value), "the remedy is the message's whole job"
    assert e.value.to_dict()["code"] == "provider_unavailable", "the envelope is what a caller reads"


# ── requivo api serve ───────────────────────────────────────────────────────────

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
