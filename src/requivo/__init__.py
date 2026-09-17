"""Requivo, the requirements engine: the model is the product, and every interface is a thin layer
over the same services. See CLAUDE.md for the architecture and the invariants.
"""

import logging

# A library stays silent with a `NullHandler` (#435, invariant 7); `web/logging_setup.py` is the one
# entry point that configures a real one. `test_default_run_leaves_the_conflict_refused_warning_off_every_stream`.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "3.3.0"
