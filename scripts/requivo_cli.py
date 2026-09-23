#!/usr/bin/env python
"""Install-free launcher: `python scripts/requivo_cli.py <command>` runs the same app() as `requivo`.

It puts src/ on sys.path first. It lives under scripts/ because a root-level `requivo.py` would shadow
the package; prefer `uv run requivo` or the installed command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from requivo.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
