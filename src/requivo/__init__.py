"""Requivo — the requirements engine.

The model is the product; every interface — the terminal CLI, the Claude Code
plugin, Requivo Web — is a thin layer over the same core, reaching it through the
shared services. Business logic, prompts, context cards, Pydantic contracts and
model.json are the single source of truth; see CLAUDE.md for the architecture and
the invariants a change must not break.
"""

import logging

# A library stays silent by installing a `NullHandler` (#435): without it a WARNING+ record from any
# `requivo.*` logger reaches `logging.lastResort` and prints to stderr, which is invariant 7's "no
# handlers, no phone-home, ever" broken by stdlib defaults rather than by this package's own code.
# An embedding application's handler still sees every record; `web/logging_setup.py` is the one
# entry point that wants that visibility and configures a real one. Pinned by
# `test_default_run_leaves_the_conflict_refused_warning_off_every_stream`, beside its must-fire control.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "3.2.0"
