"""Giving the `requivo.web` logger a handler — from the entry point, never from an import (#291).

The 500 page says "no details are shown here; check the server logs", and for a local single-user
app the operator's terminal *is* that log. `requivo.web` had no handler configured anywhere, so its
records reached `logging.lastResort`: the bare message, with no timestamp, no level and no logger
name, interleaved with uvicorn's formatted lines. A 5xx investigated an hour later cannot be tied to
a request time.

`lastResort` is also fixed at WARNING, which makes the second half of this worse than unformatted.
`web/spend.py` writes the operator's cost line at INFO, from a `finally`, and its docstring promises
"the operator sees it always, in the terminal they started the server in" — and that line was being
dropped entirely. *No handler* and *nothing was spent* printed identically, which is the absence this
project is careful about everywhere else.

Why this is a function and not a module-level `basicConfig`
-----------------------------------------------------------
`requivo.web` is importable, and `create_app()` is a factory a third party can mount inside their own
service. A handler installed at import — or inside `create_app()` — takes that host's configuration
of this logger away from them, quietly, in a process they own. `basicConfig`/`dictConfig` are worse
again: they touch the **root** logger, so every library in that process starts printing in Requivo's
format.

That is invariant 7's argument one layer out (the package talks to its caller, not to the process)
and exactly why `configure_streams()` sits behind `cli.app()` rather than at import (invariant 16).
So the rule is: this package only ever calls `getLogger`, and whatever entry point owns the process
calls this once.

`test_building_the_app_configures_no_logging` and `test_the_root_logger_and_uvicorns_are_never_touched`
are the two guards on that split; `test_a_configured_web_log_line_carries_a_timestamp_a_level_and_the_logger_name`
is the guard on what the operator actually gets.
"""

from __future__ import annotations

import logging
import sys

WEB_LOGGER = "requivo.web"

# Timestamp, level, logger name, message — the four things an operator correlating a 5xx with a
# request time needs, and nothing more. Deliberately close to uvicorn's own shape, because these
# lines land interleaved with uvicorn's in the same terminal.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# The API's logger, configured by `requivo api serve` through the same function (#425 slice 4).
# `api/usage.py` writes the operator's cost line to it at INFO from a `finally` -- the exact record
# #291 found being dropped on the web surface -- so a serve verb that did not attach a handler would
# reproduce that defect one surface along. Named here, beside `WEB_LOGGER`, rather than importing
# anything from `requivo.api`: this module is stdlib-only and stays that way.
API_LOGGER = "requivo.api"


def configure_web_logging(stream=None) -> logging.Logger:
    """Attach one formatted stderr handler to `requivo.web`, and return that logger. The web-named
    entry point of `configure_surface_logging`, kept under the name every caller and test has used
    since #291."""
    return configure_surface_logging(WEB_LOGGER, stream)


def configure_surface_logging(name: str, stream=None) -> logging.Logger:
    """Attach one formatted stderr handler to the named surface logger, and return that logger.

    **Only an entry point that owns the process may call this** — `requivo web`, `requivo api
    serve`, or a script that is the program rather than a library inside somebody else's. Importing
    this module does nothing.

    Idempotent, and it declines rather than competing. Three states, and the third is the point:

    * the logger has no handlers → one is attached, at INFO, with `propagate=False` so these lines
      are not also handed to whatever the root logger is doing;
    * this function already ran (`--reload`, a repeated entry, a test) → nothing happens, so no line
      is ever printed twice;
    * **somebody else already configured this logger** → nothing happens either. `requivo web` owns
      the process; it does not own a logger another caller has already spoken for, and taking it over
      would be the import-time hijack this module exists to avoid, arriving one function later.

    The returned logger is the same object `getLogger(name)` returns, whichever of the three
    happened — so a caller can log through it without having to know.

    `stream` is for tests. Left `None`, it resolves `sys.stderr` **at call time**, not at import: by
    then `configure_streams()` has already given that stream `errors="backslashreplace"`, so a
    character the console cannot encode is escaped rather than lost (invariant 16). A `logging`
    handler cannot kill the process on one either way — `StreamHandler.emit` routes an encoding
    failure to `handleError` — but it would drop the record silently, which is the same hole.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
