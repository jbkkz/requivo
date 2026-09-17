"""Who configures the `requivo.web` logger, and who must not (#291)."""

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

# `2026-08-31 14:03:07,123 INFO requivo.web: …`.
_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?P<level>[A-Z]+) (?P<logger>[\w.]+): (?P<msg>.*)$")


@pytest.fixture(autouse=True)
def pristine_web_logger():
    """`requivo.web` is a process-global logger, so a test that configures it would otherwise leak into every
    test after it — including the two below that assert nothing configured it."""
    logger = logging.getLogger(WEB_LOGGER)
    before = (list(logger.handlers), logger.level, logger.propagate)
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    yield logger
    logger.handlers, logger.level, logger.propagate = before


def test_building_the_app_configures_no_logging(pristine_web_logger):
    """The half that protects somebody else's process."""
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
    """`basicConfig`/`dictConfig` are the reflex here and they are the hijack."""
    root = logging.getLogger()
    uvicorn = logging.getLogger("uvicorn.error")
    root_before = (list(root.handlers), root.level)
    uvicorn_before = (list(uvicorn.handlers), uvicorn.level, uvicorn.propagate)

    configure_web_logging(stream=io.StringIO())

    assert (list(root.handlers), root.level) == root_before, "the root logger was reconfigured"
    assert (list(uvicorn.handlers), uvicorn.level, uvicorn.propagate) == uvicorn_before


def test_a_configured_web_log_line_carries_a_timestamp_a_level_and_the_logger_name():
    """What the operator gets once the entry point has configured it, for both records this app writes."""
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
    """The other end of the split: the entry point that owns the process has to actually make the call."""
    served = []
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *args, **kwargs: served.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    # must fire: the state this starts from is the bug, and without asserting it the test would pass against a handler some earlier import had already installed.
    assert pristine_web_logger.handlers == []

    _cmd_web(argparse.Namespace(host="127.0.0.1", port=8765, no_open=True, reload=False), None)

    assert served, "uvicorn.run was never reached, so nothing about the ordering was observed"
    assert len(pristine_web_logger.handlers) == 1, (
        "`requivo web` served without giving requivo.web a handler — its 5xx and spend records go to "
        "logging.lastResort, unformatted, and the INFO ones are dropped entirely")
    assert pristine_web_logger.level == logging.INFO
    assert pristine_web_logger.propagate is False


def test_a_character_the_console_cannot_encode_is_escaped_rather_than_dropped(monkeypatch):
    """Invariant 16, on the one stream this change newly writes to (#291)."""
    # `handleError` writes to the real stderr when this is on.
    monkeypatch.setattr(logging, "raiseExceptions", False)

    def cp1252(errors: str):
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors=errors, newline="")

    safe = cp1252("backslashreplace")
    # An em dash IS representable in cp1252; U+2192 is not, and this product prints arrows.
    configure_web_logging(stream=safe).info("answers spent 1 call — cost → unknown")
    safe.flush()
    written = safe.buffer.getvalue().decode("cp1252")

    assert "—" in written, "a character cp1252 can represent must survive unchanged"
    # Derived from the same codec rather than spelled out, so this asserts *that an escape arrived* rather than pinning one particular rendering of it.
    escaped = "→".encode("cp1252", "backslashreplace").decode("ascii")
    assert escaped in written, (
        "the character cp1252 cannot represent has to arrive as a visible escape — a reader must be "
        f"able to tell a substituted character from one that was never there; expected {escaped!r}")
    assert "cost" in written and "unknown" in written, "the rest of the record has to arrive intact"

    # must fire, and it is the whole argument for the ordering.
    logging.getLogger(WEB_LOGGER).handlers = []
    strict = cp1252("strict")
    configure_web_logging(stream=strict).info("answers spent 1 call — cost → unknown")
    strict.flush()
    assert strict.buffer.getvalue() == b"", (
        "a strict console was expected to lose this record silently")


def test_configuring_twice_leaves_one_handler(pristine_web_logger):
    """`--reload`, a repeated entry, a test calling it after the app did."""
    configure_web_logging(stream=io.StringIO())
    configure_web_logging(stream=io.StringIO())
    assert len(pristine_web_logger.handlers) == 1


def test_a_logger_somebody_else_configured_is_left_alone(pristine_web_logger):
    """The third state, and the reason this is not simply `addHandler`."""
    theirs = logging.StreamHandler(io.StringIO())
    pristine_web_logger.addHandler(theirs)

    configure_web_logging(stream=io.StringIO())

    assert pristine_web_logger.handlers == [theirs], (
        "a handler somebody else installed was displaced or joined — requivo web owns the process, "
        "but it does not own a logger another caller has already spoken for")
    assert pristine_web_logger.propagate is True, "their propagation choice was overwritten too"
