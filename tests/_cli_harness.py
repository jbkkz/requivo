"""The shared harness for the deterministic-CLI test modules (#141): the `_fakes` names under their older spellings."""
from __future__ import annotations

import re

from _fakes import forge_meta as _forge_meta  # noqa: F401
from _fakes import full_model as _full_model  # noqa: F401
from _fakes import run_cli as _run  # noqa: F401
from _fakes import run_cli_json as _run_json  # noqa: F401
from _fakes import run_cli_stdin as _run_stdin  # noqa: F401
from _fakes import slot as _slot  # noqa: F401

_SESSIONS_ROW = re.compile(r"^  [✅❌🟡] sessions\b")
