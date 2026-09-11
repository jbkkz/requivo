# A type-checking-only alias for the optional SDK class

**Slug:** `type-checking-only-anthropic-alias`

## Context

`client.py` binds `Anthropic` to `None` at runtime when the `anthropic` extra is not installed
(invariant: the deterministic core + CLI must work with no SDK present). `new_client()` still needs
to say, in its own signature, that it returns a real client. Annotating it `-> Anthropic` looks like
the obvious fix and fails a static check: pyright merges every binding of one name into a single
declared type across the whole module, `if TYPE_CHECKING:` branches included, so importing the real
`Anthropic` under `TYPE_CHECKING` too does not narrow anything — the name still widens to
`type[Anthropic] | None`, because the runtime branch's `None` binding is part of the same merge.

This is a fact about pyright's inference, discovered while closing #271. That issue's own commit
message names it directly as one of "two narrower gaps" fixed in `providers/anthropic/` alongside the
issue's main body of work (widening `[tool.pyright]`'s include to the whole package) -- the other
narrower gap is a `dict[str, object]` annotation on a literal in `completion.py`, too short a comment
to need this treatment. Distinct from the three facts `0005-the-typed-generation-seam.md` records
about the same issue: those concern `DiscoveryService.generate()`'s return typing, this concerns an
SDK-optionality return annotation, and neither record's count of "facts from #271" should be read as
covering the other's. Nothing in this repository's test suite can go red for how a type checker
merges bindings — that is `tests/`' own limit, stated in `docs/decisions/README.md`'s first shape.

## Decision

Import the real class under a **different** name, `_AnthropicClient`, inside the same
`if TYPE_CHECKING:` block, and annotate `new_client()`'s return with that name instead of
`Anthropic`. A distinct name has no binding to merge with, so it stays exactly `type[_AnthropicClient]`
regardless of what the runtime `try`/`except` block binds `Anthropic` itself to. The block is free at
runtime (`TYPE_CHECKING` is `False`, so it never executes) and costs pyright nothing extra to resolve
(it is always `True` for the checker).

`new_client()`'s actual `return` statement wraps the resolved client with
`cast("_AnthropicClient", client)`, a string forward-reference rather than the bare name, because
`_AnthropicClient` does not exist at runtime and a bare reference would raise `NameError` the moment
that line executed. `cast()`'s first argument is never evaluated as a value — it only has to parse as
an expression — so the quoted string satisfies pyright without the module needing the name to exist
outside `TYPE_CHECKING`.

## What breaking it cost

Nothing shipped broken — this was caught by the Types (pyright) CI leg before merge, as
`reportInvalidTypeForm` on `new_client()`'s own return annotation. The cost of getting it wrong is
therefore the CI leg going red on a change that looks like a plain type-hint fix, for a reason the
diff itself does not explain (pyright's cross-branch merge behaviour is not visible from the two
lines that trigger it).

## Alternatives rejected

- **Annotate with the real `Anthropic` name, imported once, unconditionally.** This is what a reader
  reaches for first and it is exactly the case that fails: the runtime `except ImportError` branch's
  `Anthropic = None` assignment is a second binding of the same name, and pyright's merge does not
  care which branch executes.
- **A runtime-real class as a lightweight protocol/stub, defined once and used everywhere.** Rejected
  as more surface than the problem needs: the only place this annotation matters is `new_client()`'s
  return type, and a second name scoped to `TYPE_CHECKING` costs one import line where a stub class
  costs a maintained shadow of the SDK's shape.
