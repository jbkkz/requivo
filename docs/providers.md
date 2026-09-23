# Providers

> The Core never calls an LLM. A **provider** does. Today there is one: Anthropic.

## The Anthropic provider

Automated discovery and generation (the CLI's `discover` / `answer` / generators, and the Web's
provider actions) call the Claude API through the provider. It is an **optional extra**:

```bash
pip install 'requivo[anthropic]'     # or:  uv tool install 'requivo[anthropic]'
export ANTHROPIC_API_KEY="…"
```

The key is read from the environment (or `.env`) and used only to authenticate those calls. In **Claude
Code** mode there is no provider and no API key — Claude reasons in your session; the deterministic CLI
applies. `requivo demo`, `requivo status` and `requivo impact` make no API call at all.

Requivo refuses early, in one line and before anything is claimed or billed, when no credential is
available — but **what counts as a credential is the SDK's answer**: `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_PROFILE`, workload identity federation, the active profile on disk (#334). If a bare
`Anthropic()` authenticates in your shell, Requivo does. `requivo doctor` reports what it resolved.

## Models

Developed and measured against `claude-sonnet-5` (the default). Any current Claude model works via the
`REQUIVO_MODEL` environment variable:

```bash
REQUIVO_MODEL=claude-opus-4-8 requivo run "…"
```

Bare `MODEL` still works as a deprecated fallback
([compatibility.md](compatibility.md#environment-variables--stable-with-one-exception)); `REQUIVO_MODEL`
always wins when both are set.

The exact model a session ran against is recorded in its revision provenance (see
[session-format.md](session-format.md)).

## What a run costs

You pay Anthropic directly, on your own key, and these are the numbers to expect before you spend
anything. `requivo demo` is free and needs no key at all.

| Step | Calls | Input tokens | Output tokens | Estimated cost |
|---|---|---|---|---|
| One provider call | 1 | 8,100–13,700 | 1,300–3,800 | **$0.03–$0.07** |
| A full interactive discovery (8 turns + the assessment) | 9 | — | — | **$0.26–$0.59** |
| Every remaining artifact (prd, stories, estimate, criteria, epic, release) | 7 | — | — | **$0.20–$0.46** |
| A complete session, end to end | 16 | — | — | **$0.47–$1.05** |

Priced at **$2.00 / $10.00 per million tokens** (input / output) for `claude-sonnet-5`, rates as of
**2026-08-29**. An estimate, never a bill. The rate table is `providers/anthropic/pricing.py`; the figures
here are arithmetic over it at the date shown, and a price change is the cue to redo them.

Two limits on these figures:

- **Tokens are estimated at four characters per token**, over the real assembled system prompt plus
  the resolved model each call attaches (every generator and every discovery turn after the first
  sends the model), measured from the replies captured in `fixtures/golden/`. They are not API counts.
- **Every call is charged at full price.** Prompt caching (below) makes a sitting cheaper than the
  table says; the retry path makes a rare call dearer. The exact figure for *your* request is printed
  by the verb that spent it.

## The usage footprint

A discovery is a few calls (one per turn, up to 8) plus one per generated artifact. The system prompt
is two blocks on the wire, cached on different terms (the rules are CLAUDE.md's *The runner*):

- **The shared block** — the schema and the context cards, ~9k tokens with the bundled cards — is
  sent first behind a `cache_control` breakpoint on every call, so each later operation in a sitting
  reads it at ~0.1x: a five-operation pipeline sends ~25k system tokens instead of ~56k (#258). The
  entry lives five minutes; a lone verb pays the 1.25x write once (~2.3k token-equivalents). A
  different `--context` selection starts its own entry. Below the model's minimum cacheable prefix
  (1,024 tokens on `claude-sonnet-5`) the API silently caches nothing and prices plain input.
- **The op-specific remainder** is cached only when the caller will send it again (`reuse_system`) —
  a discovery's turns, a golden capture's K runs. A one-call verb does not cache it, which saves the
  flat ~25% write surcharge (#9); the price is paying 2.0x instead of 1.35x on the rare JSON retry
  (`decision: retry-regression-under-reuse-system-false`). An implementation without caching may
  ignore the flag.

Every command that hits the API prints its footprint — calls, tokens (with the cached share),
latency, and an estimated cost from a dated rate table. The Web reports the same (#253): in the
fragment a paid action answers with, or, after a redirect, in the `requivo.web` log, through the same
`UsageLedger`.

## Adding a provider

A provider implements the `ReasoningProvider` protocol in `providers/base.py`:

| Member | Is |
|---|---|
| `name` | an attribute — short identity of the implementation (`"anthropic"`), stamped on the session |
| `analyze(request, current_model=…, answers=…, only=…, reuse_system=…)` | a validated `EngineOutput` — one discovery turn |
| `generate(artifact_type, model, only=…, **kwargs)` | the typed contract for that artifact |
| `model_name()` | the reasoning model, recorded on the session |
| `provenance(op, only=…)` | provider / model / prompt identity, recorded on each revision |

`name` is an attribute because `DiscoveryService` reads it when it claims the session, before any
reasoning — an implementation without one fails on its first `discover`. The protocol is
`@runtime_checkable`: `isinstance(p, ReasoningProvider)` checks members are **present** (not their
signatures); `issubclass()` raises `TypeError`, as for any protocol with a non-method member. Nothing
in Requivo runs it — it is a self-test for an implementation.

`analyze` returns a *resolved* model: a refinement reply omits the reasoning collections, so a provider
parses a `ModelProposal` and resolves it against the `current_model` it was given (invariant 10). Skip
that and the apply path faithfully stores an empty reasoning layer.

Two things a second implementation needs are deliberately **not** in the Anthropic package, so
nothing has to import a competitor's module to reach them (#167):

| Need | Where |
|---|---|
| A transport failure the surfaces already catch (`provider_unavailable`, 502 on the web) | `providers/errors.py` — `EngineError`, no SDK behind it |
| Recording what a call spent | `requivo.usage` — `CallRecord`, `UsageLedger`, `track_usage`, `record_call` |

The ledger holds **no price table**: each record carries the `rate_per_mtok` and `priced_as_of` the
provider stamped when filing it, so an implementation brings its own rates and an estimate spanning a
price change is right on both sides. A provider that prices nothing leaves both absent, and the CLI
says *no price on file*.

`DiscoveryService` talks to the protocol and nothing else, so *pointing it at* a second provider is a
constructor argument rather than a fork of the orchestration:

```python
DiscoveryService(MyProvider()).start("A leave approval system.")
```

`AnthropicProvider(client=…, model=…)` takes an optional fixed model id (#434) that wins over the
`REQUIVO_MODEL`/`MODEL` chain with no env read, so two instances run two models in one process (see
[cloud-boundary.md](cloud-boundary.md#3-the-upstream-change-set)).

**That is the cost of the swap, not of the implementation** (#273): roughly 400 lines under
`providers/anthropic` are vendor-free orchestration — message builders, `_GENERATORS`/`_OP_PROMPTS`,
`prompt_version()`, JSON extraction, contract validation, the retry loop, the truncation policy — that
a second implementation must copy. Extracting it is deferred with a trigger
(`decision: deferring-the-neutral-provider-layer`); budget against that, not the constructor.

Provenance comes from the provider rather than being assembled by the service, so a revision produced
by another implementation is stamped with *its* name and *its* prompt hash — nothing hard-codes
`"anthropic"`. `tests/test_sessions.py` runs a whole discovery through a provider that has no vendor
behind it; that test is what keeps the seam honest. The Core stays provider-free either way.
