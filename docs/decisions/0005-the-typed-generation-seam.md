# The typed generation seam

**Slug:** `typed-generation-seam`

## Context

`DiscoveryService.generate()` dispatches on a runtime string: `provider.generate(artifact_type, …)`.
No single code path can promise a single contract, so for a release the seam said exactly that —
`Generated.artifact` was a bare `object`, and `_WRITERS` was an untyped dict literal.

That was correct about what the implementation can prove and useless to every caller. Every call
site in `cli.py` reading `result.artifact` was an unchecked `object` use, and pyright reported eight
errors across the package that no amount of care at the call sites could remove.

Three separate facts about **pyright's inference** came out of fixing it (#271), and none of them is
reachable by a test in this repository: they are statements about what a type checker concludes, not
about what this code does at runtime. `tests/` cannot go red for any of them. Recorded here for that
reason, per this directory's own first shape — *a fact about something outside the repository* — and
its second, since two of the three are alternatives that were tried and rejected.

## Decision

Three, taken together, because each alone leaves the seam untyped:

1. **`Generated` is generic**, and its type parameter is resolved by `generate()`'s overloads rather
   than by the class.
2. **`generate()`'s public signature is six `@overload`s**, not the implementation. Five are keyed by
   `Literal` on the artifact types that save a document; the sixth takes a plain `str` and returns
   `Generated[object]`.
3. **`_WRITERS` is annotated `dict[str, Callable[[Any], str]]`**, deliberately rather than left to
   infer.

The effect is that a call site written with a literal string — which is every call site in this
codebase today — gets the right contract back with no cast anywhere the caller can see:
`disco.generate(slug, "prd")` is `Generated[PRD]`.

## What breaking it cost

Eight pyright errors, and an `object`-typed `artifact` that every caller in `cli.py` used unchecked.
The concrete shape of (3) is worth stating because it is the counter-intuitive one: each writer's own
signature is narrow (`prd_markdown(prd: PRD)`, `criteria_markdown(ac: AcceptanceCriteria)`, …), so an
untyped dict literal infers a *union* of four narrow callables, and calling `writer(...)` then demands
an argument assignable to all four contracts at once — which nothing is. `Any` is the honest static
type at that one dispatch point: which writer an `artifact_type` string resolves to is a runtime fact
`_WRITERS` encodes, not one pyright can see through a dict lookup.

The Types leg is what goes red if any of the three is undone. That leg is a CI check and not a
pytest, which is the whole reason this file exists instead of a test docstring.

## Alternatives rejected

- **A plain `Union` of the five contracts on `Generated.artifact`.** It moves the cast to every call
  site instead of removing it: `render_brief(gen.model, gen.artifact)` still needs `gen.artifact`
  narrowed from the union to `Brief` before it type-checks. `Literal`-keyed overloads let each call
  site's own string argument do that narrowing for free.
- **Leaving `artifact` as `object`.** The state this replaced. Honest about the implementation and
  silent about the seven-eighths of call sites that do know their type statically.
- **Dropping the plain-`str` overload.** A caller that holds the type name in a *variable* at that
  point — `web/routes/artifacts.py`'s `generate_artifact`, whose type comes from a route parameter —
  cannot be narrowed by `Literal` matching. Without the sixth overload that call would not resolve at
  all; with it, it gets `Generated[object]`, exactly what it had before this change and exactly as
  much as a runtime-chosen type can honestly promise.
- **A test asserting pyright's output.** That is a new source-scanning guard tier, which `CLAUDE.md`
  funds only from two named instances of the drift it would have caught (#288). There are none.
