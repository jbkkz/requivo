# The local HTTP API

> **EXPERIMENTAL.** The API is not frozen: a path, method, status or response shape may still change
> in any release, without a major-version bump, and `docs/compatibility.md` is silent about it on
> purpose. The freeze is a named event, not a version number: `decision: the-http-api-facade` names
> its preconditions. When it happens, that page gains an API section and this notice comes down. The
> same notice heads the OpenAPI description at `/docs`.

`requivo api serve` puts a JSON REST facade over the same application services the CLI and Requivo
Web use. Each route calls one service method and serializes its answer; none composes core calls or
re-derives readiness, staleness or a blast radius. The bodies are the shapes the CLI's `--json`
outputs already publish, and every refusal is the same error envelope.

## Install and serve

```bash
pip install 'requivo[api]'          # or: uv tool install 'requivo[api]'
requivo api serve                   # http://127.0.0.1:8767, docs at /docs and /redoc
requivo api serve --port 9000 --workspace ~/work/acme
```

Sessions live under the workspace's `.requivo/sessions/`, as with every other interface. Reads need
no key; the paid routes (discovery, answers, generation) need `ANTHROPIC_API_KEY` in the server's
environment. The interactive docs (`/docs`, `/redoc`) and the schema (`/openapi.json`) are served
from bundles shipped in the package, never from a CDN.

## Binding and the token

- **Loopback** (`127.0.0.1`, `localhost`, `::1`, the default) needs no authentication.
- **Any other bind refuses to start** unless `REQUIVO_API_TOKEN` is set, before a port is bound.
- **Once `REQUIVO_API_TOKEN` is set**, whatever the bind, every route under `/api/v1` except
  `/api/v1/health` requires `Authorization: Bearer <token>`. A missing or wrong token is
  `401 unauthorized` with a `WWW-Authenticate` challenge; the comparison is constant-time.
- **The host allowlist** is loopback plus whatever `REQUIVO_WEB_ALLOWED_HOSTS` lists (the name keeps
  its `WEB`: it governs both surfaces). A bind to one address allowlists that address; a wildcard
  bind (`0.0.0.0`) allowlists nothing, so set the variable to the name clients will actually use.

The token is a bearer secret, sent in clear over plain HTTP: beyond a network you control, put the
API behind TLS.

## Cross-site posture

The API sets no cookie and serves no form, so it carries no synchronizer token. Three checks stand
in for one, in this order, before any route runs (the bearer token, when set, is checked between the
first and the second):

1. The `Host` header must be on the allowlist (`403 host_not_allowed`), which defeats DNS rebinding.
2. On `POST`, `PUT`, `PATCH` and `DELETE`, a `Sec-Fetch-Site` or `Origin` naming another site is
   refused with a `403` (`cross_site_request`, `cross_site_fetch`, `origin_mismatch`, ...).
3. The same methods must send `Content-Type: application/json` (`415 unsupported_content_type`),
   bodyless ones included. A cross-origin page cannot send that header without a CORS preflight,
   and the API never answers one.

## Routes

Every path is under `/api/v1`, and a session is addressed by its `slug`. A paid route answers a
`usage` object (`calls`, `tokens`, `cached`, `model`, `cost`, `unpriced_reason`, `rates_as_of`), or
`"usage": null` when the provider reported nothing. `tests/api/test_api_openapi.py` holds this table
to the live application row by row, in both directions, and checks that each handler calls the
method this table names.

