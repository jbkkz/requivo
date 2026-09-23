# A failed reply's debug dump uses the ambient workspace root

**Slug:** `debug-dump-ambient-root`

## Context

`_save_failed_reply` (`providers/anthropic/completion.py`) writes a reply that never validated under
`.requivo/debug/` for a bug report. #272 made the workspace root constructor state on
`Store`/`FileSessionRepository` so two callers in one process can address two workspaces. But
`_save_failed_reply` sits deep in `_complete()`'s give-up path, which knows the failed contract and
nothing about the session or repository behind the call; the provider is built once per process and
has no workspace of its own.

## Decision

`_save_failed_reply` calls `debug_root()`, the ambient-default wrapper #272 kept for callers with no
rooted repository to ask. `.requivo/debug/` is a human-read diagnostic, not session data, so using
the process's ambient workspace is an accepted, documented limitation.

## What breaking it cost

Nothing observed. The accepted cost: in a process serving several workspaces — the shape #272
unblocks — a dump lands under the ambient root, which may not be the triggering session's, and a
user told to attach `.requivo/debug/<file>` finds it missing.

## Alternatives rejected

- **Thread a root through `_complete()` and every generator** — widens the one function every call
  funnels through, for a side channel that fires only when a reply fails to parse.
- **An `AnthropicProvider` per workspace** — the provider is process-scoped by design; rebuilding it
  per call is a wider change than a debug path justifies.
