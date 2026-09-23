# Integrations — driving Requivo from an automation

How to drive Requivo from an n8n flow, a CI job or a script: the contract the CLI keeps for machines,
the epic export envelope and its tracker plans, a worked email-to-issue flow, polling for change, and
what is deliberately not built. n8n is the worked example because the tracker adapters were built for
it — the authenticated push stays out of this repo and a flow consumes the plan — but everything here
applies to any orchestrator that can run a command.

## The automation contract

The stable automation surface is the **CLI**. The web app's writes are CSRF-locked and its HTML
bodies are not a contract ([compatibility.md](compatibility.md)); the HTTP API is experimental (see
[The HTTP API](#the-http-api-experimental) below).

### The workspace is the state directory

Every session lives under `<workspace>/.requivo/sessions/<slug>/`. An automation owns exactly one
workspace and names it explicitly on every call — `--workspace DIR` or `REQUIVO_WORKSPACE` — rather
than inheriting the process cwd:

```
requivo --workspace /data/requivo <verb> …
```

The workspace is durable state: mount it, back it up, keep it out of container images. Two processes
touching one session serialise on the session lock; the loser of a concurrent write gets a
`revision_conflict`, which is a real answer, never a half-applied session.

Credentials travel as environment (`ANTHROPIC_API_KEY`), injected by the orchestrator per run — not
as a `.env` file sitting in the workspace, where every verb run from that directory would load it.

### Which verbs, and which channel to read

| Verb | Paid? | Machine channel |
|---|---|---|
| `discover <file> --once` | yes (1 call) | exit code; state via `status --json` after |
| `answer <slug> "<text>"` | yes | exit code; state via `status --json` after |
| `brief` / `prd` / `criteria` / `epic` / `release` `<slug>` | yes (1 call each) | exit code; files under `artifacts/` |
| `epic <slug> --export-json --github --gitlab` | (same single call) | **files**: `artifacts/epic.json`, `epic.github.json`, `epic.gitlab.json` |
| `status <slug> --json` | no | **stdout JSON** — the state read |
| `session list --json` | no | stdout JSON (workspace sweep) |
| `session export <slug> --json` | no | stdout JSON `{slug, archive}` + the `.requivo.zip` archive |
| `model apply <slug> <file> --expected-revision N --json` | no | stdout JSON (`UpdateResult`) |
| `model diff`, `artifact list --json`, `session verify --json` | no | stdout JSON |

Two rules make this a contract rather than a habit:

1. **On a `--json` verb, stdout is always parseable JSON** — the success payload on exit 0, the
   structured error envelope `{code, message, path?, details?}` on exit 1. Branch on the exit code,
   then on `code`. Codes are stable identifiers; message text is not — never match on it. Every
   `--json` output is public and pinned (see
   [compatibility.md](compatibility.md#the---json-outputs-are-public)).
2. **On a human verb (`discover`, `answer`, `epic`, …), stdout is not a contract.** Parse nothing
   from it. The machine-readable results are the exit code, `status --json` afterwards, and files at
   the documented paths.

### Exit codes

From [cli.md](cli.md#exit-codes-and-what-3-and-4-mean), with the automation reading added:

| Exit | Means | What a flow does |
|---|---|---|
| 0 | Success | proceed |
| 1 | Clean, expected failure (invalid input, missing session, provider error) | branch on the envelope's `code`; for a failed `discover`, retry later (see below) |
| 2 | Bad arguments | a flow-authoring bug — alert, do not retry |
| 3 | **The work finished; the output could not be encoded.** The message says whether a call was billed | treat as success with unreadable rendering — do **not** blindly re-run a paid verb; read state via `status --json` |
| 4 | **The work was done and part of the answer was unreachable**; stdout carries everything produced, in full | consume stdout, flag the run degraded |
| 130 | Operator interrupt | not a refusal; safe to re-run |

### Deterministic slugs, and what a failed call leaves behind

`requivo discover <path> --once` derives the session slug from the **file stem**, normalised through
the same strict slug validation used everywhere (`"Leave Approval v2.md"` would become a valid
slug; `req-4812.txt` lands exactly at `req-4812`). Writing the request to a file named for the
ticket is therefore the reliable way for a flow to know its slug without parsing prose.

Failure semantics, verified:

- **The paid call fails** (provider down, bad key): the session is still claimed — the request is
  captured at revision 0, stderr says nothing was drafted and the model on disk was not modified.
  **Re-running the identical command retries cleanly** (claiming is idempotent and the
  revision-zero gate still passes). Exit 1.
- **The call succeeded earlier and the trigger fires again** (duplicate email, replayed webhook):
  the re-run is **refused before paying** — a first discovery only lands on revision 0, and the
  refusal names the ways forward (`requivo answer`, or a new slug). Exit 1. This is a free
  idempotency guard: a duplicate trigger cannot double-spend or overwrite refinement work.

Distinguish the two cases by what `status <slug> --json` answers: the refined session answers with
its state; the claimed-but-unanalysed one answers with an error envelope (there is no model yet to
report on). `status --json` is defined from revision 1 onward.

### The state read: `status <slug> --json`

The gate node of every flow. Always present: `slug`, `readiness` (`{ready, blocking_slots}`),
`understanding`, `questions`, `summary`, `remaining_gaps`. Present when the reference is a canonical
session (which an automation's always is): `revision`, `context_cards`, `artifacts` — the latter a
map `type → {revision, filename, stale}` (`updated_at` lives on `artifact list --json`, not here).

Three fields do all the routing work:

- `readiness.ready` — generate, or go back to the requester.
- `questions[]` — what to send back when not ready.
- `revision` — the session's monotonic cursor (see *Watching for change* below).
- `artifacts.<type>.stale` — the freshness **verdict** for each generated artifact, computed from
  the dependency graph. Never infer staleness by comparing revision numbers; the flag is the answer
  (see [session-format.md](session-format.md#artifacts-and-freshness)).

## The epic export envelope (`requivo-epic`)

`requivo epic <slug> --export-json` writes `artifacts/epic.json` — a stable, versioned, tool-neutral
envelope built to be validated and consumed outside this repo. `--github` and `--gitlab` write
issue-creation plans beside it, derived from the envelope by pure transforms. The shape promise, and
the per-version pinned skeleton, live in
[compatibility.md](compatibility.md#the-epic-export-envelope--stable-and-versioned)
(`test_the_epic_export_skeleton_is_pinned_to_its_version`).

### Field by field — envelope, version 1

Top level:

| Field | Type | Meaning |
|---|---|---|
| `format` | string | always `"requivo-epic"` — validate it before anything else |
| `version` | int | the envelope version this file obeys (`1`) — a consumer refuses a version it does not know |
| `epic` | object | the epic itself |
| `issues` | array | the child issues, in creation order |
| `open_questions` | array of string | unresolved questions the discovery left open — surface these to a human; they are not issues |

`epic`:

| Field | Type | Meaning |
|---|---|---|
| `title` | string | epic title |
| `description` | string | Markdown: the goal, then `**Business value:**`, `**In scope:**`, `**Out of scope:**` blocks when present |
| `labels` | array of string | always includes `"epic"` |
| `milestone` | string or null | a milestone **name**, never a tracker id — the consumer resolves it |

Each entry of `issues`:

| Field | Type | Meaning |
|---|---|---|
| `ref` | string | envelope-scoped ordinal (`"#1"`, `"#2"`, …) — **not** a tracker issue number; it exists so `depends_on` can point inside the envelope |
| `title` | string | issue title |
| `description` | string | issue body (Markdown) |
| `labels` | array of string | issue labels |
| `milestone` | string or null | copied from the epic |
| `depends_on` | array of string | refs of the issues this one depends on |

### The tracker plans

**GitHub** (`epic.github.json`) — GitHub has no native epic or dependency, so the plan degrades
honestly: `target: "github"`, `idempotency_label: "requivo-epic:<slug>"`, a `tracking_issue`
(`title` prefixed `Epic:`, `body` = description + a task-list of issue titles, labels + the
idempotency label, `milestone` name) and `issues[]` (`ref`, `title`, `body` — description plus a
`**Depends on:**` line naming dependency titles plus a `_Part of epic:_` line — `labels` including
the idempotency label, `milestone`).

**GitLab** (`epic.gitlab.json`) — maps more faithfully: same skeleton with `description` instead of
`body`, plus `links[]` of `{source_ref, target_ref, type: "blocks"}` — structured dependencies the
consumer wires after creation using its ref→iid map.

### Consumption rules

1. **Idempotency first.** Search the tracker for the `requivo-epic:<slug>` label. Anything found
   was created by an earlier run — skip or update it, never duplicate.
2. **Children before the tracking issue**, in envelope order (dependencies point backward).
3. **Resolve `milestone` by name** to the tracker's numeric id; create it if policy allows.
4. **Wire dependencies after creation** — GitHub: already stated in bodies; GitLab: create issue
   links from `links[]` using the ref→iid map collected in step 2.
5. **`open_questions` go to a human**, not the tracker.

### Freshness and provenance — version 2 (#274)

The exports sit outside artifact tracking (no status row or stale flag of their own), so **version 2**
stamps provenance into the envelope and both plans:

| Field | Type | Meaning |
|---|---|---|
| `source_revision` | int | the model revision this export was rendered from — the same revision the paired `epic.md` save recorded |
| `slug` | string | the session it belongs to (already implicit in the plans' idempotency label; explicit here so the neutral envelope is self-identifying) |

The v2 skeleton is pinned beside v1 ([compatibility.md](compatibility.md)). How a consumer uses it:

- **`source_revision` identifies; it never judges.** Comparing it against the session's current
  revision and concluding "stale" is exactly the inference the staleness model exists to replace —
  a model change that misses the epic's dependencies leaves the epic fresh.
- **The verdict is `status --json` → `artifacts.epic.stale`.** The exports are written at the same
  instant, from the same generated `Epic`, as `epic.md` — so the tracked epic's stale flag is their
  stale flag.
- The flow's guard, in order: read `epic.json` → check `format`/`version` → read `status --json` →
  if `artifacts.epic` is missing or `artifacts.epic.stale` is true, regenerate
  (`requivo epic <slug> --export-json --github`) before creating anything → confirm the fresh
  export's `source_revision` equals `artifacts.epic.revision`.

## A worked n8n flow: client email → discovery → GitHub issues

Assumes self-hosted n8n (the Execute Command node needs a shell) with `requivo` installed in the
n8n container or a sidecar it can reach, a mounted `/data/requivo` workspace, and
`ANTHROPIC_API_KEY` provided per run from an n8n credential.

**Flow A — intake (trigger: email received)**

1. **Email Trigger (IMAP)** — a client request arrives.
2. **Set / Code** — derive a ticket slug (`req-4812`) from your ticketing convention.
3. **Read/Write Files from Disk** — write the email body to `/data/requivo/inbox/req-4812.txt`.
   The file stem is the session slug: choose it slug-shaped (lower case, hyphens).
4. **Execute Command** — one paid discovery pass:

   ```
   requivo --workspace /data/requivo discover /data/requivo/inbox/req-4812.txt --once
   ```

   Branch on the exit code. `0`: continue. `1`: the request is captured at revision 0 and nothing
   was billed for a drafted model — schedule a retry of the same command; it is safe to repeat.
5. **Execute Command** — the machine read:

   ```
   requivo --workspace /data/requivo status req-4812 --json
   ```

6. **IF `readiness.ready` is false** — email the client back with `questions[]` (each entry
   carries the question text and the topic it targets), and stop. The session waits on disk.
7. **IF true** — jump to Flow C.

**Flow B — the client replies (trigger: reply received, slug recovered from the thread)**

1. **Execute Command** — fold the answers in (one paid call):

   ```
   requivo --workspace /data/requivo answer req-4812 "HR must sign off above 10 days; the legacy tool stays the balance source of truth for the pilot."
   ```

2. Re-run the `status --json` gate from Flow A, step 5 — loop to questions or fall through to
   Flow C.

**Flow C — generate and create the issues (readiness reached)**

1. **Execute Command** — one paid call; writes the tracked `epic.md` and the machine views:

   ```
   requivo --workspace /data/requivo epic req-4812 --export-json --github
   ```

2. **Read File** — `/data/requivo/.requivo/sessions/req-4812/artifacts/epic.github.json`.
3. **Freshness guard** — as specified above: `status --json`, check `artifacts.epic.stale`; on v2
   envelopes, record `source_revision` alongside the created issues.
4. **GitHub: search issues** — label `requivo-epic:req-4812`. Found → skip/update, don't duplicate.
5. **GitHub: resolve milestone** — plan's `milestone` name → milestone number.
6. **GitHub: create issues** — iterate `issues[]` in order (`title`, `body`, `labels`,
   `milestone`), collecting `ref → issue number`.
7. **GitHub: create the tracking issue** — `tracking_issue` as-is (the task list is titles;
   optionally rewrite entries to `#<number>` links from the map).
8. **Slack** — post the tracking issue link and the envelope's `open_questions` for a human.

The GitLab variant differs at steps 2–7 only: read `epic.gitlab.json`, use `description` fields,
and wire `links[]` as issue links (`blocks`) after creation.

## Watching a session for change (polling)

There is no push channel, on purpose (next section). What exists is better than it sounds:

- **The cursor is `revision`.** Poll `status <slug> --json` on a Schedule trigger; compare
  `revision` with the last value your flow stored (n8n static data). It is monotonic per session.
- **The verdicts are the flags.** `readiness.ready` flipping, or any `artifacts.<type>.stale`
  turning true, is the actionable change — e.g. "the PRD this flow published to Confluence is now
  stale; regenerate and republish."
- **Sweep a workspace** with `session list --json` (exit 4 = the listing is complete but degraded;
  consume stdout and flag it).
- **What changed, precisely**, when you hold two revision numbers: `requivo model diff` between the
  frozen revisions answers offline.

## Outbound events: what OSS ships, and what it deliberately does not

**OSS ships no webhook delivery mechanism.** Three structural reasons, not one policy reason:

1. **There is no daemon.** The CLI is a process that exits; the web app is request-driven. Delivery
   needs a worker that outlives requests — retries, backoff, a dead-letter store, secret
   management. That is hosted-product infrastructure, and it belongs to the hosted product.
2. **A webhook fired from inside a verb couples a paid operation to a third party's uptime.** An
   HTTP failure must never fail, delay, or double-run `answer`.
3. **The primitive already exists.** The revision log in `session.json` is ordered, timestamped,
   provenance-stamped and monotonic, and every applied model is frozen under `revisions/`. Events
   are therefore a **pure derivation** — the diff between two frozen revisions, the readiness on
   each side, the artifacts a change flagged — recomputable offline, on read. A derived stream
   cannot drift from the state it describes; a second, stored copy of the truth could.

So the sequence is:

- **Today**: poll `status --json` (previous section). Zero new code; it is enough for
  deploy-and-notify, stale-artifact, and readiness-gate flows.
- **Later, with the HTTP API and demonstrated need** (#436 — two real flows that polling could not
  serve, the same bar this repo applies to every new guard tier): a **read-only events derivation** —
  `requivo events <slug> --since-revision N --json` and `GET /api/sessions/{slug}/events` — computed
  from the revision log, the frozen revisions and `artifact_status` at read time. Not an outbox
  table, not a queue, not a daemon.
- **Delivery is cloud.** The hosted product wraps the same derivation with a store and a worker,
  under the posture below.

### The event envelope (design, shared by the derivation and any future delivery)

Types, each anchored to a real service-layer fact: `session.created` (a session claimed),
`discovery.completed` (the first apply, revision 0→1, a discover surface), `revision.applied`
(every validated apply — carries the diff), `session.rescoped` (a revision whose model hash did not
move), `artifact.saved`, `artifact.went_stale` (attributed to the revision whose diff flipped it),
`readiness.changed`.

```json
{
  "format": "requivo-event",
  "version": 1,
  "id": "7f445b8f312c4b719ceeb9fce8d7548c:3:revision.applied",
  "type": "revision.applied",
  "ts": "2026-09-01T17:31:13Z",
  "session": {"slug": "req-4812", "session_id": "7f445b8f312c4b719ceeb9fce8d7548c"},
  "revision": 3,
  "surface": "cli-answer",
  "payload": {
    "changed_slots": ["business_rules", "permissions"],
    "stale_artifacts": ["prd", "epic"],
    "readiness": {"ready": false, "blocking_slots": ["acceptance"]}
  },
  "ref": {"model": "revisions/0003-model.json"}
}
```

- **`id` is deterministic** — `<session_id>:<revision>:<type>` — because events are derived, not
  minted: every read of the same history yields the same ids, dedupe is a set lookup, and an event
  seen by polling and later by push is recognisably one event.
- **`ts` is the revision's `created_at`** — the time the fact happened, not the time it was read.
- **The payload is thin and the model travels by `ref`.** Request text and model content are a
  client's confidential material; they do not belong in third-party webhook logs. A consumer
  fetches the referenced revision with its own credential.
- Honest limit: `artifact_status` records current state — a re-saved artifact overwrites its row —
  so `artifact.saved` is derivable only for each type's latest save, and the derivation says so
  rather than inventing history.

### Webhook delivery posture (constraints for the hosted product — not OSS code)

Recorded now so the envelope above survives delivery unchanged:

1. **Signing**: HMAC-SHA256 over `"<t>.<raw body>"` with a per-endpoint secret;
   `Requivo-Signature: t=<unix>,v1=<hex>`; reject when `|now − t| > 300s`; constant-time compare;
   two active secrets during rotation.
2. **Idempotency**: consumers dedupe on the envelope `id` (deterministic, mode-independent);
   `Requivo-Delivery: <uuid>` identifies the attempt, for support and log correlation only.
3. **Ordering**: promised per session, by `revision`, and nothing more. Consumers reorder on
   `(session.slug, revision)` and tolerate replay.
4. **Retries**: at-least-once; any 2xx acks; exponential backoff, bounded (on the order of 8
   attempts over 24h); then a dead-letter the operator can see and re-drive.
5. **Payloads stay thin** (see above) — fetch-on-receipt with the consumer's credential.
6. **Egress hygiene**: HTTPS only, redirects not followed, response bodies ignored beyond the
   status, short timeouts.
7. **Versioned like `requivo-epic`**: `format` + `version`, skeleton pinned per version, bumps
   announced.

## The HTTP API (experimental)

`requivo api serve` (the `[api]` extra; see [cli.md](cli.md#local-http-api-experimental)) is a local
REST facade over the same services, under `/api/v1`, with the OpenAPI document at `/docs`. Its bodies
are the existing `--json` payloads and the `{code, message, details}` error envelope, so a flow
migrates by swapping Execute Command nodes for HTTP Request nodes without changing its parsing. It is
not frozen: the design and the freeze conditions are `decision: the-http-api-facade`. An events
read (`--since-revision N`) stays behind the demonstrated-need gate above.

## Distribution sequence for n8n users

1. **Now — no Requivo code**: this page, plus an importable example workflow (the three flows
   above) under `examples/` (#437). Works with n8n's stock Execute Command / HTTP Request / GitHub nodes.
2. **Next — the MCP façade** (#438), before any n8n community node (the recorded distribution
   decision — `decision: the-http-api-facade`).
   One façade serves Claude Code, agent frameworks, and n8n's own MCP client support.
3. **Then — a community node** (`n8n-nodes-requivo`: declarative; resources `session`,
   `artifact`; a credential type carrying base URL + bearer token). Gated on measured stability,
   not a date: the API marked stable in compatibility.md, at least one minor release with zero
   breaking API changes, and at least two external flows observed using it. A declarative node
   hardcodes endpoints and payload shapes — built earlier, it is a consumer that breaks silently or
   freezes the API out of fear.
4. **Last — a trigger node**, only once the events derivation exists to poll.

## What is deliberately not here

- **Authenticated push** to GitHub/GitLab from Requivo itself — the flow owns credentials and
  writes; Requivo produces the plan. That keeps a paid generator from ever holding a tracker token.
- **Webhook delivery from OSS** — see above; the revision log is the outbox, derivation is the
  primitive, delivery is the hosted product's job.
- **A Jira adapter** — on the roadmap; it will be another pure `to_<tracker>()` over the same
  neutral envelope, and everything on this page about idempotency and freshness will apply to it
  unchanged.

