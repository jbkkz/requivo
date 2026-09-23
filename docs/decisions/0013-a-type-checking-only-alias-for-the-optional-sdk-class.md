# A type-checking-only alias for the optional SDK class

**Slug:** `type-checking-only-anthropic-alias`

## Context

`client.py` binds `Anthropic = None` when the optional SDK is absent, so the core and CLI work
without it. `new_client()` still has to say it returns a real client. `-> Anthropic` fails: pyright
merges every binding of one name across the module, `if TYPE_CHECKING:` branches included, so the
name widens to `type[Anthropic] | None`. A fact about pyright found closing #271 (commit `de80961`),
separate from `decision: typed-generation-seam`; no pytest can go red for it.

## Decision

Import the real class under a **different** name, `_AnthropicClient`, in the `TYPE_CHECKING` block,
and annotate `new_client()` with it — a distinct name has no binding to merge with. The return wraps
the client in `cast("_AnthropicClient", client)`: quoted, because the name does not exist at runtime
and `cast()` never evaluates its first argument.

## What breaking it cost

Nothing shipped: the Types leg caught it as `reportInvalidTypeForm` before merge — red on what looks
like a plain type-hint fix, for a reason the diff does not show.

## Alternatives rejected

- **The real `Anthropic` name imported unconditionally** — the `except ImportError` branch's `None`
  is still a second binding of that name.
- **A runtime stub or protocol** — a maintained shadow of the SDK's shape, where one import line
  under `TYPE_CHECKING` suffices.
