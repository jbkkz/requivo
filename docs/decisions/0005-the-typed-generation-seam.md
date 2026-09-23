# The typed generation seam

**Slug:** `typed-generation-seam`

## Context

`DiscoveryService.generate()` dispatches on a runtime string, so `Generated.artifact` was a bare
`object` and `_WRITERS` an untyped dict: honest about the implementation, useless to callers — every
`result.artifact` in `cli.py` was an unchecked use, and pyright reported eight errors. The three
facts below (#271) are about what a type checker concludes, not about runtime behaviour; no pytest
can go red for them, only the Types CI leg.

## Decision

Three, together, because each alone leaves the seam untyped:

1. **`Generated` is generic**, its parameter resolved by `generate()`'s overloads.
2. **`generate()` has six `@overload`s**: five keyed by `Literal` on the saved artifact types, a
   sixth taking a plain `str` and returning `Generated[object]`. `disco.generate(slug, "prd")` is
   `Generated[PRD]` with no cast visible to the caller.
3. **`_WRITERS` is annotated `dict[str, Callable[[Any], str]]`.** Left to infer, a dict of narrow
   writers (`prd_markdown(prd: PRD)`, …) is a *union* of callables, and calling one demands an
   argument assignable to every contract at once. `Any` is the honest type at that one runtime
   dispatch point.

## What breaking it cost

Eight pyright errors and an `object`-typed artifact used unchecked across `cli.py`. Undoing any of
the three turns the Types leg red.

## Alternatives rejected

- **A `Union` of the contracts** — moves the cast to every call site instead of removing it.
- **Leaving `object`** — the state this replaced.
- **Dropping the `str` overload** — `web/routes/artifacts.py` holds the type in a route parameter,
  which `Literal` cannot narrow; without the sixth overload that call does not resolve.
- **A test asserting pyright's output** — a new guard tier, with no named instance of the drift.
