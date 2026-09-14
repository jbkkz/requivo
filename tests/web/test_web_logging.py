"""Who configures the `requivo.web` logger, and who must not (#291).

The 500 page tells the reader "no details are shown here; check the server logs", and the operator's
terminal is that log. With no handler configured anywhere, the app's own records reach
`logging.lastResort` — the bare message, no timestamp, no level, no logger name, interleaved with
uvicorn's formatted lines — so a 5xx investigated an hour later cannot be tied to a request time.
And `lastResort` is fixed at WARNING, so the `INFO` line `web/spend.py` writes from a `finally` to
give the operator what a paid call cost is not merely unformatted: it never appears at all, on a
promise that module's own docstring makes in as many words ("the operator sees it always, in the
terminal they started the server in").

**A library must not configure logging at import, and `requivo.web` is importable.** A third party
mounting this FastAPI app inside their own service imports this package and calls `create_app()`; a
handler attached there — with `propagate=False`, which is what a handler on a named logger needs to
be useful — takes their own configuration of `requivo.web` away from them, quietly. That is the same
reasoning that put `configure_streams()` behind `cli.app()` rather than at import (invariant 16), and
invariant 7's one layer out: the package talks to its caller, not to the process.

So the split is: this package only ever calls `getLogger`, and the **entry point that owns the
process** calls `configure_web_logging()`. These tests hold both halves — that calling it does what
the operator needs, and that nothing reaches it by merely importing or building the app.

Offline, isolated workspace per test; the fixtures live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import types

import pytest

from requivo.cli import _cmd_web
from requivo.web.app import create_app
from requivo.web.logging_setup import WEB_LOGGER, configure_web_logging

# `2026-08-31 14:03:07,123 INFO requivo.web: …` — the three things an operator correlating a 5xx with
# a request time actually needs, asserted as a shape rather than as an exact format string, so the
# format may be tuned without this test pinning its spelling.
_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?P<level>[A-Z]+) (?P<logger>[\w.]+): (?P<msg>.*)$")


@pytest.fixture(autouse=True)
def pristine_web_logger():
    """`requivo.web` is a process-global logger, so a test that configures it would otherwise leak
    into every test after it — including the two below that assert nothing configured it."""
    logger = logging.getLogger(WEB_LOGGER)
    before = (list(logger.handlers), logger.level, logger.propagate)
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    yield logger
    logger.handlers, logger.level, logger.propagate = before


def test_building_the_app_configures_no_logging(pristine_web_logger):
    """The half that protects somebody else's process: importing this package and building the
    app the way an ASGI host does must leave the logger exactly as found -- no handler, no level,
    still propagating to whatever the host configured. Must-fire control at the end: the same call
    has to demonstrably change something when asked to, or this passes against a
    `configure_web_logging` that does nothing at all."""
    create_app()

    assert pristine_web_logger.handlers == [], (
        "building the app attached a handler to requivo.web — a host that mounted this app would "
        "lose its own configuration of that logger")
    assert pristine_web_logger.propagate is True
    assert pristine_web_logger.level == logging.NOTSET

    # must fire
    configure_web_logging(stream=io.StringIO())
    assert pristine_web_logger.handlers, (
        "configure_web_logging attached nothing, so the assertions above were about a function that "
        "does nothing rather than about the app leaving the logger alone")


def test_the_root_logger_and_uvicorns_are_never_touched(pristine_web_logger):
    """`basicConfig`/`dictConfig` are the reflex here and they are the hijack: they install a handler
    on the **root** logger, so every library in the host process starts printing in Requivo's format.
    Only the one named logger this package writes to may be configured."""
    root = logging.getLogger()
    uvicorn = logging.getLogger("uvicorn.error")
    root_before = (list(root.handlers), root.level)
    uvicorn_before = (list(uvicorn.handlers), uvicorn.level, uvicorn.propagate)

    configure_web_logging(stream=io.StringIO())

    assert (list(root.handlers), root.level) == root_before, "the root logger was reconfigured"
    assert (list(uvicorn.handlers), uvicorn.level, uvicorn.propagate) == uvicorn_before


def test_a_configured_web_log_line_carries_a_timestamp_a_level_and_the_logger_name():
    """What the operator gets once the entry point has configured it, for both records this app
    writes. The `INFO` row is not decoration: `logging.lastResort` is fixed at WARNING, so the
    spend line `web/spend.py` writes is today dropped entirely rather than merely printed plainly
    -- the case where "no handler" and "nothing happened" are indistinguishable, which is the
    absence this whole fix is about."""
    stream = io.StringIO()
    logger = configure_web_logging(stream=stream)

    logger.info("answers spent 1,234 tokens over 1 call(s)")
    logger.error("internal_error serving POST /sessions/x/answers: boom")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2, (
        f"expected the INFO and the ERROR record, got {len(lines)} line(s): {lines!r}")

    parsed = [_LINE.match(line) for line in lines]
    for line, m in zip(lines, parsed):
        assert m is not None, (
            f"a log line carried no timestamp, level and logger name: {line!r} — an operator "
            f"correlating a 5xx with a request time has nothing to go on")
    assert [m.group("level") for m in parsed] == ["INFO", "ERROR"]
    assert {m.group("logger") for m in parsed} == {WEB_LOGGER}
    assert "1,234 tokens" in parsed[0].group("msg")


def test_the_web_verb_configures_the_logger_before_it_serves(pristine_web_logger, monkeypatch):
    """The other end of the split: the entry point that owns the process has to actually make
    the call, or every guard above is about a function nobody runs. `uvicorn` is stubbed with a
    module recording its `run` rather than binding a port. Asserting `run` was reached makes the
    ordering assertable: a handler configured after the server started would pass a bare
    "is there a handler" check and still leave every startup record unformatted."""
    served = []
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *args, **kwargs: served.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    # must fire: the state this starts from is the bug, and without asserting it the test would pass
    # against a handler some earlier import had already installed.
    assert pristine_web_logger.handlers == []

    _cmd_web(argparse.Namespace(host="127.0.0.1", port=8765, no_open=True, reload=False), None)

    assert served, "uvicorn.run was never reached, so nothing about the ordering was observed"
    assert len(pristine_web_logger.handlers) == 1, (
        "`requivo web` served without giving requivo.web a handler — its 5xx and spend records go to "
        "logging.lastResort, unformatted, and the INFO ones are dropped entirely")
    assert pristine_web_logger.level == logging.INFO
    assert pristine_web_logger.propagate is False


def test_a_character_the_console_cannot_encode_is_escaped_rather_than_dropped(monkeypatch):
    """Invariant 16, on the one stream this change newly writes to. Until #291 a `requivo.web`
    record reached no handler at all; on a cp1252 console `StreamHandler.emit` routes an encoding
    failure to `handleError`, silently losing the record -- the same hole `streams.py` rejects
    `errors="replace"` for. Safety comes from ordering: `cli.app()` configures streams before
    `_cmd_web`, driven against a real cp1252 encoder as `tests/test_encoding.py` does."""
    # `handleError` writes to the real stderr when this is on; the strict half below deliberately
    # triggers it. monkeypatch restores it even if an assertion fails.
    monkeypatch.setattr(logging, "raiseExceptions", False)

    def cp1252(errors: str):
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors=errors, newline="")

    safe = cp1252("backslashreplace")
    # An em dash IS representable in cp1252; U+2192 is not, and this product prints arrows — a
    # provider string reaching a message could carry either.
    configure_web_logging(stream=safe).info("answers spent 1 call — cost → unknown")
    safe.flush()
    written = safe.buffer.getvalue().decode("cp1252")

    assert "—" in written, "a character cp1252 can represent must survive unchanged"
    # Derived from the same codec rather than spelled out, so this asserts *that an escape arrived*
    # rather than pinning one particular rendering of it.
    escaped = "→".encode("cp1252", "backslashreplace").decode("ascii")
    assert escaped in written, (
        "the character cp1252 cannot represent has to arrive as a visible escape — a reader must be "
        f"able to tell a substituted character from one that was never there; expected {escaped!r}")
    assert "cost" in written and "unknown" in written, "the rest of the record has to arrive intact"

    # must fire, and it is the whole argument for the ordering: on a strict stream the same record is
    # dropped entirely, and the process survives, so nothing announces the loss. If this ever stops
    # being true the assertions above have stopped being about the stream configuration.
    logging.getLogger(WEB_LOGGER).handlers = []
    strict = cp1252("strict")
    configure_web_logging(stream=strict).info("answers spent 1 call — cost → unknown")
    strict.flush()
    assert strict.buffer.getvalue() == b"", (
        "a strict console was expected to lose this record silently")


def test_configuring_twice_leaves_one_handler(pristine_web_logger):
    """`--reload`, a repeated entry, a test calling it after the app did: none of them may double
    every line. The same argument `_atomic_write`'s retry makes — the operation is idempotent, so
    saying so is cheaper than requiring every caller to remember."""
    configure_web_logging(stream=io.StringIO())
    configure_web_logging(stream=io.StringIO())
    assert len(pristine_web_logger.handlers) == 1


def test_a_logger_somebody_else_configured_is_left_alone(pristine_web_logger):
    """The third state, and the reason this is not simply `addHandler`: a host that has already
    configured `requivo.web` has said what it wants, and taking over would be the import-time
    hijack this module exists to avoid. Asserted on the logger, not the return value --
    `configure_web_logging` returns the same `getLogger(WEB_LOGGER)` object in all three states by
    design, discarded by its one caller, so there is nothing meaningful in the return to read."""
    theirs = logging.StreamHandler(io.StringIO())
    pristine_web_logger.addHandler(theirs)

    configure_web_logging(stream=io.StringIO())

    assert pristine_web_logger.handlers == [theirs], (
        "a handler somebody else installed was displaced or joined — requivo web owns the process, "
        "but it does not own a logger another caller has already spoken for")
    assert pristine_web_logger.propagate is True, "their propagation choice was overwritten too"
