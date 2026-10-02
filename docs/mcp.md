# The MCP server

`requivo mcp serve` speaks the [Model Context Protocol](https://modelcontextprotocol.io) over
stdio, so an MCP client (Claude Code, an agent framework, n8n's MCP support) can drive Requivo
sessions as tools. **Experimental**, like the [HTTP API](api.md) it mirrors.

```json
{ "mcpServers": { "requivo": { "command": "requivo", "args": ["mcp", "serve"] } } }
```

## What the tools are

One tool per row of [api.md](api.md)'s route table, health excepted: the same resource operations,
the same arguments, the same service method behind each, called in-process. The server composes
nothing a service does not already own, so there is no second apply, generation or staleness rule.
`TOOLS` in `src/requivo/mcp/server.py` states the projection, and
`tests/mcp/test_mcp_server.py` holds it to that table in both directions (and, through
`tests/api/test_api_openapi.py`, to the live app).

| Tool | Route it projects |
|---|---|
| `list_sessions`, `create_session`, `get_session` | `GET`/`POST /sessions`, `GET /sessions/{slug}` |
| `get_model`, `list_revisions`, `get_revision` | `GET .../model`, `.../revisions`, `.../revisions/{revision}` |
| `apply_revision`, `preview_revision` | `POST .../revisions`, `.../revisions/preview` |
| `get_status`, `get_impact`, `rescope_context_cards` | `GET .../status`, `.../impact`, `PUT .../context-cards` |
| `run_discovery`, `submit_answers` | `POST .../discover`, `.../answers` (**paid**) |
| `list_artifacts`, `get_artifact`, `save_artifact` | `GET`/`PUT .../artifacts[/{artifact_type}]` |
| `generate_artifact` | `POST .../artifacts/{artifact_type}` (**paid**) |

The analyses routes were retired into the artifacts route (`decision: the-estimate-graduates`), so
an estimate or an epic export is `generate_artifact` and `get_artifact`; `DELETE` has no route and so
no tool.

## Keys, gates and errors

- **Keyless tools never build a provider client**, so everything except the three paid tools works
  with no `ANTHROPIC_API_KEY`. The paid ones read the key from the server's environment and say so
  in their description; they carry the services' own pre-payment gates (a stale
  `expected_revision` is refused before any call, invariant 13, #205) and answer a `usage` object.
- A refusal is a tool result with `isError: true` whose text is the API's error envelope
  (`{code, message, details?}`): `revision_conflict`, `session_locked`, `invalid_request` and the
  rest keep the meaning [api.md](api.md) gives them. Bad arguments are `invalid_request`.
- There is no port and no token: the server runs as the caller, in the caller's workspace
  (`--workspace`, `REQUIVO_WORKSPACE`). A *remote* MCP client should speak the HTTP API instead,
  with its bearer token.

## Why no SDK and no extra

The server is newline-delimited JSON-RPC 2.0 (`initialize`, `ping`, `tools/list`, `tools/call`), a
few dozen lines over the standard library. Taking the `mcp` SDK would add a runtime dependency, a
`RUNTIME_EXTRAS` decision and a wheel smoke leg for a transport that needs none of its machinery;
`pip install requivo` already carries it. If the protocol grows past tools (resources, prompts,
streamable HTTP), that is the moment to reopen the question. A sibling under `plugins/` was
rejected: the plugin is not in the wheel and mirrors a pinned CLI commit.

## Directory submission

Not done here. Prerequisites for any registry listing (a tagged release carrying this server, the
experimental label reviewed, the install line above verified against a published wheel) are to be
checked before an announcement, and nothing in this change announces anything.
