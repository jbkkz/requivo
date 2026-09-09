"""Requivo API -- the local HTTP facade over the same services as the CLI and Requivo Web (#425).

**Experimental.** `create_api()` lives behind the optional `[api]` extra
(`pip install 'requivo[api]'`); nothing here is a promised contract yet -- see
`docs/decisions/0004-the-http-api-facade.md` for the design and the three preconditions that gate
the freeze, and `docs/compatibility.md` for the one line that says so today.

This package imports nothing from `fastapi` at this level, so `import requivo.api` succeeds with
the base install. Only calling `create_api()` (in `requivo.api.app`) needs the extra, and it fails
with a clean, actionable message rather than a bare traceback when it is missing -- the same shape
`requivo.web`'s `create_app()` is reached through from `cli.py`'s `_cmd_web`, moved one layer in
because slice 1 of #425 ships no CLI verb yet to own that lazy import.
"""

from __future__ import annotations
