"""Giving a surface logger a handler from the entry point, never from an import (#291): `lastResort`
drops INFO (the cost line) and formats nothing, and a handler installed at import or in `create_app()`
would hijack a host's own configuration (invariant 7). `test_building_the_app_configures_no_logging`,
`test_a_configured_web_log_line_carries_a_timestamp_a_level_and_the_logger_name`.
"""

from __future__ import annotations

import logging
import sys

WEB_LOGGER = "requivo.web"

# Timestamp, level, logger name, message: close to uvicorn's shape, since the lines interleave.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# The API's logger, configured by `requivo api serve` through the same function (#425); named here
# so this module stays stdlib-only.
API_LOGGER = "requivo.api"


def configure_web_logging(stream=None) -> logging.Logger:
    """Attach one formatted stderr handler to `requivo.web`; the name every caller has used since #291."""
    return configure_surface_logging(WEB_LOGGER, stream)


def configure_surface_logging(name: str, stream=None) -> logging.Logger:
    """Attach one formatted stderr handler to the named surface logger and return it. Only an entry
    point that owns the process may call this. Idempotent, and it declines rather than competing:
    nothing happens when a handler is already attached, by this function or by somebody else.
    `stream` defaults to `sys.stderr` at call time, after `configure_streams()` (invariant 16)."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
