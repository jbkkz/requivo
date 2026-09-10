"""Where this app's own static files live -- the vendored docs assets and favicon (#504).

Framework-free (no fastapi import), the same reason `paths.py` resolves `ASSETS` from its own file
location rather than the caller's: it has to resolve identically from a source checkout and from an
installed wheel, and `pyproject.toml`'s `[tool.setuptools.package-data]` glob (`api/static/**/*`)
is what ships it in the wheel. Mirrors `web/templating.py`'s `STATIC_DIR`, minus the Jinja2
environment this package does not need.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
