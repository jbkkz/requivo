# Requivo Web

The **primary Requivo interface**: a local, single-user, self-hostable browser workspace over the same
Core, services and session format as the CLI and the Claude Code plugin. It is where the product's
workflow lives — paste a request, work through what could change the solution, and leave with a
decision brief. Sessions it creates open in the other two, and theirs open here.

"Primary" is about weight, not capability. The CLI can do everything this can and more; it is
infrastructure. This is the one to hand someone who has a request and half an hour.

Requivo Web is deliberately small — see [Scope](#scope).

## Install

```bash
uv tool install "requivo[web,anthropic]"   # web UI + the Anthropic provider (for discovery/generation)
uv tool install "requivo[web]"             # web UI only — review existing sessions, no provider
```

For development from a checkout:

```bash
uv sync --extra web --extra anthropic
uv run requivo web --no-open --port 8765
```

The web dependencies (FastAPI, Uvicorn, Jinja2, python-multipart) are an **optional extra** — they are
never imposed on CLI or Claude Code users. The templates, CSS and a vendored copy of HTMX ship inside
the wheel, so nothing loads from a CDN and the UI works offline.

## Run

```bash
requivo web \
  --workspace .        # where sessions live (default: current directory)
  --host 127.0.0.1     # default; localhost only
  --port 8765          # default
  --no-open            # do not open a browser automatically
  --reload             # auto-reload on code changes (development)
```

By default the server binds to `127.0.0.1`, prints its URL, and opens your browser. A credential
(`ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` for a bearer-token setup) is read from the **server
environment** — it is only needed for provider actions (discovery, generation); reviewing existing
sessions needs no key. The credential is never shown in the browser, never a form field, never logged.

### Binding beyond loopback

`--host` accepts any address, and reaching the app from another machine needs two things:

```bash
REQUIVO_WEB_ALLOWED_HOSTS=192.168.1.50 requivo web --host 0.0.0.0
```

- **The bind address** (`--host`) is what the process listens on. `0.0.0.0` (or `::`) is never a
  value a browser sends back — a client addresses the hostname or IP it actually connected to.
- **The allowed-host list** (`REQUIVO_WEB_ALLOWED_HOSTS`) is what the DNS-rebinding guard's `Host`
  allowlist accepts (see [Security](#security-local-by-default)); it governs the HTTP API too since
  #508. It must name the real LAN IP or hostname clients will put in their `Host` header, because
  that check runs on every request, reads and writes alike.

A wildcard bind is **not** auto-allowlisted — it would allow nothing a real client sends, so every
LAN request would get a 403 `host_not_allowed`. A specific address (`--host 192.168.1.50`) is.

**There is still no authentication and no TLS.** Beyond loopback, the request token and every
session's content cross the network in plain text; a TLS-terminating reverse proxy is the supported
way to go further than a trusted LAN.

## The workflow

One path leads the product, and the interface is built around it rather than around the model:

```text
paste a request
  → read what Requivo understood
  → answer the few questions that could change the solution
  → see what those answers moved
  → generate one decision brief
  → change an answer later and see what needs review
```

### What it looks like

Four moments from one session, in order. The engine's own vocabulary — slots, coverage, revisions —
is not on any of these screens; the translation is defined once in `web/viewmodels/labels.py`.

All four are produced by `python scripts/shoot_doc_images.py`, from the bundled example seeded as a
real session — no key, no network. `python scripts/shoot_doc_images.py --check` says whether the web
surface has moved since they were taken (#329); re-shoot when it has.

![The home page: a single request box, with the sessions already in progress listed below it.](images/web-home.webp)

![The session page: the objective Requivo derived, the request it read, and its reading of the request — with what it is assuming stated as assumptions.](images/web-session.webp)

![What could change the solution: each open question with why it matters and the area it would move, the answer form below it, and the Are we ready? verdict with its reasons.](images/web-questions.webp)

![The decision brief, rendered as a document: the request and objective, the current understanding, what is confirmed, and the assumptions that matter.](images/web-brief.webp)

- **Explore a worked example** — one button under the request box: a finished analysis *and* its
  decision brief without a key (#429). It materialises the bundled sample (the email `requivo demo`
  replays) as a real session through `SessionService.create_session` + `update_model` and
  `ArtifactService.save` — a revision, a readiness verdict, openable in the CLI and Claude Code;
  nothing is called. Refining it or generating another document needs a key. Clicking twice returns
  to the session you already have, and it is labelled *Example* by the request it carries (#226).
  With no provider configured the main form still captures a request (*Save request*).
- **Home** — the request box *is* the home page; below it, the requests in progress with what was
  asked, whether it waits on you and whether a document needs updating, newest first, times as
  *3 days ago* with the exact instant on hover (#237). Sessions from the CLI or Claude Code appear
  too. A session that cannot be read is **a row that names itself** (#7) with one plain sentence —
  the store's own remedy when it wrote one (#240) — and sorts last; its page states the failure in
  full (409 for a newer format, 500 for a store that could not answer), also logged in the server
  terminal. The order lives in the view model; `requivo session list` stays sorted by slug.
- **Advanced settings** — session name, context cards, analyse now or just save. Collapsed: the server
  already knows whether a provider action can run. The API key is never a form field.
- **Session** — the request, what Requivo understood, at most five questions (each with *why it
  matters* and its likely area of impact), *Are we ready?* with its reasons, and the decision brief.
  **The answer form is unconditional**: when questions run out it reads *Anything to add?*, because a
  correction or late constraint still has to reach the model (#49).
- **Answer** — a new, optimistic-locked revision; the page leads with **What changed**: the parts that
  moved, the decisions and assumptions to review, the documents needing an update — computed from the
  dependency graph, never generated.
- **Generate** — the decision brief is the primary action; the other six live under *More documents*.
  Each is saved with its source revision and marked *Draft* while high-impact topics are unresolved.
  Nothing is regenerated on your behalf. The estimate saves its stories beside it (#519).
- **Traceability details** — one disclosure with everything the engine knows: per-topic understanding,
  coverage, every open question, decisions and contested premises, provenance, the raw model export.
  A decision derived while a topic under it was assumed, now confirmed, is tagged *Worth re-reading*
  (#493, the same service as `requivo impact`); one the review could not check says so.
- **Danger zone** — *Delete this session…* leads to a confirmation page naming it and suggesting
  `session export` first (no trash), then removes it (#238).

## Architecture

Requivo Web is a thin layer — it owns **no business logic**:

- Routes parse the request, call an application **service**, and render a Jinja template. They never
  touch the filesystem, never read or write `model.json`, and never shell out to the CLI.
- Discovery, answers and generation all go through `DiscoveryService` — the *same* orchestration the CLI
  uses — which calls the provider and applies the result through `SessionService` (validate → diff →
  propagate → revision → stale-flag) and `ArtifactService` (save with source revision).
- Readiness, the understanding split and staleness are computed in the Core; the templates only render
  the `SessionService.status()` projection through small view models — no logic in Jinja.

```
browser ──HTTP──> routes ──> DiscoveryService / SessionService / ArtifactService ──> Core ──> .requivo/
                     │                        (the same services the CLI calls)
                     └── Jinja templates + view models (presentation only)
```

### When something goes wrong

A structured `RequivoError` becomes a clean page, never a traceback, and its status answers **is this
about what you sent, or about this server?** — 4xx for your request (`404` unknown name, `400`
malformed input, `413` over the ceiling, `409` a raced write), 5xx for the server or what it depends
on (`500` unreadable or missing cards, `502` invalid model output after every retry, `503` a lock
that did not clear). Every code has an explicit mapping, held by a test; the table is in
[compatibility.md](compatibility.md#http-statuses-in-requivo-web).

A 5xx, and the `INFO` line reporting what a paid call cost, are logged in the server terminal with a
timestamp, level and logger name (#291). The handler is attached by the `requivo web` verb, never at
import or in `create_app()`: mounted in your own service, your configuration of the `requivo.web`
logger applies, and the root and uvicorn loggers are never touched. Under `--reload`, uvicorn's worker
re-imports the app without the entry point and logs unformatted.

## Security (local by default)

- **Loopback by default.** A non-loopback `--host` prints a warning: there is **no authentication**. A
  wildcard address also needs `REQUIVO_WEB_ALLOWED_HOSTS` — see
  [Binding beyond loopback](#binding-beyond-loopback).
- **Writes are guarded against cross-site requests**, since any page in the same browser can post to
  a local port and here writing is the damage (sessions created, calls billed). `web/security.py`
  runs four checks: a host allowlist (the DNS-rebinding guard, the only one that also runs on reads),
  `Sec-Fetch-Site`, an `Origin`/`Referer` trust-domain match, and a per-process request token in every
  form. The first three live in `requivo.host_policy`, shared with the HTTP API (#508, #425). A page
  held open across a restart needs a reload for the new token.
- **The guard declines what it cannot read**: a request naming no host is refused as
  `undetermined_host` (#45; HTTP/1.0 without `Host` is not a supported caller), and an authority it
  cannot parse — `evil.com@127.0.0.1`, `127.0.0.1 evil.com` — is refused by the parser (#51).
- **One code per fact** (#52), all 403: `undetermined_host`, `host_not_allowed`, `cross_site_fetch`,
  `opaque_origin`, `origin_mismatch`, `missing_request_token`.
- **The three loopback spellings are one origin** (#43); hosts you list in `REQUIVO_WEB_ALLOWED_HOSTS`
  must match exactly. `Origin: null` is refused; no origin header is accepted (so `curl` with a valid
  token works). The port is deliberately not compared (#46): the token gates the write, and a page on
  another port cannot read it, since the same-origin policy counts the port and no CORS headers are sent.
- Slugs are validated in the Core, so no request escapes `.requivo/sessions/`; only the package's
  `static/` is served; the key is read from the server environment, never rendered or logged.
- Everything rendered is autoescaped except a saved artifact, which `render/html.py` builds tag by tag,
  escaping every run of text first and emitting no attributes (#235).
- **Headers**: `X-Content-Type-Options`, a same-origin `Content-Security-Policy` (HTMX is vendored,
  recorded in `THIRD-PARTY-NOTICES.md`), and `Referrer-Policy: same-origin` — `no-referrer` would make a
  browser send `Origin: null` on the app's own form posts (#47).
- **`Cache-Control: no-store`** on everything except the package's own assets (#218), keyed on the
  path and failing closed, so your material never lands in the browser's disk cache and a cached page
  never replays an old revision or token.
- **Input is length-bounded and refused, not truncated.** No field carries `maxlength`, which clips a
  paste silently (#8); past 80% of the ceiling a live counter appears, rendered from the server's own
  limit, and never blocks the submit (#239). Bodies are capped before parsing; an unknown context card
  is an error. **A refusal keeps your submission**: the form re-renders in place with what you sent and
  the refusal beside the field (#30).

## Limits of this first version

- Generation covers every document the service produces; the buttons come from its vocabulary. The
  epic's tracker exports stay CLI-only.
- Provider calls are synchronous (in a worker thread) with an HTMX loading state; after ten seconds the
  status reports elapsed time (#236). No job queue, no WebSockets.
- **What a paid action cost** is stated where it can be and logged always (#253). Fragments carry it;
  after a 303, `web/spend.py` holds the figure in memory keyed by slug and the landing GET pops it once
  — never on the URL. The log line is written from a `finally`, so a failed call that spent tokens is
  still traced. Tokens are exact; the cost is an estimate dated by its rate table.
- **One provider call at a time per page** (#50): while one is in flight every submit button is muted.
  Not a safety mechanism — the server holds the revision lock either way.
- Artifacts render in the closed dialect `render/markdown.py` emits; anything else degrades to escaped
  text, with no Markdown library (#235). **Download** serves the exact bytes on disk.
- Readiness is binary, as in the Core. **What changed** is not persisted: a reload loses the narrative,
  while each document's *needs updating* flag stays on disk.
- Single user, no live multi-tab update: a second tab learns of a change when it submits, and **before
  paying** (#205) — the answers form carries its rendered revision and is refused at snapshot time.

## Scope

Requivo Web is intentionally bounded to: local, single-user, filesystem-backed, no authentication, no
organizations, no collaboration, no billing, no remote storage, no telemetry, no database, no SaaS
infrastructure — a scope decision that keeps the security posture simple enough to state in a
paragraph (see [SECURITY.md](../SECURITY.md)).