| Method | Path | Success | Backing service method | Notes |
|---|---|---|---|---|
| GET | `/api/v1/health` | 200 | none | Liveness; never needs the token |
| GET | `/api/v1/sessions` | 200 | `SessionService.list_entries` | `session list --json` rows; an unreadable session is a row, not a failure |
| POST | `/api/v1/sessions` | 201, 200 | `SessionService.create_session_report` | `{request, context_cards?, slug?}`: 201 created, 200 the same identity again, 409 `session_exists` for a slug taken by another |
| GET | `/api/v1/sessions/{slug}` | 200 | `SessionService.meta` | `session show --json` |
| GET | `/api/v1/sessions/{slug}/model` | 200 | `SessionService.load_model` | The current validated model |
| GET | `/api/v1/sessions/{slug}/revisions` | 200 | `SessionService.meta` | The revision log, provenance and usage included |
| POST | `/api/v1/sessions/{slug}/revisions` | 200 | `SessionService.update_model` | The apply, `{proposal, expected_revision?}`: the external-reasoner write |
| GET | `/api/v1/sessions/{slug}/revisions/{revision}` | 200 | `SessionService.load_revision` | One historical model |
| POST | `/api/v1/sessions/{slug}/revisions/preview` | 200 | `SessionService.diff` | The apply as a dry run: nothing written |
| GET | `/api/v1/sessions/{slug}/status` | 200 | `SessionService.status` | `status --json` |
| GET | `/api/v1/sessions/{slug}/impact` | 200 | `SessionService.impact` | `?slots=a,b`: what rests on those slots |
| PUT | `/api/v1/sessions/{slug}/context-cards` | 200 | `SessionService.rescope` | `{context_cards}`; `null` selects every card, `["none"]` no product context (#721) |
| POST | `/api/v1/sessions/{slug}/discover` | 200 | `DiscoveryService.run_discovery` | Paid. The first discovery; 409 above revision 0 |
| POST | `/api/v1/sessions/{slug}/answers` | 200 | `DiscoveryService.answer` | Paid. `{answers, expected_revision}`, the revision required |
| GET | `/api/v1/sessions/{slug}/artifacts` | 200 | `ArtifactService.list` | Every saved artifact and its `stale` flag |
| GET | `/api/v1/sessions/{slug}/artifacts/{artifact_type}` | 200 | `ArtifactService.show_with_status` | A JSON envelope, or the saved Markdown under `Accept: text/markdown` |
| POST | `/api/v1/sessions/{slug}/artifacts/{artifact_type}` | 200 | `DiscoveryService.generate` | Paid and not idempotent: each call pays and overwrites |
| PUT | `/api/v1/sessions/{slug}/artifacts/{artifact_type}` | 200 | `ArtifactService.save` | `{content, source_revision}`, the revision required |

`POST .../artifacts/estimate` is the CLI's own `estimate`: the stories are reasoned from the same
snapshot and saved as `stories`, then the estimate is saved beside them against the same revision
(`decision: the-estimate-graduates`). Its `artifact` carries `draft`, `soft`, `confidence`,
`stories` and `stories_status`. There is no separate analyses route.

Not in this version: deleting a session, archive export and import, `session migrate`, `doctor`.

## Errors and recovery

Every refusal is `RequivoError.to_dict()` verbatim, `{code, message, path?, details?}`, with the
status `requivo/http.py` assigns its `code`; a body that does not validate is `400 invalid_request`.
Branch on `code`, never on `message`.

The API is synchronous: a paid call holds the connection until the provider has answered. When a
call does not go through:

| Answer | Meaning | What to do |
|---|---|---|
| `503 session_locked` | Another write holds this session; yours never started | Resubmit unchanged after `Retry-After` |
| `409 revision_conflict` | Your `expected_revision` is no longer current | Re-read `/status` or `/model`, rebase, resubmit with the new revision |
| Client timeout | Unknown: the write may have landed | Do **not** resubmit; read `/status` and `/revisions` to see whether the revision moved |
| `502` | The provider failed, or answered something unusable | Back off and retry; what it spent is in the server log |
| `403 spend_ceiling_reached` | An injected `SpendPolicy` refused the next provider call (`requivo api serve` injects none) | Raise the ceiling or stop: waiting does not reset it |
| `413 input_too_large` | The request exceeds an input cap | Shorten it; nothing is truncated for you |

A stale `expected_revision` on `/answers` is refused before the provider is paid. Generation is not
idempotent, so a retry after a timeout pays again: read `/artifacts` first.
