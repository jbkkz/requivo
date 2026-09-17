"""The deterministic CLI surface: no LLM, no API key. The axis is *plumbing* (this package: session,
model and artifact CRUD, install diagnostics) versus *journey* (`cli.py`); three journey verbs,
`status`, `demo` and `impact`, are no-LLM too and stay in `cli.py` (#296). `register(sub)` composes
four `register_*` functions by name, so a module that stops registering is an `ImportError`, never a
quietly shorter `--help`: `test_the_deterministic_package_still_registers_every_verb`. The call order
is the order `--help` prints, a public surface.
"""

from __future__ import annotations

from requivo.deterministic._shared import EXIT_DEGRADED, is_file_argument, print_json, read_source, read_user_text
from requivo.deterministic.artifacts import register_artifacts
from requivo.deterministic.doctor import register_doctor
from requivo.deterministic.model import register_model
from requivo.deterministic.sessions import register_sessions

# Re-exported because they are read from outside the package (#301, #360). `docs/compatibility.md`
# publishes the value 4, never the name `EXIT_DEGRADED` (#144, #145):
# `test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name`. Nothing private is re-exported:
# a second binding is what a test would patch in vain.
__all__ = ["EXIT_DEGRADED", "is_file_argument", "print_json", "read_source", "read_user_text",
           "register"]


def register(sub) -> None:
    """Attach the deterministic verb groups to the main `requivo` subparser."""
    register_doctor(sub)
    register_sessions(sub)
    register_model(sub)
    register_artifacts(sub)
