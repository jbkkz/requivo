# A failed reply's debug dump uses the ambient workspace root

**Slug:** `debug-dump-ambient-root`

## Context

`_save_failed_reply` (`providers/anthropic/completion.py`) writes the raw reply that never validated
under `.requivo/debug/` so a bug report has something to attach. #272 turned the workspace root from
an ambient, process-wide default into constructor state on `Store`/`FileSessionRepository`, precisely
so that two callers in one process can address two different workspaces without racing each other's
environment.

`_save_failed_reply` is called from deep inside `_complete()`'s retry-give-up path, which knows the
contract that failed to parse and nothing about which session, or which repository's root, triggered
the call. Threading an explicit root down to it would mean either constructing the Anthropic provider
per-workspace (the provider is built once per process/client and reused across whatever session it is
next asked to reason about — it has no single workspace of its own to carry as constructor state), or
widening `_complete()`'s signature with a root parameter that every call site — discovery, every
generator, the golden harness — would have to thread through for the sake of one failure path nothing
else on the success path needs.

## Decision

`_save_failed_reply` calls `debug_root()`, the ambient-default wrapper #272 kept for exactly this
kind of caller: one that legitimately has no explicitly-rooted repository to ask. `.requivo/debug/` is
a human-read diagnostic aid, not part of any session's data, so addressing the *process's* ambient
workspace rather than whichever session's repository triggered the call is accepted as a known,
documented limitation rather than fixed or silently left unstated.

## What breaking it cost

Nothing observed yet. The accepted cost is stated so it can be recognised if it ever is hit: on a
process serving more than one workspace at once — the exact shape #272 exists to unblock — a failed
reply's debug dump lands under the *ambient* root, which may not be the root the triggering session
used. A user attaching `.requivo/debug/<file>` to a bug report from the wrong workspace would see a
directory that exists but does not contain the file they were told to attach.

## Alternatives rejected

- **Thread an explicit root through `_complete()` and every generator that calls it.** Rejected as
  disproportionate: it widens the signature of the one function every provider call funnels through,
  for the sake of a diagnostic side channel that only fires when a reply fails to parse at all.
- **Construct `AnthropicProvider` per workspace.** Rejected for the same reason #272's scope amendment
  rejected it elsewhere: the provider is process-scoped by design, reused across whatever session it
  is asked to reason about next, and re-constructing it per call is a wider change than a debug-only
  side channel justifies.
