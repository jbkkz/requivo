# The HTTP API façade

**Slug:** `the-http-api-facade`

## Context

The roadmap has named "an HTTP API / MCP façade — another thin layer over the same Core" since the
surfaces were first ordered. What makes it worth deciding now rather than gesturing at is that the
seam it would stand on is already API-shaped, and the one consumer that could not wait for it has
already paid the price of its absence.

**What is already wire-ready, verified against v3.0.0:**

- `RequivoError.to_dict()` is a stable serializable envelope — `{code, message, path?, details?}` —
  published in `docs/compatibility.md` with the rule *assert on the code, never the message*, and
  with per-code `details` contracts argued out one by one (#82, #35, #57).
- Every error code has an explicit HTTP status in `requivo/http.py`'s `STATUS_BY_CODE`
  (`web/app.py`'s private `_STATUS_BY_CODE` when this record was written -- relocated by #422, which
  is section 3's own proposal below, landed), guarded by
  `test_every_error_code_has_an_explicit_http_status` so the table cannot fall behind the
  vocabulary. The 4xx/5xx split is reasoned per row (whose fault is this?), including the deliberate
  refusals of plausible-but-RFC-wrong statuses (409 over 426 for a format from the future).
- Optimistic concurrency is already wire-shaped: `expected_revision` rides `update_model` and
  `answer`, the web form carries it today, and a stale precondition is refused **before** the paid
  provider call (`_require_no_conflict_yet`, #205). `revision_conflict` answers 409;
  `session_locked` answers 503 with the documented meaning *the write never started; retrying it
  unchanged is correct* — which covers both the write lock and the non-blocking first-discovery
  guard (#209).
- `SessionService.status()` is already the status resource: a pure projection returning the exact
  payload `requivo status --json` publishes. Every `--json` payload is shape-pinned
  (`test_every_public_json_payload_keeps_its_recorded_top_level_shape`), which is a response-body
  discipline an API section can inherit rather than invent. This said *fifteen* and the registry
  holds sixteen since `session delete` landed -- a count in prose that no test can falsify, which is
  `CLAUDE.md`'s own named failure mode; the property is what matters here, not the number.
- Input caps live at the service layer (`require_input_within_bounds` in `create_session`,
  `answer`, `draft_turn` — #255), which is invariant 14 doing the API's request validation already.
- Creation is idempotent on identity — the request **and** its context-card selection — with the
  slug claim atomic in the store (invariant 11).
- Timestamps are already one format everywhere: UTC, second precision, Z-suffixed ISO-8601
  (`persistence._now()`), and `SessionMeta` already carries a `session_id` (uuid4 hex) beside the
  slug.

**What is missing:**

- No HTTP write surface exists for a machine client. Requivo Web's ten routes are an HTML/HTMX app
  behind a synchronizer token minted per process and rendered into forms — the right ceremony for a
  browser and a dead end for a script, which has no form to read the token out of.
  `docs/compatibility.md` promises those routes' paths and statuses but explicitly not their bodies.
- The status table is trapped in the `[web]` extra: any second HTTP surface either imports
  `requivo.web.app` (dragging Jinja2 and the browser middleware in) or copies the table. (**Freed
  since**, by this record's own §3 proposal: #422 moved it to `requivo/http.py`.)
- The workspace root is ambient process environment (#272): `paths.py` resolves
  `REQUIVO_WORKSPACE`/cwd per call, so an API process can only ever serve the one workspace its
  environment names, and two `FileSessionRepository` instances are indistinguishable. (**Landed
  since**, #446: the root is constructor state, `FileSessionRepository(root=...)`, with
  `paths.workspace_root()` still the default a surface falls back to — see section 6.)
- Provider calls are synchronous and non-streaming, 15–100 s typical against a 16k output ceiling
  (#256 open).
- There is no auth story of any kind, and no spend ceiling: the one thing standing between a
  runaway client loop and the server's Anthropic key is the cross-site guard, which exists for
  browsers. (**The ceiling landed since**, #427 — read section 5 for what it is and is not; auth
  is still nothing, and is slice 4's.)
- Sessions have no delete (#238 — **landed since**, see section 6); `estimate` is a terminal-only
  analysis with no artifact type — it is absent even from `ARTIFACT_FILENAMES`, so it cannot be
  persisted by any path.

**The standing cost that makes this concrete.** The hosted product — in its own private repo —
consumes `requivo` as a PyPI dependency and was built before any facade existed to stand on. It
shows the expected shape: an integration module that re-orchestrates reason-then-apply against
provider internals, reaching past the `ReasoningProvider` protocol and past `DiscoveryService`
entirely; a conservative pin that starves against this repo's major cadence; and per-workspace
addressing by `os.environ` mutation under a process-global lock — one engine call at a time.
Every hardening the services have grown since that scaffold — the pre-payment conflict refusal
(#205), the concurrent-first-discovery guard (#209), usage provenance on revisions (#292), the
brief's two-fact conflict answers (#208) — is absent there, because it re-orchestrated the seam
instead of standing on it. That is the second implementation this architecture exists to forbid,
already alive, already diverging, and pinned to a major this repo has moved two past.

The question: what does the HTTP API look like, where does it live, and what may be promised when?

## Decision

**Build `create_api()` in this repo as an `[api]` extra (tracked as #425) — a sibling factory to `create_app()`,
REST, synchronous, translation-only over the existing services — and do not freeze its contract
until three named preconditions land.** The resource model is transport-neutral so the MCP facade is
a projection of it, not a second design. Everything below is the design, stated so it can be
disagreed with.

### 1. The resource model — the domain as it exists, one service method per operation

The rule that governs every row: **an API route that adds logic is wrong by this repo's
architecture.** A route relabels, selects and serializes; it never re-derives readiness, staleness,
or a blast radius, and it never reaches past a service (the same line `web/dependencies.py` already
draws: the filesystem is only ever reached through a service, never from a route).

Base path `/api/v1`. All request and response bodies are JSON; all timestamps are the persisted
format (UTC, second precision, `Z`); every session envelope carries both `slug` (the address) and
`session_id` (the uuid identity).

| Route | Method | Backed by (1:1) | Notes |
|---|---|---|---|
| `/health` | GET | — (version constant) | liveness, mirrors the web's |
| `/schema` | GET | the core slot-schema read (`slot_meta`/`schema_slot_ids` — what `requivo schema` prints) | what a proposal-writing client must know |
| `/context-cards` | GET | the core card enumeration (what `requivo context` prints) | install-level vocabulary |
| `/sessions` | POST | `SessionService.create_session_report` (built by slice 2, #425 — this row first named `DiscoveryService.create_only`, which cannot say whether *this* call created the session; the additive `_report` sibling returns that fact, and its `strict_slug` opt-in is what the 409 rests on) | body `{request, context_cards?, slug?}`; 201 fresh, 200 idempotent re-init of the same identity; 409 `session_exists` when an explicit slug is taken by a different identity |
| `/sessions` | GET | `SessionService.list_entries` | degraded rows come back as `{slug, error}` — *could not be read* and *not analysed yet* render differently, invariant 15's third state on the wire |
| `/sessions/{slug}` | GET | `SessionService.meta` | the session envelope: slug, session_id, timestamps, current revision, cards, provider, artifact_status |
| `/sessions/{slug}` | DELETE | `SessionService.delete_session` | *not built* — slice 2 shipped every other write and left this one to #238. The service verb this row was reserved against was #238's future work when this record was written and exists now; what is still missing is the route |
| `/sessions/{slug}/model` | GET | `SessionService.load_model` | the durable product; named `model`, not `export`, because `session export` already means an archive in this vocabulary |
| `/sessions/{slug}/revisions` | GET | `SessionService.meta` (`revisions`) | the provenance log — provider, model, surface, prompt hash, and since #292 what each apply spent; this is also where historical usage lives, so no separate usage resource is needed |
| `/sessions/{slug}/revisions/{n}` | GET | `SessionService.load_revision` | the basis for "what moved since?" |
| `/sessions/{slug}/revisions` | POST | `SessionService.update_model` | **the apply**: body `{proposal, expected_revision}` → `UpdateResult.to_dict()`. A revision is appended, which is exactly what the method does — and this is the wire path for an external reasoner (the Claude-Code shape: something else reasons, the validated path applies). Proposal semantics are invariant 10's, unchanged: complete slots, tri-state reasoning collections |
| `/sessions/{slug}/revisions/preview` | POST | `SessionService.diff` | the dry run of the append — `UpdateResult` with `status: "planned"`, nothing written |
| `/sessions/{slug}/status` | GET | `SessionService.status` | verbatim — the payload is already public as `requivo status --json`'s canonical-session form |
| `/sessions/{slug}/impact?slots=a,b` | GET | `SessionService.impact` — **the one new method** (XS: `propagate(load_model(slug), resolve_slots(...))` behind the service seam) | pure DAG query, no provider, no write; the addition exists so the route stays translation-only rather than computing in the handler |
| `/sessions/{slug}/discover` | POST | `DiscoveryService.run_discovery` | first turn on a captured request. 409 above revision 0 (the gate is taken before payment, invariant 13); 503 when a concurrent first discovery holds the guard |
| `/sessions/{slug}/answers` | POST | `DiscoveryService.answer` | **answers-as-turns**: a turn is not a stored object — it becomes a revision, which is why the response is the `UpdateResult` naming the revision it minted. `expected_revision` is **required** in the API body (stricter than the service's optional default, deliberately: an API is a concurrent surface by definition, and the web form already carries it) |
| `/sessions/{slug}/artifacts` | GET | `ArtifactService.list` | freshness is the explicit stale flag — the dependency graph's verdict, never revision drift (invariant 1, stated in the response docs so clients don't reinvent the wrong rule) |
| `/sessions/{slug}/artifacts/{type}` | POST | `DiscoveryService.generate` | provider-backed generation + save; the response carries the `ArtifactStatus`, the typed contract's dump, and the `usage` object for what it spent. `brief` keeps its documented composite behaviour (reasoning absorbed as a revision; on a lost race the paid document is still saved stale and the 409 message says both facts — #208) |
| `/sessions/{slug}/artifacts/{type}` | GET | `ArtifactService.show` (+ the `list` row for provenance) | JSON envelope `{type, filename, source_revision, updated_at, stale, content}`; `Accept: text/markdown` returns the raw document |
| `/sessions/{slug}/artifacts/{type}` | PUT | `ArtifactService.save` | the external-reasoner save: body `{content, source_revision}`. `source_revision` required — 400 `unstated_source_revision`, the service's own refusal, unchanged |
| `/sessions/{slug}/analyses/stories` | POST | `DiscoveryService.reason` | terminal-only analysis, nothing persisted; POST because it pays |
| `/sessions/{slug}/analyses/estimate` | POST | `SessionService.snapshot` + `DiscoveryService.reason_from` ×2 | the one composed row, and it composes exactly what the CLI composes: stories then estimate against those stories, from **one** snapshot (invariant 12, #135). The response names the `revision` the snapshot read — the fact the terminal rendering could never state |
| `/sessions/{slug}/context-cards` | PUT | `SessionService.rescope` | → `RescopeResult.to_dict()`; re-scoping semantics unchanged (#168: revision minted only when a model exists, nothing marked stale, next turn reasons under the new selection) |

**Deliberately out of v1**, each with its reason: session archive **export/import** over HTTP (the
CLI covers the sharing story today; import is a 400-heavy, filesystem-shaped path with a documented
TOCTOU — put it on the wire when two machines without a shared filesystem actually need it);
`session migrate` (a local-filesystem concern by definition); `doctor` (it answers for an install,
and the operator of this server has the CLI on the box — a remote doctor is a different product
question). Usage is not a standalone resource: paid responses carry their own `usage` object (the
same view `web/spend.py` logs), and history lives on `/revisions`.

### 2. The style: REST, synchronous, with recovery semantics a client can actually follow

One provider call per paid operation, 15–100 s typical, non-streaming (#256). v1 does not hide
that; it documents it:

- **Client timeouts.** Set read timeouts generously (≥ 300 s recommended) and know the recovery: a
  synchronous handler completes and applies even if the client hung up, so **the answer to a
  timeout is `GET /sessions/{slug}/status`, not a resubmit** — if the revision advanced, the turn
  landed.
- **503 `session_locked` → resubmit the identical request** after a short delay (the code's own
  contract: the write never started). The response carries `Retry-After`.
- **409 `revision_conflict` → never resubmit unchanged.** Re-read (`/status` or `/model`), rebase
  intent, resubmit with the new `expected_revision`. The refusal is issued before payment wherever
  the staleness is already knowable (#205).
- **502 `provider_unavailable` / `provider_output_invalid` → retry with backoff.** The domain
  bounds double-payment: a discover retry that lost a race meets the revision-zero gate, and an
  answers retry meets its precondition.
- **Streaming and jobs are deferred, with triggers, not vibes.** An SSE variant of the paid POSTs
  becomes possible when #256 moves the provider itself to streaming — the API cannot stream what
  its provider does not. A jobs resource (202 + polling) is warranted when a deployment needs paid
  calls to survive worker restarts or horizontal scaling — concretely, when the hosted product consumes
  this API in production, or when measured p95 wall-clock exceeds what the fronting gateway will
  hold open. A local single-user API gains only moving parts from jobs, and the recovery semantics
  above already cover the disconnect case.

### 3. The contracts

- **Errors.** The failure body is `RequivoError.to_dict()` **verbatim** — the envelope every
  `--json` verb already publishes, per-code `details` shapes included. No new wrapper, no
  problem+json translation: one envelope, three surfaces. The HTTP status comes from the existing
  table, **relocated** out of `web/app.py` into a small surface-neutral module (#422 — the
  `paths.py`/`streams.py`/`usage.py` shape: a cross-cutting facility belonging to no layer; the
  module's name is the implementation's call) so the `[web]` extra is not the price of a classification two HTTP
  surfaces share. The table was private when this was written, so the move was free;
  `test_every_error_code_has_an_explicit_http_status` moves with it. **Landed** as
  `requivo/http.py`'s `STATUS_BY_CODE`, with that test in `tests/test_http_status_table.py`. This is the `usage.py` lesson
  (#167) applied before the leak instead of after: when a second surface needs a thing packaged
  under the first, the fix is a neutral home, not an import across.
- **IDs.** Routes address by **slug** — the workspace-scoped, human, directory-naming id every
  other surface speaks. Every session envelope also carries **`session_id`** (the uuid4 hex
  `SessionMeta` already records) as the identity a multi-workspace future keys on. v1 does not
  route by uuid, because one API process serves one workspace root — #272's constraint — and
  promising uuid routing would promise a server shape this repo cannot yet construct.
- **Timestamps.** ISO-8601 UTC, second precision, `Z` — the format `persistence._now()` already
  writes everywhere. The API states the existing format rather than minting one.
- **Pagination: none in v1**, and the reason is stated so the absence reads as a decision: the
  store is a working set in a directory, tens of sessions; a cursor over a directory listing
  promises an ordering stability the file backing cannot cheaply keep, and `list_entries` must
  return degraded rows, which no offset arithmetic survives honestly. Revisit when a backing where
  listing is a query (the Postgres repository the seam exists for) arrives, or when a measured
  listing crosses ~1k sessions.
- **Idempotency rides the domain.** Creation is idempotent by identity (request + cards): a repeat
  POST returns the same session, 200 against the first call's 201. First discovery is exactly-once
  by construction: the revision-zero gate plus the non-blocking discovery guard, both taken before
  payment. `POST /answers` requires `expected_revision`, so a double-submit is a clean 409 refused
  before the provider is paid. Artifact generation is **not** idempotent — each POST pays and
  overwrites — and is documented as such. No `Idempotency-Key` machinery in v1: it would be a
  second, weaker implementation of gates the services already enforce at the integrity boundary.

### 4. OpenAPI

The spec is generated from `create_api()` — FastAPI produces it from the route signatures, and the
factory turns `docs_url`/`openapi_url` **on**, where Requivo Web deliberately keeps all three off
(and keeps keeping them off; a browser app's routes are not an invitation to script it).

**Docs are on, and the assets are local — the paragraph this section was missing (#504).** Turning
`docs_url` on was this decision; *how* `/docs` and `/redoc` get their JavaScript, CSS, favicon and
fonts was never weighed, and FastAPI's own default is a CDN — `swagger-ui-dist@5`, a floating major
with no lockfile, plus a third-party favicon and a Google Fonts stylesheet, all loaded into the same
origin `GET /api/v1/sessions` answers 200 with no credential. That is not the trade this decision
made; it is an unconsidered side effect of it, found by the v3.2.0 release audit alongside the
missing security-header middleware (#503) landing three weeks after this decision shipped. The
remedy keeps the decision and removes the side effect: `swagger-ui-dist` and `redoc`'s standalone
bundles are vendored into `src/requivo/api/static/` (pinned versions, refreshed by hand — see
`THIRD-PARTY-NOTICES.md`), `api/routes/docs.py` serves `/docs` and `/redoc` from that mount instead
of FastAPI's CDN-backed defaults, and neither page's Google Fonts request survives (ReDoc renders in
the browser's own default sans-serif instead). This is also what makes `script-src 'self'` — the one
CSP directive `requivo.security_headers` never widens for this app, unlike `style-src` (see
`api/app.py`'s own comment for why that one directive does widen) — land without a CDN exception:
the two issues were fixed together because either alone leaves the other broken, not because they
shared an author's convenience.

What the spec's existence promises, precisely: **paths, methods, statuses and the error envelope
are the contract; the generated JSON document is not byte-stable and never will be promised as
such** — FastAPI's rendering moves under it release to release. The pin is a committed skeleton
test in the `epic.json` style (`test_the_epic_export_skeleton_is_pinned_to_its_version` is the
model): the path/method/status skeleton recorded per version, red when a route vanishes or moves.
Response-body shape pinning arrives at freeze, as the same one-testable-sentence contract the
`--json` payloads carry — top-level key set and JSON types, additive changes free and recorded.
Request bodies are declared as real models (they are small); response models start as documented
dicts — the services' dataclasses already own `to_dict()`, and hand-mirroring them into pydantic
response models is a drift surface this repo has a named failure mode for (invariant 8's mirror
rule), taken on only where it pays.

### 5. Auth, and the spend ceiling

**None of the three steps below is built**, and the reason is one fact rather than three: this
extra ships **no listener**. `[api]` declares fastapi and no ASGI server, there is no `requivo api
serve` verb, and nothing outside the package calls `create_api()` — running it at all means
installing uvicorn yourself and writing a launcher. Auth is a property of a bind, so it arrives with
slice 4, which is the slice that binds. **The progression, in three explicit steps:**

1. **Local, loopback: no auth.** Default bind `127.0.0.1`, same wildcard-bind refusal discipline
   `requivo web` already implements. This is the CLI-parity mode: the user on the machine already
   holds the key.
2. **Bound beyond loopback: a static bearer token, required.** If the bind address is not loopback,
   the server refuses to start unless `REQUIVO_API_TOKEN` is set — the same deliberate-act shape as
   `REQUIVO_WEB_ALLOWED_HOSTS`. `Authorization: Bearer`, constant-time compare, every route. One
   token, no users, no roles: this is "my other machine may call this", not identity.
3. **Cloud identity: never in this repo.** Accounts, orgs, quotas, billing are the hosted product's
   private concern, standing on the API (or on the services directly) behind its own identity
   layer. The open-core boundary is already decided; this record just restates which side of it
   auth lives on.

**Cross-site, without cookies.** The API deliberately does not reuse the web's synchronizer token —
that ceremony exists because HTML forms carry ambient trust, and the API has no forms and no
cookies. Of the web guard's four checks it keeps three, and **they do not all ship at the same
time**, which this paragraph originally did not say (#509):

- **The host allowlist — shipped, since #508.** `requivo.host_policy.check_host`, the same
  definition Requivo Web calls, installed inside the header middleware by `api/app.py`. It lands
  ahead of the rest because DNS rebinding is transport-level and applies to **reads**, which is all
  this surface serves today: before #508, `GET /api/v1/sessions` with `Host: evil.example.com`
  answered 200 where Requivo Web answered 403. `tests/test_host_allowlist.py` is parameterised over
  both surfaces.
- **The `Sec-Fetch-Site`/`Origin` checks on unsafe methods — slice 4.** When this was written
  there was no unsafe method on this surface for them to run on; slice 2 (#425) has since shipped
  the writes, and these two checks are still slice 4's, alongside the bind discipline.
- **`Content-Type: application/json` required on unsafe methods — shipped with slice 2 (#425),
  brought forward from slice 4** where this record first placed it: a write surface with none of
  this posture in front of it is the state #508 was filed about, and this check is the one leg cheap
  enough to ship with the writes themselves (`api/app.py`'s `require_json_content_type`, 415
  `unsupported_content_type`). This is the addition that does the token's job for a JSON API: a
  cross-origin page cannot send that content type without a CORS preflight, and the API sends no
  CORS headers, so the preflight fails — the browser attack the web guard exists for (a hostile
  page burning the server's key with fire-and-forget form posts) has no JSON-shaped equivalent.

**The ordering constraint, because it is the one that can go wrong:** the host guard lands with, or
before, `requivo api serve`. A serve verb shipping first would turn a hypothetical into a default,
which is why #508 was fixed ahead of the slice that owns the rest of this posture rather than inside
it.

**The spend ceiling is a service-layer seam, not an HTTP feature (#427). Landed**, in the shape
proposed here. The pieces already existed: `requivo.usage` scopes a ledger per operation, every call
is recorded on every exit (including failures), and each record carries the rate it was billed at.
The addition, now in the tree: `DiscoveryService` consults an optional, injected `SpendPolicy`
immediately before each provider call — the same
chokepoint the `_usage_since` bookkeeping already brackets — and refuses with a new structured code
(`spend_ceiling_reached`) when the ledger says the budget is spent. Proposed status: **403** — the
server understood and refuses to authorize a costed action; 429 was considered and rejected because
it asks the client to retry later and a budget does not replenish with time (the same
right-in-English-wrong-in-RFC test that chose 409 over 426). The knob is denominated in estimated
USD over the ledger's own stamped-rate arithmetic, with the unpriced-call case surfaced rather than
guessed — invariant 6's provenance rule applied to money. Living at the service layer, it protects
the CLI and every future surface too, which is the identical argument that moved input caps into
services (#255). It is deliberately not auth and not a cloud quota, and the hosted product layers
its own accounting above it.

**The ceiling is per-operation, not machine-wide, and that is a real gap rather than a rounding
error (#466).** `requivo.usage.track_usage()` scopes the ledger `SpendPolicy.check()` reads against
a `ContextVar`, opened fresh by `cli.py` around one CLI command and by `web/spend.py`'s
`track_web_usage()` around one paid web action — not every incoming request, only the routes that
make a provider call — and discarded the moment that scope exits, so the figure the ceiling
compares against resets to $0 on the very next call. Against the threat model #427 itself was filed
against — a scripted client issuing many separate requests, each individually under any per-request
ceiling — this mechanism gives no protection at all: cumulative spend across a session, or across a
box, is unbounded. What actually closes that gap is named in #427's own "Out of scope" list (its
issue text, not this record) and nowhere built yet: rate limiting at the HTTP layer, once the facade
in this record exists to put it in front of, or a persistent, cross-request `SpendPolicy` variant
with state that survives past a single call. Until one of those ships, read this ceiling for exactly
what it is — a brake on one runaway operation — and not as "nothing on this box spends more than X
without a human."

### 6. Versioning: what freezes, and the preconditions

Path-versioned: `/api/v1/...`. **The extra ships experimental** — the spec's title says so, the
docs say so, and `docs/compatibility.md` says so in one line ("the API exists and is not yet
promised"). **The freeze is an event with a definition**: the day `docs/compatibility.md` gains its
API section — paths, methods, statuses, the error envelope, and each response's top-level key set
and types — with the shape-pin tests landing in the same change, exactly as the `--json` perimeter
is kept.

**v1 is not frozen until all three of these land**, each named because it reshapes contract
semantics if it lands after. **Two of the three have landed since this record was written** — the
status is marked on each below rather than left for a reader to check, and the estimate-artifact
decision (#426) is the one still open:

1. **#272 — the workspace becomes constructor state. Landed** (`FileSessionRepository(root=...)`,
   #446), so this precondition is met. It read, before it did: until then, an API process serves
   whatever workspace its process environment names, per call. Freezing first would bake "slug uniqueness is
   scoped by an environment variable" into a public contract, and would leave
   environment-swap-under-a-lock as the only multi-workspace story. (#272's own trigger — the start of real
   cloud work against this repo — is arguably met by this record; that call belongs to its issue,
   not here.)
2. **The estimate-artifact decision (#426).** `estimate` is terminal-only today — absent even from
   `ARTIFACT_FILENAMES` — so it enters the API as `/analyses/estimate`. If it becomes a saveable
   artifact type, the analyses route is either demoted to a convenience or retired, and the
   artifact vocabulary grows. Either answer is fine; an unrecorded answer freezes a route whose
   permanence nobody decided. (A new artifact type is additive per invariant 8/#260 — the format
   tolerates it; this is about which *routes* are promised, not about the format.)
3. **#238 — delete. The service verb landed** (`SessionService.delete_session` + `requivo session
   delete`, #469), so this precondition is met; the route itself is still slice-4 work. It read,
   before it did: `DELETE /sessions/{slug}` cannot be routed until the service verb exists,
   with its semantics settled where they must be: the lock file at `.requivo/locks/<slug>.lock`
   removed, the slug claim genuinely released, a concurrent writer conflicting cleanly rather than
   corrupting. Shipping a frozen resource model with no lifecycle end teaches API clients to
   `rm -rf` around the store — the wrong-cause class the resolver work (#402, #414) just spent two
   issues closing, reintroduced by omission.

What freezes: paths, methods, statuses, the envelope, top-level response shapes. What explicitly
does not: nested shapes beyond the documented ones (the same line the `--json` promise draws), the
generated OpenAPI document byte-for-byte, field ordering, and the HTML web routes, which keep their
own existing verdict (paths stable, bodies not).

### 7. Where the factory lives

**In this repo, as `create_api()` under an `[api]` extra — recommended over proving it in
the hosted repo first.** The weighing, since both were on the table:

*For cloud-first:* the effort lands where the deadline is; multi-tenant needs (Postgres, identity,
quotas) might reshape the resource model; nothing public is minted before it is proven — and this
repo has a real, measured aversion to minting surface that `docs/compatibility.md` then makes
expensive to remove (the frozen diagnostics tier is that lesson in writing).

*Against it, decisively:* cloud-first **is already running as an experiment, and the result is in.**
The scaffold bypassed the services, re-orchestrated against provider internals, pinned
conservatively, and
serializes every engine call behind a process-global environment swap — not because its authors
were careless but because no HTTP-ready facade existed to stand on. Proving the API there means
growing the second orchestration further, in a private repo whose lessons the open one cannot see,
and then paying the extraction anyway. Meanwhile the demand is not singular: the MCP facade and the
n8n automation flows on the roadmap want the same resource model locally, with no cloud in sight.
Two consumers, named — which is the two-instance bar this repo applies before funding anything.

The surface-minting risk is real and is answered by **splitting build from freeze** (section 6),
not by moving the build. The experimental extra costs what the `[web]` extra cost: a factory, a
test suite that runs offline against a fake provider, and honesty in the docs about what is not yet
promised.

**The MCP facade is a client of this same decision.** Local MCP is a stdio server in (or beside)
the wheel calling the services in-process — no port, no token, tools mapped 1:1 onto the resource
operations table above. Remote MCP is a client of the HTTP API with the bearer token. In both
cases the tool list is a projection of the resource model, which is why the model is designed
transport-neutral here rather than REST-first: same services, different transport, never a second
design.

### 8. The Claude Code plugin, unchanged

The plugin needs nothing from any of this and is changed by none of it. Its topology is different
in kind: **Claude is its reasoning provider** — reasoning happens outside Python, and the
deterministic CLI (`session init`, `model apply --json`, `artifact save --revision N`) is its
validated apply path, with the session store and the fifteen pinned `--json` payloads as its wire
format. It is the existence proof that the services seam supports an external reasoner, and
`POST /revisions` + `PUT /artifacts/{type}` are that exact shape given an HTTP body — an agent
*off* the machine does over the API what the plugin does over the filesystem. The API does not
replace the plugin; it extends the same seam to clients that have no filesystem.

## What breaking it cost

Nothing in this repo has broken yet — this record exists so the API is a decision with named
preconditions rather than a roadmap line that either never lands or lands frozen too early. Two
costs are already real, and they are what funds doing it now:

- **The second orchestration exists.** The hosted scaffold's integration module re-implements the
  reason-then-apply seam against provider internals, without the pre-payment gates, the discovery
  guard, or usage provenance, one global lock wide, pinned behind. Every week it grows is
  extraction work added later — and it is the precise failure the architecture section of CLAUDE.md
  describes from both ends (#77's CLI loop, #167's render import), happening in the one consumer
  the boundary tests cannot see.
- **The status table's location taxes the next surface.** Any HTTP-speaking consumer today either
  depends on the `[web]` extra for one dict or copies it — and a copied classification table is the
  documented drift shape (#34: six codes on a wrong default, one noticed).

And one cost this record *avoids* by sequencing rather than by building: freezing before #272 /
#238 / the estimate decision would put breaking-change rows in `docs/compatibility.md`'s ledger for
questions that already have owners. The ledger records four deliberate breaks shipped in 1.0.0
alone; the way to keep the API's ledger short is to not open it until the known movers have moved.

## Alternatives rejected

- **Prove it in the hosted repo first, extract later.** Rejected above (section 7): the experiment
  has effectively run, and its result is the bypass this record keeps citing. Extraction-later
  pays for the facade twice and locks the learning in the private repo.
- **Defer entirely with a trigger, the `decision: deferring-the-neutral-provider-layer` shape.**
  Rejected because the deferral bar it applies is not met in the deferring direction: that record
  defers because there is zero demand for a second provider; here there are two named consumers and
  a standing divergence. Deferring again would be re-litigating the roadmap line without new
  evidence.
- **Mount the API into the web app (one process, one factory).** Rejected for v1: the web's
  middleware stack is browser furniture — synchronizer token, `no-store` defaults, HTMX error
  retargeting — that a JSON API must not inherit, and the composition (`requivo web` also serving
  `/api`) is a deployment question a later change can answer by mounting both factories; nothing
  about two factories forecloses it.
- **A new API error wrapper, or RFC 9457 problem+json.** Rejected: the envelope is already public,
  already consumed (the CLI's `--json`, Claude Code), and already carries per-code `details`
  contracts. A translation layer over your own error contract is a second implementation of it;
  problem+json's `type`-URI machinery buys nothing a stable `code` does not already give.
- **`Idempotency-Key` header machinery.** Rejected in v1: the domain's own gates (identity-keyed
  creation, the revision-zero gate, required `expected_revision`) are enforced at the service
  layer for every surface, refuse before payment, and cannot be bypassed by a caller that skips
  the header. A header store would be weaker and second.
- **Streaming or a jobs queue in v1.** Rejected with triggers rather than forever (section 2):
  streaming waits on #256 (the provider must stream before the API can), jobs wait on a deployment
  that needs them. Both bought now would be moving parts serving nobody local.
- **Routing by `session_id` uuid.** Rejected for v1: it promises a multi-workspace server shape
  #272 has not yet made constructible, while the slug is what every doc, verb and directory speaks.
  The uuid rides every envelope so the future needs no migration to start keying on it.
- **A 402 or 429 for the spend ceiling.** 402 is reserved-for-future-use and would be the exact
  right-in-English-wrong-in-RFC trap the status table documents; 429 asks the client to retry
  later, and a budget does not reset with time. 403 states what is true: understood, refused by
  policy.
- **Freezing v1 at first ship.** Rejected: three known movers are named in section 6, each with an
  owner. A contract cut ahead of them converts three decisions into three breaking-change rows.

