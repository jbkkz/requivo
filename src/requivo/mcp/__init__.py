"""Requivo MCP: a stdio server whose tools are a 1:1 projection of the HTTP API's resource operations (#438).
Dependency-free (newline-delimited JSON-RPC 2.0); recorded in `docs/mcp.md`. Nothing here imports `fastapi`.
"""

from __future__ import annotations
