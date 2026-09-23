# Architecture

> How Requivo is put together. For the model itself, see
> [requirements-model.md](requirements-model.md); for storage, [session-format.md](session-format.md).

Requivo is a provider-independent **Core** with interchangeable interfaces on top. The reasoning is a
single LLM call per turn, and that call lives in a **provider**, never in the Core.

```text
       Web          Claude Code        CLI / API
   (the product)   (an integration)  (infrastructure)
         \               |                /
                    Requivo Core
            validated, versioned model
```

Every interface reads and writes the **same session format** and goes through the **same validated
apply path** — no fork, no interface holding business logic of its own. See "Building on Requivo as a
library" below for the package's stable import surface.

## Layers

The code is the `requivo` package under `src/`. The layers form a strict DAG:

- **`core/`** — the deterministic engine. No LLM, no provider, and no argv, standard streams,
  environment or process exit — all of it enforced by `tests/test_source_form.py`, which walks the
  package recursively and fails rather than passing when it finds nothing to scan. Reading and
  writing files *is* core's job. It validates, versions and reasons over the model; it never *produces*
  one. Holds the Pydantic contracts, validation, readiness, the dependency graph, persistence and the
  structured error hierarchy.
- **`providers/`** — the only place an LLM is called. The `anthropic/` package (behind the optional
  `requivo[anthropic]` extra) turns a request into a model and a model into an artifact:
  `client.py`, `pricing.py`, `completion.py`, `generators.py`, `provider.py`. `errors.py` beside it
  is the seam's own failure type and holds no vendor code. What a run *cost* is not a provider
  concept and lives outside this package entirely, in `requivo.usage`.
- **`services/`** — the application seam shared by every interface. `SessionService.update_model` is
  the single validated apply path (validate → diff → propagate → revision → stale-flag).
  `DiscoveryService` is the provider-backed orchestration (start / answer / generate, plus
  `claim_session` and the un-persisted `draft_turn` an interactive loop takes and repeats) the CLI and Web both call, so there is one pipeline, not two. Storage is injected as a `SessionRepository`
  (`FileSessionRepository` today; Postgres-swappable). Its workspace root is constructor state
  (#272): `FileSessionRepository(root=...)` is fixed for the instance's lifetime; `root=None`, the
  CLI's default, resolves `REQUIVO_WORKSPACE`/cwd on every call.
- **`render/`** turns data into strings; **`cli.py` + `deterministic/`** are the only layers that
  touch argv/stdout/TTY. `deterministic/` is a package of one module per verb group (`doctor`,
  `sessions`, `model`, `artifacts`, over a `_shared`), composed into the single `register(sub)` the
  CLI binds through. **`web/`** is a thin FastAPI + Jinja2 + HTMX layer over the same services.
- **`streams.py`** owns the *encoding* of stdout and stderr, as `paths.py` owns the environment —
  one place where "what happens when the console cannot represent this character" is answered.
  `cli.app()` calls it once, before anything can print, so a renderer cannot kill a process after the
  paid mutation it reports has landed (#29).

Every interface — the terminal CLI, the Claude Code plugin, the local Web app — is a thin layer over
the same Core. There is no second implementation of the apply path, and none of the generation path
either: a surface owns its input and its rendering, and reaches the provider only through
`DiscoveryService`.

`tests/test_source_form.py` holds both halves (#77, #76): a surface may reach only the provider names
`_SURFACE_PROVIDER_ALLOWLIST` names, keyed by `(file, name)`, and only the store functions its storage
allowlist names, keyed by `(file, function)`, each with its reason — and an entry nothing uses fails as
loudly as a use nothing allowed. A shared seam makes a rule reachable from every path, not applied at
the same point on each: the interactive loop takes `claim_session` itself, before its first paid call,
as `start()` does (#133).

The services are the **integrity boundary**, not the interfaces: an external consumer calls this
layer directly, so a rule the CLI happens to enforce is not enforced. Concretely: context cards are resolved in
`create_session` rather than trusted; `DiscoveryService`'s artifact service defaults to the *session
service's* repository, so a Postgres session store cannot end up paired with a local artifact store;
and a first discovery is refused above revision 0 in the service, not by hiding a button.

## Reading a session before reasoning

Every provider-backed operation reads the session once, through `SessionService.snapshot()`: the
revision, the model *at* that revision, the request and the card selection, all under the session
lock. The lock is released before the call — a call takes minutes and cannot be made atomic — so two
different mechanisms cover two different windows:

| Window | Mechanism |
|---|---|
| Between reading the revision and reading the model | the snapshot's lock — otherwise revision N pairs with the model of N+1 |
| Between reading and writing (the provider call itself) | `expected_revision` — a concurrent change becomes a clean `revision_conflict` |

A mismatch in the first window is undetectable afterwards: the recorded revision is plausible and
describes a different model (#135).

## The three interfaces

- **CLI** — provider verbs (`discover`, `answer`, generators) plus offline deterministic verbs.
- **Claude Code** — Claude reasons in your session and writes a proposal; the deterministic CLI
  validates and applies it. No API key. Lives in `plugins/claude-code/` (not shipped in the wheel).
- **Web** — `requivo web`, a local single-user UI over the services. See [web.md](web.md).

## Building on Requivo as a library

The package ships a [PEP 561 marker](https://peps.python.org/pep-0561/) (`py.typed`) and declares a
small, deliberately stable import surface — the services, the `SessionRepository` and
`ReasoningProvider` protocols, the boundary contracts, the error vocabulary, `requivo.usage` — priced
like every other promise on the [compatibility page](compatibility.md). Pin exactly (`requivo==X.Y.Z`);
everything not on that list, including `providers.anthropic` internals, can move in a minor. Building
a non-file `SessionRepository` (a Postgres backing, most concretely)? `pip install 'requivo[testing]'`
and subclass `requivo.testing.repository_conformance.SessionRepositoryConformance` in your own pytest
suite — the same behavioural proof `FileSessionRepository` and this repo's in-memory fake both run
against, extracted so an external implementation can hold itself to it too.

## Bundled assets

Prompts, the per-perimeter schemas, context cards and the demo payload live inside the package at
`src/requivo/assets/`, so they ship in the wheel and a `pip install` works outside a clone. The Web
interface's templates and static files ship the same way.

## Tuning behaviour

Behaviour is tuned by editing the Markdown/JSON assets (prompts, context cards, schema), not the
Python. Because the engine is non-deterministic, changes are measured with the golden harness — see
[evaluations.md](evaluations.md).
