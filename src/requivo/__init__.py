"""Requivo — the requirements engine.

The model is the product; every interface — the terminal CLI, the Claude Code
plugin, Requivo Web — is a thin layer over the same core, reaching it through the
shared services. Business logic, prompts, context cards, Pydantic contracts and
model.json are the single source of truth; see CLAUDE.md for the architecture and
the invariants a change must not break.
"""

import logging

# The stdlib-documented way a *library* stays silent (#435): a `NullHandler` on the top-level
# logger does nothing itself, but it is what stops a WARNING+ record from any `requivo.*` logger
# (`requivo.services.discovery`, `.sessions`, `.artifacts`, `requivo.providers...`) reaching
# `logging.lastResort` -- Python's own fallback, which prints straight to stderr the moment no
# handler exists anywhere in a logger's propagation chain. `web/logging_setup.py` documents the
# same trap from the other side, where `requivo.web` genuinely wants that visibility and an entry
# point configures a real handler; everywhere else, invariant 7's "no handlers, no formatters, no
# phone-home, ever" means the *absence* of a handler has to survive contact with stdlib defaults,
# not just with this package's own code. An embedding application's own handler, attached to any
# of these loggers or to "requivo" itself, still sees every record -- a `NullHandler` only ever
# adds a silent sink, it never removes or shadows one somebody else attaches.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "3.2.0"
