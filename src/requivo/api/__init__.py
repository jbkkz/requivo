"""Requivo API: the local HTTP facade over the same services as the CLI and Requivo Web (#425).
Experimental, behind the `[api]` extra (`decision: the-http-api-facade`). Nothing here imports
`fastapi`; only `create_api()` needs the extra.
"""

from __future__ import annotations
