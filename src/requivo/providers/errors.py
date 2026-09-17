"""The failure vocabulary of the provider seam: provider-neutral, SDK-free, importing only
`core.errors`, so `web/app.py` can map a transport failure onto 502 without the SDK (#167).
"""

from __future__ import annotations

from requivo.core.errors import RequivoError


class EngineError(RequivoError):
    """A clean provider-transport failure (API unavailable, output truncated); nothing was written.
    A `RequivoError`, so one `except` at the boundary catches both families. `code` is
    `provider_unavailable`, published in the `--json` envelope (`docs/compatibility.md`)."""

    code = "provider_unavailable"
