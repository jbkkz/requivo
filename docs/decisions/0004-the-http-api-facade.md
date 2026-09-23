# The HTTP API façade

**Slug:** `the-http-api-facade`

## Context

The roadmap named "an HTTP API / MCP façade" from the start, and the seam was already API-shaped: a
stable error envelope (`RequivoError.to_dict()`), an HTTP status per code (now `requivo/http.py`,
held by `test_every_error_code_has_an_explicit_http_status`), wire-shaped optimistic concurrency
(`expected_revision`, refused before payment — #205, #209), a status projection already public as
`status --json`, input caps and idempotent creation at the service layer (#255, invariant 11).
Missing: a machine write surface (the web's routes need a per-process form token), a status table
outside `[web]`, a workspace that is not ambient (#272), auth and a spend ceiling, delete (#238).
The hosted product had meanwhile re-orchestrated reason-then-apply against provider internals, past
`DiscoveryService`, without the pre-payment gates — the second implementation this architecture
forbids, already diverging.

## Decision

**Build `create_api()` in this repo behind an `[api]` extra (#425): REST, synchronous,
translation-only over the services — and do not freeze it until three named preconditions land.**

1. **Resources, one service method per route**, under `/api/v1`, JSON, addressed by `slug` with
   `session_id` in every envelope. A route relabels and serializes; it never re-derives readiness,
   staleness or a blast radius. Sessions (create — 201 fresh, 200 same identity, 409 on a taken
   slug; list with degraded rows; get; delete), `model`, `revisions` (GET, and POST = the validated
   apply with `expected_revision`; `preview` = `diff`), `status`, `impact` (the one new service
   method), `discover`, `answers` (`expected_revision` **required**), `artifacts` (list; POST
   generates and pays; GET; PUT saves with a required `source_revision`), `context-cards` (rescope).
   The planned `/analyses/{stories,estimate}` routes were retired by `decision: the-estimate-graduates`.
   Out of v1: archive export/import, `session migrate`, `doctor`; usage rides each paid response
   and the revision log.
2. **Synchronous, with recovery a client can follow.** Timeout → read `/status`, do not resubmit;
   503 `session_locked` → resubmit unchanged (`Retry-After`); 409 `revision_conflict` → re-read and
   rebase; 502 → back off. Streaming waits on #256; a jobs resource on a deployment that needs paid
   calls to survive restarts.
3. **Contracts.** Errors are `to_dict()` verbatim — no wrapper, no problem+json. The status table
   moved to a neutral module (#422, landed). Timestamps are the persisted UTC `Z` format. No
   pagination (a directory of tens of sessions; revisit with a queryable backing or ~1k sessions).
   Idempotency rides the domain's gates; generation is not idempotent and says so.
4. **OpenAPI on, assets local.** `/docs` and `/redoc` serve vendored bundles from
   `src/requivo/api/static/` rather than FastAPI's CDN defaults, so `script-src 'self'` holds
   (#503, #504). Paths, methods, statuses and the envelope are the contract; the generated document
   is never byte-stable.
5. **Auth, cross-site and spend.** Loopback: no auth. Beyond loopback: refuse to start without
   `REQUIVO_API_TOKEN`; once set, the bearer token gates every `/api/v1` route except `/health`,
   whatever the bind (slice 4, `api/auth.py`). Cloud identity: never here. Cross-site without
   cookies: the host allowlist (#508), the `Sec-Fetch-Site`/`Origin` checks shared with the web
   (`requivo.host_policy`), and `Content-Type: application/json` on unsafe methods (415) — a
   cross-origin page cannot send it without a preflight the API never answers. The spend ceiling is
   a service-layer `SpendPolicy` (#427), 403 `spend_ceiling_reached`. It is **per operation**: the
   ledger resets each command or paid web action, so it is a brake on one runaway call, not a
   machine-wide bound (#466); rate limiting or a persistent policy would close that.
6. **Experimental until three preconditions land**, then `docs/compatibility.md` gains an API
   section with shape-pin tests: #272 workspace as constructor state (landed, #446); #426 the
   estimate as an artifact (settled by `decision: the-estimate-graduates`); #238 delete (service
   verb landed, #469). Nested shapes, field order and the generated document never freeze.
7. **In this repo, not proven in the hosted one first** — that experiment already ran and produced
   the bypass. MCP (stdio in-process, or remote over this API) is a projection of the same model.
8. **The Claude Code plugin is unchanged**: it is the external-reasoner shape over the filesystem
   that `POST /revisions` and `PUT /artifacts/{type}` give an HTTP body.

## What breaking it cost

The second orchestration was already real: the hosted scaffold lacked #205, #209, #292 and #208
and serialized every call behind a process-global environment swap. A copied status table is the
documented drift shape (#34). Freezing before the three movers would have opened the API's
breaking-change ledger on questions that already had owners.

## Alternatives rejected

- **Prove it in the hosted repo, extract later** — pays twice and hides the lessons.
- **Defer with a trigger** — two named consumers (MCP, n8n) already meet the two-instance bar.
- **Mount into the web app** — its middleware is browser furniture a JSON API must not inherit.
- **A new error wrapper or problem+json**, or **`Idempotency-Key`** — second, weaker
  implementations of what the services already enforce.
- **Streaming or jobs in v1**, **routing by uuid** (#272 had not made it constructible), **freezing
  at first ship**.
- **402 or 429 for the ceiling** — 402 is reserved; 429 means retry later, and a budget does not
  reset with time.
