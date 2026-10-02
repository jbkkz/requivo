# n8n example workflows

Three importable workflows for the automation contract in [../../docs/integrations.md](../../docs/integrations.md#a-worked-n8n-flow-client-email--discovery--github-issues):

| File | Flow |
|---|---|
| `a-email-intake.json` | email received, request written to `inbox/req-N.txt`, `discover --once`, `status --json` gate |
| `b-reply-answer.json` | reply received, `answer`, the same `status --json` gate |
| `c-epic-to-github.json` | freshness guard, `epic --export-json --github`, idempotency search, milestones, issues, tracking issue, Slack |

Import each with *Workflows, Import from file*. All three are inactive on import. The data in them is
synthetic (`req-4812`); no real address or tracker appears.

## Prerequisites

- **Self-hosted n8n.** The Execute Command node needs a shell, so n8n Cloud cannot run these.
- `requivo` installed where n8n runs (in its container, or a sidecar it reaches), and a workspace
  mounted at `/data/requivo`. Every command passes `--workspace /data/requivo`; change that path in
  the Execute Command nodes if yours differs.
- `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, because the flows read `$env.*` (below).

## Credentials: environment names only

No credential is in any workflow JSON, and the nodes export without a `credentials` block. Set these
in n8n's own process environment:

| Name | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | `requivo` itself, inherited by every Execute Command node (A, B, C) |
| `GITHUB_TOKEN` | flow C, a token allowed to create issues and milestones |
| `GITHUB_REPOSITORY` | flow C, `owner/name` of the target tracker |
| `SLACK_WEBHOOK_URL` | flow C, the incoming-webhook URL |
| `REQUIVO_REPLY_FROM` | A and B, the sender address of the questions email |

The IMAP trigger and the email-send node use n8n's own credential store: pick yours in the editor
after import. Nothing about them is exported.

## How the contract is used

- **Machine reads only.** `status <slug> --json` is the only stdout a flow parses. Each paid verb
  (`discover`, `answer`, `epic`) has its stdout discarded and only its **exit code** is read, so a flow
  never depends on human-readable text. A non-zero exit goes to a Stop and Error node.
- **Re-running is safe.** A failed `discover` leaves the request captured at revision 0 and re-running
  the same command retries cleanly; a duplicate trigger is refused before paying.
- **Shell safety.** A slug is built from digits only (`req-<n>`) and flow C refuses anything but
  `[a-z0-9-]`. The reply text of flow B reaches the shell only as one single-quoted word.
- **Freshness.** `source_revision` in the export says which revision it was rendered from; it never
  judges. Flow C regenerates the epic unless `status --json` reports `artifacts.epic.stale` false, then
  checks that the export's `source_revision` equals `artifacts.epic.revision`. It never compares
  revision numbers to decide staleness.
- **Idempotency.** Flow C searches the tracker for the plan's `requivo-epic:<slug>` label and skips
  when anything is found.
- Flow C starts from a Manual Trigger with a `req-4812` default; feed `slug` from flow A or B, or swap
  in a Schedule or Webhook trigger.

## Checking an import

CI parses these files and checks their wiring, never an import into n8n. Before relying on a flow,
import it into your n8n, point `GITHUB_REPOSITORY` at a scratch repository and run it once.
