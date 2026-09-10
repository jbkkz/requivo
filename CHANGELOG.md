# Changelog

All notable changes to Requivo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release opens with a **Highlights** block — at most six single-line, user-facing bullets,
before the `Added`/`Changed`/`Fixed` detail. It is a summary of what already follows, never a
replacement for it: nothing below a Highlights block is trimmed or rewritten because of it. When
cutting a release, write the block for that release's own section before tagging (#229) — the
fragments in `changelog.d/` are the material to summarize from.

## [Unreleased]

## [3.2.0] - 2026-09-10

### Highlights

- A local HTTP API behind a new `[api]` extra — read routes over the same services the CLI and Web use, OpenAPI on and self-hosted, shipping experimental until a named freeze event.
- `requivo session delete` completes the session lifecycle: an irreversible, locked removal that refuses a nonexistent slug rather than half-succeeding.
- A spend ceiling you can set — `SpendPolicy(ceiling_usd=…)` refuses the next provider call once a session's estimated spend reaches it, before the money is spent.
- The all-cards default now states what it will cost before the first paid call, instead of being knowable only by reading the source.
- The engine's output language is a stated rule: questions and the understanding mirror the client's language, while every buildable artifact stays English for dev teams and trackers.
- Windows text is correct end to end — files the store writes no longer gain `\r\n`, and generated documents no longer print a visible `\r` on every line.

### Added

- `requivo session delete <session>` irreversibly removes a session — its directory and its `.requivo/locks/<slug>.lock` file — and refuses a nonexistent slug with the structured `session_not_found` error. The store's own locked removal is now on the `SessionRepository` protocol too, so a non-file backing (a hosted Postgres repository) can implement the identical operation. Requivo Web gained the matching control: a *Delete this session…* affordance in a "Danger zone" on the session page, behind an explicit confirmation page that suggests `session export` first as the undo story — there is no trash. This is the product's erasure primitive: until now nothing removed a session end-to-end, and the only cleanup was an `rm -rf` the docs never suggested, which skipped the lock file entirely (#238).

- The all-cards default now discloses its own cost before the paid call, rather than only being knowable by reading the source. `requivo discover` prints which cards it is about to reason over (and the measured average per-card weight) whenever `--context`/`--cards` was not given; the Web create form's card selector carries a matching one-line hint. Measured on 2026-09-03: the 4 bundled cards total 23,262 bytes (~5.8k tokens) and are 65–78% of every generator's assembled system prompt. This is disclosure only — the all-cards default is unchanged, and a card is loaded exactly as before either way; `docs/context-cards.md` states the measured figures and cites the existing `financial-reporting`/`doc-reapproval` dilution finding (#257).

- The neutral epic export (`epic.json`, plus its `--github`/`--gitlab` tracker plans) now stamps `source_revision` and `slug` into the envelope, bumping `EPIC_EXPORT_VERSION` 1 → 2. These files are written outside `ArtifactService` and carry no staleness row of their own, so the one consumer that cannot exercise judgment about freshness — an n8n flow acting on `epic.json` directly — previously had no signal at all that the session had moved on since the export was written. `source_revision` identifies the basis it was rendered from; it never judges freshness by itself — that verdict is `requivo status --json`'s `artifacts.epic.stale`, per invariant 1. `_cmd_epic` stamps it from the same `Generated.status.revision` snapshot the paired `epic.md` save used, never a second read of the session's current revision (#274).

- Line coverage is now measured: `pytest-cov` joins the `[dev]` extra, a `[tool.coverage.run]` block in `pyproject.toml` points it at `src/requivo`, and the `Test (py3.12)` CI leg gains one `continue-on-error` step that prints a `term-missing` table into its log. Deliberately not a gate — there is no threshold, no `fail_under`, no `omit` list and no report upload, and the step cannot change that leg's pass/fail status: a percentage a pull request must clear invites padding, and a padded number that has passed a check is worse than no number at all. Until now this repository, whose culture is that a claim gets a guard or comes out, was mapping its own coverage by grep. The first table it produced corrected that map: `render/terminal.py`'s `render_stale` was recorded as having zero tests on the strength of a name search over `tests/`, and it is in fact exercised indirectly through the CLI at 7 of its 8 lines — the one uncovered line being an early return no caller in the tree can reach. The table also states its own blind spot rather than leaving one: coverage instruments the pytest process and nothing it spawns, so code exercised only by running the real CLI in a subprocess reads as `Missing` even though a test runs it every time -- `src/requivo/__main__.py`'s `app()` line is the standing example, and both `pyproject.toml` and `CONTRIBUTING.md` say so at the point a reader would otherwise mistake it for a gap. Enabling subprocess tracing was measured and declined: it works, and it costs 83.0s against 53.6s, past the ceiling this change is held to. Running the suite under coverage otherwise costs about 14% of its runtime, measured on one machine, and nothing else in the project changes: the plugin is inert on every run that does not ask for it (#295).

- CI's compatibility-axis matrix gained a sixth leg, `Test (py3.14)`, beside the existing `3.9`–`3.13` five (step 1 of #298). This does not move the supported floor: `requires-python`, `[tool.ruff] target-version`, `[tool.pyright] pythonVersion`, the `tomli` marker and the platform matrix's `3.9`/`3.13` ends are all unchanged — raising the floor off the now-EOL `3.9` is a separate decision pending pypistats download-share data (step 2, not in this change). The new leg is not yet in `main`'s branch-protection required-checks list; that is a manual, documented API call the maintainer makes by hand (#298).

- Added the local HTTP API facade as a new `requivo.api` package behind the optional `[api]` extra (`pip install 'requivo[api]'`) -- slice 1 of #425: `create_api()` builds a FastAPI app with OpenAPI docs on (the `/docs`, `/redoc` and `/openapi.json` routes, with an EXPERIMENTAL notice in the spec description), a `RequivoError -> {code, message}` error handler at the correct HTTP status, `/api/v1/health`, and the read routes for sessions, models, revisions, status, artifacts and a new dependency-graph impact query -- each backed 1:1 by an existing `SessionService`/`ArtifactService` method, with no second implementation of an apply, a generation or a staleness rule. **The whole surface ships experimental**: no path, method, status or response shape here is promised yet, and none of them is a breaking change to move until the freeze event `docs/decisions/0004-the-http-api-facade.md` defines actually lands. Write routes, the bind/token/cross-site posture and the `requivo api serve` CLI verb are later slices of the same issue, not part of this change.

- `DiscoveryService` accepts an optional injected `SpendPolicy(ceiling_usd=...)`, consulted immediately before every provider call. When the session-scoped ledger's estimated USD spend is already at or above the ceiling, the next call is refused with a new structured error code, `spend_ceiling_reached` (HTTP 403), before it is ever constructed — so the ledger shows the refusal cost nothing. A call already sitting in the ledger with no rate on file also refuses, rather than being silently priced at $0. With no policy injected, behaviour is unchanged. This is a service-layer seam only: no CLI flag or web setting wires a ceiling yet (#427).

- `requivo.services.discovery`, `.sessions` and `.artifacts` (plus `requivo.providers.anthropic.completion`) now carry named stdlib loggers, emitting INFO/DEBUG/WARNING records at real seams -- a session created, a model applied to a new revision, a write conflict refused, an artifact saved with its stale verdict, and a provider call started/finished/failed (with its own attempts and latency). No handler is configured anywhere in this package, so a default run is silent exactly as before -- an embedding operator (a hosted API facade, `requivo web` under systemd) attaches a handler to any of these loggers to see them. `CallRecord` (`requivo.usage`) also grows an optional `operation` field, stamped with the same verb vocabulary `cli.py`'s subcommands already use (`analyze`, `brief`, `stories`, `prd`, ...), so a ledger read back after the fact can split spend per verb without reconstruction. Both changes are additive and behaviour-preserving for every existing consumer: `operation` defaults to `None` and nothing currently reads it, `render_usage()`'s output is unchanged, and nothing prints to any stream unless a caller explicitly attaches a `logging.Handler` (#435).

### Changed

- README's How-it-works, `docs/roadmap.md`'s Available-today list, and the canonical example's table now lead with the pre-commitment arc — the decision brief and computed change impact — before any generated document. In README's narrative prose, tracker adapters (GitHub/GitLab issue plans) and release notes are named only once, in a trailing clause after that arc, rather than getting equal billing with the brief — an emphasis fix in the two passages that shape a first read, not a literal single-mention rule for the repo: `docs/roadmap.md`'s own tracker-adapter bullet and Jira-adapter line, and the canonical example's untouched `## Reproduce it` command reference, still name GitHub/GitLab where they always did. The canonical example's table groups the five delivery files (epic, its export, both tracker plans, release notes) under one "delivery outputs" row instead of listing them as steps 6 through 10. README's audience line now leads with the self-serve technical persona (solutions engineers, consultants, technical PMs, agency leads) and states the PM/client hand-off as one sentence, rather than naming Product Managers first for a product every install path requires a shell to reach. Copy only — no verb changed, removed, or hidden in code (#231).

- Requivo's output-language policy is now stated and enforced rather than emergent: the questions and the understanding rendered each turn **mirror the language of the client's request**, while every buildable artifact -- the decision brief, PRD, stories, acceptance criteria, epic and release notes -- is written in **English**, because those feed dev teams and trackers. It is anchored as one sentence in `engine.md`'s output-format block and one identical sentence in the six artifact prompts, written up in `docs/requirements-model.md` ("The language of the outputs"), and corrected in `CLAUDE.md`, whose opening claimed the engine's own output was English throughout. Two edges are deliberately left open and named as such in the write-up rather than decided here. The saved decision brief is **bilingual by construction**: `brief_markdown` is the only writer that also receives the model, and its objective, current understanding, confirmed topics and inferred assumptions are projected from it verbatim -- the design rule being that a restatement can drift from the model while a projection cannot -- so the English anchor on the brief covers the judgment the provider writes, and moving the whole artifact to the mirroring side is the larger decision this change does not take. And `estimate`: it is the one generator that produces no file, so its reader is the person at the terminal rather than a dev team or a tracker, and assigning it either way would extend the decision rather than record it. No language detection is added and nothing is stored about a session's language: both halves are instructions to the model about what it is writing (#277).
- Requivo Web no longer announces the client's language as English. `base.html` declared `<html lang="en">` and nothing else in the template tree carried a `lang` attribute, so a French objective, question or request blockquote was read out by a screen reader with English pronunciation rules. The regions the policy says mirror the request now declare an empty `lang` -- HTML's "the language is unknown", which is the strongest true claim available without detection -- while the chrome, and the decision brief's own content, keep the document's `en`. What this buys is stated with what it does not: an empty `lang` removes a false claim, it does not satisfy [WCAG 3.1.2](https://www.w3.org/WAI/WCAG22/Understanding/language-of-parts), and a reader defaulting to English still pronounces French with English rules. Closing that needs a real BCP 47 value, which needs detection or a question to the user, and neither is decided (#277).
- `fixtures/golden/requests.md` gains `maintenance-rounds-fr`, a single-pass French request, so the next paid golden capture measures this behaviour for the first time; no baseline is committed for it yet, which the harness already treats as "nobody has paid for this capture" rather than as drift (#277).

- Applied the repository's own bug-narrative rule -- CLAUDE.md's *Where a bug narrative lives* -- to
  the four worst call-site prose files: `core/persistence.py` (2,344 -> 1,963 lines), `core/context.py`
  (349 -> 292), `services/sessions.py` (723 -> 650) and `core/integrity.py` (451 -> 434), 528 lines of
  comment and docstring in total. Each recounted bug story is now one line at the call site -- the
  invariant, the cost of breaking it, and the name of the test that goes red when the guard is removed
  -- with the full narrative living in that test's docstring, where a reader cannot skim past it. The
  four files went from 31 issue-citing blocks naming no test to none; across `src/` and `scripts/` the
  same scan drops from 152 to 117. Nothing was deleted for being verbose: reasons attached to a guard,
  MUST-FIRE notes, `docs/compatibility.md` contracts and the invariant list are all untouched, and the
  two narratives no test held -- #40's persisted-card door and #469's lock-file unlink window -- were
  moved into `test_check_selection_reports_a_hostile_persisted_card_rather_than_raising` and
  `test_a_session_removed_through_session_delete_leaves_no_lock_residue` rather than dropped.
  Comment-and-docstring only, with zero behaviour change: the AST minus docstrings is byte-identical
  to `main` for every file touched, and an independent per-line count puts `src/` at the same 6,681
  code lines and 1,680 blank lines before and after (#285).
- Compatibility: compatible - no code line, public `--json` payload, session-format key or error code
  changed; `src/` prose is 52.0% -> 50.5% of lines.

- The licence metadata moves to PEP 639's SPDX form: `license = "Apache-2.0"` with `[project] license-files`, and the `License :: OSI Approved :: Apache Software License` classifier is removed rather than swapped, because PEP 639 rejects a build carrying both. The wheel now publishes `License-Expression: Apache-2.0` at `Metadata-Version: 2.4`. This trade-off had been recorded as declined in `pyproject.toml` and was re-taken rather than reversed in passing: all three checks #337 named were run, and all three pass -- setuptools 82.0.1 installs on Python 3.9.6 (the oldest interpreter here, and the question the original decision turned on), the built wheel carries the expression and no `License:` field, and pip 21.2.4 (the declared pip floor) on Python 3.9 installs it and reads the metadata back. LICENSE, NOTICE and THIRD-PARTY-NOTICES.md still ship, so the Apache-2.0 section 4(d) compliance argument is untouched; they now land in `dist-info/licenses/` rather than directly in `dist-info/` (#337).
- Compatibility: compatible - no runtime dependency, no public payload and no session-format key changes. The two floors that move are build-time and dev-only: `[build-system] setuptools` to `77.0.1` (below 77 cannot parse the SPDX `license` string at all, failing in `get_requires_for_build_wheel`; 77.0.0 was never published), and the `dev` extra's `packaging` to `24.2`. A `pip install requivo` is unaffected in both cases.
- The `packaging` dev floor rises to `24.2`, found by the dependency-floor CI leg rather than reasoned about. setuptools 77's `dist.py` imports `packaging.licenses.canonicalize_license_expression` from the **top-level** `packaging`, which an installed copy shadows over setuptools' vendored one -- so at the previously declared `packaging>=23.0` the two in-process build tests died in fixture setup with a bare `ModuleNotFoundError: No module named packaging.licenses`. Bisected: 24.0 and 24.1 do not carry the module, 24.2 is the first that does, and it installs on Python 3.9.6. This is the second real floor the leg has caught since it was added for #91 (#337).

- Continued the bug-narrative sweep into `services/discovery.py`, the file with the largest remaining
  concentration (#483). Its 13 issue-citing comment and docstring blocks that named no test are now
  none: each call site keeps the invariant, the cost of breaking it and the name of the test that
  goes red when the guard is removed, with the reasoning moved into that test's docstring. Across
  `src/` and `scripts/` the same scan drops from 140 blocks in 43 files to 127 in 42.
- Wrote the test that was missing rather than relocating a story with nowhere to go (#483):
  `tests/test_generation_needs_a_model_152.py` pins #152's guard -- generating from a session with no
  model is a structured refusal naming the remedy, not the `AttributeError` traceback a caller got
  for a release -- with the provider call count as the assertion, since an `AttributeError` also
  never reaches the provider, and a must-not-fire control so a guard that refused every session could
  not pass.
- Added `docs/decisions/0005-the-typed-generation-seam.md` for the three #271 typing decisions that
  no test in this repository can reach, because their guard is the Types leg rather than pytest: why
  `Generated` is generic, why `generate()`'s signature is six overloads, and why `_WRITERS` carries
  an annotation instead of inferring one (#483).
- Compatibility: compatible - comment, docstring and test only. The AST minus docstrings is identical
  to `main` for every modified file; no code line, public `--json` payload, session-format key or
  error code changed, and no test was deleted (1,667 -> 1,670, the three added above).

- Second pass of the bug-narrative sweep, on `deterministic/sessions.py` (#483). Its 12 issue-citing
  comment and docstring blocks that named no test are now none: each site keeps the invariant, the
  cost of breaking it and the name of the test that goes red when the guard is removed. Across `src/`
  and `scripts/` the same scan drops from 140 blocks in 43 files to 128 in 42.
- Every relocated block landed on a test that already existed -- the legacy-root scan's three
  outcomes, the interrupted-migration receipt, `session verify`'s three exit codes, the archive
  entry-count ceiling, the remembered occupied-slug answer, the rename-as-claim window, the locked
  forced swap, and the `slug`/`path` payload spelling. Nineteen test names were introduced and all
  nineteen resolve (#483).
- Compatibility: compatible - comment and docstring only. The AST minus docstrings is identical to
  `main` for the one modified file; no code line, public `--json` payload, session-format key or
  error code changed, and the suite is unchanged at 1,667 with no test added or deleted.

- Third pass of the bug-narrative sweep, on `deterministic/doctor.py` and `cli.py` (#483). Their 16
  issue-citing comment and docstring blocks that named no test are now none: each site keeps the
  invariant, the cost of breaking it and the name of the test that goes red when the guard is
  removed. Across `src/` and `scripts/` the same scan drops from 82 blocks in 37 files (already down
  from the 109/41 the issue was filed against, after the two prior passes) to 66 in 35.
- Fifteen of the sixteen relocated blocks landed on a test that already existed. One did not: the
  claim that a session note must not pull `doctor`'s sessions-row glyph down to the same warning
  state a lock timeout or an unexaminable entry earns was true of the code and untested, so
  `tests/test_cli_doctor.py::test_a_note_does_not_move_the_sessions_glyph` was written for it and
  confirmed red against a mutated `_print_sessions` before being confirmed green again.
- Self-review (`Explore`) found the same gap one disjunct over: the relocated comment's own claim
  that an *unexaminable* entry shares that middle glyph had no test either -- only the `locked` arm
  did. `tests/test_cli_doctor.py::test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick`
  closes it, driven by monkeypatching `scan_session_root` rather than a real `chmod 000` so it runs
  on every platform, and confirmed red the same way. The comment now cites a test for each of the
  three could-not-look disjuncts by name. Thirty test names were introduced across the two files and
  all thirty resolve (#483).
- Compatibility: compatible - comment, docstring and test only. The AST minus docstrings is identical
  to `main` for both modified source files; no code line, public `--json` payload, session-format key
  or error code changed. The suite grows by the two tests above (1,701 -> 1,703) and none was
  deleted.

### Fixed

- Re-shot the four screenshots in `docs/images/` and added `scripts/shoot_doc_images.py`, which
  takes all four from the bundled example seeded as a real session -- no key, no network (#329).
  Two had gone stale in content: the home page predated the recent-first listing and human-formatted
  timestamps, and the decision brief predated the rendered document that replaced its `<pre>` block.
- Corrected `docs/web.md`'s caption for the session screenshot, which described a "what is confirmed"
  section the page does not have (#329).
- `tests/test_doc_images.py` now fails when the web surface changes after the screenshots were taken,
  so a stale image is a red test rather than something a reader has to notice. The freshness digest
  is recorded **per image**, not once for the manifest: the script accepts shot names, and a shared
  digest let a partial re-run certify the screenshots it had not re-taken. It watches the whole
  `web/viewmodels/` package rather than two of its four modules -- `sessions.py`, which decides the
  title and ordering of every session row on the home page, was outside it (#329).

- Added missing security headers (`Content-Security-Policy`, `Referrer-Policy`, `X-Content-Type-Options`, and `Cache-Control`) to unhandled 500 error responses in the web interface, ensuring they match standard responses rather than falling back to browser defaults (#340).

- Isolate the incomplete-model provider test's failed-reply dump in a temporary workspace, and detect new `.requivo` entries left by the test suite without redirecting unrelated tests (#432).

- `SessionService.snapshot()` — the read every provider-backed operation (`run_discovery`, `answer`, `generate`, `reason`) takes before it calls out — now names the correct workspace root when it refuses a missing session against an explicitly-rooted `SessionService`. It previously raised through the process-ambient `no_session_message`, so an embedding consumer holding a service rooted at one workspace could be told the session was missing from a different one entirely (#457).

- `requivo prd`, `criteria`, `epic` and `release` no longer print a visible `\r` at the end of every line when the generated document carries Windows line endings. The same document read back with `requivo artifact show` was already rendering correctly, so the two verbs disagreed about one file; they now agree, and a lone carriage return is still escaped because it moves the cursor rather than ending a line (#460).

- Fixed (#461): `golden_diff`'s baseline-freshness readout (`_show_freshness`,
  `scripts/golden_diff.py`) had three print sites sharing one `--format` string — #456 routed `date`
  and `sha` through `display_token` for defense in depth there, but left the third, `reason`, raw.
  `reason` is the one of the three that actually carries text from outside the process (git's stderr,
  or `str(exc)`), rather than a fixed git format like `%cI`/`%H`, so it had the strongest claim to the
  guard and was the one left unguarded. No route to reach it with a crafted string was found — the two
  `git log` invocations this function drives echo only `HEAD` or a `%H` sha, never a caller-supplied
  path, and every other producer of `reason` stringifies to plain prose or hex — so this is
  unranked, defense-in-depth, on the same argument #456 already made for its two siblings.
- Compatibility: compatible - `scripts/` ships in neither the wheel nor the sdist, so this is a
  developer-tooling fix with no session format, `--json` payload, or public API change. An ordinary
  freshness reason renders exactly as before.

- The web app's security headers are stated in one place instead of two, so a header added to the middleware can no longer go missing from the unhandled-500 page the way the whole set did before (#340, #462).

- Every file the store's own atomic writer produces (`model.json`, `session.json`, each frozen revision, every saved artifact) is now written with universal-newline translation disabled. On Windows, writing text with the default `newline=None` translated every `\n` in the content to the platform's own `\r\n` — so a lone carriage return already in the content (a provider reply that carries one) became `\r\r\n` on disk, a line the document never had. The write now asks for no translation at all and lands the string's own bytes on every platform, the same guarantee `encoding="utf-8"` already made for character sets one keyword along (#464).

- `docs/decisions/0004-the-http-api-facade.md` section 5 stated the spend ceiling in language that read as a machine-wide guarantee ("nothing on this box spends more than X without a human"). The `SpendPolicy` #427 shipped is scoped to the `ContextVar`-backed ledger `requivo.usage.track_usage()` opens fresh per CLI command or per web request, and resets on the next call — it bounds one operation's cost, not cumulative spend across many requests. The record now says so plainly and names what actually closes that gap: HTTP-layer rate limiting (already out-of-scope for #427) once the facade exists to put it in front of, or a future persistent, cross-request `SpendPolicy` variant. No code changed (#466).

- `DiscoveryService.start(finalize=True)` no longer discards a paid `analyze()` call when the following decision-brief call fails or is refused. It used to make both provider calls before writing either, so a transport error, or the `#427` spend ceiling being reached by the `analyze()` call alone, threw away the already-billed discovery and left the session at revision 0 with nothing applied. It now persists `analyze()`'s result as revision 1 immediately, before the brief is even attempted — mirroring `#202`'s fix for the CLI's interactive loop in the same file — so a failed or refused brief leaves the discovery applied and the brief retryable through the ordinary `generate(slug, "brief")` path, rather than a total loss of the paid call (#467).

- The `Dependency floor` CI leg went red on every pull request when `anyio 4.15.0` stopped exposing `anyio.abc` through its lazy `__getattr__`: the development-only `httpx>=0.25.0` floor resolves to `httpx 0.25.0`, which pins `httpcore 0.18.x`, which reads that attribute at import time — while `anyio` is transitive and deliberately left at its newest release. The floor is now `httpx>=0.26.0`, the lowest release that resolves to `httpcore 1.x` and imports cleanly under `anyio 4.15.0`, bisected rather than guessed. No upper bound was added to `anyio`: holding a transitive bound would reverse a decision `.github/workflows/ci.yml` already records. `httpx` is test-only here and nothing shipped imports it, so the raised floor costs a consumer nothing (#472).

- The Claude Code plugin now writes down the field name a question is built from. `Question`'s text field is `q`; `question` is the natural guess, the contract is `extra="forbid"`, and no page named the field at all -- so the guess cost a whole validate cycle, two errors per question (12 for a six-question turn), before the reader could see which field to use. Named in `REASONING.md`'s proposal loop and at the point both the `discover` and `answer` skills ask for questions, and held by `test_the_pages_that_build_a_proposal_name_every_field_a_question_is_made_of`, which checks every page that builds a proposal against the contract's own required-field set rather than against a sentence. Reported from a real plugin session by an external contributor (#489).
- The `discover` skill now names the product context cards a session was grounded on when it presents the understanding. `context.status: ok` means the cards are present and readable; it does not mean they are *relevant*, and there is no status that does. The shipped cards describe B2B enterprise domains, impact is estimated against whichever cards a session holds, and a request from outside that domain is scored against a product it has nothing to do with -- producing a model, reaching `ready`, and saying nothing on screen about it. That is the shape the `empty` case was already warned about, one level up. Naming the cards back to the reader does not detect relevance; it puts the one fact a human can judge in front of them (#489).
- The `discover` skill now offers `--workspace` before creating a session *about the repository the user is sitting in*. The existing warning covers running from the wrong directory; this is the case where the right directory is still the wrong one, and `.requivo/` lands untracked inside the tree the session is reasoning about (#489).

- `scripts/dependency_floor.py` no longer treats an unrecognised leading-dash argument as an
  output filename. Running `--help` used to write the constraints list to a file literally named
  `--help` in the current directory and exit 0; any typo of `--verify` (e.g. `--verfiy`) did the
  same, silently, with a success exit code -- the reader believing a verification had run and
  passed when nothing had run at all. The script now has a real `--help`/`-h`, and refuses any
  other unrecognised `-`-prefixed argument with a message on stderr and a non-zero exit; the
  existing `--verify` and bare-output-path behaviours are unchanged (#494).

- The HTTP API now carries the same security headers Requivo Web does — `X-Content-Type-Options`, `Content-Security-Policy`, `Referrer-Policy` and `Cache-Control`, error responses included — from one shared `requivo.security_headers.apply_security_headers`, so a header added to the policy reaches both surfaces (and any future one) without a second edit (#503). `/docs` and `/redoc` no longer load Swagger UI, ReDoc, a favicon or a font from a third-party CDN into the same origin that serves session data with no credential: both are vendored into the wheel at pinned versions and served from the API's own static mount, and the spec validator badge no longer phones home either. This is also what lets the API's `Content-Security-Policy` set `script-src 'self'` with no exception carved for the CDN — the two fixes land together because either alone leaves the other broken (#504).

- Corrected `docs/decisions/0006-codeql-sanitizers-it-cannot-see.md`, which still described the
  API traversal guard on pull request #499 as not yet merged. It landed one commit later, and the
  record now names the tests directly: `tests/api/test_api_traversal.py`'s
  `test_no_slug_shaped_traversal_reaches_the_filesystem`,
  `test_no_artifact_type_traversal_reaches_the_filesystem` and the must-fire
  `test_the_refusal_is_the_slug_guard_and_not_merely_a_missing_session` (#505).

### Security

- Every `RedirectResponse` target under `web/routes/` is now guarded by a test proving a hostile
  slug is *refused* rather than redirected, that the refusal is the slug guard's own -- not the
  accident of the target being absent -- and that an honest slug still reaches the redirect it
  should, at all four sites. The refusal is the always-firing claim on purpose: the obvious shape,
  `if the response was a redirect, check its target`, is vacuous, because a hostile slug never
  produces a redirect, so the branch never runs. Written that way first, it survived two review
  rounds and stayed green with the fixed `/sessions/` prefix deleted from a route *and* `_SLUG_RE`
  widened to admit `/` and `:` -- both halves of the failure it is named after, applied at once. This closes the gap CodeQL's `py/url-redirection` query correctly
  found: the four sites (`sessions.py`'s `analysis_failed`, `discovery.py`'s `run_discovery` and
  `submit_answers`, `artifacts.py`'s `generate_artifact`) had no equivalent to the API layer's own
  path-traversal guard (#425/#499). A new decision record, `docs/decisions/0006-codeql-sanitizers-it-cannot-see.md`,
  states the standing position on both open CodeQL query classes on this repo (`py/path-injection`,
  `py/url-redirection`) -- why the existing sanitizers make today's 31 open alerts false positives,
  which test would go red if any of them stopped holding, and what would reverse the position (#500).

## [3.1.0] - 2026-09-02

### Added

- The file session backing's workspace root is now constructor state, not ambient process environment (#272). `core.persistence.Store` holds one resolved workspace root and exposes every storage operation as a method on it; `FileSessionRepository(root=...)` addresses that root for its own lifetime, independently of every other instance in the same process, which is what a hosted, multi-tenant caller (`requivo-cloud`'s "Gap A") needs and could not previously get without mutating `REQUIVO_WORKSPACE` around every call. `FileSessionRepository(root=None)` — the default, and what `requivo`'s CLI still uses via `default_repository()` — stays ambient, resolved fresh from `REQUIVO_WORKSPACE`/cwd on every call, so `requivo --workspace <dir>` behaves exactly as before. No session format, CLI flag or environment variable name changed.
- Compatibility: compatible - purely additive constructor state; every existing call site (`FileSessionRepository()`, every `core.persistence` module-level function) keeps its exact prior signature and behaviour. `Store` and `FileSessionRepository.store()` are new public names — read them as provisional until #423 declares the storage seam frozen; a shape change there is not expected to need a further compatibility note here, but is called out in case it does.

- Declared the Python import seam a downstream consumer may build against, and shipped `py.typed`
  (#423). `docs/compatibility.md` names the services (`SessionService`, `DiscoveryService`,
  `ArtifactService` + their result types), the `SessionRepository` and `ReasoningProvider` protocols,
  `FileSessionRepository`, the boundary contracts (`EngineOutput`, `ModelProposal`, `SessionMeta`,
  `ArtifactStatus`, the artifact contracts), the failure vocabulary (`RequivoError` and its
  subclasses, `EngineError`), and `requivo.usage` as stable — priced like every other promise on that
  page, a major and a line for a break. Everything else in `core`/`services`/`providers`/
  `deterministic`, most pointedly `providers.anthropic` internals, stays explicitly unstable, and
  `render`/`paths`/`streams`/`cli`/`web` are classified as not part of the Python import surface (the
  page's own "neither column is a bug" rule). One empty `src/requivo/py.typed` file, declared in
  `[tool.setuptools.package-data]`, turns the declared surface from prose into something a type
  checker holds -- the whole package was already pyright-clean.
- Compatibility: compatible - purely additive. No name moved, no signature changed; this narrows what
  was previously undocumented-and-implicitly-anything down to a stated list, and every name on that
  list already existed and already had the shape it is now promised to keep.

- Extracted a `SessionRepository` conformance suite an out-of-repo backing can actually run (#424).
  CLAUDE.md has long claimed "a Postgres repository reuses [the orchestration] verbatim", and the
  proof used to be real but private -- an in-memory fake and one test, both inside
  `tests/test_sessions.py`, runnable by nothing outside this repository. `requivo.testing.
  repository_conformance.SessionRepositoryConformance` is the same proof, importable: subclass it,
  implement `make_repository()`, and pytest collects tests covering create-claims-the-slug atomicity
  (invariant 11), `expected_revision` refusal (invariant 2), lock mutual exclusion and per-thread
  re-entrancy (invariant 9), the known/unexaminable partition (invariant 15's shape at this layer),
  `load_artifact`'s absent-vs-refused split, and unknown-key preservation through a save/load round
  trip (invariants 8/10). Both implementations this repo ships -- `FileSessionRepository` and the
  in-memory test fake -- now run against it in-tree; the fake needed a real per-slug `threading.RLock`
  to actually pass the mutual-exclusion half, where its `lock()` used to be a documented no-op.
  `pip install 'requivo[testing]'` (a new extra) pulls in the pytest floor this suite is built
  against; `requivo.testing` itself ships in the base wheel, since it is part of the declared Python
  import seam (#423). The `dev` extra's `setuptools` floor moved to `70.1.0` -- the CI leg that
  installs the literal declared floors caught that `setuptools<66.1.0` cannot even `import
  pkg_resources` on Python 3.12, and `<70.1.0` cannot build a wheel at all without a separately
  installed `wheel` package (see pyproject.toml's own comment for the bisected reasoning); the two
  build-backend tests this issue adds also degrade to a named skip if a future or externally-pinned
  setuptools is too old to run, rather than surfacing a bare traceback in fixture setup -- checked
  through `importlib.metadata.version("setuptools")` rather than `import setuptools`, after a
  reviewer caught that the first version of this check imported the very package it was checking the
  safety of importing, so it never actually fired for the range it existed to catch.
- Compatibility: compatible - purely additive. Nothing that existed before moved; `tests/
  test_sessions.py`'s own `InMemorySessionRepository` and its behaviour are unchanged from a caller's
  point of view except that `lock()` now actually excludes a concurrent thread instead of a no-op --
  a strengthening, not a narrowing, of what it already promised to do.

- `AnthropicProvider` now takes an optional fixed model id (`AnthropicProvider(client=…, model=…)`, #434). Before this, `_complete()` resolved the model ambiently, per call, from `REQUIVO_MODEL`/`MODEL` — so one process could not run two models at once without racing a mutable environment variable, which per-tenant or per-plan model selection (the hosted product) and the deferred Fable A/B evaluation both need. `model=None` (the default) is unchanged and byte-identical: every call still resolves through `current_model_name()`'s env chain. Set explicitly, the id is threaded into every completion call, `model_name()` and `provenance()`, and wins outright with no env read at all — two providers constructed with two different ids in one process now call, price and record independently.
- `DiscoveryService` is not wired to accept or forward a model id yet; a caller reaching for per-tenant model selection constructs its own `AnthropicProvider(client=…, model=…)` and passes it to `DiscoveryService(provider=…)`. Pricing an id the rate table does not know continues to degrade honestly — `rate_per_mtok`/`priced_as_of` stay absent and `cost_usd()` says "no price on file" rather than guessing, unchanged by this issue.
- Compatibility: compatible - `AnthropicProvider(client)`/`AnthropicProvider()` with no `model=` keyword is unchanged; every existing call site (`services/discovery.py`, the test suite) keeps its exact prior behaviour. `model=` is a new, optional constructor keyword; the `ReasoningProvider` protocol itself (`analyze`/`generate`/`model_name`/`provenance`) is untouched.

- The 2026-09 readiness audit's written deliverables (#441): `docs/audits/2026-09-product-readiness-audit.md`
  (the full 16-domain report — scorecard, prior-audit disposition, findings bound to tracker
  issues #419/#421–#440, risk register, dependency-ordered roadmap, the NOT-NOW list),
  `docs/cloud-boundary.md` (the consumption contract for any hosted deployment and the minimal
  upstream change set), `docs/integrations.md` (the CLI automation contract, the `requivo-epic`
  envelope field by field, a worked n8n flow, and the recorded no-webhook-delivery decision), and
  `docs/decisions/0004-the-http-api-facade.md` (the API design with its three named freeze
  preconditions).
- The README links the demo video (published as v3.0.0 release assets, closing #224) and its
  keyless-example paragraph now promises exactly what ships (#429's copy half). A small verified
  drift pack rides along: decision record 0001 records the two required-check appends executed by
  the audit (14 → 16), the doctor row in cli.md names `REQUIVO_MODEL`, the PR template gains the
  pyright line, `getting-started.md` gains an Upgrading section, compatibility.md's web-route table
  gains the `POST /sessions/example` row it under-counted, TRADEMARKS.md names the live official
  identity, `pyproject`'s Homepage points at requivo.com (#234's second half), and the root
  `conftest.py` drops a dead path insert.

### Changed

- `FileSessionRepository`'s docstring now states both halves of what its "holds no state, so it is safe to construct per call" sentence leaves out (#272): every method delegates to a module-level `core.persistence` function, and those resolve the workspace root ambiently (`REQUIVO_WORKSPACE`/cwd) rather than from `self`, so two instances of the class are indistinguishable and cannot address two different workspaces in one process. The seam is documented as backing-agnostic; on this backing today it is not addressable by construction. This is a documentation correction only — it changes no code path and does not close #272, whose deferred refactor (threading an explicit root through the store) is what actually makes the file backing addressable.
- Compatibility: compatible - a docstring change only. No code path, `--json` payload, exit code or file format is touched.

- `RequivoError` code -> HTTP-status classification moved out of the `[web]` extra and into a new
  top-level, framework-free module, `requivo.http` (#422): the table (`STATUS_BY_CODE`), the
  unclassified default (`UNCLASSIFIED_STATUS`) and the classifying function (`http_status_for`) are
  now importable with no extra installed, so a second HTTP surface — the hosted product that already
  imports this package, or a future local API facade — never has to reach a private name out of
  `requivo.web.app` or fork the table. `web/app.py` keeps thin private aliases for its own call
  sites; the guard test, `test_every_error_code_has_an_explicit_http_status`, moved with the table to
  `tests/test_http_status_table.py`.
- Compatibility: compatible - the names that moved (`_STATUS_BY_CODE`, `_UNCLASSIFIED_STATUS`,
  `_status_for`) were private to `requivo.web.app`; the new public names are `requivo.http.STATUS_BY_CODE`,
  `requivo.http.UNCLASSIFIED_STATUS` and `requivo.http.http_status_for`. Every status a request or a
  page reaches the browser as is byte-identical to before.

- Editorial pass over the audit-batch documents (#441): the hosted-product references in
  `docs/cloud-boundary.md`, `docs/decisions/0004-the-http-api-facade.md` and the 2026-09 audit
  report are now stated generically — the arguments and every observed failure shape stand, without
  forensic detail of a private codebase. No promise, route, or contract sentence moved.

### Fixed

- `golden_diff` now names whether the committed baseline it is about to compare against even
  predates a real change, before printing a single slot or assessment movement (#405). It reports
  whether a commit touching `src/requivo/assets/{prompts,context,framework}/` or
  `src/requivo/providers/anthropic/generators.py` landed since the baseline's own commit in HEAD —
  `WATCHED_PATHS` in `scripts/golden_lib.py`, funded by two reproduced instances: #405 itself (three
  asset commits landed between one committed baseline and the next, unnoticed for a month) and #410
  (`ba526f6` dropped `indent=2` from the JSON `generators.py` sends as the user message for every
  `--brief` capture — invisible to `prompt_version()`, which hashes only the system prompt, and to
  `tests/test_golden_baselines.py`, which compares only `request`/`answers`). Without this, a
  maintainer editing one prompt and running the harness saw the combined effect of their own edit
  *and* every earlier watched-path change nobody had re-captured against, with no way to tell them
  apart — the reader was the control. Three states, never collapsed: `current` (no watched-path
  commit since capture), `stale` (named, with the capture date and each commit), and `unknown` (git
  unavailable, a shallow clone, or a baseline with no commit history) — `unknown` never renders as
  `current`, the same rule `golden_diff` already applies to a byte-identical capture. Scope is
  deliberately narrower than "everything that can move a capture" — `core/context.py`'s own assembly
  logic and `completion.py`'s retry/parsing are real gaps with no reproduced instance yet, and the
  printed line says exactly which paths it checked rather than reading as coverage it does not have.
  `golden_run.py` is intentionally left untouched by this change; the acceptance criterion is about
  `golden_diff`'s per-request readout, and a capture there always supersedes whatever staleness
  existed.
- Self-review folded four findings back into the same change: commits since the baseline are now
  listed oldest-first (`git log --reverse`), matching the truncation reader — a maintainer previously
  saw the five *most recent* watched-path commits with the earliest, most likely offending one folded
  into "... and N more"; `golden_diff.py --questions` (`questions_one`) now prints the freshness line
  too, since it did not reach it before; a commit subject is contributor-written text and is now
  rendered through `display_token`, the same guard this file's own untrusted-output class already
  applies to a provider-written question (invariant 14, #40); and the git call for the baseline's own
  commit failing outright is reported with its real error rather than folded into "no commit history
  for this baseline."
- One of the integration tests read this real repository's own history and asserted `stale` against
  a commit known to postdate the golden re-capture — which failed on CI's shallow checkout
  (`actions/checkout`'s default `fetch-depth: 1`), where `baseline_commits_since` correctly reports
  `unknown` rather than guessing, since the truncated history genuinely cannot be trusted. That was
  the function doing its job; the test pinned an environment-dependent verdict. Replaced with a
  synthetic, full-history repo built in the test itself (same shape as the ordering test above), and
  added a dedicated test that clones a synthetic source repo with `--depth 1` to pin the
  `unknown`-on-shallow behaviour end to end, independent of the depth of whatever checkout runs it.
- Compatibility: compatible - a new offline `git log`-backed check in `scripts/golden_lib.py`
  (`baseline_commits_since`, `WATCHED_PATHS`) and new lines in `golden_diff`'s per-request stdout
  output (`diff_one` and `questions_one`). No session format, `--json` payload, CLI flag, or committed
  baseline file changes.

- `doctor` now reports a non-session directory whose name is a Windows reserved device name (`con`,
  `nul`, `lpt1`, ...) as taken, instead of staying silent about it (#408). `_describe_non_session`
  asked `is_slug` for `slug_shaped` -- the *unconditional* creation-time refusal `validate_slug`
  applies -- so a `con` directory holding no `session.json` read `slug_shaped: false` and `doctor`'s
  `[name taken]` hint never named it, even though `create_session('con', ...)` reads straight through
  the very same directory under #372's *conditional* read-time rule and loses its rename to it: the
  exact consequence the hint exists to warn about. `slug_shaped` asks the read-time question now, the
  same one `lock_path`, `_child_of`, `_is_lock_stem` (#409) and two web/service call sites already
  ask -- in this caller's case reducible to shape alone, since the directory being described already
  occupies the path in question.
- `is_slug`'s callers were enumerated in the course of this change: it now has none in this codebase,
  and stays as the creation-time predicate for a caller that genuinely is asking about creating a
  fresh name from nothing, which `_describe_non_session` and `scan_lock_root` are both not.
- Compatibility: compatible - `doctor --json`'s `sessions.non_sessions[].slug_shaped` is unchanged in
  shape (still a bool per entry) and `true` where it used to be `false` for a reserved-device-name
  directory that is not a session; nothing changes for a non-reserved name. See
  `docs/compatibility.md`'s `--json` section for the declared entry.

- `doctor` no longer reports a reserved-device-name lock file as one Requivo does not recognise just
  because the session it was written for has since been deleted (#409). `_is_lock_stem` re-asked the
  #401 conditional rule (`_slug_shape` plus `_refuse_new_reserved_slug` against `session_root()`), so
  a `nul.lock` file `session_lock` legitimately wrote while a `nul` session existed kept reading as
  "not a lock file Requivo recognises... a name here did not come from `session_lock`" once that
  session was removed -- the very sentence the report prints being false about a file this release's
  own code had written. A lock file's provenance is fixed the moment it is written; the classifier
  made the answer depend on whether a *different* directory still existed, in violation of
  `scan_lock_root`'s own docstring ("never *is `slug` still a session*"). The stem's shape alone now
  decides whether either writer here could have produced the file; a session still matching it is
  `locks.unmatched`'s question, asked separately.
- A lock or guard file whose stem is not a shape either writer produces -- a stray file, a directory,
  a symlink, a malformed stem -- is still reported. `nul.discovering` and `nul.lock` with **no**
  session ever having existed at that name are indistinguishable from the deleted-session case (this
  store cannot tell them apart from the directory alone) and are now classified the same way, as an
  ordinary orphaned lock rather than residue.
- Compatibility: compatible - `doctor --json`'s `locks.unexpected` is unchanged in shape (still an
  array of names, and the `locks` skeleton is untouched) and narrower in content, on the same terms
  as #391's and #401's narrowings of the same key. On a workspace that has ever locked a
  reserved-name session and later removed it, a consumer sees `locks.total` count one more lock,
  `locks.unexpected` name one fewer entry, and the stem appear in `locks.unmatched` instead; on every
  other workspace, including every workspace created on Windows, nothing changes. See
  `docs/compatibility.md`'s `--json` section for the declared entry.

- `docs/evaluations.md`'s "Known stale baseline" section, and `tests/test_golden_baselines.py`'s
  own module docstring, stopped describing a state the code had already left (#410). The doc closed
  on a section asserting `training-budget`'s baseline was stale and named as a declared exception in
  `_DECLARED_DRIFT`; `_DECLARED_DRIFT` has been empty since the seven-baseline re-capture in #405,
  and the comment sitting right above the dict already said so. The doc section is replaced with a
  description of the mechanism rather than a dated claim about one baseline's state, and the test
  module's docstring now agrees with its own `_DECLARED_DRIFT` comment twelve lines below it instead
  of contradicting it. `core/dependencies.py`'s `resolve_slots` docstring also no longer renders
  the pre-#248 `requivo impact <model> ""` example without naming what the positional is actually
  called (`session`, since #248).
- No new guard: this is one instance of prose drifting from the code it describes, and `CLAUDE.md`'s
  own budget rule on `tests/test_narrative_references.py` and its sibling meta-guards asks for two
  named instances of a drift class before a new check is warranted, plus a mechanism that fits the
  existing tier. This drift is a content-equality question (does a docstring's claim match a comment
  twelve lines away) rather than a name-resolution one, so it would not fold into
  `test_narrative_references.py`'s existing check even with a second instance in hand. Decided
  against, in the open, rather than left unaddressed.
- Compatibility: compatible - documentation and comments only, no code or session-format change.

- `session migrate`'s slug scan aborted the whole pass with a raw `PermissionError` when one legacy
  directory under the `out/` root could not be stat'd into (#411). `#371` hardened every per-slug
  guard inside the migration loop against exactly this class -- an unreadable canonical `session.json`,
  an undecodable legacy `request.md` -- and left the scan that *produces* the loop's rows unguarded:
  `(p / "model.json").exists()`, outside every `try`, and `Path.exists()` re-raises `EACCES`. One
  blocked directory therefore aborted the entire sweep before a single receipt was printed, however
  many healthy legacy sessions sat on either side of it in the listing.
- The scan is now a three-outcome partition, `_scan_legacy_root`, on the identical terms
  `core.persistence._scan_session_root` already applies to the *canonical* session root: a name is a
  legacy session, is not, or could not be examined. An entry in the third bucket is reported under a
  new `unreadable` key -- named, with its OS error -- and is never silently dropped and never counted
  as a session to migrate. Every other legacy session in the sweep still migrates.
- **Found in review of this same change**: wrapping only the per-entry probe left the scan's own
  `root.iterdir()` outside any guard too -- a legacy `out/` root the process cannot even open into
  (not merely one unreadable entry inside it) raised the identical uncaught `PermissionError`, one
  level up. `_scan_legacy_root` still raises for that case, deliberately, mirroring
  `core.persistence._scan_session_root`'s own stated contract for the canonical root -- a whole-root
  failure is genuinely different from a per-entry one, and there is no partial receipt to print. The
  caller now converts it into a clean `SessionUnreadableError` instead, which exits `1` -- "no
  answer", since nothing was examined at all, not `4`'s "the answer is incomplete."
- Compatibility: compatible - `session migrate --json` gains an additive `unreadable` key, on the
  same terms `interrupted` and `errors` were added under #262. The command's exit code for this new
  state is `4` (`EXIT_DEGRADED`), the same code `errors`/`interrupted` already use for "the work was
  done and part of the answer was unreachable" -- but the condition it now covers was never a
  documented `0`: it was an uncaught crash with an undefined exit code and no output at all, so
  converting it into a receipt moves nothing off a promised code. The whole-root-unlistable case
  above is the same reasoning one level up: it exits `1` where it previously crashed with an
  undefined code, never a documented `0` either.

- `SessionService.resolve_slug`'s directory branch carried the identical wrong-cause class #402
  fixed for the `model.json`/`session.json` branch, one branch over (#414). `p.exists() and
  p.is_dir()` mined ANY directory's own name as a slug, unconditional on whether a session actually
  lived behind it -- so a directory reference that merely shared its final path segment with an
  unrelated real session silently resolved to and operated on that session, and a reference to a
  directory naming no session at all failed under a slug the user never typed.
- A directory is now only mined for its own name when it carries a session's own marker file --
  canonical `session.json` or legacy `model.json` -- the directory-shaped analogue of "the file is
  actually there" that #402 already applies to the file branch. A directory with neither is refused,
  naming the path exactly as given. Reachable only through `deterministic/` verbs (the eight
  generator verbs opt out of path acceptance entirely since #402) and only when the referenced
  directory's own name collides with an unrelated session's slug -- narrower than #402, and the same
  class: `resolve_slug` must not derive a slug from a path segment and then fail, or worse succeed,
  on a name the user never wrote.
- **The seam question #402 opened, answered for this lane**: one rule about *when* `resolve_slug` may
  mine a path at all versus a per-branch patch. This is the fourth site in the class (#381, #390,
  #402, now #414), and per-branch was kept rather than folded into a single seam rule. The two
  branches mine different evidence of "a session lives here" -- a file that exists versus a directory
  that carries a marker -- and a single boolean gate over `accept_path` cannot express that difference
  without becoming, in effect, this same per-branch check written once and dispatched from a
  different place. `accept_path` remains the seam for the *coarser* question `#402` built it to
  answer -- may a path be mined at all, for this caller class -- and the finer, per-shape "is this
  actually a session" check stays where the evidence for each shape already lives.
- **Found in review of this same change**: the directory branch's own entry gate --
  `p.exists() and p.is_dir()`, unchanged by the fix above -- was itself still unguarded. It stats
  the referenced path directly and re-raises `PermissionError` when an *ancestor* of the reference
  denies traversal, a distinct case from the marker probe (the fix above) failing on the referenced
  directory's own contents. A directory that is otherwise a perfectly healthy session reference
  could still crash with a bare traceback, purely because something above it on the path could not
  be examined. Both probes are wrapped now, on the same terms, so both re-raise as the same clean
  `SessionNotFoundError` rather than one of the two escaping uncaught.
- Compatibility: breaking - a directory reference whose final path segment happened to collide with
  an unrelated real session's slug used to resolve silently to that session; it is now refused, naming
  the given path. A directory that genuinely is a session's own directory (it carries `session.json`
  or `model.json`) is unaffected.

- The test suite's "no API calls, no network" promise was environment-conditional, not structural
  (#419): `cli.py` loaded the repo's `.env` at module import, `client=None` in the CLI harness meant
  "build the default client", and one journey test made a real paid Anthropic call and went red on
  any machine with a resolvable credential — green in keyless CI, ~$0.07 billed per full-suite run
  on the documented dev setup. Three layers close it: a suite-wide autouse net in `tests/conftest.py`
  (every credential variable scrubbed, `load_dotenv` no-opped in-process, `ANTHROPIC_BASE_URL`
  pointed at an unroutable loopback port so an escaped call dies unpaid in milliseconds), the shared
  credential machinery moved to `tests/_credentials.py`, and `load_dotenv()` moved from import time
  into `app()` — importing `requivo.cli` no longer mutates the environment of any process that
  embeds it, while every CLI verb still honours a `.env` in the directory it runs from.
  Must-fire pair: `test_the_net_fires_when_a_credential_is_ambient` and
  `test_importing_the_cli_leaves_the_environment_alone`.

- `requivo answer <slug> "…"` (and the Web's `POST /sessions/{slug}/answers`) on a revision-0
  session — claimed but never analysed, exactly what a failed or aborted `discover` leaves — used to
  make a full paid provider call instead of refusing before payment (#421). Worse than a mis-gated
  call: at revision 0 `snap.model` is `None`, so the provider's `analyze()` fell through to its own
  first-discovery branch, and the answers the caller typed appeared in no kwarg of the call. The
  reply was still applied as revision 1, with `cli-answer`/`web-answer` provenance, and the write
  bypassed `run_discovery`'s own double-submission guard. So the user paid for a turn that ignored
  what they typed, and the session recorded an answer turn that never happened.
- `DiscoveryService.answer()` now refuses via `_require_a_model`, the same gate `generate`/`reason`
  already use (#152, which fixed the identical defect on those two and left `answer` out) — before
  `_need_provider()` is ever reached, so no client is built and nothing is billed.
- `_require_a_model`'s own remedy text used to suggest `requivo answer <slug>` "if a discovery is in
  progress" — i.e. it routed a reader straight back into the path this fix closes. Since #202 an
  interrupted discovery lands at revision 1, so `answer` is never the right verb at revision 0; the
  remedy now names `requivo discover` only.
- Compatibility: breaking - `requivo answer`/`POST /sessions/{slug}/answers` on a revision-0 session
  used to return a paid success (CLI exit 0, web 200) building a bogus revision 1; it now refuses with
  a structured `RevisionConflictError` (CLI: structured error naming the remedy; web: 409) before any
  provider construction. Graded honestly rather than as a bugfix with no surface effect: the return
  code genuinely changes for a caller on this exact path. No legitimate flow is affected — the Web
  never renders the answers form at revision 0, the interactive CLI loop never calls `answer()`
  directly, and the plugin applies through `model apply` — and what the old "success" delivered was
  never meaningful: a model that ignored every word the caller typed.

- The three htmx-only forms (the answers form, and both generate-document forms) now carry a
  `method="post" action="..."` fallback beside their `hx-post`, matching every other form in the
  tree (#428). Without JavaScript a form with neither attribute submits as a plain GET to the current
  URL -- the typed answers were silently discarded, and the process-lifetime CSRF token plus the
  full answer text landed in the URL and browser history. `submit_answers` and `generate_artifact`
  now branch on the `HX-Request` header: an htmx request still gets the fragment swap it always did,
  and a plain POST performs the same operation and returns a 303 to the session page, with the
  reader's typed answers honoured either way. The oversized-answers refusal (#30) gets the same
  split -- a no-JS submit that exceeds the character ceiling gets a full page with the typed text
  preserved, not a bodyless fragment. `app.css`'s "the page works fully without JavaScript" claim is
  now true rather than needing to be softened.

- The keyless "Explore a worked example" button on Requivo Web's home page now seeds the bundled
  decision brief alongside the model, so the one click delivers what README.md has always promised
  -- "the understanding, the open questions, the readiness verdict and the decision brief, all read
  from the payload bundled with the install" -- rather than stopping one document short of it (#429).
  Previously the Documents card showed "Nothing generated yet" and `GET .../artifacts/brief` 404d;
  README.md and docs/web.md had already been softened to describe that gap rather than close it.
  `web/example.py`'s `example_brief()` reads a new bundled `assets/demo/brief.md` -- the real
  `brief_markdown()` rendering of the same run's decisions, challenges and opportunities `model.json`
  already carries, produced once offline rather than assembled at request time -- and `seed_example`
  saves it through the same `ArtifactService.save` path a live generation uses, so it carries a
  computed freshness rather than an asserted one and is never re-seeded once a real brief (seeded or
  the reader's own) already exists for the session.
- Compatibility: compatible -- an additive change to what one existing route (`POST
  /sessions/example`) produces; no persisted shape, `--json` output or CLI contract changes. See
  `docs/compatibility.md`.

- The sdist shipped a `tests/` directory that could not even be collected -- 66 files without the
  underscore helpers they import (`_fakes.py`, `_cli_harness.py`, `_scan.py`, `_credentials.py`,
  `conftest.py`), without `tests/web/`, the root `conftest.py`, `scripts/` or `fixtures/golden`
  several guard tests read (#431). `MANIFEST.in` now prunes `tests/` from the sdist entirely --
  ship the wheel, not a half-copy of the suite; the wheel-install CI job and the publish gates are
  what verify a built artifact, in place of a distro packager re-running this repository's own
  self-scanning tests against an extraction that was never their subject.
- Compatibility: compatible - the sdist previously could not be collected as a test suite at all
  (`ModuleNotFoundError` at the first import), so nothing that worked before stops working; the
  wheel, the only artifact `pip install requivo` resolves by default, is unchanged.

### Security

- `requivo artifact show` no longer prints a saved artifact's content to the terminal raw (#430). A
  hostile client request that steers the model into saving an artifact carrying a raw ANSI escape
  sequence used to reach the operator's terminal verbatim and could move the cursor, clear the
  screen, or forge a line in Requivo's own voice. `core.selectors.display_document` — a
  document-shaped sibling of #213's own `display_text`, which escapes control characters but
  preserves a real newline and a real tab so a multi-paragraph document's own layout survives — is
  applied at print time only. The saved file on disk and the web download route are untouched: both
  stay byte-identical, which is what `core/integrity.py`'s hashing rests on. SECURITY.md now states
  that on-disk artifacts are unsanitized markdown and that opening one directly (`cat`, an editor) is
  at the reader's own risk.
- Review of this change found the identical gap still open on four sibling verbs — `requivo prd`,
  `criteria`, `epic` and `release` print each generator's markdown straight to the terminal in
  `cli.py`, with no neutralization at all. Those are **not** fixed by this change; #430's own scope
  was `artifact show` only, and the sibling gap is reported separately.
- Compatibility: compatible - `artifact show`'s output is unchanged for any document with no C0/C1/DEL
  control character, which is every artifact this engine has ever produced in practice. Only a
  document carrying such a character (never seen from an honest reply) now renders escaped instead of
  raw. No file format, `--json` payload, or exit code changed.

- Fixed (#449): `requivo prd`, `criteria`, `epic` and `release` printed their generator's Markdown
  straight to the terminal, through none of `display_token`/`display_text`/`display_document` — the
  same #213 class #430 closed for `artifact show`, on a path that fires on every ordinary generation
  rather than only a later read-back. A hostile client request that steers the model into an
  artifact carrying a raw ANSI escape sequence used to reach the operator's terminal verbatim, and
  no saved artifact ever had to exist for it to happen: the reply reaches the terminal on the one
  paid call that produced it. All four now route their print through `core.selectors.display_document`
  at print time only, the same shape #430 established for `artifact show`: the file each verb reports
  writing, and the web download route, stay byte-identical, which is what `core/integrity.py`'s
  hashing rests on. `display_document`, not the plainer `display_text`, because each of the four
  writers (`prd_markdown`, `criteria_markdown`, `epic_markdown`, `release_markdown`) produces a
  genuine multi-paragraph document — headings, bullet lists, a requirements table — whose newlines
  and tabs are its layout, not incidental whitespace. SECURITY.md and docs/cli.md are updated; two
  shipped source comments that described `artifact show` as "the last unguarded member of the #213
  class" are corrected to name these four as the actual last members.
- Compatibility: compatible - an ordinary generation with no control character in its reply renders
  exactly as before. Only a reply carrying a C0/C1/DEL control character (never seen from an honest
  model reply) now renders escaped at the terminal instead of raw. No session format, saved-artifact
  content, `--json` payload, or exit code changed.

- Fixed (#456): `golden_diff`'s baseline-freshness readout (`scripts/golden_lib.py`'s
  `baseline_commits_since`) could have a crafted commit subject forge a second row past
  `display_token`, the guard that is applied to the rendered subject at print time. The defect sat
  one layer earlier than the guard: `str.splitlines()` treats nine characters as ending a `git log
  --format` record — `\r`, `\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85`, U+2028 and U+2029 — none of
  which `git log` itself treats as a record boundary, so a subject carrying one became a second,
  attacker-controlled row whose forged `sha`/`date` printed unescaped. `\r` needed a second fix, not
  only the split: `subprocess.run(text=True)`'s own universal-newlines translation silently rewrites a
  lone `\r` into `\n` before any application-level code ever sees the string, so the `\r` case in
  particular would have survived a change to the split call alone. `scripts/golden_lib.py`'s `_git`
  now captures git's output as bytes and decodes it explicitly with UTF-8, and
  `baseline_commits_since` splits records on `"\n"` rather than `.splitlines()`. `date` and `sha` are
  now routed through `display_token` at print time too, for defense in depth, even though neither is
  presently attacker-reachable.
- Compatibility: compatible - `scripts/` ships in neither the wheel nor the sdist, so this is a
  developer-tooling fix with no session format, `--json` payload, or public API change. An ordinary
  commit subject renders exactly as before.

## [3.0.0] - 2026-09-01

### Highlights

- **Breaking** — `answer`, `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic` and `release` now take a session slug only; a `model.json` path is refused by name instead of quietly resolving to a session you never typed.
- **Breaking** — `DiscoveryService.draft_assessment` and six uncalled symbols are gone, and `requivo discover -` refuses a terminal or empty stdin instead of exiting 0.
- New `requivo session restore <session>` copies a trusted revision over a torn `model.json` — the documented recovery path that previously did not exist.
- Requivo Web opens without an API key: **Explore a worked example** turns the bundled sample into a real session you can read on a first run.
- Sessions stored under a Windows reserved device name (`con`, `nul`, `com1`…) are reachable again from the CLI, from Requivo Web and from `doctor`, which no longer reports their own lock files as residue.
- Session pages, model exports and artifact downloads answer `Cache-Control: no-store`, so a shared proxy or a borrowed browser cannot retain them.

### Added

- Added `requivo session restore <session> [--revision N]` — the documented recovery path for a torn or inconsistent session (#210). It copies a readable, *trusted* `revisions/NNNN-model.json` over `model.json`, under the session's write lock, without touching the revision log: no new revision, no bump to `current_revision`, no new file under `revisions/`. "Trusted" means it checks the file's content hash against the one `session.json`'s own revision log recorded for it — not merely that the file parses — the same check `session verify`'s `revision_hash_mismatch` finding already makes on the read side, so this repair tool never trusts what that diagnostic would refuse. Defaults to the newest revision this build can still read and trust, falling back to an older one when the last revision's own file is itself unreadable or tampered; that fallback is a **partial** repair and says so — `session verify` will keep reporting the session as inconsistent afterwards, correctly, because the last revision's content is genuinely unrecoverable. `--revision N` picks a revision explicitly and is refused, never silently substituted, when it does not exist, cannot be read, does not parse, or does not match its recorded hash. `session verify` now names the same recovery when it reports `invalid_model`, `model_is_not_the_last_revision` or `missing_model` — the three problems this verb can help with — printing the newest trusted revision and the exact command to run, or saying plainly when it could not check. `doctor` and `session verify` remain strictly read-only; `session restore` is the explicit, user-invoked repair beside them.
- Compatibility: compatible - a new verb and new terminal output only; no existing `--json` payload, exit code or file format changes. `session restore` deliberately does not accept `--json`.

- Requivo Web has a keyless activation path: **Explore a worked example** on the home page (#226). Web is the declared product experience, and a first run with no API key showed an empty page and a provider notice — the CLI got `requivo demo` because "a visitor shouldn't need a key, a clone, and a venv before feeling what the product does", and the primary surface never got the equivalent. One click now materialises the bundled sample — the same messy client email `requivo demo` replays — as a real session in your workspace, through the ordinary validated path (`SessionService.create_session` + `update_model`), so it has a revision, a frozen copy under `revisions/`, a readiness verdict computed by the same code as yours, and it opens in the CLI and in Claude Code. Nothing is called and nothing is reasoned: the payload already shipped in the wheel and is only read. The revision it writes records the surface that applied it and claims no provider and no model name, because none reasoned it (invariant 6). Three things the issue left open are decided here and argued in `web/example.py`: a second click returns you to the session you already have rather than minting a second or refusing; the button stays after you have sessions of your own, since showing it only on an empty workspace puts the example one real session out of reach; and the sample is an ordinary writable session, so its own page says in as many words that reading it costs no API key and that refining or generating does need one. It is labelled *Example* on its row and on its page, and that label is decided by the request the session carries rather than by the slug it landed under — a workspace already holding a session of that name would otherwise push the sample to a derived slug, losing the badge and handing it to the squatter.
- Compatibility: compatible - purely additive. One new route (`POST /sessions/example`), one new module (`src/requivo/web/example.py`), one new caption. No session-format change, no `--json` payload change, no existing route's status or body altered, and nothing new is required of an install.

- The request and answers fields in Requivo Web now show a live character count once you pass 80% of the 20,000-character ceiling, and say plainly that the submission will be refused once you are over it (#239). It **counts and warns and never clips**: it does not write to the field, does not add an HTML `maxlength`, and does not block a submit, so an over-long paste still reaches the server and still comes back refused with your text preserved. That is invariant 3 at the one place a reader meets it — a browser that trimmed the paste would leave an over-long request arriving at exactly the ceiling and passing the very check written to catch it (#8), which is why the affordance had to be this shape and `docs/web.md` asked for it in these words. The number shown is rendered from `web/config.py`, so the count and the server's refusal cannot drift. With JavaScript off nothing changes: no counter, and still no clipping.
- Compatibility: compatible - a `data-limit` attribute added to two textareas and a counter element created client-side. No route, payload, session format or error code changes; the server-side ceilings and refusals are untouched, and no template gained a clipping attribute.

- `CONTRIBUTING.md` now maps the guards that read your source and your prose, so a red one is a two-minute fix instead of a mystery (#289). Sixteen guard files police form rather than behaviour — an import, an encoding declaration, a version string, a comment that names a test, a heading a test parses — and a small pull request can trip several of them. The new table says what trips each one and where the fix goes, and calls out the two couplings that run in the direction no newcomer predicts: a test's *name* is load-bearing API for source prose, so renaming one breaks documentation until you grep for it; and `docs/compatibility.md` is parsed as data by three assertions across two test files, so editing a heading breaks the build in a file that looks like documentation. Two stale claims in the same file were corrected while there: the local check block omitted `pyright`, which is a required check, and the page said nothing in the test suite reads the maintainer-loop config when `tests/test_version_sites.py` cross-checks `.oss.json`'s `version_sites` in both directions.
- Compatibility: compatible - documentation only. No code, no test, no CI leg and no published surface changed.

- Per-call token usage is now stamped into revision provenance, so a session's cost is answerable after the fact (#292). Every provider-backed apply (`requivo discover`, `answer`, `brief`) now records the input/output/cache tokens its call(s) actually spent, plus the exact `(input, output)` USD-per-million-token rate they were billed at and the rate table's own date, into that revision's entry in `session.json`. `requivo status` sums every revision that carries this into a cumulative "SESSION COST" line, using the same three-state rendering `render_usage()` already uses for one run (exact tokens, a labelled estimate, or "no price on file") — silent, never `$0.00`, when nothing on the session carries it. A deterministic apply (a hand-authored `model apply`, a Claude Code turn, `session import`) and every revision written before this shipped carry none of these fields, by design.
- Compatibility: compatible - purely additive fields on `RevisionRecord` (`usage_input_tokens`, `usage_output_tokens`, `usage_cache_read_tokens`, `usage_cache_write_tokens`, `usage_rate_per_mtok`, `usage_priced_as_of`); `format_version` stays 1, and a session written before this change round-trips unchanged (`docs/session-format.md` documents the new keys).

### Changed

- The ten session-taking verbs (`answer`, `status`, `impact`, `brief`, `prd`, `stories`,
  `estimate`, `criteria`, `epic`, `release`) now call their positional `session` instead of
  `model`, matching every verb under the deterministic surface. `requivo status` with no argument
  said "the following arguments are required: model" about a thing the user supplies as a session
  slug, which is engine vocabulary and reads especially badly beside the `model` verb group -- the
  rendered usage was `requivo model <model>`. The accepted value set is unchanged: a slug, or a
  path to a saved `model.json` (#248).
- Compatibility: compatible - the name is an argparse `dest`/`metavar`, which is internal to the
  parser, and these positionals are passed by position rather than by name. No invocation changes;
  only the `--help` usage line and the missing-argument error read differently.

- `[tool.pyright]`'s `include` widens from `core/` + `services/` to the whole package (#271), the
  condition this project's own comment set for widening -- "once these have stayed clean" -- having
  been met once `services/discovery.py`'s `GenerateResult.artifact: object` (renamed `Generated`
  since the issue was filed) was given a real type. `Generated` is now generic
  (`Generated[T]`), and `DiscoveryService.generate()` carries five `Literal`-keyed `@overload`s (one
  per artifact type it actually saves a document for) plus a plain-`str` fallback for a caller that
  only holds the type name in a variable -- so `disco.generate(slug, "prd")` resolves to
  `Generated[PRD]` at the call site with no cast anywhere the caller can see, and every one of the
  six `.artifact` reads in `cli.py` type-checks with zero edits to that file (nine `pyright`
  diagnostics before the fix, not the eight the issue measured -- re-measured directly rather than
  trusted; nine rather than six because `epic`, read once at line 782, is then passed to four
  separate renderer/export calls, each its own diagnostic). Two narrower gaps in
  `providers/anthropic/client.py` and `completion.py` (an SDK-optionality return annotation, and a
  dict literal whose inferred value type couldn't hold a nested `cache_control` block) are also
  fixed. `src/requivo/render/` is excluded from the widened scope rather than folded in: it carries
  five pre-existing `pyright` diagnostics from two `None`-narrowing gaps this issue's own Scope
  section does not name and its acceptance criteria does not ask this change to fix; filed
  separately.
- Compatibility: compatible - a type-checking and dev-tooling change only. No runtime behaviour, no
  session format, no `--json` payload and no CLI surface changed; `pyright src/requivo` now exits 0,
  which is the CI leg's own existing command (`.github/workflows/ci.yml` already runs bare
  `.venv/bin/pyright`, so no workflow edit was needed to pick up the widened scope).

- Two sentences that oversold the cost of writing a second reasoning provider now say what is actually true (#273). `services/discovery.py` and `docs/providers.md` both said "a second provider is a constructor argument". That is true of the *protocol* — `DiscoveryService` takes a `ReasoningProvider` and nothing else — and false of the *build cost*: roughly 400 of the 1,057 lines under `providers/anthropic` are provider-neutral orchestration (the per-operation message builders, the generator tables, `prompt_version()`, the JSON extraction and fence-stripping, the contract validation, the corrective-nudge retry loop and the parse-first truncation policy), so a second implementation re-implements or copies them. Both places now state the swap cost and the implementation cost separately. The extraction itself is deliberately deferred, with a written trigger, in a new decision record (`decision: deferring-the-neutral-provider-layer`) — the neutral layer was measured at ~350 lines two days earlier and at ~400 now, and that growth is the trigger's own evidence rather than a correction to it.
- Compatibility: compatible - prose only. No code moved, no module was added or removed, no import path changed, and the `ReasoningProvider` protocol is untouched.

- Documented the meta-guard budget and froze the persistence diagnostics tier (#287). CLAUDE.md's "Where a bug narrative lives" section now states that the meta-guard estate — prose/CI/script tests that guard the repo's own self-description or source form rather than exercising shipped runtime code — is at budget (measured 2026-08-29: 24% and 10% of the then-suite respectively), and that a new one needs the same two-real-instances-of-drift bar, named by issue number, that #288 already applies to a new source-scanning tier; folding into an existing meta-guard file is preferred over opening a new one. It names #169 (the product-validation protocol) as where the next testing investment goes, not another guard. A new top-level "The persistence diagnostics tier is frozen" section states, retroactively and as a decision rather than a deletion, that no new report-only diagnostic lands in `core/persistence.py`/`deterministic/doctor.py` without a reproduced field instance of the state it reports and a named user action — the shipped lock-residue check stays, and the tier stops there.
- Compatibility: compatible - documentation only; no code, schema or `--json` change.

- Consolidated the triplicated scan plumbing across this suite's source-scanning guard tiers into one shared `tests/_scan.py` (#288). `test_boundaries.py` and `test_encoding.py` each carried a nearly byte-for-byte duplicate `scan()`, `_parse()` and `_write_tree()`; `test_narrative_references.py` re-derived the same "an empty or missing scan root is an error, not a clean answer" refusal (#10) a third time, over a wider suffix set and multiple roots. All three now import the shared implementation; every existing test function, docstring and assertion in all three files is unchanged, and the empty-scan refusal is exercised from all three exactly as before. `test_narrative_references.py` had no positive control at all for its own refusal before this change -- `test_the_guard_refuses_a_scan_it_could_not_make` closes that gap, found while doing this consolidation rather than invented for it. CLAUDE.md's "Where a bug narrative lives" section gains one paragraph stating the bar for adding a *fourth* scanning-guard tier: the same two-real-instances-of-drift bar this repository already applies to automatic context-card relevance routing, not a plausible first instance. Deliberately out of scope: `test_version_sites.py` and `test_plugin_cli_drift.py` were left untouched, per the issue's own scope list, and no reason attached to a specific guard was moved away from the line it explains -- only the mechanical walk-and-refuse plumbing moved.
- Compatibility: compatible - test-only; every guard's pass/fail behaviour is unchanged. `tests/_scan.py` is new, internal test infrastructure, not part of any public interface.

- Every provider-bound serialization is compact (#300). Six of the seven generators plus the stories
  JSON that `estimate` embeds were dumping the model with `model_dump_json(indent=2)` while
  `answer_turn` on the same code path already sent it compact -- seven sites, one measured at
  15,696 bytes against 14,110 on the demo model, so roughly 10% of those bytes were indentation
  nobody reads. It is small per call and paid on every artifact of every session. The system prompt
  is untouched, so `prompt_version()` -- which hashes that prompt and not the user message -- is
  unchanged, and no revision's recorded provenance moves. The issue described eight sites; there are
  seven, because `estimate` dumps the stories and not the model.

- `render/` joins the pyright scope, and `[tool.pyright]`'s `include` now covers `src/requivo` with
  nothing excluded (#393). It was the one directory of the shipped package left outside after #271,
  held out by five diagnostics from two `None`-narrowing gaps in `render/html.py` that were *believed*
  to be false positives -- and believed was the whole of it, on the one file the checker was not
  reading. Both were verified rather than suppressed, and both were right: `_INLINE_MARKUP` has three
  named alternation branches and no unnamed one, so `match.lastgroup` cannot be `None` for a match
  reaching `_inline`'s callback; and `markdown_to_html` gathers a line into a list block only when
  `_BULLET` or `_ORDERED` matched it, so the `_ORDERED` match in `_list_items` cannot be `None`.
  Each is now an `assert` at the site that depends on it rather than a `# type: ignore`, because the
  fact each rests on lives somewhere else in the file and an ignore would preserve neither -- an edit
  adding an unnamed alternation branch used to falsify the first silently. Two tests go red when
  either fact moves, one of them asserting the pattern's own group counts so it fires on a branch
  nobody here has thought of yet. CI already runs a bare `.venv/bin/pyright`, so the widened scope is
  what the leg checks with no workflow edit.
- Compatibility: compatible - a type-checking change plus two asserts that state invariants already
  true. No rendered output moves, and no session format, `--json` payload or CLI surface changes.

### Removed

- `DiscoveryService.draft_assessment` is removed (#300). It reasoned a decision brief for a model
  that was not a session yet, and lost its last caller in #202, when the CLI's interactive discovery
  stopped buying a brief before its one write and began persisting the converged model first and
  generating through `generate(slug, "brief")` -- the path every other surface already took. It was
  kept on the argument that the operation was coherent and the contract public; two majors on, the
  only thing exercising it was its own test. A caller that has a session should use
  `generate(slug, "brief")`, which absorbs the reasoning, cuts a revision and saves the document,
  none of which the removed method did.
- Six more symbols nothing called, and the dead surface around them, are gone too (#300):
  `core/persistence.py`'s `session_artifact_files` and `list_non_session_entries`,
  `core/analysis.py`'s `_is_deferred`, `render/terminal.py`'s `_wrap`, and `core/errors.py`'s
  `StaleArtifactError` together with the `"stale_artifact": 409` row it had in `web/app.py`'s
  error-to-status table. The first was worth more than its six lines: its docstring described "the
  on-disk set change-detection intersects with" as if a caller existed, where the real staleness
  resolution intersects `set(meta.artifact_status)` in `services/sessions.py` -- a dead function
  making a false claim about the algorithm this project's first invariant is about.
  `list_non_session_entries` was production-dead rather than unreferenced: `doctor` reads
  `scan_session_root` directly, and the only two callers were tests, which now read the same
  partition the product does. Its narrative -- what #67 was about, and why this is a report and
  never a repair -- moved onto `scan_session_root`, which is where the second part now comes from.
  `web/viewmodels/sessions.py`'s `session_detail` also stops assembling five keys no template reads
  (`remaining_gaps`, `question_count`, `summary`, `artifacts`, `generatable`); two of them carried
  raw engine-shaped `status()` values on the one view model most likely to be extended next, which
  is an invitation past the vocabulary layer rather than a spare.
- Compatibility: breaking - and it is the first bullet alone that earns the word. An external caller
  holding a `DiscoveryService` and calling `draft_assessment` directly gets an `AttributeError`.
  Graded breaking although `docs/compatibility.md` already classes `requivo.services` under *Python
  internals -- importable and documented, but not a published API*: a removal is graded on what it
  does to a caller, and the softer promise is a reason nobody should be surprised, not a reason to
  grade it down. No surface, verb, route or interface in this repository called it, and there is no
  deprecation window because it has had no production caller since 1.3.0 (PR #318, which fixed
  #202) -- this is the major where retiring it is legitimate. The second bullet is compatible on its
  own and is stated here rather than in a fragment of its own, because a fragment carries one
  verdict and the stronger one governs: none of those six was reachable from outside the package.
  `docs/compatibility.md` does publish the structured error envelope `{code, message, path?,
  details?}`, so the error-code vocabulary is a public surface and `stale_artifact` was checked
  against it rather than assumed -- nothing has ever raised `StaleArtifactError`, so no envelope
  could carry that code and no `409` could come from that row; a consumer branching on it was
  branching on something unreachable. The `session_detail` keys are internal to templates that
  never read them, and no `--json` payload, session-format key or CLI surface changes.

### Fixed

- A paid decision brief is no longer discarded when its model apply loses a revision race (#208). `DiscoveryService.generate(slug, "brief")` pays for the assessment before applying it; if a concurrent write (a second tab, the CLI, a Claude Code turn) landed on the session meanwhile, the apply used to raise and the paid content vanished with it — not saved, not rendered, not mentioned. It is still saved now: the document is written against the revision it was actually reasoned from, honestly flagged stale, and the raised `revision_conflict` states both facts and the remedy (`requivo brief <slug>` will refresh both) rather than only the conflict. A parallel, rarer case is closed the same way: an `OSError` from an artifact write after any paid generation (a full disk, a permissions error) now surfaces as a structured `artifact_write_failed` error naming the target, not a bare traceback.
- Compatibility: compatible - a new, additive error code (`artifact_write_failed`) and new optional `details` keys on `revision_conflict` (`artifact_saved`, `artifact_type`, `artifact_stale`); the message text on the brief-conflict path is longer, and `docs/compatibility.md` already says to assert on the code, never the message. No session format change, no removed or renamed field.

- Paid web actions are now guarded server-side against cross-tab and refresh double-submission (#209). Two concurrent first-discovery calls on one session (a second browser tab, or a refresh-and-resubmit during the minutes-long call) both used to pass the revision-zero gate and both paid for a provider call, with only the loser's write refused afterwards by the optimistic lock. A new, non-blocking per-slug guard is now held for the span of the provider call plus its apply: the second caller is refused immediately, before it spends anything, with the same `session_locked` (503) response the store already uses for "the write never started; retrying it unchanged is correct". A crashed holder releases the guard the instant its process dies — no stale-lock cleanup is needed. Covers both first-discovery doors (`POST /sessions`, `POST /sessions/{slug}/discover`), since both route through the same service-layer entry points.
- Compatibility: compatible - purely additive: a concurrent first-discovery request can now receive an existing, already-documented `session_locked`/503 response in a circumstance where it previously reached the provider; no code, `--json` key or session format changed.

- A session nobody could read no longer leads the home page with the engine's own words, and the page its row sends you to now states them in full (#240). The degraded row is invariant 15's third state and it was right about what it did not know; what it printed was `str(e)` — an absolute path and a pydantic class name for a truncated `model.json`, `[Errno 21] Is a directory` for a `request.md` a crash left as a directory — on the one screen whose design rule is that engine vocabulary never leads. The row now carries one line in the product's own register and keeps the full text for the two places somebody acting on it is already standing: the session page and the server log. **Where the store had already written a sentence for a reader, that sentence stays.** A session from a newer Requivo says *session format v2 is newer than this Requivo understands (v1) — upgrade requivo*: one line, no path, no class name, and it carries the one thing a generic sentence cannot, which is what to do. Replacing it would have been a strict loss rather than a trade, and it is the over-correction this issue had to avoid; a positive control asking whether each break mode had anything to leak at all is what caught it, since no assertion about engine vocabulary can fail on a message that contains none. The row is otherwise deliberately no friendlier than before — the badge still reads *Could not be read*, still in the danger style, and the row still states no timestamp, question count or freshness verdict it did not read. Opening such a session used to raise: `home.html` has promised since #7 that the session screen is where the full error is stated and that this is the one place a reader can act on it, and the reader instead got a generic error page naming neither the session nor anything to run. There is a page for it now, naming the session, quoting the failure untruncated, and pointing at `requivo session verify` — the same remedy and the same wording the CLI already uses for this state. Its guard wraps only what the route *reads*: rendering happens outside it, so a bug in a template can no longer be reported as a corrupt session, and the existence probe is inside it, so a session whose directory cannot be stat-ed reaches the same page rather than the generic one.
- Compatibility: compatible - the HTTP status of a session page is unchanged on every arm and is pinned by a new test: 409 for a session written by a newer Requivo, 500 for a store that could not answer, 404 for a session that is not there, 200 for one that reads. What changed is the body. The home row's view-model dict gains a `hint` key alongside the `error` key it already had; `error` still carries the untruncated text and is unchanged, and neither is a published payload — `session list --json`, which is, is untouched.

- `--workspace` is now accepted after the command as well as before it (#249). It was declared on
  the root parser alone, so the natural phrasing `requivo status <slug> --workspace DIR` died with
  argparse's bare `unrecognized arguments: --workspace DIR` at exit 2 -- a message that names a flag
  this CLI absolutely does know as unknown, and sends the reader hunting for a typo. The constraint
  ("Place before the command") existed only in `--help` text, which the person who has just hit the
  error is by definition not reading. It is now re-declared on every subparser at every depth,
  including the nested `session`/`model`/`artifact` groups where it has to sit on the leaf parser to
  be reachable at all, each copy with `default=argparse.SUPPRESS` so an absent per-verb flag still
  leaves a global `requivo --workspace DIR <command>` exactly as it was -- the pattern `web` has
  carried alone since it was written. The global flag's help drops the sentence stating the rule that
  no longer exists. An unknown flag after the command is still refused at exit 2, unchanged.
- Compatibility: compatible - widening only. Every invocation that worked before works identically,
  and `requivo --workspace DIR <command>` is untouched; what changes is that an invocation which was
  previously refused at exit 2 now succeeds. No condition moves off a documented exit code in the
  narrowing direction, no `--json` payload, error code or session format changes, and the exit-code
  table in `docs/compatibility.md` still means exactly what it says.

- Fixed the one silent gap in the "adding a slot" workflow (#269): nothing previously caught a new schema slot that no specific artifact's staleness map (`core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW`) consumed — such a slot would mark nothing stale for `prd`/`stories`/`estimate`/`criteria`/`epic`/`release` when it changed, only the assessment via `brief`'s `*` entry, invariant 1's own failure shape landing on the most routine change the schema will ever see. `tests/test_dependencies.py::test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted` now fails on a required slot in neither an artifact's set nor the named-and-reasoned `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT` exemption list (`current_process` and `reporting` today, each with its own reason). CLAUDE.md's "Extending" section gains an "Adding a slot" checklist naming the three mandatory files, the six conditional ones, the golden re-capture consequence, and which parts are guarded versus checked by eye; `docs/requirements-model.md`'s "Dependencies and staleness" section states the coverage guarantee for a product-facing reader.
- Compatibility: compatible - test and documentation only; no schema, contract or `--json` change.

- The artifact-type vocabulary -- `_GENERATORS`/`_OP_PROMPTS` (`providers/anthropic/generators.py`),
  `_WRITERS`/`GENERATABLE` (`services/discovery.py`), `_ARTIFACT_SLOTS_RAW`/`ARTIFACT_FILES`/
  `ARTIFACT_FILENAMES`/`REASONING_CONSUMERS` (`core/dependencies.py`) and `ARTIFACT_LABELS`
  (`web/viewmodels/labels.py`) -- had no test asserting these ~8 registries agree on their key sets
  (#270). The dangerous drift was silent: a type present in `ARTIFACT_FILENAMES`/`_GENERATORS`/
  `_WRITERS` but missing from `_ARTIFACT_SLOTS_RAW` is never flagged stale, because
  `services/artifacts.py`'s `_stale_since` reads both `REASONING_CONSUMERS` and `propagate()` off
  that one map alone -- invariant 1's own "a stale document reports itself as up to date" failure,
  reachable from the single most routine change this vocabulary sees (adding a generator).
  `tests/test_dependencies.py` now asserts every table against `_ARTIFACT_SLOTS_RAW` as the one named
  canonical source, with a positive control (a deliberately broken fixture copy the same check must
  reject) rather than only a passing test over the current tables. **`ARTIFACT_FILES` was itself
  missing from the guard's first cut and from this fragment's own checklist, found in review of this
  change**: `services/sessions.py`'s `_resolve_stale`, run eagerly on *every* apply rather than only
  at save time, iterates `for t in ARTIFACT_FILES` -- a third table distinct from the
  `_ARTIFACT_SLOTS_RAW`/`ARTIFACT_FILENAMES` pair the save-time path (`_stale_since`) reads, so a type
  present everywhere else and absent only from `ARTIFACT_FILES` was never auto-flagged stale by that
  path, which this change's first cut left uncovered -- now checked and pinned by its own positive
  control. `CLAUDE.md`'s
  "Adding a generator" checklist named only three of the eight registration points and misplaced one
  of them (`_WRITERS` under `render/markdown.py` rather than `services/discovery.py`, where the table
  actually lives); it now names all eight, including `ARTIFACT_FILES`, and points at the new guard.
  `web/routes/artifacts.py`'s download route no longer invents a filename
  (`f"{artifact_type}.md"`) for a type `ARTIFACT_FILENAMES` does not know -- a fallback that could
  never actually fire, since the route already refuses an unknown type one line above it, against the
  repo's own refuse-don't-guess rule (invariant 3).
- Compatibility: compatible - a test-suite and documentation change, plus a dead fallback branch
  removed from one route. No product code path, session format or `--json` payload changed; the
  download route's observable behaviour for every type it has ever actually served is unchanged.

- The README no longer dates itself against the release it ships in, and the docs index reaches the decision records (#290). `README.md`'s status line said "at **1.0**" while 1.2.0 was on PyPI, was corrected to "stable on the 1.x line", and was false again within hours of 2.0.0 — the same prose-dates-itself class the 1.0.0 release audit named. It now states what is stable (SemVer, and the published session format) without naming a release line, so no future version can falsify it. `docs/README.md` gained a **Decision records** entry pointing at `docs/decisions/`, which the index had never mentioned; `CLAUDE.md`'s docs list names it too. Separately, the hardcoded golden-harness total ("a full six-request cycle is 18") is gone from `CLAUDE.md` and `scripts/golden_run.py`: a new `planned_calls()` derives the API-call ceiling from the requests a given invocation actually parsed and selected, and `golden_run.py` prints that live figure before spending anything, so adding a request to `fixtures/golden/requests.md` can no longer leave two documents quietly under-counting what a capture costs.
- Compatibility: compatible - documentation and a harness script only. `planned_calls()` is new and additive in `scripts/golden_run.py`, which is not shipped in the wheel; the number `main()` prints is unchanged for today's request set. No product code, no session format, no CLI surface, no `--json` payload.

- `requivo web` now gives the `requivo.web` logger a formatted stderr handler, so the app's own records carry a timestamp, a level and the logger name instead of reaching Python's last-resort fallback as a bare message interleaved with uvicorn's lines (#291). The 500 page tells the reader to check the server logs, and until now a 5xx investigated an hour later could not be tied to a request time. `logging.lastResort` is also fixed at WARNING, which made the second half worse than unformatted: the `INFO` line `web/spend.py` writes to tell the operator what a paid call cost was dropped entirely, so "no handler" and "nothing was spent" printed identically. **What an importing host gets is unchanged**: the handler is attached by the `web` verb, which owns the process, and never at import or in `create_app()` — a service that mounts this FastAPI app keeps its own configuration of `requivo.web`, the root logger and uvicorn's loggers are untouched, and a logger somebody else has already configured is left exactly as they set it. Known limit: under `--reload` uvicorn spawns a worker process that re-imports the app and has not been through the entry point, so that development flag's own worker still logs unformatted.
- Compatibility: compatible - a handler on one named logger, attached only by the `requivo web` entry point. No route, payload, session format or error code changes, and nothing is added at import time.

- Fixed a vacuous lock-wait assertion in `test_export_excludes_the_lock_file_and_waits_for_the_writer` (#293). The test's `held.is_set()` check ran *after* `t.join(timeout=10)`, but `join()` blocks until the writer thread finishes regardless of whether `session export` ever waited for its lock -- and the writer always calls `held.set()` on its own way out -- so the assertion read True whether or not `session export` actually serialised against a concurrent writer. Confirmed directly: monkeypatching `session_lock` to a no-op left the original assertion order green. The read now happens the instant `session export` returns, before `t.join()`, which can only be True if the export's own read genuinely blocked on the writer's lock; verified red against the same no-op patch, then green again with the lock restored. The `time.sleep(0.05)` standing in for "the writer has the lock by now" is also gone, replaced by an `Event` the writer signals the instant it acquires the lock -- a scheduling guess with no positive control of its own, which a slow runner could let `session export` outrun without the test ever noticing it had measured nothing.
- Compatibility: compatible - test-only; no production code changed.

- Merged three near-duplicate surface utilities into their shared home (#301). `cli.py`'s `_is_file_arg` re-implemented the same three pathlib traps (blank string, a directory, an over-long name raising `OSError`) that `deterministic/_shared.py` already handled under a different name -- both now share `is_file_argument`, promoted to public and re-exported from `requivo.deterministic` alongside `read_user_text`. `cli.py`'s `status --json` and its own top-level error envelope called `json.dumps(..., indent=2)` directly instead of `_shared.py`'s `print_json`, which carries the #70 `ensure_ascii` contract against control-character forgery -- both now route through `print_json` (also promoted to public), so that contract has one enforcement point instead of one enforced site and one that happened to match it by using the same defaults. `framework/model_schema.json`'s `slots` list was parsed independently at four call sites across `core/contracts.py` (`schema_slot_ids`, `_schema_order`) and `core/analysis.py` (`slot_meta`, `_default_impacts`) -- all four now project from one new cached `schema_slots()` in `core/contracts.py`, so the file is read and parsed once rather than four times on a cold cache; each projection keeps its own shape and cache, since they slice the rows differently. No behaviour changed: same three traps, same JSON contract, same schema data, fewer copies of each.
- The literal grep in the issue's own acceptance criteria ("model_schema.json opened by exactly one function in src") is not met by design: `core/context.py` and `deterministic/doctor.py` also open the file, but for its raw text -- prompt substitution and the `requivo schema` verb's literal output -- not the parsed slot list, and folding those into the parsed cache risks changing the prompt hash (invariant 2) or the verb's console output, both explicitly out of scope for this issue.
- `docs/compatibility.md`'s not-stable section said `requivo.deterministic`'s `__all__` was three names; it is five now that `is_file_argument` and `print_json` joined it, so the count and the enumeration are corrected in the same change.
- Compatibility: compatible - internal-only; `is_file_argument` and `print_json` were `_`-prefixed and unexported, `schema_slots` is new. No `--json` payload, session format or documented CLI behaviour changed.

- #302: `core/analysis.py`'s `_label`, `_slot_meta`, `_readiness_blockers` and `_state_of` were underscore-private and imported across four other modules anyway (`core/dependencies.py`, `cli.py`, `render/terminal.py`, `render/markdown.py`) -- the privacy marker was false everywhere it mattered, and a rename inside the module would have broken all four with no deprecation surface. Renamed to `slot_label`, `slot_meta`, `readiness_blockers` and `state_of` and made genuinely public: each is core domain logic (schema projections, the readiness rule, the confidence-to-state classification) that more than one surface legitimately needs, so publishing the name is the honest fix rather than moving the logic into whichever caller happened to import it first. `slot_labels` (the list-form convenience) is unchanged. No behaviour changed -- mechanical rename, `grep -rn "analysis import _" src/requivo` now returns nothing.
- Compatibility: compatible - internal rename only; these names were never documented as public API and carry no format or `--json` promise (docs/compatibility.md does not name them).

- Requivo Web no longer tells a reader to install a provider they have already installed (#339). The
  probe in `web/config.py` wrapped its whole import in a bare `except Exception` that answered
  `sdk = False`, so any failure to import `requivo.providers.anthropic` -- a broken transitive
  dependency, a partially installed package, an SDK major this Requivo cannot import, a syntax error
  in an installed module -- rendered as "not installed", and the UI stated "Install the provider: pip
  install 'requivo[anthropic]'" over a cause it had never established and never named. `ProviderStatus`
  now carries three states per fact rather than two: `True` installed, `False` absent, and `None` for
  the probe could not look, with the exception's type and message on a new `probe_error` field and in
  the reason the page renders. Genuine absence is still `False` and still names the install -- that
  case is the one where the import succeeds and `Anthropic` is None, which is a fact the probe really
  did establish. The credential half now has its own arm rather than sharing the import's `except`, so
  an import that succeeded no longer discards the SDK answer it had already obtained; `available`
  reads `is True` on both, so an unestablished fact can never enable a paid action. A stale comment in
  `web/routes/sessions.py` naming a `_analysis_failed` that has never existed is corrected to
  `analysis_failed` in the same change, as the issue asked.
- Compatibility: compatible - `ProviderStatus` is internal to the Web surface: not a documented Python
  API, named nowhere in `docs/compatibility.md`'s stable-interface sections, reaching no `--json`
  payload (`doctor --json`'s `provider_anthropic` rows come from `deterministic/doctor.py` and are
  untouched), and crossing to the templates only as `available` and `reason`. `available` keeps
  exactly its meaning in both previously-reachable states; only the message text changes, and the same
  page already says to assert on codes rather than on messages. No exit code, error code or session
  format changes.

- `tests/test_boundaries.py`'s storage-boundary guard now scans `providers/` (#355). The guard's whole job is to require an allowlist entry for anything outside `services/` that reaches `core.persistence` directly, bypassing the injected `SessionRepository` seam -- and its scan set only ever covered `cli.py`, `deterministic/` and `web/`. `providers/anthropic/completion.py` already reached `core.persistence`'s private, underscore-prefixed `_atomic_write` and `ensure_store_dir` to preserve a malformed provider reply for a bug report (#283), with no allowlist entry and nothing watching for a second, less justified one to join it unnoticed. The import itself was already defensible (the alternative is a second atomic-write implementation, which invariant 16 exists to prevent); what was missing was the guard actually watching that directory. Both names now have their own `_SURFACE_STORAGE_ALLOWLIST` entries, keyed by `(file, name)` like every other one. No production behaviour changed -- the reach was already there and already correct; only the guard's blind spot closed.
- Compatibility: compatible - test-only; no runtime code changed, and the guard's own pass/fail behaviour over the pre-existing import is unchanged (it now has a reason on file instead of none).

- `CLAUDE.md`'s golden-harness section documented a bare `golden_run.py` as capturing every
  interactive request, which stopped being true when #276/#356 shipped the opposite default (a bare
  run now skips every interactive request, naming each skip and the command to capture it alone;
  `--all` or an explicit slug opts back in) (#358). The section now says so, in both places the old
  behaviour was stated: the walkthrough after the `bash` block, and the `Cost:` paragraph, which used
  to present "capture it alone" as a recommendation the script did not enforce, when as of #276 it
  does. The follow-up this issue also names -- pointing `framework/elicitation.md`'s hand-kept-vs-
  guarded bullet at `decision: elicitation-schema-hand-kept` -- was already carried by an earlier
  change (#383/c15fd8b) by the time this one landed; re-verified rather than duplicated. The edit
  itself is the issue's own subject: #356's attempt was refused by the harness's permission
  classifier on this exact file, on every route it tried, and the maintainer hit the identical
  refusal afterwards -- retried here first, before anything else in this change, and it went through
  cleanly, so whatever caused the refusal was environment-specific rather than a standing block on
  this filename.
- Compatibility: compatible - documentation only.

- `requivo discover -` now reads the request from stdin, like every other verb that takes a document
  (#360). `session init -`, `model apply <slug> -` and `artifact save --file -` all route through
  `deterministic/_shared.py`, which special-cases a bare `-`; `discover` called `is_file_argument`
  directly instead, and `is_file_argument("-")` is False -- correctly, `-` is not a file -- so the
  argument fell through to the treat-as-literal-text branch and the engine was asked to discover a
  product from the two-character request `-`. Quiet, plausible-looking, and billed: the obvious shape
  for exactly the input this product exists to consume (`cat request.txt | requivo discover -`) spent
  a paid call producing a session about nothing. `discover` now shares `read_source`, promoted to
  public in `requivo.deterministic` for the same reason `is_file_argument` was in #301. A source that
  turns out to be empty -- an empty pipe, an empty file -- reaches the same "discover needs a request"
  refusal a blank literal argument already reached, before any provider call; `-` with a terminal on
  stdin is refused rather than hung on, by the shared reader's own guard. A file path argument and a
  literal request, including a one-character one that is not `-`, behave exactly as before.
- Compatibility: breaking - three conditions move off exit 0. `requivo discover -` with a terminal on
  stdin now exits 1 rather than discovering on the literal `-`; `discover -` with an empty pipe, and
  `discover <path>` where the file is empty, now exit 2 rather than paying for a call on nothing; and
  `discover -` from a non-empty pipe now discovers on the piped text rather than on `-`. Every one of
  those old behaviours was the defect this fixes, and none was ever documented -- `docs/cli.md`'s
  "Documents on stdin" section has always listed the other three verbs and never claimed `discover`
  was one of them. But the rule at the foot of the exit-code section in `docs/compatibility.md` is
  mechanical rather than a judgement about harm, and the direction here is the narrowing one: an
  invocation that previously succeeded can now fail. That is the same shape as #255, which was
  declared compatible on a harm argument and corrected to breaking before its tag, so it is declared
  breaking here rather than argued down. Nothing else moves: no `--json` payload, no error code, no
  session format, and a literal request and a non-empty file behave exactly as before.

- `session migrate` no longer aborts the whole pass -- with no receipt printed at all -- when a
  legacy session's `request.md` is not valid UTF-8 (#371). #262 wrapped `repo.read_meta(slug)` on the
  occupied-slug branch in `except RequivoError`, but the two reads that decide `interrupted` vs.
  `skipped` once that succeeds -- `repo.request_text(slug)` and the legacy directory's own
  `request.md`/`request.txt` -- sat outside that guard: neither an `EACCES` `Path.exists()` re-raises
  nor a `UnicodeDecodeError` is a `RequivoError`, so an undecodable legacy request escaped invariant
  15's own per-slug isolation exactly the way an unparseable `model.json` used to before #262, taking
  every other slug in the same sweep down with it. Both reads now sit inside the same `try`, widened
  to `(RequivoError, OSError, UnicodeDecodeError)`, and the broken slug is reported under `errors`
  like any other unreadable legacy session.
- Compatibility: compatible - the previous behaviour on this input was an unhandled traceback with no
  defined exit code or `--json` shape, never a documented one; nothing that read a stable contract is
  affected, and every already-passing legacy session migrates exactly as before.

- `status`, `session show`, `session verify`, `session export` and `session list` no longer refuse a
  session already on disk under a Windows reserved device name (`con`, `nul`, `lpt1`, …) (#372). #221
  refused
  the name unconditionally through `validate_slug`, which every read path funnels through via
  `canonical_dir`, so a session already on disk under such a name -- created before #221 shipped, or
  on a platform that never refused it -- was unreachable by every verb, `session export` included, the
  documented way to move it off the reserved name entirely. Creation stays exactly as strict:
  `validate_slug` itself is unchanged and unconditional, and `canonical_dir` (which `create_session`
  calls) still refuses a *new* reserved slug the moment nothing already occupies it -- only a name a
  session already claims on disk earns the read-only exception, in `canonical_dir`/`legacy_dir`
  (`core/persistence.py`'s `_child_of`) and in the read-consistency lock `session export` takes
  (`lock_path`), both checked against whether a session already occupies the name rather than a
  general relaxation of #221.
- Compatibility: compatible - restores the behaviour `docs/compatibility.md`'s own #221 section
  already promised ("an existing session already holding one of these names on disk is unaffected"):
  the refusal this fixes was never the documented contract, only what 2.0.0 shipped for its own first
  release. Creation is unweakened -- #221's portability guarantee for a *new* session still holds on
  every platform.

- `docs/compatibility.md` now documents #255's `input_too_large` refusal (#373). `session init`
  moved from accepting a request of any size to refusing one over 20,000 characters at exit 1 in the
  same release its sibling #250 got a matching `###` section for the identical shape of change --
  #255 got none. The exit-code table's generic row now names "an oversized request" among its
  examples, and a new `### session init and an oversized request` section states the refusal, its
  `code` (`input_too_large`), and the 20,000-character ceiling (`MAX_INPUT_CHARS` in
  `core/contracts.py`), matching how #250's own section reads.
- Compatibility: compatible - documentation only; no code changed.

- Two `tests/test_boundaries.py` allowlist entries justified themselves with "no client is built" for `credential_present()` and `credential_diagnosis()` -- true when written, and false since #334 moved both through `_resolve_client()`, which constructs a transient `Anthropic()` to read the SDK's own credential resolution (measured by spying on `Anthropic.__init__`: one construction per call). #364's earlier sweep of this same table corrected the two entries below these and left them stale (#374). Both reasons are corrected, along with a stale twin in `web/config.py`'s `provider_status()` docstring ("without importing a client"), and a new mechanical check -- `test_an_allowlist_reason_claiming_no_client_is_built_is_true_of_the_function_it_names` in `tests/test_provider.py` -- derives every allowlist entry still making this exact claim and spies on the function it names, so the next stale one is a failing test rather than prose nobody re-reads. No production behaviour changed; the guard's own pass/fail verdict over the pre-existing imports is unchanged.
- Compatibility: compatible - test- and comment-only; no runtime code changed.

- `docs/compatibility.md`'s CLI exit-code promise gains the direction it was missing (#382). It said
  "moving a condition from one code to another is breaking," with no account of which way the move
  went, and one release declared one move of each direction differently: #360 (exit 0 to 1/2) as
  breaking, #249 (exit 2 to 0) as compatible. The section now states the rule it was already being
  read by: moving a condition onto 0 is compatible, moving one onto a nonzero code or between two
  nonzero codes is breaking. The paragraph at the foot of the #360 subsection that pointed at this
  issue as open is replaced with the settled statement. Neither #360 nor #249's own declaration
  changes under the new clause -- #360 still narrows and is still breaking, #249 still widens and is
  still compatible -- so no other fragment in this release needed re-declaring.
- Compatibility: compatible - documentation only; no code, `--json` payload, exit code or session
  format changes.

- The orphan check on decision records can fire again for the shape a record normally has, and a `decision:` reference split across a line wrap is now refused (#384). `tests/test_narrative_references.py` built its referrer set from `subjects()`, which includes `docs/decisions/` itself, so a record that quoted its own slug satisfied its own reachability check — and a record almost always does quote it, because explaining where its pointer belongs requires naming it. Measured: at `3245e7e`, `0002-elicitation-schema-hand-kept.md` was referenced from nowhere but itself and the guard was green. The records now come out of the referrer set entirely rather than only excluding self-reference, because excluding self alone still passes a cluster of records that cite each other and nothing outside, which is exactly the unreachable island the guard is named for; they stay *subjects*, so their own references are still resolved. The second half is a new `_WRAPPED_DECISION` detector: a slug broken across a line wrap is invisible to `_DECISION_REF` altogether — the closing backtick is on the far side of the newline — so the reference resolves to nothing and nothing said a word about it. A wrap between the prefix and the slug leaves the slug whole and greppable and is deliberately left alone.
- Compatibility: compatible - a test-suite guard only. No product code, no session format, no CLI surface, no `--json` payload.

- `requivo status` no longer prints a persisted `usage_priced_as_of` rate-table date raw (#388). `render_session_cost` read that field back off a revision's provenance in `session.json`, and `session import` is the documented channel through which someone else's archive -- and its `usage_priced_as_of` -- arrives; a value chosen by whoever authored an archive could reach column 0 of Requivo's own output, indistinguishable from a line the product wrote, with `session verify` and `doctor` both green throughout. It is now routed through `display_token`, the same chokepoint `deterministic/sessions.py` already uses for every persisted string it prints -- a no-op on an ordinary date, and a `repr()` on anything that tries to open a line of its own. `tests/test_render_untrusted_output.py`'s `_NON_PROSE_RENDERERS` exemption for `render_session_cost` is removed (it is swept now, with its own dedicated forged-input test), and `render_usage`'s neighbouring exemption is reworded to say *why* its ledger-sourced `as_of` is safe where this one was not: it is built from this run's own provider calls and never written to disk, so nothing between the API reply and the renderer is a channel for someone else's input.
- Compatibility: compatible - this changes what `requivo status` prints only for a hostile-shaped input (a persisted `usage_priced_as_of` carrying a control character), which no legitimate provider-priced revision produces; an ordinary date renders byte-for-byte unchanged. No session-format field is added, removed or renamed, and no `--json` payload changes.

- `render_session_cost` no longer re-implements `UsageLedger.cost_usd()` (#389). It carried its own copy of the cache-read (0.1x) and cache-write (1.25x) rate multipliers, which made `usage.py`'s own module docstring -- "Cost is arithmetic here and nowhere else" -- false the moment this renderer shipped, and no test asserted the printed dollar figure to catch the drift: a mutation control that moved the cache-read multiplier in `render_session_cost` alone, leaving `usage.py` untouched, produced zero added failures across the full suite. Each priced `RevisionRecord` is now wrapped in a `CallRecord` and summed through a scratch `UsageLedger`, so `usage.py` is the only place the arithmetic runs; a new test asserts the exact printed figure across every rate tier at once (input, cache read, cache write, output) and is itself a mutation control -- it fails if either multiplier moves again. Found in review: delegating to `UsageLedger.priced_as_of` changed the rule for an empty-string `usage_priced_as_of` from "silently skipped" to "printed as a real date", which could leave a dangling `" · "` on the cost line for a revision no real provider path produces but a persisted `session.json` could still carry -- normalized on the way in so the renderer's own behaviour for that shape is unchanged.
- Compatibility: compatible - the printed figure is unchanged for every already-correct input, since the two implementations agreed on every multiplier; only a divergence between them (which could not previously happen without the guard now added) would have changed the number. No session-format field, `--json` payload, or exit code changes. `usage.py`'s module docstring makes no other claim than the one this restores.

- A session already on disk under a Windows reserved device name (`con`, `nul`, `com1`...) can be
  discovered again, not only read (#390). #372 made the reserved-name refusal conditional -- refused
  at creation, tolerated for a name a session already occupies -- and swept `_child_of` and
  `lock_path` onto the conditional form. `_discovery_guard_path`, the in-flight first-discovery
  guard, had been added one commit earlier and was missed by that sweep, so it kept calling
  `validate_slug` unconditionally: such a session listed, showed, exported and locked fine, and
  `requivo discover` alone refused it with `invalid session slug 'con'`. It now validates exactly as
  `lock_path` does -- the shape unconditionally, the reserved device name only when nothing already
  occupies the slug.
- Compatibility: compatible - a widening only, in one direction. No slug accepted before is
  refused now, and the single case that changes is the one named above -- an existing on-disk
  session at a reserved name reaching the discovery guard. Creating a *new* session at such a
  name is still refused on every platform, unchanged since #221.

- `doctor` no longer turns yellow after one ordinary `requivo discover`, and no longer prints two
  statements the code contradicts about a file it just wrote (#391). `_discovery_guard` writes
  `<slug>.discovering` into `lock_root()` and never unlinks it -- correctly, on the same POSIX
  reasoning `session_lock` already relies on to leave its own `.lock` file behind for a deleted
  session (#209) -- but `scan_lock_root` was written the release before that second file shape
  shipped and had never been taught it, so it classified the guard file as `unexpected`: "not a lock
  file Requivo recognises... a name here did not come from `session_lock`," about a file this
  release's own code had just written. `scan_lock_root` now recognises a well-formed
  `<slug>.discovering` file -- a regular file, not a symlink, whose stem is a valid slug -- the same
  way it recognises `<slug>.lock`, and excludes it from `unexpected`. A stray file, a directory or a
  symlink at either name, or a malformed stem, is still reported exactly as before.
- Compatibility: compatible - `doctor --json`'s `locks.unexpected` array is unchanged in shape
  (still an array of names, and the `locks` skeleton is untouched) but narrower in content: it no
  longer names a `<slug>.discovering` file left behind by a first discovery. A consumer flagging
  every entry as unrecognised sees one fewer false positive per session that has ever run a first
  discovery; a consumer counting `len(unexpected)` sees a workspace-dependent decrease. See
  `docs/compatibility.md`'s `--json` section for the declared entry.

- Requivo Web can reach a session already on disk under a Windows reserved device name (#396). The
  shared `{slug}` dependency behind every session route validated the path segment with the
  unconditional, creation-time refusal, so one call put the whole surface out of reach for such a
  session: the session page, the model download and the saved-artifact view all answered 400 for a
  session `requivo session show` opens without complaint, as did discovery, answers and artifact
  generation. All six routes now take the read-time form of the check (#372's split), on the
  reasoning that each of them addresses a session that must already exist -- refining one is a read
  of its name, not a request to make one.
- The create form is deliberately unchanged: `POST /sessions` is the one route that can bring a slug
  into existence, and it still refuses a reserved device name outright.
- Compatibility: compatible - a widening only. No slug accepted before is refused now.

- `doctor` no longer reports a reserved-name session's own lock and guard files as residue nobody
  recognises (#401). `scan_lock_root` classified each entry under `.requivo/locks/` with the
  *creation-time* slug rule, which refuses `con`, `nul`, `lpt1` and their siblings unconditionally.
  Both writers of that directory apply the conditional read-time rule instead (#372), so for a
  session already on disk under such a name they write `con.lock` and `con.discovering` and
  `scan_lock_root` then called both "not a lock file Requivo recognises... a name here did not come
  from `session_lock`" -- said about two files this release's own code had just written. That is
  #391's defect one predicate over, and the third site #372's sweep missed after #390 and #396. The
  stem question is now `lock_path`'s rather than `validate_slug`'s: a stem either writer could
  actually have been given.
- A lock or guard file whose stem is a reserved device name that **no** session occupies is still
  reported, as is a stray file, a directory, a symlink at either name, and a malformed stem.
  Tolerating is not trusting: the widening is conditional on a session existing, and on nothing
  else.
- Compatibility: compatible - `doctor --json`'s `locks.unexpected` is unchanged in shape (still an
  array of names, and the `locks` skeleton is untouched) and narrower in content, on the same terms
  as #391's narrowing of the same key. On a workspace holding such a session a consumer sees
  `locks.total` count one more lock and `locks.unexpected` name two fewer entries; on every other
  workspace, including every workspace created on Windows, nothing changes. See
  `docs/compatibility.md`'s `--json` section for the declared entry.

- `answer`, `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic` and `release` no longer
  advertise a `model.json` path they could not open (#402). Their shared help said "a session slug,
  or a path to a saved model.json", inherited from `status`/`impact`, but `SessionService.resolve_slug`
  only ever mined a slug out of such a path -- `p.parent.name`, whether or not the file existed --
  and these eight verbs never open the file they are handed at all: they resolve a slug and read and
  write the *store's* copy of the session (`ArtifactService.save` refuses anything that is not
  `has_meta(slug)`). So a fabricated or stray path used to be reported on under a slug carved out of
  its own parent directory -- a name the user never typed -- or, worse, silently resolved against
  whatever session happened to carry that name.
- These eight verbs now take a session slug only, and their `--help` says so. Passing a path is
  refused outright, naming the path exactly as given, before any slug is mined from it. `status` and
  `impact` are unchanged: they read a `model.json` path's own bytes directly and never resolve
  through the store, so they were never exposed to this and keep the wider help.
- `SessionService.resolve_slug` itself is fixed at the root for every other caller that still accepts
  a path (every `deterministic/` verb): a `model.json`/`session.json` reference is now only mined for
  its parent directory's name when the file actually exists. A reference to a file that was never
  written is named as given, not carved into an unrelated slug.
- Compatibility: breaking - a real, existing session's own `model.json` path, previously accepted by
  `answer`/`brief`/`prd`/`stories`/`estimate`/`criteria`/`epic`/`release` as an alternate way to spell
  its slug, is now refused; pass the slug instead (`requivo session list` shows it). `status` and
  `impact` are unaffected. No other invocation changes.

- `docs/providers.md`'s published input-token range now prices a whole provider call, not just its
  system prompt (#404). `tests/test_cost_claims.py`'s `measured_input_tokens()` docstring said "This
  is what a call actually sends" while measuring `build_prompt`'s system prompt alone -- a real call
  sends that plus a user message, and for every generator (`brief`, `stories`, `estimate`, `prd`,
  `criteria`, `epic`, `release`) and every discovery turn but the first, that user message is the
  whole resolved model. The gap understated a generator call's input by something approaching 40%.
  The corrected measurement adds a real resolved-model size on top of the system prompt for every
  operation but the first discovery turn (which has no model yet to attach), split by whether the
  assessment has absorbed its reasoning into the model yet -- `advise` (`brief`) and a refinement turn
  see a smaller, unreasoned model; every other generator sees the model after `absorb_reasoning` has
  copied `advise()`'s own decisions/challenges/opportunities onto it, which is materially larger. Both
  are measured from every captured discovery reply in `fixtures/golden/`, including the interactive
  captures' own per-turn `turns` (a deep refinement turn's model is the largest state a real call
  sends, and was silently excluded from an earlier draft of this fix before self-review caught it).
  The published "One provider call" row widens from 7,300-8,900 to 8,000-12,600 input tokens; every
  dollar figure downstream of it in `docs/providers.md` moves with it, arithmetically, and the test
  that recomputes both is what keeps them agreeing. The README's own `$0.03 to $0.06 per call` figure
  is unchanged -- it already had enough rounding margin to still hold -- but its flat "under $1"
  session claim did not survive the correction (the honest ceiling is now $1.01) and is now the
  derived `$0.46 to $1.01` instead, the same way the per-call figure already was.
- Compatibility: compatible - a documentation and test correction only. No session format, no
  `--json` payload, no CLI surface and no runtime behaviour changed; the rate table
  (`providers/anthropic/pricing.py`) is untouched.

### Security

- Requivo Web now answers `Cache-Control: no-store` on everything except the assets it ships, so a session page, an HTMX fragment, a model export or an artifact download is not written to the browser's disk cache (#218). Every one of those carries the reader's own client request or the model built from it, plus the cross-site request token — on a shared machine a disk copy outlives the "sessions stay on this machine" promise in spirit, and a cached page is also where the two stale states this app apologises for come from (an old `expected_revision` reaching a 409, an old token reaching a 403 after a restart). The rule is keyed on the path and fails closed rather than on the content type: `/static/…` and `/favicon.ico` stay cacheable and everything else is `no-store`, because a `text/html` rule would have left `/sessions/{slug}/export` (`application/json`) and the artifact download (`text/markdown`) — the two responses carrying the most of the reader's material — cacheable. Nothing is said about *how long* the bundled assets may be cached; an ETag or `max-age` strategy for them is a separate question.
- Compatibility: compatible - one added response header on responses that previously carried none. No route, payload, session format or error code changes; static assets are untouched, and `setdefault` leaves any `Cache-Control` a route sets for itself.

## [2.0.0] - 2026-08-31

### Added

- The web surface now ships a favicon (the existing surveyor's-station brand mark, as `/static/favicon.svg`, linked from every page) and serves it at `/favicon.ico` too, so the browser's own implicit probe for that path stops 404ing into the operator's logs on every page load (#241). No sized PNG/ICO fallback is shipped — evergreen browsers render the SVG natively, and a hand-authored binary icon risked shipping a malformed one for no real gain.
- The session page's browser-tab title now uses the same human objective the page's own heading already computes, falling back to the slug only while a session has no objective yet (pending, or a first analysis that failed before writing one) — previously every session showed its slug in the tab regardless (#241).
- Compatibility: compatible - presentation only.

- Every Requivo Claude Code skill's shared preflight now checks whether the installed CLI is the version this plugin build was tested against, instead of only documenting "keep the two in step" (#251). The doctor report the preflight already fetches carries `requivo_version`; the plugin's own `.claude-plugin/plugin.json` carries the version it was tested against, read live rather than duplicated as a second number that could drift from the manifest. An older CLI gets a one-line warning and the skill continues — refusing was considered and rejected, since the plugin is keyless and most verbs work across a minor version. A newer or equal CLI is silent, on purpose, so a healthy install is not told the same thing on every run. When the check itself cannot be made — the doctor JSON did not parse, or carried no `requivo_version` — the skill says so explicitly and continues; it never reports "in step" for a comparison it never made. `plugins/claude-code/scripts/version_skew.py` is the tested reference implementation for the comparison (also runnable standalone: `python3 plugins/claude-code/scripts/version_skew.py`), and the plugin README's "keep the two in step" paragraph now points at the automatic check instead of leaving detection to the reader.

- The web surface now shows what a first analysis cost, on the two paths that had none (#253). `create_session` and `run_discovery` both answer with a redirect, which has no body to put a figure in — PR #327 already showed the spend on the answers turn and every artifact generation, and left these two logged to the operator's terminal only. The figure is now carried server-side, per session slug, to the page the redirect lands the reader on: a small in-memory, read-once store, not a query parameter — a parameter would have put a forgeable cost claim on the URL, which is worse than showing nothing. It is per-process state, so it does not survive a server restart between the redirect and the following request (unobservable in practice — the hop is milliseconds), and it is read once: a plain reload of the landing page does not repeat the line, on purpose, since it is a receipt for the action that just happened rather than an ongoing charge. A first analysis that spends tokens and then fails still surfaces the recorded spend on the retry page, matching the existing "billed even on give-up" contract the answers turn already had.
- Fixed alongside it: a first analysis that failed because the provider's JSON retry loop gave up (`ProviderOutputError`, distinct from the transport failures `EngineError` already covered) was not recognised by the create/discover routes' recovery logic, so the reader was sent to a bare error page instead of back to the session that had already been saved with a retry button — the same failure #207 fixed for transport errors, left open for this other provider-failure class.
- Compatibility: compatible - no persisted format changed; both are corrections to what the web surface renders and which failures it recovers from.

- An offline guard now checks that each of the eight prompt assets' `# Output format` example is accepted by the Pydantic contract its operation actually parses replies with (#266). The eight pairs listed in CLAUDE.md's "The output contract (keep in sync)" were synchronized by hand, and a drift between them was invisible to the offline suite while costing real money at runtime: a model obeying a stale example produces a reply the contract refuses, which `_complete()` retries twice before raising `EngineError` - up to three times the call cost, on every invocation. The new `tests/test_prompt_contracts.py` parses each example and validates it, with the contract read out of the generator's own source rather than from a table, so a generator switched to a different contract goes red too. No prompt or contract changed: all eight pairs already agreed.

- The shape of every public `--json` payload is now pinned by an offline guard (#267).
  `docs/compatibility.md` declared all fifteen of them public and `CLAUDE.md` invariant 8 repeated
  it, but the only enforcement was *membership*: the page had to name each verb, and nothing looked
  at what a verb printed. Four breaking changes to those payloads shipped in the 1.0.0 release alone
  (#87, #84, #88, #107) — all four deliberate, all four correctly recorded on that page, because
  somebody audited the surface by hand while the 1.0 contract was being cut. Nothing in the tree
  would have gone red if a fifth had been made by accident, or its row forgotten.
  `test_every_public_json_payload_keeps_its_recorded_top_level_shape` now runs every `--json` verb
  against a fixture workspace and compares the top-level key set and the JSON types of its values
  with a recorded table. A removed or retyped key is reported as breaking; a new key is reported
  separately as additive, wanting one line added to the record rather than a revert. The verb list is
  read off the built parser, so a new `--json` verb with no recorded shape fails rather than passing
  quietly.
- The neutral epic export envelope gets a version ratchet (#267). Its full key skeleton — the
  envelope, the `epic` object and each entry in `issues` — is recorded per `EPIC_EXPORT_VERSION` by
  `test_the_epic_export_skeleton_is_pinned_to_its_version`, so changing a key is red until the
  version moves. The previous assertion compared `version` with the constant the payload was built
  from, which stays true whatever the keys are.
- `docs/compatibility.md` now states what *public* means for a payload in one testable sentence: the
  top-level key set and the JSON types of those values are the contract (#267). It also records that
  `requivo status --json` has two shapes — `revision`, `context_cards` and `artifacts` are present
  only when the reference resolves to a canonical session, not when it is a path to a bare
  `model.json`. That was already true and was written down nowhere.

- Added an offline guard, `tests/test_golden_baselines.py`, that checks every committed golden baseline (`fixtures/golden/<slug>.runs.json`) against the current `fixtures/golden/requests.md` it is supposed to have been captured from (#275). Nothing tied the two together before this: `golden_run.py` writes a baseline from `requests.md`, `golden_diff.py` compares a baseline against a *fresh* capture, and neither ever checked the committed baseline against the request set it claims to measure -- so a baseline could silently drift out of step with `requests.md` and the harness would answer a different question than it reports, with no warning. That is the exact state `training-budget`'s baseline is in (#194): its answer sheet predates the deepening of `training-budget`'s interactive layers in `requests.md` from ~2 per slot to up to 10. The guard makes no network call and distinguishes three situations rather than one collapsed verdict -- a request with no committed baseline yet is legitimate (nobody has paid for the capture), a baseline whose stored request/answers disagree with `requests.md` is drift (named per slug, per field), and a baseline with no matching request left in `requests.md` is orphaned -- and `training-budget` is recorded as a declared, named exception (`_DECLARED_DRIFT["training-budget"] = "#194"`) until the paid 15-call re-capture lands and the exception is removed. That re-capture is issue #194's own action and is not part of this change: #194 stays open.
- Compatibility: compatible - a new offline test file and a new `## Known stale baseline` section in `docs/evaluations.md`; no runtime, CLI, or session-format change.

- The README now links the discovery-feedback issue template, in `## Status` (#282). `.github/ISSUE_TEMPLATE/discovery-feedback.md` is the entire product-learning channel for a tool that correctly ships no telemetry, and it was linked from nowhere — not the README, not `docs/`. It is placed after the existing maturity/roadmap paragraph rather than in `## Start here`, so it does not compete with the first thing a new reader needs to see; `## Status` is also the section already talking about how good the output is, which is what the report exists to test. `docs/product-validation.md` — the issue's other named target — is not touched here: it sits outside this change's file set.
- Compatibility: compatible - adds a reference-style link and its definition; no command, output, or session behaviour changes.

- When a provider reply fails the JSON/contract retry loop three times, the final raw reply is now
  saved to a file under `.requivo/debug/` and named in the `ProviderOutputError` message ("the reply
  that failed validation was saved to …") (#283). Previously the raw text was discarded on give-up
  and only the last validation error string surfaced, so a user filing "the provider returned output
  that did not match the contract" had nothing to attach and the maintainer could not tell a prompt
  regression from a model-side change. Successful calls and transport-level failures write nothing.
  `.requivo/debug/` sits under the same store root as `.requivo/sessions/`, so it inherits the
  privacy `.gitignore` written the first time that directory is created — a raw reply can contain the
  client's request text verbatim. The directory keeps the newest 20 replies and prunes older ones on
  every write. The bug report template's "Anything else" section now asks for this file when the
  error message names one. A prune failure (deleting an older file past the retention cap) can never
  discard the path of a reply that was just saved successfully -- the write and the prune are two
  separate failure domains.

### Changed

- The three most recently released entries in `CHANGELOG.md` (1.3.0, 1.2.0, 1.1.0) now open with a "Highlights" block of at most six single-line, user-facing bullets before the `Added`/`Changed`/`Fixed` detail (#229). The [1.2.0] entry alone ran 742 lines of multi-paragraph narrative bullets, which is not something a reader can answer "what changed in this release?" from, and this repository's own launch audit flagged the essay style as the most quotable "AI-written" exhibit in the tree. Nothing existing is deleted or rewritten — a Highlights block summarizes what already follows below it, it does not replace it, and the historical entries say exactly what they said before.
- The changelog's own header now states the convention for whoever cuts the next release: write the Highlights block for the new section before tagging, summarizing the `changelog.d/` fragments being folded in. The fold tooling itself (`.oss/assemble_changelog.py`) is managed by the oss plugin and overwritten on every scaffold refresh, so the convention is documented in `CHANGELOG.md` rather than encoded there, where an update would silently discard it.
- Compatibility: compatible - `## [x.y.z]` headings, the trailing link-reference block, and the `changelog.d/` fold are untouched; `.oss/assemble_changelog.py --check` and `--check-links --untagged 0.6.1` both still report `ok`.

- The model override environment variable is now `REQUIVO_MODEL`, not bare `MODEL` (#268). Every
  other environment variable this package reads is `REQUIVO_`-prefixed; `MODEL` was the one
  exception, and it is a generic name other tools set too — a CI job, a docker-compose file, an
  unrelated script sharing the same shell — so it could collide silently and steer Requivo at a
  differently-priced or nonexistent model with no hint the value came from outside. `REQUIVO_MODEL`
  is read first; bare `MODEL` still works as a deprecated fallback, read only when `REQUIVO_MODEL` is
  unset, so nothing already working today stops working. `.env.example`, `docs/providers.md` and
  `CLAUDE.md` now teach only `REQUIVO_MODEL`. `requivo doctor --json`'s `model.source` — which used
  to read bare `MODEL` a second time to decide "default" vs "env" — now agrees with the actual
  resolution, so a reporter who has already moved to `REQUIVO_MODEL` is not told their override is
  the default.
- Compatibility: compatible - `REQUIVO_MODEL` is additive and bare `MODEL` keeps working as a
  documented, deprecated fallback (see `docs/compatibility.md`); nothing that worked before stops
  working.

- A bare `python scripts/golden_run.py` (no slug, no flag) now captures only single-pass requests and skips every interactive one by default, printing which it skipped and the exact command to capture it alone (#276). The script's own documented workflow (its docstring's step 3, run after any prompt edit) was the bare invocation, and until now it captured every request including interactive ones -- contradicting CLAUDE.md's own cost guidance that an interactive request (K x `GOLDEN_TURNS` calls, 15 at the defaults) belongs in a capture of its own, never folded into a full-set run. Naming a slug explicitly, or passing the new `--all` flag, still captures an interactive request exactly as before; the skip is the bare-invocation default, not a restriction on what can be captured. The selection is `golden_run.select_runs()`, a pure function with no client and no network call, pinned by `tests/test_golden_run_selection.py`.
- Compatibility: compatible - a behaviour change to the *default* (unqualified) invocation of a dev-only harness script that is not part of the installed package or any public API; every explicit and `--all` invocation is unchanged.

- Recorded the decision on #278 in `docs/decisions/0002-elicitation-schema-hand-kept.md`: `framework/elicitation.md`'s consistency with `model_schema.json` stays hand-kept, with no new test. Measured rather than assumed: elicitation.md names only one slot by its literal schema id (`config_vs_custom`) and otherwise paraphrases labels rather than quoting them, so the byte-containment test the issue proposed would fail against the current, correct files for editorial reasons alone; the one literal id is already referenced, unguarded, in four context cards and a prompt, so a guard scoped to elicitation.md alone would close the smallest slice of a larger, already-present, already-unguarded gap; and no drift has ever actually occurred for this pair. `elicitation.md`'s only reader is `requivo schema --framework`, so the cost of a future miss is one CLI subcommand's prose going momentarily stale -- not a corrupted model or a silent behaviour change.
- Compatibility: compatible - a decision record only; no code, prompt, or schema change.

- `docs/cli.md` is restructured into a flag reference followed by a "Design notes" section, instead of interleaving both (#284). The page opened "every command and flag" while roughly 300 of its 564 lines were design history — the `doctor` non-session-entry taxonomy, the control-character escaping story, the import-code rationale duplicating `docs/compatibility.md` — and real flags were missing: `session init --slug`/`--provider`/`--json`, `schema --framework`, `context --list`, `session export --output`. Every offline verb group (`session`, `model`, `artifact`) now has its own table naming every flag it accepts; the narrative moved to a `## Design notes` section at the foot of the page, and the import-code history now points at `docs/compatibility.md#the-import-path-names-the-archive-not-the-model-101` instead of retelling it. `docs/decisions/` — where this repository's own rule for narrative sends unbacked archaeology — was not written to: every moved paragraph is backed by an existing test, and the directory was held by another branch while this split ran.
- `tests/test_cli_flag_names.py` gains `test_every_real_flag_is_documented_in_the_cli_reference`, the inverse of the existing `test_documented_cli_commands_exist`: it reads every long option string off the built parser and refuses a flag that appears nowhere in `docs/cli.md`, rather than trusting a hand-maintained table to stay in sync with `--help`.
- Compatibility: compatible - no command, flag, or `--json` shape changed; every existing anchor other pages link into `docs/cli.md` with (`#something-here-that-is-not-a-session`, `#context-cards-a-session-can-no-longer-find`, `#a-card-name-cannot-write-a-line-of-the-receipt`) is unmoved, because the heading text that generates each anchor did not change, only its section.

- A new offline test pins the four `anthropic.types.Usage` field names the billing ledger reads
  through `getattr(u, name, 0) or 0` (#294) — a rename inside the supported SDK range would otherwise
  zero that field's token count silently, with nothing going red. Writing it found that two of the
  four, `cache_read_input_tokens` and `cache_creation_input_tokens`, were already absent from the
  SDK's `Usage` class at the declared floor (`anthropic==0.40.0`/`0.41.x`) — not a hypothetical future
  rename but a live gap: any install resolved near the old floor has been under-reporting cache
  read/write spend since this extra existed, with nothing anywhere catching it. The `anthropic` extra's
  floor is raised to `>=0.42.0,<2`, the first release where all four fields exist, which is the fix
  rather than narrowing the pin to the two fields that happened to always be present. The test skips
  cleanly (not an error) on an install without the `anthropic` extra.
- Compatibility: compatible - the `anthropic` extra's floor moves from `0.40.0` to `0.42.0`, both
  inside the already-declared `<2` ceiling; a resolver that was already picking a newer release (the
  common case — `--resolution lowest-direct` is only exercised by this repo's own dependency-floor CI
  leg) sees no change at all.

- `.github/workflows/plugin-validate.yml`'s pinned Claude Code CLI install is now cached (#299). The gate job resolves a ~320MB platform-native ELF as an ordinary `npm` `optionalDependency`, and it was re-downloaded on every pull request even though the version is pinned. The cache key is derived from `CLAUDE_CLI_VERSION`, the same env var the pin itself uses, so bumping the pin misses the cache by construction rather than by luck — a cache that could go on serving a stale CLI after a version bump would turn a validation gate into one that passes against a version nobody ships, which would be worse than the download it saves. A cache hit and a cache miss are now printed as distinct lines in the run log rather than being indistinguishable. The advisory `@latest` job stays deliberately uncached, since its whole point is currency.

- Requivo is now licensed under the **Apache License 2.0**, replacing the MIT License it carried through v1.3.0 (#336). The project stays open source on the same terms of use: it remains free to use, modify, redistribute, self-host and use commercially, and Apache-2.0 adds an explicit patent grant that MIT did not. Releases already published under MIT remain available under MIT; the rights they granted are not withdrawn. The wheel now ships a `NOTICE` file beside `LICENSE`, as Apache-2.0 section 4(d) requires of a redistribution. The Requivo name and identity stay separate from the code licence, in `TRADEMARKS.md`.
  - Compatibility: compatible - a licence change grants no fewer rights than before and touches no API, session format or command; nothing installed or persisted behaves differently.

### Fixed

- `requivo discover` and every other CLI verb now catch `KeyboardInterrupt` and exit **130** (the conventional SIGINT code) instead of dying in a raw traceback (#206). A Ctrl-C reaching `app()` used to be an unhandled Python exception — no usage summary, and, on the interactive `discover` loop's own `except`-and-`SystemExit` paths, exit **1**, the code otherwise reserved for "a clean, expected failure". It is now 130 everywhere, so a script can tell "the operator stopped it" from "the provider refused this".
- Every abort point after `requivo discover` has claimed a session now says so and names the continuation verb — including the non-interactive `--once`/piped-stdin path, which claims a session and makes exactly one paid call and, until now, had no rescue of its own if that call was interrupted or the provider failed. An interrupt reaching `app()` **before** any session is claimed correctly names none: a message inventing a session that does not exist would be worse than the traceback it replaces.
- Found in review of this diff: three `except (EngineError, KeyboardInterrupt)` sites in `cli.py` (the interactive loop's own turn-draft catch, the quick path's new rescue above, and `_rescue_drafted`'s own save) did not catch `ProviderOutputError` — a `RequivoError` sibling of `EngineError`, not a subclass of it, raised when the provider's JSON retry loop gives up — so that specific provider failure reached `app()` with the claimed session and any already-drafted turns unnamed, exactly the silence this issue exists to close. Widened to `except (RequivoError, KeyboardInterrupt)` at all three sites. The third also gained its own `KeyboardInterrupt` handling: a second Ctrl-C landing on the rescue's own save used to propagate bare and silent.
- Compatibility: breaking - three abort paths inside `requivo discover`'s interactive loop and its decision-brief step used to exit **1** on a `KeyboardInterrupt` specifically (shipped in #202/#320); they now exit **130**, like every other command. `docs/compatibility.md`'s CLI exit-code table gains the new row and a dedicated note; a script gating on exit 1 to detect an interrupted `discover` needs to gate on 130 instead.

- Requivo Web's 1MB body cap (`MAX_BODY_BYTES`) now runs before a request body is read, not after
  (#216). It previously checked only a *declared* `Content-Length`; a chunked request (no such
  header) was read in full by `await request.body()` regardless, and the size was only measured once
  the whole thing was already sitting in memory -- a caller with direct socket access could push an
  unbounded body past the "1MB cap" the comment above the constant claimed was enforcing itself.
  Browsers cannot emit a chunked cross-site form post, so this was reachable only from a caller
  already inside the local, single-user threat model this app assumes -- but the guard's own comment
  made a claim the code did not keep. A request with no valid declared length is now refused outright
  before a byte is read; every supported caller (this app's own forms, curl, httpx, requests) always
  declares one, so nothing that worked before is affected. Covered by
  `test_a_chunked_body_is_refused_before_being_read`, which asserts the read never starts (an
  instrumented `receive` that must not be called), not just the eventual status code -- the old code
  also answered 413 for this, just after paying to buffer the whole request first.
- Compatibility: compatible - a request declaring a valid `Content-Length` behaves exactly as before.
  Only a request with no valid declared length (chunked, or a missing/malformed header) on an unsafe
  method now gets refused earlier than it used to be measured; it was never a supported input.

- `requivo web --host 0.0.0.0` (or `--host ::`) no longer silently 403s every LAN request while
  looking like it worked (#217). The wildcard bind address used to be auto-allowlisted verbatim --
  `REQUIVO_WEB_ALLOWED_HOSTS` defaulting to the literal string `"0.0.0.0"` -- which no browser's
  `Host` header is ever going to equal, since a client addresses the server by the interface it
  actually connected to, not by "every interface". The process bound, printed its URL and opened a
  browser on loopback, so the flag *appeared* to work; every other machine on the LAN got a 403
  `host_not_allowed` with nothing pointing at the fix. A wildcard bind address is no longer
  auto-allowlisted -- fail-closed was and stays the right default -- and the warning now names
  `REQUIVO_WEB_ALLOWED_HOSTS` explicitly with a copy-pasteable example
  (`REQUIVO_WEB_ALLOWED_HOSTS=192.168.1.50 requivo web --host 0.0.0.0`). Binding to a real,
  non-wildcard LAN address (`--host 192.168.1.50`) is unaffected: that literal value IS a legitimate
  `Host` value a browser will send, so it is still auto-allowlisted exactly as before.
  `docs/web.md` gains a "Binding beyond loopback" section with the recipe and the no-TLS/no-auth
  caveat. Covered by
  `test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var`, with the
  real-LAN-address and loopback-default cases as must-fire controls in the same fixture. The wildcard
  check itself recognizes every IPv6 spelling of "every interface" (`::0`, the fully-expanded
  all-zeros form, not just the canonical `::`), via `ipaddress.ip_address(...).is_unspecified` rather
  than a two-string literal comparison -- a literal check against `"::"` alone would auto-allowlist
  `--host ::0` verbatim and reproduce the exact bug this fixes under an unlisted spelling, caught by
  review before this shipped and covered by
  `test_an_equivalent_spelling_of_the_wildcard_address_is_caught_too`.
- Compatibility: compatible - `--host 127.0.0.1` (the default) and any non-wildcard `--host` are
  unaffected. `--host 0.0.0.0`/`--host ::` no longer auto-allowlist the literal wildcard string, which
  never satisfied any real client's `Host` header anyway -- nothing that worked before stops working;
  a deployment relying on the old behavior was already refusing every LAN client it was meant to serve.

- `session import` now bounds the *total* number of entries in an archive (files and directories
  together), not just files (#219). `MAX_ARCHIVE_FILES` and `MAX_ARCHIVE_BYTES` were both computed
  over file entries with directory entries filtered out, so an archive built entirely of nested
  directory entries declared zero files and ~zero bytes and sailed past both caps while the
  extraction loop still created every one of them -- an inode/directory-creation exhaustion DoS
  through a door neither existing cap covered. A new `MAX_ARCHIVE_ENTRIES` cap (a small multiple of
  `MAX_ARCHIVE_FILES`) refuses such an archive with a structured `invalid_archive`
  (`problem: too_many_entries`) before any extraction happens. Path traversal via a directory entry
  was already unreachable (stdlib `zipfile.extract` strips `..`/leading separators), so this closes
  the resource-exhaustion half only, which is what the existing caps were introduced for in the
  first place.
- The extraction loop now iterates exactly the entry list `_inspect_archive` validated and bounded,
  rather than re-reading `z.infolist()` a second time, so the extracted set is structurally
  guaranteed to be the validated set.
- Compatibility: compatible - `invalid_archive`'s `problem` vocabulary gains one new value,
  `too_many_entries`, alongside the seven this repository's own docs already enumerate; every
  existing `problem` value keeps its meaning and its `details` shape. A consumer matching the
  vocabulary by an unrecognised default (rather than refusing an unknown value outright) is
  unaffected; one that refuses an unrecognised `problem` value was already refusing this repo's own
  contract, which explicitly reserves room for new archive shape checks.

- `validate_slug` and `validate_filename` now refuse a Windows reserved device name (`con`, `prn`,
  `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`), case-insensitively, on every platform -- a slug or
  artifact-filename stem before the first dot, so `con`, `CON`, `con.md` and `con.tar.gz` are all
  refused (#221). Windows cannot create a file or directory with one of these names, so a session
  slugged `con` was creatable and exportable on macOS/Linux and then unopenable by `session import`
  on Windows -- a portability hole `.requivo/sessions/`'s own promise never mentioned. Refusing it
  on every platform, not only Windows, is what keeps a created archive portable everywhere the
  format claims to be, at the cost of a POSIX user losing a legal name they will almost never want.
- Compatibility: breaking - a slug such as `con` (or an artifact filename such as `con.md`) that
  `validate_slug`/`validate_filename` accepted before this change is refused now. A session already
  on disk under such a name is **also** affected, which this fragment first denied: every read path
  funnels through `validate_slug`, so `status`, `session show`, `session verify` and `session export`
  all refuse it, and there is no rename path -- the data is on disk and unreachable by any verb. That
  is a defect rather than the intent (#372): creation must stay strict to keep an archive portable,
  and the read paths must tolerate a name that already exists.

- `requivo status <slug>` and `requivo model show <slug>` now give the same remedy on a revision-0 session ("only the request was captured. Run `requivo discover` on the same request to analyse it") instead of the engine-flavoured "has no model yet (apply a proposal first)" (#250). Reproducing the issue as filed found `status` and `model show` already exiting the same code (**1**, not the 1-vs-0 split the issue reported) — that half of the report did not hold up against this tree, so only the wording changed there, and the report's other finding is what follows.
- `requivo impact <slug> <unknown-slot>` now exits **1** instead of 0, so a wrong probe is no longer indistinguishable from an empty but valid result; whatever slots *did* match are still rendered above the exit, unchanged.
- Alongside it: `requivo model show` on a slug that was **never created at all** used to raise the identical "has no model yet" message a claimed-but-undiscovered session gets — the same underlying `session_not_found` code covers both, and the friendlier wording above would have made that worse by claiming a request was captured when none was. `model show` now checks existence first, like `status`/`impact` already did, and gives the ordinary "no such session" remedy for a slug nobody has used.
- Compatibility: breaking - `requivo impact` on an unknown slot moves from exit 0 to exit 1, a documented CLI exit code (docs/compatibility.md's "moving a condition from one code to another is breaking" rule). The three reworded messages are compatible on their own: error `message` text has never been part of the stable contract (`code` is), and the `--json` envelope's `code`/`details` shape is unchanged throughout.

- The 20,000-character request/answers size cap now holds at the service layer
  (`DiscoveryService`/`SessionService`), not only in Requivo Web's route handlers (#255). Invariant 14
  says the service layer is the integrity boundary, not the interfaces -- but the only enforcement of
  this cap lived in `web/config.py` and `web/routes/`, so `requivo discover <file>` (the CLI), an
  interactive draft turn, or any future direct `DiscoveryService` caller sent unbounded text straight
  to a billed provider call. `claude-sonnet-5`'s 1M-token context window means a multi-megabyte paste
  did not even fail at the API -- it just billed, and the interactive loop re-sends the request on
  every turn, multiplying the cost across up to eight turns plus the assessment. The cap is now one
  constant, `MAX_INPUT_CHARS` in `core/contracts.py`, enforced by `require_input_within_bounds` at
  every text-entry point: `SessionService.create_session` (covers both immediate discovery and
  "capture now, discover later"), `DiscoveryService.answer`, and `DiscoveryService.draft_turn` (the
  interactive loop's own un-persisted turn, checked before any provider call, since the loop resends
  the request every turn before a session -- and its cap -- exists). `web/config.py`'s
  `MAX_REQUEST_CHARS`/`MAX_ANSWERS_CHARS` are now aliases of the same constant rather than a second,
  independently-kept 20,000; the Web's own early check is unchanged and still gives its friendly
  in-place re-render. Covered by `tests/test_discovery_service_input_bounds.py`, asserting zero
  provider calls on an oversized request/answers from the service layer directly, with an
  at-the-ceiling call count as the must-fire control in each case.
- Compatibility: breaking - the limit is unchanged (20,000 characters) and Web behavior is
  unchanged, but `session init` -- a public payload -- moves from exit 0 to exit 1 on a request over
  the ceiling, and `docs/compatibility.md`'s own rule is that moving a condition from one exit code to
  another is breaking. This fragment first declared it compatible on the grounds that a caller was
  relying on a billed call succeeding; that defence does not reach `session init`, which is offline
  and spends nothing. Corrected before 2.0.0 was tagged (#373 carries the matching page section).

- A session recording an artifact type this Requivo has no generator for is no longer reported as broken (#260). `docs/compatibility.md` lists "a new artifact type" among the changes that need no `format_version` bump, and the integrity checker refused one outright — so the first new generator a later Requivo shipped would have made every session it touched fail `requivo session verify`, appear as inconsistent in `requivo doctor`, and be refused by `requivo session import` on a colleague's older install, while the session itself opened without complaint. Such a type is now reported as a note instead: named in `session verify` (under a new `notes` key in `--json`, beside `problems`) and in `doctor` (`sessions.notes`), counting towards neither `ok` nor the exit code, and accepted on import. Every other check on that entry is unchanged — the recorded filename still goes through the bare-filename and containment guards, the file must still be present, and the revision it claims must still exist.
- Because tolerating an unknown type accepts what used to be refused, the key must now look like an artifact type: a plain lowercase name such as `risk-register`, at most 64 characters, the shape every built-in type already has. One that does not is refused as `unsafe_artifact_type`, a code of its own so a consumer can tell a type from the future apart from junk or a forgery (#260). Nothing Requivo has ever written is affected.
- Compatibility: compatible - `problems` keeps its meaning for every caller that gates on it, `session verify` and `doctor` gain fields rather than changing any, and no session on disk needs migrating. The one behaviour change is the intended one: a session an older build called broken for this reason is now called sound.

- A crash or a disk-full error partway through applying a revision no longer leaves the session serving a model that no revision records (#261). Applying a revision is three writes and no transaction, and `model.json` used to be the first of them — so a machine that died in that window left every read path returning the new model while `session.json`, the provenance log and the staleness machinery all still named the previous revision. Anything generated in that state recorded a source revision whose frozen file holds different content, which is the plausible-looking-but-false revision number the session snapshot exists to prevent, produced here by Requivo rather than by a racing writer. The frozen `revisions/NNNN-model.json` is now written first: nothing reads it until `session.json` names it, so the first gap leaves the recorded revision readable and `requivo session verify` reports only `orphan_revision_file`, which the next apply overwrites — the revision number is never spent by a write that did not finish. The later gap, once `model.json` has been replaced too, is unchanged and still reported — as `model_is_not_the_last_revision`, or as `model_without_revision` when the interrupted apply was the first one the session ever had.
  - Compatibility: compatible - the session layout, the file contents and every recorded value are identical; only the order in which two existing files are written has changed, and no reader observes that order.

- `session migrate` no longer aborts the whole pass when one legacy session's `model.json` will not
  parse -- that session is now named under a new `errors` list with its own message, and every other
  legacy session in the sweep still migrates (#262). This is invariant 15's "a listing survives its
  own members" applied to the migration loop, which its own docstring had admitted did not yet hold.
- `session migrate` no longer reports an interrupted migration as `skipped_already_present`, which
  means the work is done. `migrate_legacy` claims a legacy slug via `create_session` before it applies
  the model, so a crash between the two leaves a revision-0 shell occupying the canonical slug with
  the legacy data never copied in; that state is now reported under a new `interrupted` list, naming
  the recovery step (delete `.requivo/sessions/<slug>` and re-run) instead of silently reading as
  "already migrated" (#262).
- `session migrate` now exits `4` (`EXIT_DEGRADED`) when the receipt names any `errors` or
  `interrupted` entries, rather than always exiting `0`, so a script that only reads the exit code
  still learns the run was not a clean success.
- Compatibility: compatible - `session migrate --json` keeps `migrated` and `skipped_already_present`
  with the meaning they had; `interrupted` and `errors` are new, additive keys. The non-JSON receipt
  gains new lines only when there is something to report. The exit code changes from always-`0` to
  `4` in the two new degraded cases only, which is additive under this repo's own three-state exit
  code convention (an unattended caller that ignored the exit code before is unaffected; one that
  checked it for zero now correctly sees a non-clean run where it previously saw a false clean one).

- `requivo session verify` and `requivo doctor` no longer report a perfectly healthy session as broken when they race a concurrent write (#263). `check_session`/`inspect_session` read `session.json`, the revision files and `model.json` with no lock, and `save_revision` writes those same three things in that order but not atomically — so a checker landing in the gap could read the *old* metadata against the *new* model and report `model_is_not_the_last_revision`, "the file was changed after it was written", about a session that is merely mid-save. `inspect_session` (and `check_session`, its blocking half) now takes the session's write lock first; writes normally hold it for milliseconds, so waiting for one costs nothing measurable. `check_session_dir` itself is unchanged and still takes no lock, so it keeps working on a directory extracted from an archive, which has no lock to take.
- A lock this call cannot take within the 30-second deadline (see #265) is reported as *could not check*, never as *inconsistent*: `session verify` exits 4 (`EXIT_DEGRADED`) with the existing "could not examine" wording, and `requivo doctor --json` gains a `sessions.locked` map (`{slug: message}`), kept out of `sessions.inconsistent` and reported with the same warning glyph as an unexaminable entry rather than the failure glyph a genuinely broken session gets.
  - Compatibility: compatible - `problems`/`inconsistent` keep their meaning for every existing caller; `doctor --json` gains one new key under `sessions` and no session on disk needs migrating.

- `read_meta` no longer lets a `session.json` the process cannot stat (`EACCES`) escape as a raw `PermissionError` traceback (#264). Its existence probe called `Path.exists()` directly, outside the `try` block that wraps `OSError`, bypassing the structured-error contract every caller — `session show`, doctor's per-session arm, the HTTP 500 mapping — is written against. This is the same class already fixed twice in this module (`_scan_session_root`, `session_exists`), both narrated at `_probe`, which `read_meta` now routes through too: an unreadable session.json now raises `SessionUnreadableError` naming the slug, and a genuinely absent session still raises `SessionNotFoundError` as before.
  - Compatibility: compatible - the only observable change is that a stat failure now raises a structured Requivo error instead of a bare traceback; every other outcome is unchanged.

- Acquiring a session's write lock against a live holder now has the same bounded wait on every platform (#265). `_LOCK_TIMEOUT_SECONDS` (30s) was honoured only in the Windows (`msvcrt`) branch; on macOS and Linux, `fcntl.flock(fd, fcntl.LOCK_EX)` blocked forever with no message, so a stuck holder (a suspended process, a debugger, an NFS-mounted workspace) froze the CLI silently on the two primary platforms instead of raising the structured `SessionLockedError` Windows already had. The POSIX branch now polls `flock(LOCK_EX | LOCK_NB)` against the same shared deadline and raises the identical error on expiry. Only `BlockingIOError` (genuine contention) is retried; a real OS-level lock failure such as "no locks available" still surfaces immediately as itself rather than being masked for 30 seconds and relabelled "locked by another process". Every write normally holds this lock for milliseconds, so the bound is never felt in ordinary use; re-entrant acquisition within a thread (invariant 9) is unaffected, since it is decided before this call is ever reached.
  - Compatibility: compatible - the only observable change is that a contended lock now fails with a clear error after 30 seconds on every platform instead of hanging indefinitely on two of the three; nothing about an uncontended acquisition, or about a genuine non-contention lock error, changes.

- Corrected two prose claims about where Requivo's CLI verbs live, which had drifted from the code (#296). `deterministic/`'s own docstring said "every command here runs with no LLM" as if that were the split's boundary, and CLAUDE.md's repo tree said `cli.py` holds "provider verbs" as if it held only those — both true taken alone, both wrong about the actual axis: three verbs (`status`, `demo`, `impact`) are no-LLM *and* live in `cli.py`, because the real split is *journey* (the sequence a user follows, `cli.py`) versus *plumbing* (session/model/artifact administration, `deterministic/`), and "no LLM" merely follows from being plumbing rather than being the boundary itself. Both statements now name the three exceptions explicitly, and a new offline guard (`tests/test_cli_deterministic_axis.py`) pins that they keep naming them.
- No code moved. Moving the three verbs into `deterministic/` was the issue's other named resolution and was considered: `deterministic/doctor.py` and `deterministic/sessions.py` are held by concurrently active work this round, a new module was reachable without touching either, but the exercise (registration order, argparse wiring, `docs/architecture.md`, CLAUDE.md's own "narrative reference" rule on every comment touched) outweighed a P3 documentation-accuracy issue's own stated effort. Restating the axis was the cheaper of the two resolutions the issue named as legitimate, and is the one taken.
- Compatibility: compatible - prose and a docstring only; no verb, flag, output shape or exit code changed.

- Give the development-only HTTPX dependency a tested minimum of 0.25.0 so minimum-version installs avoid older releases that fail to build, import, or construct supported provider clients, and guard external dependency declarations against missing lower bounds (#312).

- A profile, workload-identity-federation or on-disk-active-profile install is no longer refused before the Anthropic SDK is given a chance to authenticate (#334). The credential guard kept its own list of two environment variable names against the five sources the SDK documents, so a setup that a bare `Anthropic()` would have authenticated in the same shell was told to set `ANTHROPIC_API_KEY`. The guard now asks the SDK, which resolves offline in single-digit milliseconds, so what Requivo accepts is whatever the installed SDK accepts and cannot drift from it again. A profile that is configured but unloadable (a missing file, a malformed `active_config`) is now a clean error quoting the SDK's own reason, which names the file, instead of a traceback out of the constructor.

- A Dependabot pip bump grouped into the `runtime` group (#346) is no longer titled `chore(deps-dev):` (#347). Grouping decides which pull request an update lands in and what the group is called; it never changed Dependabot's own production/development classification, which for pip is "outside `[project.dependencies]` counts as development" and is applied per dependency regardless of group — so a bump to `anthropic`, `fastapi`, `uvicorn`, `jinja2` or `python-multipart` still fell back to Dependabot's default `chore(deps-dev)` prefix even after #346's fix, because every one of them lives in `[project.optional-dependencies]`. `.github/dependabot.yml`'s `pip` block now sets one `commit-message.prefix` (`"chore(deps)"`) for every pip bump and deliberately does not set `prefix-development`, so the group name in the title (`the runtime group` / `the dev-tooling group`) is what tells a security-relevant runtime bump from a tooling one apart — Dependabot's prefixes are per ecosystem, not per group, so this is the only way to stop the wrong prefix without inventing a right one. Not independently confirmed against a live Dependabot run yet; the next real pip bump after this merges settles it.

- The saved-reply path #283 attaches to a retry-exhausted first analysis is no longer lost on the web surface (#362). `ProviderOutputError.message` puts that path at its own tail, and the recovery page truncates the notice it renders at 300 characters — so on a realistic contract violation the path never reached the first 300 characters and was gone entirely, and on the shortest possible cause the notice ended mid-filename, at a path that did not resolve and looked complete when it was not. The path now rides its own field, rendered in full and never truncated; the notice text it sits beside no longer has the path clause spliced into what gets cut. The CLI's message is unchanged — this is a web-only rendering fix.
- Compatibility: compatible - a new, additive `analysis_failed_path` query parameter and template block; no persisted format changed, and the CLI's `ProviderOutputError.message` is untouched.

- `plugins/claude-code/scripts/version_skew.py`'s standalone `main()` no longer crashes with a raw
  traceback when `requivo doctor --json` runs past its 30-second subprocess timeout (#363).
  `subprocess.TimeoutExpired` inherits `SubprocessError -> Exception`, not `OSError`, so it fell
  through the existing `except FileNotFoundError` / `except OSError` pair and escaped `main()`
  uncaught -- the exact silent-third-state collapse this module exists to prevent, now happening in
  its own failure path. Reachable, not theoretical: #263 gave `doctor` a per-slug session write
  lock with a 30-second timeout, the same 30 seconds this script waits on the subprocess, so a
  workspace with two stuck sessions can legitimately make the call outlive the wait. A timeout now
  reports `COULD_NOT_LOOK` (exit 3) with its own message, distinguishable from "the `requivo`
  command was not found on PATH" -- a reader told the CLI took too long should look at what is
  stuck (a held session lock), not at their install. Covered by
  `tests/test_plugin_version_skew.py`, including a must-fire control (a genuine version skew still
  reports skew) alongside the could-not-look assertion, and a check that the timeout and
  missing-binary messages read differently.
- Compatibility: compatible - on the timeout path, `main()`'s exit code moves from Python's
  unhandled-exception default (1, from the traceback this issue describes) to the explicit
  `COULD_NOT_LOOK` (3) the other two arms already return; nothing could have relied on the crash,
  since it was never a documented or tested exit value. `check()` and `compare()` are unchanged.

- Corrected a stale justification in `tests/test_boundaries.py`'s `_SURFACE_PROVIDER_ALLOWLIST` (#364). The entry for `deterministic/doctor.py`'s reach to the model-id lookup still described the pre-#268 resolution (`os.getenv("MODEL", "claude-sonnet-5")` alone); it now names the actual `REQUIVO_MODEL`-first precedence. The neighbouring entry is renamed from `credential_present` to `credential_diagnosis` to match #365's fix in the same delta, with an updated reason. No behaviour changed — the guard fired correctly throughout; only its stated reasoning was out of date, and this repository's own class of defect (a written rule true on the day it was written, read later as measured) had landed inside the file that guards against exactly that class.
- Compatibility: compatible - test-only; the guard's pass/fail behaviour is unchanged.

- `requivo doctor` no longer tells the reader to set an API key when the real fault is a credential profile the Anthropic SDK could not load (#365). `credential_present()` deliberately flattens "no credential configured" and "a credential that is configured and unloadable" onto the same boolean — the right answer for a caller that only ever wants a yes/no — but `doctor` is a second reader whose whole job is naming the remedy, and it read that bool alone. A new `credential_diagnosis()` in the Anthropic provider hands back the SDK's own reason for the unloadable case (nothing for the ordinary "no credential visible" case, which is unchanged), and `doctor` now reports "credential configured but could not be loaded" with that reason, instead of "no API key", whenever it applies.
- Compatibility: compatible - `provider_anthropic.api_key_present` keeps its meaning and type; the new `credential_problem` key is additive (`null` in the unaffected, far more common case) and the human rendering changes only for the one install state this fixes.

## [1.3.0] - 2026-08-30

### Highlights

- New `requivo session rescope` re-scopes an existing session's context cards without redoing discovery or losing its artifacts.
- `requivo doctor` and `requivo --version` now print the version, OS and model together — the three facts a bug report needs, in one paste.
- Requivo Web shows what every paid action cost, and sets honest expectations for how long a call takes instead of promising "a few seconds".
- Requivo Web renders saved artifacts (brief, PRD, stories…) as formatted documents instead of raw Markdown in a code block.
- Session slugs read as words again in any Latin language — `track-vendor-invoices`, not `we-need-a-way-to` — instead of the first five tokens of the request verbatim.
- The tracked `.claude/settings.json` no longer silently enables the maintainer's own Claude Code plugins on your machine when you clone this repository.

### Added

- `requivo session rescope <slug> --context <cards>` re-scopes an existing session's context-card
  selection (#168) — the documented recovery for a card that only exists on one machine used to be
  hand-editing the `context_cards` key in `session.json`. Once the session has a model, the re-scope
  is recorded as its own revision (the model carries forward unchanged; `surface: "session-rescope"`
  is what tells it apart from a reasoning turn in the history), so `session.json` shows exactly where
  the selection changed. Existing artifacts are left alone — context is not a dependency edge that
  invalidates them — and nothing is re-run: only the *next* turn reasons against the new selection.

- `requivo doctor` reports candidate residue under the write-lock root, `.requivo/locks/`
  (#180). #113/#179 moved the per-session write lock outside the session directory it guards, and
  there is no `session delete` verb, so a hand-deleted session — the ordinary way one goes here —
  leaves its lock file behind. The `locks` block in `doctor`'s report now names the total lock-file
  count and, of those, which slugs currently name no session, alongside anything under that root
  that is not a `<slug>.lock` file at all. It never concludes "orphan": the lock scan and the
  current session list it is checked against are two reads a moment apart, and it says only what
  the directory can support, the same discipline `doctor` already applies to a non-session entry
  under the session root (#67).

- `requivo demo` now ends on the change-impact step (#223). It stopped at the decision brief, one
  beat short of the only part of the walkthrough a strong prompt cannot also produce: a slot changes,
  and Requivo reports which decisions have to be re-validated, which premises are back in question
  and which documents go stale. The block is computed by walking the dependency graph the discovery
  recorded, not reasoned, so the same change gives the same answer every time -- and it needs no API
  key, which is why the keyless demo is where it belongs.
- The demo's closing step now names something a reader without a key can do (#223). It ended on
  `requivo discover`, which needs one, so the walkthrough written for a visitor with no key left that
  visitor with nothing to try. `requivo web` and `requivo impact` are offered first; `discover` is
  still there, marked as the one that costs something.

- The repository ships visual proof of the product for the first time (#224). It contained zero
  tracked images while a finished demo video sat untracked, and `docs/web.md` -- the page for what the
  project calls its primary interface -- described that interface entirely in prose. Four screenshots
  of Requivo Web now live in `docs/images/`: the home page, the session page, the open questions with
  the readiness verdict, and the decision brief. The README carries the session page near the top,
  through an absolute `raw.githubusercontent.com` URL so it renders on the PyPI project page too,
  which is where a relative link would 404; `docs/web.md` carries all four.
- They are lossless WebP at 2x, and none of them ships in the wheel or the sdist, so the package is
  the same size it was.
- `tests/test_doc_images.py` is the guard, because a broken image is invisible to the person who
  broke it: the README renders on two sites the author is not looking at, and there is no import to
  fail. It checks that every image URL pointing into this repository names a file that exists, that
  no README image is relative, and that every relative image in `docs/` resolves.

- The README now states how Requivo is built (#233): by AI coding agents, under maintainer direction
  and review, with every change landing as a squash-merged pull request that passed every required
  check. Most commits already carried an agent co-author trailer and nothing public said so, which
  left the most interesting thing about the repository to be discovered rather than told. The
  paragraph names the controls -- the pull-request-only flow, the required checks, and the hermetic
  suite whose meta-guards exist to catch exactly the mistakes that development model makes -- and
  invites the reader to judge those rather than the authorship.

- `requivo status` ends by naming the single next command (#246). It is the verb you run coming back
  to a session, and its human view stopped at the question list. Every other surface here already
  followed the rule and said so -- `discover` closes with `requivo answer`, `answer` closes with
  either `requivo brief` or "keep going", and the plugin's status skill states it outright as *point
  at the next step, once*. The CLI's `status` was the one place it was written down and not done.
- One line, never a menu, and the order is a judgment: open questions win over a stale artifact,
  because regenerating a brief against a model that is about to move is a paid call thrown away; a
  stale artifact wins over a missing brief. A converged session whose brief is fresh gets no pointer
  at all, because there is no single next step and offering three would be the menu this refuses.
- `--json` is untouched -- a machine consumer picks its own next step, and a line printed beside the
  payload would break every caller that pipes it into `jq`.

- `requivo --version` exists, and `requivo doctor` reports the OS and the model (#247). A bug
  reporter's first reflex -- and what this repository's own bug template asks for -- is the version,
  the OS and the model in use. `requivo --version` answered `the following arguments are required:
  <command>` and exited 2, and `doctor` reported Python and Requivo but neither the platform nor
  whether `MODEL` was overridden. Assembling a bug report meant three lookups; it is now one paste.
- The version string is **read** from `requivo.__version__`, not written into `cli.py`.
  `tests/test_version_sites.py` enforces agreement across the four files that declare the version and
  does not scan `cli.py`, so a literal there would have been an unguarded fifth declaration added by
  the change whose whole subject is telling people the right version.
- `doctor` gains two additive `--json` keys, `os` and `model` (`{name, source}`), and two rows in the
  human view. `model.source` distinguishes `env` from `default`, because a reporter with `MODEL` set
  and forgotten is the case the row exists for and a resolved name alone reads identically in both.
- `name` and `source` are read from the same fact, so they cannot disagree. An exported-but-empty
  `MODEL` is an override, not the default -- the fallback in `current_model_name` fires on the
  variable being *absent*, and testing the value for truth instead reported a name of `""` under a
  source of `default`, naming neither the default it claimed nor the override really in force. The
  human row also refuses to print a tick over an empty model id, because that is not a working
  install: every provider call would send no model at all.
- Compatibility: compatible - both `doctor` keys are additive, and no existing key changes shape.

- The docs state what a run costs, in dollars, before the first paid command (#252). You paste your
  own key and pay with your own money, and nothing readable beforehand named a figure: the README
  never mentioned cost and `docs/providers.md` explained the caching mechanics without one. The CLI
  printed the real number, but only after the spend -- the wrong side of the decision.
- Roughly $0.03 to $0.06 per call, which puts a complete session under $1. `docs/providers.md` has
  the per-step table with its call counts, token ranges, method and limits; the README carries the
  headline where somebody decides whether to set a key at all.
- **Every figure is derived, not typed.** `tests/test_cost_claims.py` recomputes each one from the
  rate table in `providers/anthropic/pricing.py` and checks the published token ranges against the
  prompts this repository assembles and the replies captured in `fixtures/golden/`. Editing the rate
  table turns the docs red rather than quietly outdating them -- which is the failure this project
  has already paid for once, when the table carried the wrong Sonnet rate for a release behind an
  expiry nobody could falsify (#254).
- One limit stated in the docs rather than glossed: the token counts are estimated at four characters
  per token, because no real ledger capture is committed here and the suite makes no API calls. The
  dollars are exact arithmetic over those estimates at the current rate.

- Added (#253): the web says what a paid action cost. Every provider call made through a browser was
  billed and recorded nowhere — `track_usage()` was opened in `cli.py` and nowhere else, and
  `record_call()` is a no-op with no active ledger. The answers turn and a document generation now
  carry their footprint in the fragment the reader lands on: exact tokens, and a cost labelled as an
  estimate with the date of the rate table behind it. A model with no price on file says so rather
  than borrowing a neighbour's rate, and a call the provider reported no usage for says nothing
  rather than reporting zero. The two paths that answer with a redirect record it to the
  `requivo.web` logger instead — a 303 has no body — and that line is written from a `finally`, so a
  call that failed after spending tokens still leaves a trace.

### Changed

- The tracked `.claude/settings.json` no longer configures anything on a contributor's machine
  (#215). It enabled four of the maintainer's own Claude Code plugins and registered a `statusLine`
  command running `.oss/statusline.py` -- 1,922 lines of maintenance tooling whose refresh path forks
  off forge calls with whatever credentials the machine holds -- for everyone who cloned the
  repository and opened it in Claude Code. Scope, since the two halves differ: the plugin enablement
  shipped in every release from 0.10.0 to 1.2.0, while the `statusLine` landed after 1.2.0 was tagged
  and reached only clones of `main`. Both moved to the untracked `.claude/settings.local.json`,
  which `.gitignore` already excluded for exactly this reason; the tracked file is now an empty JSON
  object. Nothing in the product, the test suite or CI reads it.
- The guard that was supposed to catch this could not see it, and now guards the class rather than
  the instance (#215). `tests/test_agent_layer.py` promises that the tracked `.claude/` layer stays
  inert for anyone without the maintainer's plugins, and it checked for a `hooks` key and nothing
  else -- so the `statusLine` added beside it passed silently, pointing at a script outside
  `.claude/` that the tracked-script scan could not see either. It now refuses any top-level key
  outside an allowlist that is deliberately empty, any command named at any depth under any key
  name, and any plugin enablement; it refuses to answer at all when its scan set is empty; and it
  keeps `.claude/settings.local.json` out of the index. Each detector has a must-fire control, so a
  clean run means the checks looked rather than that they were unable to.

- GOVERNANCE.md, SECURITY.md and CODE_OF_CONDUCT.md agree with each other and with the repository's
  live settings (#220). GOVERNANCE described a code of conduct as something that "can be introduced"
  beside one that has shipped since 2026-08-18; SECURITY told reporters to find an email on a public
  profile while GitHub private vulnerability reporting is enabled and the issue chooser already
  pointed there — two instructions for one act, with the weaker one in the canonical file. All three
  now name the same private channel.

- The canonical `leave-approval` example was regenerated end to end (#223). Its committed model
  carried no reasoning layer at all -- no decisions, no challenges, no opportunities -- so
  `requivo impact` on the example the README calls "the one to read first" listed stale artifacts and
  nothing else, while that same README promised it would name the decisions resting on the
  integration topic. The differentiator was claimed on the canonical example and reproducible only on
  the other one.
- All nine files come from one run in one sitting: a real discovery, three answers, then the brief,
  the PRD, the acceptance criteria, the epic with its three exports, and the release notes, every one
  of them generated from the same model at the same revision. The decision brief that ships is
  therefore the current one, and the example README's note apologising that it "predates the current
  decision-brief layout" is gone with the thing it apologised for.
- Two tests now hold the pair together with no API call: the brief's `What is confirmed` and
  `Important assumptions` sections are projections of the model, so they are re-derived from the
  committed `model.json` and compared; and `impact` on that model must return a decision, a premise
  and an artifact for the integration topic. Neither could be written while the shipped assessment
  was a frozen capture from an older run.
- The example README's change-impact walkthrough now describes what the committed model actually
  contains, and says where the reasoning layer comes from: the brief is what produces it, so `impact`
  has decisions to report only after `requivo brief` has run.

- The README's first runnable command no longer needs an API key (#225). "Start here" opened on
  `uvx --from "requivo[web,anthropic]" requivo web`, which requires uv *and* an Anthropic key before
  anything happens -- while `requivo demo`, the one asset that needs neither, was mentioned once,
  parenthetically, sixty lines further down. The section now leads with `uvx --from requivo requivo
  demo` (verified against a bare wheel install with no extras and no key), and the web command
  follows it, saying in the same breath that analysing is what needs the key.
- `requivo demo`'s closing block no longer points a wheel install at files it does not have (#225).
  It proved "everything else is a view" by naming `examples/<slug>/epic.md` and
  `acceptance-criteria.md` -- paths that exist in a clone and nowhere else, so the walkthrough's
  closing evidence was two dead pointers for every install the README actually recommends. It prints
  the browsable URL now, and the one command in that block that needs a key says so on the line below
  it rather than leaving a keyless reader to find out.

- The public copy states one maturity story and no version number (#230). README's Status said "at
  **1.0**" while PyPI served 1.2.0 — prose that dates itself against the next release, which is the
  class `tests/test_version_sites.py` exists for and cannot reach — and the PyPI classifier said
  Beta while SECURITY.md and CONTRIBUTING.md said "early open-source beta". The classifier is now
  Production/Stable and the prose is version-free.
- The PyPI description names what Requivo does that a strong prompt does not, and drops the word
  "validated" (#230): validation is `docs/product-validation.md`'s protocol, and it has not been run.
- `docs/cli.md` no longer says most verbs accept a path to a saved `model.json`. Only `status` and
  `impact` do; the seven generator verbs resolve a session, because they write a revision or an
  artifact back into one (#230).
- README links are written as references and resolved absolutely (#230). `pyproject` sets
  `readme = README.md`, so PyPI renders this file as the project page and leaves relative hrefs
  alone — every one of the fifteen 404'd there. Keeping the URLs in one block at the foot is what
  stops the fix from costing the prose its line width.

- Changed (#235): saved artifacts are rendered as formatted documents on the web instead of raw
  Markdown in a code block. The decision brief is this product's stated primary deliverable, and the
  audience the web vocabulary exists for was handed literal `# Decision Brief` and `**Objective:**` at
  the exact moment the product delivers its value. No new dependency: the dialect is closed to what
  `render/markdown.py` actually emits, and the renderer builds every tag itself around text it has
  already escaped, so content a language model wrote or a user edited on disk cannot become live
  markup and there is no sanitizer to keep in step with a parser. Anything outside the dialect
  degrades to escaped text. The Download link still serves the exact bytes on disk.

- Changed (#236): the web now tells the truth about how long a paid call takes. The copy beside a
  provider-backed button promised "a few seconds" while this project's own invariants describe those
  calls as taking seconds to minutes — an expectation that expires on a page that blocks, and what a
  reader does when it expires is resubmit, buying a second session and a second paid call. It says
  "usually under a minute" and what the wait is for, and after ten elapsed seconds the status text
  starts reporting how long it has been running. Nothing changes before then, deliberately: a label
  that churns from the start says nothing about a slow call, so the change itself is the signal. The
  deferred-analysis page gained the status text it never had, and with JavaScript off the static copy
  still stands on its own.

- `requivo --help` is ordered around the user journey and marks the verbs that spend money (#244).
  argparse renders subcommands in registration order, and the deterministic package registered first
  -- so the six plumbing entries led and `demo` and `discover`, the two verbs a new user needs, sat
  seventh and eighth. The order is now demo, discover, the refinement verbs, the generators, the
  offline plumbing, then `web`.
- Nine verbs carry an `(API)` marker and the other ten carry none, so it is possible to tell from
  `--help` that `brief` will bill you and `status` will not. A closing paragraph names the first
  command to run and says what the marker means -- a marker nobody defines is a decoration.
- `tests/test_cli_help.py` checks the marker in **both** directions, and derives the expected set
  from the provider's own operation table rather than listing it: a list in a test is a second copy
  of a decision, and a generator added later would otherwise arrive unmarked with the guard still
  green.

- Session slugs are derived from the content words of a request, in any Latin language (#245). The
  handle you retype into `answer`, `status`, `brief` and `prd` was the first five tokens of the
  request verbatim, so the most common opening produced `we-need-a-way-to` -- unmemorable, and shared
  by every other request that opens the same way, which then differed only by a collision hash.
  Function words are dropped before the five are taken, so *"We need a way to track vendor invoices"*
  becomes `track-vendor-invoices`.
- Accents are folded first, which fixes a worse half of the same defect: `[a-z0-9]+` treated an
  accented letter as a *separator*, so it split the word rather than losing the accent.
  *"Nous aimerions un système d'approbation des congés"* produced `nous-aimerions-un-syst-me`; it now
  produces `systeme-approbation-conges`. Letters that carry no combining mark for NFKD to strip --
  eszett, the ligatures, the stroked letters -- are spelled out before the fold, or it would delete
  them and mangle the word one letter along.
- Compatibility: compatible - sessions already on disk keep their names, nothing re-derives a slug
  for a session that exists, and the emitted alphabet is unchanged so every existing slug stays
  valid. What changes is idempotent re-discovery: re-running `requivo discover` on a request first
  analysed by an older Requivo now creates a second session instead of resolving to the first, where
  it used to resolve and then be refused for free by the revision-zero gate.
  `docs/compatibility.md` carries it, with the remedy.
- Two limits are stated rather than left to be found. A request in a script the ASCII fold cannot
  romanize -- Japanese, Cyrillic -- still lands on `discovery`, and the second on
  `discovery-<hash>`. And matching is case-folded ASCII, so a short function word collides with an
  acronym: `er` eats the ER in "an ER diagram", as do `im`, `am`, `us`, `et`, `est` and `par`.
  Neither is fixable by pruning the list, and `session init --slug` is the way past both.
- The rule that keeps the list honest -- a word is in it only if it is a function word in some
  in-scope language and not a content word in any of them -- is a test rather than a paragraph.
  `son` was in it anyway, two lines under the comment naming it as deliberately excluded.

- The `anthropic` extra now accepts the SDK's 1.x major (`anthropic>=0.40.0,<2`), so
  `pip install 'requivo[anthropic]'` resolves 1.x on Python 3.10 and above (#314). The ceiling had
  been `<1` since before that major existed, and this repo's rule is that a ceiling states what we
  have actually been run against -- so the widen carries that evidence rather than a bot's guess:
  the suite green at 1.2.0 across the py3.10-3.13, macOS and Windows legs, and, because no CI leg
  makes a wire call, one real discovery run and one real rejected-credential run against 1.2.0
  exercising the paid path and the typed provider-error arms. Nothing in Requivo's use of the SDK
  is touched by the 1.0 break: it constructs no `httpx` object and passes none in.
- Because anthropic 1.x requires Python 3.10 while Requivo still declares 3.9, the installed SDK
  major now depends on the interpreter -- 0.x on 3.9, 1.x above it. Both are supported and tested;
  no action is needed. Users who want the SDK's move to the maintained `httpx2` fork need Python
  3.10 or newer.

### Fixed

- `tests/test_narrative_references.py` never looked under `docs/` or `tests/`, and never recognised
  a `.js` file, so a narrative reference living in any of those was invisible to the guard CLAUDE.md
  names as the thing that keeps a reference honest (#156). `docs/` now joins the resolution scan
  (8 references, all clean) and `.js` joins the recognised suffixes; the wrap check -- which has no
  pointer-versus-mention ambiguity -- also now runs over `tests/`, where the motivating instance
  (`tests/web/busy_harness.js`) originally sat. The *resolution* check is deliberately not widened
  to `tests/`: measured, it turns up eleven apparent dangling references, all false positives (nine
  historical mentions of a module name that no longer exists on purpose, two of this guard's own
  fixture strings), and resolving that needs a rule that tells a pointer from a mention -- filed
  separately rather than attempted here.

- The golden harness's `training-budget` interactive request had 15 of its 29 answer-sheet layers
  unreachable (#163): the engine asks almost exclusively about `business_rules`, `workflow`,
  `permissions` and `edge_cases`, so those four ran dry by turn 4 and the loop converged for want of
  anything left to say rather than because the engine was finished. `fixtures/golden/requests.md`
  now carries ten layers on each of those four slots (up from 2-3), leaving every other slot as it
  was, so a re-capture can reach five or more turns on all three runs instead of one in three.
  `scripts/golden_lib.py`'s `AnswerSheet.remaining()`, removed as dead in #137, is wired back in as
  `unreached_layers()`: on a capture that stayed SHALLOW, `golden_diff.py` now names which slots
  still had something on the sheet the conversation never came back to ask, replacing the by-hand
  diagnosis that explained the 4/5/4 depths in the first place. The re-capture itself (15 API calls)
  is not run by this change and is left as a spend decision for the maintainer -- the committed
  `training-budget.runs.json` baseline still measures the old, shallower request set until it is
  re-captured.

- The "Decision brief" rename now reaches `assets/framework/elicitation.md`, the human-readable
  spec of the framework: it named the artifact "a solution assessment" after every user-facing
  caption had already moved to "Decision brief" in #171 (#166). That file is not read by
  `build_prompt()`/`load_context()` -- it is only printed by `requivo schema --framework`, for a
  human or a Claude Code agent -- so nothing here is golden-measured and the rename cost no API
  spend. `assets/prompts/brief.md`, the sibling asset the issue also named, is left as-is on
  purpose: it is fed into the model's system prompt verbatim, so its wording is a golden-harness
  spend decision, not a caption fix, and the reason it still says "solution assessment" is now
  recorded at its call site (`providers/anthropic/generators.py:advise`).

- `examples/event-checkin-reconciliation/solution-assessment.md` (and its bundled twin under
  `requivo demo`) was stale against the current renderer: the readiness block named six blockers
  where the live model now produces eight, and the banner was missing the `DRAFT ` prefix the
  renderer prints for a model with blockers (#172). Both are corrected, and a new test,
  `test_the_browsable_examples_deterministic_half_matches_the_renderer`, re-derives the readiness
  block, the banner and its draft sub-line (now `render/terminal.py`'s `DRAFT_NOTE` constant, so
  the test imports it rather than duplicating the literal) from the example's own `model.json` and
  compares them against the captured file, so this class of drift is caught with no API call. The
  LLM-authored prose (challenges, risks, opportunities, next steps) is unchanged and is not covered
  by the new guard -- regenerating it is a separate spend decision.

- `scripts/plugin_cli_drift.py` now survives a console that cannot encode a character in the
  plugin-derived text it prints (a directory name, a released version string) instead of letting
  the crash overwrite a real drift finding with a false could-not-look verdict (#174). Its own
  `_harden_streams()` now also reports, on stderr, a stream it could not harden -- the third state
  `requivo.streams.configure_stream` reports for the product -- rather than swallowing that case
  silently.

- `tests/test_boundaries.py`'s provider guard now scans `web/` and `deterministic/` in addition to
  `cli.py` and `render/` (#183), so the two provider imports #167 called legitimate --
  `web/config.py`'s SDK probe and `web/app.py`'s `EngineError` -- are now covered by an allowlist
  entry instead of by nothing. Neither import changed; the guard just watches them now.

- `tests/test_narrative_references.py`'s *resolution* check now covers `tests/` too, closing the
  gap #156 opened and #188 measured but declined to close (#190). Widening the glob alone produced
  eleven apparent dangling references and all eleven were false positives, of exactly two kinds:
  eight files recount the same "Split out of `test_cli_deterministic.py`" provenance idiom, worded
  slightly differently per file -- naming a module deleted on purpose, to recount what happened
  rather than to point a reader at a guard -- and the guard's
  own file supplied the other three, being unable to resolve its own wrap-detector fixtures and its
  own prose quoting that idiom. Both are now recognised mechanically rather than left as coverage
  that reads as more than it is: `_HISTORICAL_MENTION` exempts the established "Split out of
  `X.py`" idiom (and only that idiom -- a rename that skips it is exactly as loud as any other
  broken pointer), and the guard excludes its own module from resolution by identity, staying fully
  in the wrap scan. `tests/web/busy_harness.js`, the motivating instance for #156, now resolves as
  an ordinary subject like any other.

- A provider verb run without an API key refuses in one line instead of a traceback (#201). This was
  the most likely first failure of a fresh `pip install requivo[anthropic]`: run the command the demo
  suggests before setting a key, and the SDK raised a bare `TypeError` out of its own auth resolution
  — not an `APIError`, so the transport arm did not see it, and not a `RequivoError`, so `cli.app()`
  did not either. It reached the operator as a stack naming neither `ANTHROPIC_API_KEY`, nor `.env`,
  nor `requivo doctor`. `new_client()` now refuses upfront, before a session is claimed and before
  anything is billed, and `ANTHROPIC_AUTH_TOKEN` is accepted so a bearer-token setup is not
  false-refused.
- Transport failures say which kind they are (#201). `AuthenticationError`, `PermissionDeniedError`
  and `RateLimitError` are all `APIError` subclasses, so a rejected credential was answered with
  "Retry the command in a moment" — advice that never works on a 401. A credential failure now names
  the key remedy and does not suggest retrying; a rate limit says to wait for the reset; a connection
  drop, a timeout or a 5xx keeps the original wording, which was right for those all along.

- A failure at the end of an interactive `requivo discover` no longer discards the whole conversation
  (#202). The loop drafts up to eight paid turns in memory and then made a ninth paid call for the
  decision brief **before** anything was written, so one transient API error on that last call threw
  away all eight turns, every answer the user had typed, and left the session at revision 0 -- with a
  transport message that named neither the session nor a way back, so retrying meant restarting the
  conversation at full price. The converged model is now persisted first and the brief is produced
  through the same path every other surface uses, which makes that failure cost one call instead of
  nine and names `requivo brief <slug>` as the retry.
- A provider failure *mid-loop* keeps the turns that succeeded, too: the model is what the loop
  carries, so the last good turn already holds every answer given so far. It is saved, and the output
  names the session and the `requivo answer <slug> "..."` continuation. A turn-1 failure has nothing to
  save and says so, pointing back at `discover` rather than at a verb with no model to refine.
- Ctrl-C landing inside a provider call -- the several-second window where it is most likely to land --
  no longer produces a raw traceback. It reports the claimed session and keeps the drafted turns.
- A finished interactive discovery now lands on revision 2 rather than revision 1: the converged model
  is one revision and the assessment's absorbed reasoning is the next, which is what `requivo brief`
  has always done on an existing session. Revision numbers are provenance, not a promise about their
  value.
- Stopping the loop on purpose (`q`, or Ctrl-C at a question) keeps its turns too. It used to discard
  them and leave the session at revision 0, which is the same loss as a failed turn wearing a friendlier
  word. A stopped discovery now lands exactly where `--once` already landed -- revision 1, questions
  still open, `requivo answer <slug> "..."` named -- so the two entry points leave one shape of session
  rather than two. The accepted cost: re-running `discover` on that same request is now refused, and
  that refusal already names both ways on (refine it, or use another slug).

- Error messages in Requivo Web are visible again -- until now every single one was dropped on the
  floor in a real browser (#203). The vendored htmx swaps only 2xx/3xx responses and nothing opted in,
  so a revision conflict from a second tab, the oversized-answers refusal, and a provider failure after
  a minutes-long **paid** generation all produced the same thing: the progress bar completed, the
  buttons came back, the page did not change, and nothing was said. On the paid one the natural next
  move is to click the button again and pay again. The whole server-side error architecture was
  unreachable past the network boundary, and the Python suite could not see it because its test client
  runs no JavaScript.
- Where each error lands is now part of the design rather than an accident. The one-line error notice
  is retargeted into a dedicated flash region, so it can never replace the region holding answers you
  have typed -- which is the destruction #30 was filed to stop, and which a naive fix would have
  reintroduced. The oversized-answers refusal keeps returning the full region with your submission
  still in it.
- A Node harness drives the real `app.js` against htmx's own swap gate, so the fix is pinned by effect
  rather than by spelling; it goes red if the opt-in is removed. A 204 "nothing to render" is still
  honoured, and ordinary successful swaps are untouched.

- A truncated or corrupt `model.json` is a structured error instead of a traceback (#204). The file
  this product calls its durable output was read through `PersistedEngineOutput.model_validate_json`
  with nothing around it, so a pydantic `ValidationError` — which is not a `RequivoError` — went
  straight past `cli.app()` as a stack trace from `status`, `impact` and `model show`, and past the
  web error handler into a generic "Something went wrong on the server" 500. The new
  `model_unreadable` code names the file, the session, `requivo session verify <slug>`, and the fact
  that `revisions/` holds every applied model — a remedy that was on disk the whole time with nothing
  saying so. `session verify` and `session list` are unchanged.
- Compatibility: compatible - `model_unreadable` is a new arm of the `invalid_session` family for a
  condition that previously carried no code at all, so nothing moved from one code to another. Added
  to the published table in `docs/compatibility.md` at HTTP 500.

- Fixed (#205): the web answers turn no longer pays for an analysis whose result is already
  guaranteed to be discarded. When a second tab, the CLI or a back-button submit had moved the
  session past the revision the form was rendered at, the conflict was certain the moment the
  snapshot was read — but it was only raised after a full provider call, so the user was billed for a
  turn nothing would ever read. The check now runs before the call; the precondition on the apply
  stays, because the session moving *during* the call is a different race.

- A first analysis that fails no longer dead-ends on an error page that hides the request it just saved
  (#207). Requivo Web claims the session *before* the provider call, deliberately, so a transient API
  failure left the pasted email safe on disk and the session's own page already offering an "Analyse
  request" retry button -- and then sent the user to "Something went wrong... check the server logs.
  Back to sessions." Nothing said the request had been saved or where, so a first-time user on a
  transient error could only conclude the product had eaten it. Both doors onto a first analysis now
  land on that session's page with the cause stated and the retry button in reach.

- Every command in the two examples' "Reproduce it" blocks now runs on a fresh clone (#222). They
  opened with `requivo brief examples/leave-approval/model.json`, and all seven generator verbs
  raised `SessionNotFoundError` there: a generator writes a revision and an artifact back into a
  session, so it resolves a slug and requires it to exist, while `.requivo/` is gitignored and no
  such session ships. The commands worked only on a machine that happened to have run them before,
  which is the first thing an evaluator tries. Both blocks now bootstrap the session offline in two
  commands -- `requivo session init` on the example's request, then `requivo model apply` on its
  committed `model.json` -- and run the generators against the slug.
- The leave-approval example no longer claims its output lands in `out/<slug>/`, a root retired in
  0.9.8 and opened since by nothing but `requivo session migrate`. Documents land in
  `.requivo/sessions/<slug>/artifacts/`, and both blocks now say so.
- The documented sequence is read out of the READMEs and executed by the test suite, so a block that
  stops working goes red on the edit that broke it rather than on the first reader who tries it.

- The README's Claude Code quickstart no longer dead-ends (#227). Adding a marketplace does not
  install a plugin, so the printed sequence reached `/requivo:discover` before the skill existed and
  failed as an unknown command at the moment of first use. It now gives the same three steps
  `docs/getting-started.md` and the plugin README already gave.

- Fixed (#237): the home page's "Recent" list is now actually recent. Rows came off
  `SessionService.list_entries()`, which sorts by slug, so with a handful of sessions the one touched
  five minutes ago could sit at the bottom while an abandoned experiment led the page. The web view
  model orders them newest-first; a session nobody could read sorts last rather than first, since it
  states no timestamp at all and an empty string is the smallest string. Timestamps read as "3 days
  ago" or "25 Aug 2026" instead of "2026-08-25T12:36:48Z", with the exact instant kept on the row.
  `requivo session list` is unchanged: its slug order is a public surface.

- The Claude Code plugin no longer offers the CLI's provider-backed generators as if they were
  keyless (#242). Its catalog description said a reader could "hand the same model to the requivo CLI
  for acceptance criteria and tracker epics" and, in the same paragraph, that "there is no API key to
  configure"; the README's *Beyond the six skills* section listed the same generators with no mention
  of what they need. Both statements about the plugin are true, and the verbs are not covered by
  them: `criteria`, `epic`, `release`, `stories` and `estimate` call the Anthropic API directly, so a
  marketplace reader who followed the pointer met the missing-SDK error first and the missing-key
  error second, having been told twice that neither applied.
- The description now says those generators run in Requivo's optional API mode and do need a key, and
  the README section names both the `requivo[anthropic]` extra and `ANTHROPIC_API_KEY`, alongside why
  that is the opposite of what its own install section says and why both are right.
- `tests/test_plugin_copy.py` holds it. The storefront is two hand-edited files saying one thing, and
  it checks both: a description claiming "no API key" while naming a CLI generator must also name the
  API mode, and the README section that lists them must name the extra and the key. The section is
  read on its own rather than the whole file, because the install section mentions the same key in
  order to say it is *not* needed, so a whole-file scan would have passed on the defect.

- Every CLI route to "there is no such session" now names the sessions root it searched and the
  command that lists what is there (#243). There were five wordings across three modules; exactly one
  named a root, none named `requivo session list`, and two leaked the word *canonical* -- engine
  vocabulary for "not the retired `out/` layout", which names a distinction a user cannot act on.
- Naming the root is the fix rather than a nicety. The plugin README spends two paragraphs on the
  trap it calls *fails in no visible way*: sessions live under the workspace you run from, so a valid
  session is invisible from one directory and present from another. `no model file or session found
  for 'leave-aproval'` said nothing about which of the two you were in.
- `--json` is untouched: `code` is still `session_not_found` and the reference is still in `details`.
  Only the human sentence changed, and `docs/compatibility.md` already places terminal output
  outside the stability promise.
- `tests/test_session_not_found.py` sweeps the thirteen verbs rather than the five raising sites,
  because each site was individually defensible and what was wrong was the set -- a test per verb
  would have passed on all five wordings, and the sixth site added later would have arrived with a
  sixth.

- Cost estimates for `claude-sonnet-5` now use its confirmed standard rate of $2/$10 per million
  tokens (#254). The table carried $3/$15 behind a launch row expiring 2026-08-31 — Sonnet 4.6's
  rate, inherited from the previous Sonnet generation rather than confirmed — so from September 1
  every printed estimate would have over-reported by exactly 50%. Anthropic's pricing page now
  states that the introductory $2/$10 is the standard price and the scheduled increase will not
  occur; the table records where the rate was read and when.

- CONTRIBUTING.md no longer states four things that are not true (#279): the removed `pc` alias, a
  `core/` layer with "no I/O" that CLAUDE.md's invariant 7 explicitly retracts, a working style of
  committing straight to `main` that has not held since every change started landing as a
  squash-merged pull request, and a `.claude/settings.json` said to carry one key when it carries
  two. The inertness argument in that section rests on the enumeration being complete, so an
  incomplete one weakened the claim it was making.

- The GitHub new-issue chooser offers one Bug report and one Feature request instead of two of each
  (#280). The older pair is gone and their better fields are merged into the survivors; the bug
  template asks for `requivo doctor --json` — one paste covering version, Python, provider, streams
  and workspace, with no session content in it — rather than a hand-filled environment checklist,
  and no template prompts for the `pc` command removed in 0.9.8. The chooser's contact links all
  resolve: the Discussions link 404'd because Discussions was disabled, which is now enabled, and a
  documentation link sits beside it.

- `demo-out/` is gitignored (#281). It held the demo video, poster frames and a nested session store,
  was referenced by nothing in `src/`, `scripts/` or `docs/`, and was the only untracked entry in
  `git status` — one `git add .` away from committing multi-megabyte media into a library repo,
  permanently.
- CONTRIBUTING.md names `.oss.json`, `.oss/` and `.supertool.json`, says a contributor needs none of
  them, and links `.oss/README.md` (#281). They are tracked maintainer tooling, and the reassurance
  already written for `.claude/` did not extend to them.

- Dependabot's pip ecosystem is pinned to `versioning-strategy: increase-if-necessary`, so it only
  edits a requirement that cannot already accept the new version (#297). On the default it rewrites
  the *floor* of every range, and a floor here is the promise `pip install requivo` makes — the one
  thing `scripts/dependency_floor.py` exists to measure. The first run proposed `anthropic>=0.40.0,<1`
  → `>=1.0.0,<2` straight through a deliberate major ceiling, `fastapi>=0.110,<1` → `>=0.141.1,<1`
  cutting off every user in between, and `python-dotenv>=1.2.3` whose releases require Python 3.10
  against a package that declares 3.9.

- Five stale facts in the packaging surfaces are corrected (#303): a ruff per-file-ignores block for
  `src/engine.py`, deleted at 0.9.8, whose comment pointed at the module docstring of a module that
  no longer exists; two CI comments directing a reader to a standalone pyright config file that does
  not exist and that `[tool.pyright]`'s own comment says must not; a CI leg count stated as a number
  in `scripts/dependency_floor.py`; and a release gate that linted a narrower scope than the merge
  gate, so a lint error in `scripts/` could reach a publish having never been seen by the workflow
  that publishes it.
- The `dev` extra references `requivo[anthropic,web]` instead of restating their five pins (#303).
  They matched, and a duplicated ceiling is one that can move on one side only — after which CI
  tests a dependency set no user can install, invisibly, which is the failure shape the floor leg
  exists to prevent for versions.
- `docs/architecture.md` names `_SURFACE_PROVIDER_ALLOWLIST` rather than counting its entries, and
  CLAUDE.md's generator checklist says where `_WRITERS` actually lives (#303). Both had drifted: the
  count said three where the allowlist holds two after #167, and `_WRITERS` is in
  `services/discovery.py`, not `render/markdown.py`. The narrative-reference guard checks that names
  resolve and cannot check a fact, which is why a name is the durable form.

- The privacy marker that keeps sessions out of git could be switched off permanently by one
  transient error (#320, hardening #211). The store decided whether to write `.requivo/.gitignore` by
  asking whether `.requivo/` already existed -- read *before* the write. So if the directory was
  created and the marker write then failed (a full disk, a permissions error, a Windows scanner
  holding a handle), the failure was visible but left the store present and unignored, and every
  later run took the "already exists" branch and never tried again. Creating the directory is now
  what decides it, and a failed marker write removes the directory again, so the two states are
  "store and marker" or "neither" and a retry starts clean.
- The same probe re-raised a permissions error from an unreadable parent directory, and that is not a
  structured Requivo error -- so the first command run in such a workspace ended in a stack trace
  instead of a refusal. Nothing probes now, and every failure creating the store is reported as
  itself rather than as "could not open the write lock".
- Ctrl-C during the decision-brief call is no longer a raw traceback (#320, hardening #202). That
  guarantee shipped for the discovery turns and not for the one remaining long provider call -- the
  call that step was moved for. A failure while saving rescued turns now says so and still names the
  provider failure that stopped the run, instead of replacing it.
- An error notice in Requivo Web is cleared when the next request starts (#320, hardening #203).
  Nothing emptied it, so an error from one action stayed on screen through a later successful one,
  and a resolved failure went on being displayed as a current one.
- Also corrected: `docs/cli.md` still described the old stop-loses-your-turns behaviour, `CLAUDE.md`
  still described a service method nothing calls, an invariant cited a test that passes without the
  guard it claimed to name, and the guard against creating store directories outside the one safe
  helper scanned a hardcoded three-file list -- it walks the package now, refuses an empty scan, and
  recognises `os.makedirs`.

- `requivo doctor --json` and Requivo Web's provider probe report a credential as present by the
  same rule `new_client()` authenticates from, instead of each keeping its own copy (#332). #201
  widened the runner to accept `ANTHROPIC_AUTH_TOKEN` alongside `ANTHROPIC_API_KEY`, but left
  `web/config.py` and `deterministic/doctor.py` reading `ANTHROPIC_API_KEY` alone — so a working
  bearer-token install built a client fine from the CLI while `requivo doctor --json` reported
  `"api_key_present": false` and the web surface silently fell back to "create session only" for
  every provider action. Both now call the one function `new_client()` itself reads from.

### Security

- Sessions no longer land in your git repository by default (#211). `.requivo/` is written into the
  workspace you run from, which for the Claude Code plugin is your project repository by construction,
  and `request.md` holds the originating request verbatim -- for most users a client's own words, often
  material they are under an obligation not to publish. A routine `git add .` published it silently,
  against the local-first confidentiality this product states as its wedge. Requivo now writes
  `.requivo/.gitignore` containing `*` on the call that first creates the store, so git ignores the
  whole directory and the user's own `.gitignore` is never edited.
- It is written **once**, on creation, and never restored: the trigger is the store root not existing
  yet, not the ignore file being absent. Deleting it to commit sessions deliberately keeps them
  committed, and editing it keeps the edit. `requivo session export` / `session import` remain the way
  to share a single session, and an import into a workspace that has no `.requivo/` yet writes the
  ignore file too.
- Compatibility: compatible - no session format change and no behaviour change for an existing
  workspace, whose `.requivo/` already exists and is therefore left exactly as it is. A workspace that
  was already committing sessions goes on committing them.

- Fixed (#212): a request token containing any non-ASCII character turned the web cross-site guard's
  403 into a 500. `secrets.compare_digest` refuses two non-ASCII `str` arguments, and the `TypeError`
  escaped the guard's own handlers and the security-header middleware with it — so the one crash path
  in the security module was also the one response served without Content-Security-Policy, nosniff or
  Referrer-Policy. Tokens are compared as bytes now, so a wrong token of any spelling gets the
  ordinary refusal. No behaviour change for any ASCII token, valid or invalid, and no bypass existed:
  it failed closed.

- LLM-authored prose can no longer write a line of the terminal render path (#213). A client request
  is untrusted business data by SECURITY.md's own framing, and the engine turns it into questions,
  challenges, opportunities and brief prose that `render/terminal.py` printed through bare f-strings.
  A steered reply carrying an embedded newline wrote the line after it at column zero, in Requivo's
  own voice -- a forged `Ready` verdict -- and a raw escape sequence cleared the screen or moved the
  cursor outright.
- The diagnostic verbs were already covered (`doctor`, `session verify`, `session show`,
  `artifact list`, `impact`, per #40). The primary path -- what a first-time user sees on `discover`,
  `status`, `brief`, `stories` and `estimate` -- was the one place the guard had never been applied.
- `streams.py` was no defence and it is worth saying why, because it looks as though it should be:
  `errors="backslashreplace"` acts on characters the console cannot *encode*, and ESC encodes
  perfectly well in UTF-8.
- `core.selectors.display_text` is the neutralizer -- the prose sibling of the `display_token` those
  diagnostic sites already call, escaping per character rather than quoting the whole value, because
  `repr()`-ing a two-hundred-character challenge over one stray byte would be a worse outcome than
  the injection and would ship green.
- `tests/test_render_untrusted_output.py` pins each named field and then **sweeps**: every
  LLM-authored string forged at once, every terminal renderer run, so a field added later and printed
  raw goes red under its renderer's name. A byte-for-byte control test pins that ordinary prose is
  unchanged.
- Known gap, stated rather than left to be found: `render/markdown.py` is out of scope here and its
  output reaches the terminal on `prd`, `criteria`, `epic` and `release`. Escaping there would change
  the bytes written to disk, which `core/integrity.py` hashes, so it needs its own change.
- Compatibility: compatible - terminal output only, no saved artifact bytes change, and prose with no
  control characters renders exactly as before.

- The PyPI publish workflow pins every action to a commit SHA instead of a mutable tag or branch
  (#214). That job holds `id-token: write` — it mints the short-lived OIDC identity PyPI trusts in
  place of a stored token — so whatever runs inside it can publish `requivo` irreversibly, and it
  consumed `pypa/gh-action-pypi-publish@release/v1`, a branch. A version comment beside each SHA
  names what it pins, and dependabot continues to move them with a reviewable PR.
- Compatibility: compatible - a workflow-internal change; no published interface, session format or
  command surface is touched.

- Dependabot watches Python dependencies, not only GitHub Actions (#297). Nothing opened an update or
  advisory PR for pydantic, anthropic or the web stack — jinja2 and python-multipart both have CVEs
  fixed above the floors this project declares — so a published advisory in that stack would have
  gone unnoticed indefinitely. Updates are grouped, because a `labeled` trigger turns one bot PR into
  a burst of workflow runs (#293), and split dev-tooling from runtime because the two are not
  reviewed the same way. `pyproject.toml`'s claim that "dependabot moves both where a maintainer can
  review the move" is true now; it was half false while pip was unconfigured.
- THIRD-PARTY-NOTICES.md documents how the vendored htmx is updated and by whom. Dependabot cannot
  watch a minified `.js` file — it appears in no manifest — so the written procedure is the whole
  mechanism, and saying that plainly is better than implying something is watching it.

- #330: `requivo discover`'s interactive prompt printed a client request's LLM-authored question
  text (`Question.q`) straight into `input()`'s prompt, unescaped -- one statement after
  `render_turn` neutralized the identical field. A steered request could carry an embedded newline
  and a live escape sequence, forging a second line of Requivo's own output (a fake readiness
  verdict, a screen clear) at column 0. Both interpretation sites -- the prompt itself, and the
  `[slot: ...] Q: ... -> A: ...` line folded back into the next turn's answers -- now route the
  question text through the same `display_text` neutralizer `render_turn` already used.

- #331: the terminal-render sweep in `tests/test_render_untrusted_output.py` only ever ran the six
  renderers explicitly imported from `render/terminal.py`, so `cli.py`'s own interactive loop --
  where #330's forged line actually reached a terminal -- sat outside a guard whose stated job was
  exactly that discipline. The renderer sweep now checks itself against every `render_*` function
  `render/terminal.py` declares (a renderer that starts touching model text and is left off both the
  sweep and its documented exemption list now fails by name), and a new static scan walks every
  `.py` file under the terminal-facing surface tree (`render/`, `cli.py`, `deterministic/`, `web/`)
  for any read of a `Question`'s `q` or `why` field whose direct parent isn't
  `display_text`/`display_token` -- not gated on sitting inside a `print()`/`input()` call, so
  `msg = q.q` followed by a `print(f"{msg}")` several lines later is caught too -- so a future
  surface that renders a question is inside the guard on the day it is written, not the day someone
  remembers to add it to a list.

## [1.2.0] - 2026-08-23

### Highlights

Mostly internal: a provider split, three CI legs, and a narrative-reference convention this
CHANGELOG's own entries follow. The changes below are the ones with a visible effect.

- `requivo discover` on a session that already has a model now refuses before spending any API calls — it used to run up to nine paid turns first and then refuse.
- Fixed a rare data-loss race: a concurrent write and `session import --force` could corrupt each other's session. The per-session write lock now lives outside the session directory it guards.
- `pip install requivo[anthropic]` could resolve a pydantic version this project does not actually work on; the declared floor is corrected.
- `session import` gives the same, clearer error on every platform when something occupies the slug you are importing into, instead of a Windows-only "could not move" message that pointed at the wrong cause.
- The "Decision brief" naming is now consistent everywhere: the CLI, the terminal banner, and `requivo answer` all said "solution assessment" in places the rest of the product had already renamed.

### Added

- A `Types (pyright)` CI leg, scoped to `core/` and `services/` (#78). Ruff and pytest were the whole
  static story, on a codebase whose architectural safety is expressed in types rather than in
  assertions: Pydantic contracts at every boundary with `StrictModel` and `PersistedEngineOutput`
  deliberately disagreeing, `Protocol` seams that nothing structural checks, and a store where
  `Optional` means *could not read* rather than *empty*. Ruff looks for none of that, and a test
  finds it only where a test happens to run the path.
- **Blocking rather than advisory.** The plan was advisory first and promote once the scoped layers
  were clean; they are clean now, and a permanently-advisory leg is one people learn to scroll past.
  Widening to `providers/` and `web/` waits until these two have *stayed* clean.
- `pythonPlatform = "All"` rather than the host's, which is the stricter setting and not a
  convenience: it type-checks the Windows-only locking path from the Linux leg, on a codebase bitten
  by Windows-only behaviour three times. `pythonVersion = "3.9"` is the floor `requires-python`
  declares, for the same reason the Dependency floor leg exists.
- The configuration lives in `[tool.pyright]` in `pyproject.toml`, not in `pyrightconfig.json`,
  because JSON has no comments and every key here has a reason worth reading. The CI leg builds the
  same `.venv` a maintainer has locally and runs the same command, so the check is not one that only
  its own leg can run.

- A `Dependency floor` CI leg that installs Requivo at the oldest release every runtime dependency
  declares, and runs the suite against it (#91). Until now `pyproject.toml`'s lower bounds were a
  promise to whoever runs `pip install requivo` that nothing checked: all fourteen legs ran
  `pip install -e ".[dev]"` and got the newest satisfying release. It found a real defect on its
  first run, above.
- The floor is installed with `uv pip install --resolution lowest-direct`, not with a generated
  `name==floor` constraints file. The constraints form was tried first and is wrong twice over:
  `jinja2==3.1` names no release that exists (the oldest is 3.1.0), and pydantic's oldest
  *installable* release is not the one its bound names. A floor is the oldest release a user can
  actually get, not the string in the manifest.
- `scripts/dependency_floor.py` owns the two halves a resolver cannot supply: which requirements the
  runtime promise covers — `dependencies` plus the `anthropic` and `web` extras, never the dev
  toolchain — and whether the environment that came out is the one that was asked for. It refuses a
  requirement with no lower bound rather than skipping it, because a requirement that drops out
  silently leaves the leg green over the newest release while reporting that it tested the floor.

- The golden harness can capture the **interactive** discovery shape, not only a single-pass one
  (#137). A request in `fixtures/golden/requests.md` that carries `answer.<slot>:` lines now drives
  `DiscoveryService.draft_turn` — the loop behind `requivo discover` — for up to `GOLDEN_TURNS` turns
  per run, answering the engine's questions off those lines. Each line is one layer: the next thing
  that client has to say when the engine comes back to that slot.
- It exists because a single-pass capture cannot see the thing #77 changed. From turn 3 the
  interactive loop is grounded on the carried model alone, where the loop it replaced re-sent the
  whole transcript; turns 1 and 2 are byte-identical between the two, so a two-turn capture measures
  nothing. `training-budget` is the first such request, on a new problem form.
- A new lens reads what the deep turns did, in three measures: questions **re-asked** after the client
  had already answered them, early confirmations the model **lost** by the end, and completeness that
  **regressed** across a deep turn. Findings are reported per run and as the unanimous set, on the
  same rule as a slot move.
- The lens carries its own third state throughout. A single-pass baseline reports *not measured*
  rather than an empty finding set, a run that converged before five turns is flagged as shallow
  rather than counted as clean, and a request that stops being interactive is reported as a lens that
  went away rather than one that went quiet — because each of those would otherwise read exactly like
  a clean result.
- What the first capture measured: the carried model does keep the client's evidence. Every slot the
  client confirmed in turns 1 and 2 was still confirmed in the final model, in all three runs. What it
  does not keep is the engine's own asking history beyond one turn, and the observable cost is
  verbatim question repetition across three consecutive turns.

- The plugin README's `requivo` verbs are checked now (#138). `plugins/claude-code/README.md` names
  `requivo estimate`, `requivo stories` and `requivo session list`, which no skill invokes, and
  nothing verified any of them. That page is the landing page a marketplace sends an uncloned reader
  to, so it is the one page whose verbs a stranger types by hand — and it was the only page whose
  verbs nothing checked. A verb renamed or dropped left the README naming it, every test green, and
  the person who found out was a new user following the page.
- It is deliberately **not** added to `scripts/plugin_cli_drift.py`'s walked set, and the reason #96
  gave for leaving it out was a real one rather than an excuse: that walk feeds whole files to a regex
  that cannot tell `requivo requires an API key` from a command. The README gets something narrower
  instead — `test_the_plugin_readme_names_only_verbs_this_checkout_has` reads only the page's code
  spans and fenced blocks, never its prose, so it cannot false-positive on a sentence because it never
  looks at one. Both halves are pinned: the reader ignores prose, and the guard fires on a bad verb
  and on a bad subcommand.
- The decision is written down at both places that previously stated the exclusion, so the silence
  there stops reading as coverage.

### Changed

- `providers/anthropic.py` is a package (#74). One 626-line module whose own docstring announced seven
  responsibilities became five, cut along them: `client.py` (the SDK handle, the optional-import guard,
  the model id), `pricing.py` (the dated rate tables), `completion.py` (`_complete`, the retry loop, the
  JSON extraction, the truncation check), `generators.py` (the discovery turn, the seven generators, the
  registry) and `provider.py` (`AnthropicProvider`). The point was never the line count: an accurate
  inventory of seven things is the signal to split.
- `_GENERATORS` and `_OP_PROMPTS` stay one table each, in `generators.py`. They are the registry every
  surface reaches through, and a registry split across two modules is a registry with two answers.
- Nothing about the prompt cache moved. `reuse_system` and its per-call-site declarations are the same
  bytes they were after #9 and #58, and `providers/base.py` — the seam a second provider would
  implement — is untouched.
- `from requivo.providers.anthropic import <public name>` still works: the package `__init__` re-exports
  the public surface. The underscore names are deliberately not re-exported, so a test that drives
  `_complete` or `_GENERATORS` now names the module it lives in.
- **The one constraint the split could have broken quietly, pinned rather than commented.** `_complete`
  records the spend *before* it surfaces a clean failure — a failed call is still billed for whatever it
  consumed — and that was two adjacent lines in one file. It is now a call across a module boundary, so
  `test_a_failed_call_is_still_recorded_on_every_exit` drives all three failure exits (transport,
  truncation, retry give-up) and asserts the tokens and attempt count on each, with the success path as
  its positive control. Shown red by deleting the recording from `_stop()` first.
- Compatibility: compatible - `requivo.providers` is explicitly not a stable API
  (`docs/compatibility.md`), and the package `__init__` re-exports every public name the old module
  exposed, so no import in or outside this repository has to change for the split alone. Nothing on
  the CLI, the `--json` payloads, the session format or the web routes moves.

- **A rule for where a bug narrative lives, and a guard that keeps it honest** (#75). This codebase
  explains itself at length — the original bug, the rejected alternatives, the issue number — because
  the person about to simplify a subtlety away is in the editor, not in a docs folder. An external
  review proposed moving all of it to decision records; the diagnosis was right and the remedy was
  not, because a pointer nobody follows is worse than the paragraph it replaced.
- The rule instead: **a comment paragraph recounting a past bug must be backed by a test that goes
  red when the guard is removed.** If it is, the paragraph belongs in that test and the call site
  keeps one line naming it. If it is not, it is a missing test or genuine archaeology. It is a rule
  rather than a preference because it is mechanically decidable — *reduce the archaeology* has no
  stopping condition, *is there a test that goes red?* has one answer per paragraph.
- **The reference is a name, never a path** — a test function, a test module, or a decision record's
  slug. Paths here move: the package was renamed once, `deterministic.py` became a package, and a
  2147-line test module became seven files, all inside a fortnight.
- `tests/test_narrative_references.py` is new and found two live defects on its first run. The
  convention was already in use across `src/` and `CLAUDE.md` and nothing checked it: two references
  were split by a line wrap — `test_the_persisted_mirror_copies_every_` on one line and
  `constraint_it_restates` on the next — so the only way anyone uses one of these, selecting it and
  grepping, silently failed while the test it named really existed. Both are reflowed.
- `docs/decisions/` exists now, for what no test can reach: a fact about something outside the
  repository, a rejected alternative, or a cost tradeoff with a threshold. The first record is the
  branch-protection story that was 33 lines inside `ci.yml` — including the destructive `PATCH` verb
  the API docs lead you to, which would have cut `main`'s required checks from 13 to 4 and answered
  200 while doing it.

- The CLI, the deterministic verbs and the Web now reach session storage through `SessionRepository`
  wherever a backing-neutral equivalent exists (#76). "Both storage and reasoning are injected, so
  the orchestration is backing-agnostic" was true of `services/` and false of everything above it:
  27 call sites reached `core.persistence` directly, so on the day a second backing exists the
  services would hold and the surfaces would break.
- Twelve of those became repository calls. The fifteen that remain are every call for which no
  backing-neutral form is possible, because the subject *is* a path — `canonical_dir` answering
  "where did this session land?", `artifact_path` validating a filename that came off disk,
  `migrate_legacy` converting one filesystem layout into another, `validate_slug` refusing a name
  before any session exists to ask a repository about. A CLI that talks about files is entitled to
  know about files; the target was never zero direct calls, only zero unjustified ones.
- `tests/test_boundaries.py` guards it now, which is what the previous release said it did not.
  It needed a different extractor rather than a second table: every surface writes
  `from requivo.core import persistence as store`, so an import-name guard would see one entry for a
  file making eighteen calls and let a single reviewed line stand in for all of them. The allowlist
  is keyed by **(file, function)** and asserted in both directions, so a new call goes red under the
  name of the file that made it, and an entry whose call site is gone goes red as unchecked prose.
- One user-visible consequence, and it is a removal of a leak rather than a change: `cli.py` was
  calling the private `core.persistence._slug` to turn a filename into a slug hint. That policy is
  `SessionService.slug_hint` now, which `create_session` also uses, so there is one definition of
  how text becomes a session name.

- `CLAUDE.md` no longer states a test count (#134). It said 324 while the suite collected 687, and
  correcting the number buys one release: every lane that adds a test invalidates it again and
  nothing goes red when it does.
- The criterion is written down beside the narrative rule, because it is the same one: a count in
  prose has to answer *what does a reader do with this?*, and if the answer is nothing it comes out
  rather than getting a guard. The exact size of the suite is not a fact anyone acts on; *no API
  calls, no network, no build step* is. A sweep of `README.md` and `docs/` found no other instance —
  every remaining three-digit number there is an HTTP status or a port.

- `requivo web` without the `[web]` extra keeps reporting error code `provider_unavailable`, and the
  reason is now written at the call site instead of being a question (#135). The type reads oddly —
  `EngineError` is the provider *transport* error — but the code travels in the `--json` envelope,
  `docs/compatibility.md` makes moving a condition from one code to another a breaking change, and
  from 1.0.0 that costs a major version. Tidying it would have been a silent break of a published
  payload.
- It is also what this product already says when an optional install is absent: `new_client()` raises
  the same code for a missing `[anthropic]` extra. A test now pins it, so the decision goes red if it
  is reversed by accident rather than on purpose.

- `tests/web/test_web.py` is split into four files along the five subjects its own docstring named
  (#142). 1111 lines and 62 tests in one file, the largest #72 and #73 did not touch.
- `tests/web/test_web_security.py` is the one that earns the split rather than being sized into it:
  the slug and traversal refusals, the cross-site layers, the API key that may not reach the browser
  and the escaping. Among sixty tests about routing and templates, a security assertion that stops
  being collected looks exactly like one that passes; in a file somebody opens on purpose, it looks
  like a shorter list.
- The other three are `test_web_routing.py` (14: the routes, plus the error-code to HTTP-status
  contract they answer with), `test_web_discovery.py` (21: create, answer, generate, and the busy
  rule, whose subject is a second paid generation rather than a template) and
  `test_web_input_bounds.py` (7: invariant 3 at the web edge, where a server-side refusal and the
  template scan proving it is reachable from a browser are two halves of one story).
- Nothing about the product changed and no test body changed. Every top-level statement moved as an
  exact line range, verified byte-identical in source and AST; the collected set is the same 70 ids
  before and after. `HIGH_EXPLICIT`, `HIGH_INFERRED` and `_make_session` moved to
  `tests/web/conftest.py`, beside the fixtures the four files already share.
- Four narrative references that named the split file by path — in `web/app.py`,
  `services/artifacts.py`, `tests/test_artifact_provenance.py` and `tests/web/busy_harness.js` — now
  name the test, which is what `CLAUDE.md` asks for and what survives a file being split. The one in
  `busy_harness.js` was also truncated mid-identifier, so it had named nothing greppable since it was
  written.

- Three names in `core/persistence.py` that other modules were already importing now carry public
  names: `_is_contained` is `is_contained`, `_hash` is `content_hash`, and `_slug` is `derive_slug`.
  A private name consumed across a module boundary is a contract wearing a disguise — the underscore
  says *do not depend on this* while `core/integrity.py` and `services/sessions.py` depend on it.
- This is the one gain taken from the proposal in #143, which argued for splitting the file and was
  refused (the measurement it asked for came back negative: the parts do not change independently,
  and 73% of recent corrections touch two or more of them). The refusal named this as the only
  value the defensible seam would have bought, and it is buyable without moving any code.
- No re-export and no private alias is left behind. An alias would be worse than the underscore:
  a test patching the alias while the code reads its own module global is a guard that stops firing
  and stays green, which is the trap `_blind_to_dangling_links` in `tests/test_integrity.py` exists
  to record.
- Compatibility: compatible - `docs/compatibility.md` promises stability for the session format and
  the `--json` payloads, not for Python import paths, and all three names were private.

- `requivo.deterministic` no longer claims that `docs/compatibility.md` publishes `EXIT_DEGRADED`
  under that name (#145). The page publishes the **value 4**; the symbol is internal, and the same
  page lists `requivo.deterministic` among the Python internals that are explicitly not stable.
- Publishing the name was refused rather than merely left unchosen, and the refusal is written down:
  a promised Python symbol costs a major version to move and buys a consumer nothing the documented
  exit code does not, since a script gating on a degraded listing reads the process's status and not
  this package's namespace. The old comment invited both mistakes at once — importing it from
  outside, and reading a rename as a breaking change.
- A test pins the corrected claim, so it cannot become a second unguarded one: promote the module to
  stable, or renumber the code without the page, and it goes red.

- The "Decision brief" rename reaches the CLI and the terminal (#166). The same artifact was called
  two things depending on where you stood: Requivo Web, the Claude Code plugin, the README and the
  generated Markdown said *decision brief*, while `requivo discover` narrated *Generating the
  solution assessment…*, `requivo demo` announced *THE SOLUTION ASSESSMENT*, `requivo brief` wrote
  *Wrote solution assessment →* and the terminal banner read *SOLUTION ASSESSMENT*. All four now say
  decision brief, as does `requivo answer` — which said *the assessment's reasoning* and *run
  `requivo brief <slug>` for the assessment* — plus the two browsable examples, the README's one
  remaining use of the old noun, and the discovery-feedback issue template.
- Captions only — no machine contract moved. The artifact type is still `brief`, the verb is still
  `requivo brief`, the contract is still `Brief`, and the file on disk is still
  `solution-assessment.md`, which `docs/compatibility.md` pins as part of the session format.
- The two model-facing assets are deliberately left alone: `assets/prompts/brief.md` and
  `assets/framework/elicitation.md` are read by the engine, not by a person, so changing them
  changes what the model is asked to produce and belongs in a measured change of its own. #166 stays
  open for that half.

- The usage ledger is provider-neutral and lives in `requivo.usage` (#167). `render/terminal.py` — the
  purest view layer in the tree — imported `PRICING_AS_OF` and `UsageLedger` from
  `providers.anthropic` to print a cost line. Nothing it needed was Anthropic's: calls, tokens, cache
  tiers and latency are concepts any provider has, and `UsageLedger`'s own docstring already said it was
  presentation-free.
- `EngineError` moved to `providers/errors.py`. It is a transport failure the `ReasoningProvider` seam
  raises, not a vendor's, and `cli.py` and `web/app.py` were importing it from the vendor module — a
  second provider would have had to import it from a competitor's. Its code, `provider_unavailable`, is
  published in the `--json` envelope and is unchanged: this is a relocation, not a rename.
  `providers/errors.py` pulls in no SDK, so the web app no longer loads the whole provider to classify
  one exception.
- **A price is stamped onto a call, not looked up when a total is printed.** Moving the ledger only
  halved the leak while `cost_usd()` still reached into Anthropic's table, so a `CallRecord` now carries
  the `(input, output)` rate it was billed at and the date of the table it came from, stamped by
  `price_call` as the provider files the call. The ledger holds arithmetic and no table; the renderer
  asks the ledger for the rate date and never the vendor. Two things follow: a second provider brings
  its own rates with no registry, and an estimate spanning a price change is right on both sides of it
  rather than re-pricing yesterday's calls at today's rate.
- Three states rather than two, in the code as well as the report: priced, unpriced (`cost_usd()`
  returns `None` and the line says *no price on file*), and priced-with-no-table-date, which prints the
  cost without a *rates as of* clause instead of borrowing a date. An undated estimate that reads as a
  dated one is the more expensive of the two mistakes.
- **The guard asymmetry is closed, and it is the part that was worth fixing regardless.**
  `tests/test_boundaries.py` watched `core/` from one end and `cli.py` from the other; `render/` was in
  neither scan set, so the layer with the weakest claim to a provider import was the one with no guard,
  and `terminal.py` sat there through the whole hardening effort that produced the allowlist. `render/`
  is in the scan set now, the allowlist is keyed by (file, name) with a reason per entry and asserted in
  both directions, and a companion test names what was scanned so an empty walk cannot read as an
  all-clear. Shown red against `main` first, naming both leaked imports.
- `track_usage` left the CLI allowlist as a consequence rather than a concession — the stale half of
  that assertion is what made deleting the entry mandatory instead of optional.
- Compatibility: compatible - the three moved names are Python internals under
  `requivo.providers`/`requivo.usage`, explicitly not a stable API (`docs/compatibility.md`, which
  now names both). `provider_unavailable` is unchanged in the `--json` envelope and the web banner.
  `UsageLedger.cost_usd()` lost its `on=` argument, which no surface passed — the calendar lives in
  `price_per_mtok`, which keeps it.

### Fixed

- **`pydantic>=2.0` was false by eleven minor versions.** The real floor is 2.11, and the manifest
  now says so (#91). On pydantic 2.0.x Requivo does not import at all — `SerializeAsAny` inside an
  `Optional[...]` is unhashable under Python 3.9's `typing`, so every model in `contracts.py` fails
  to build — and from 2.0.3 up to 2.10 the two guards that pin the persisted contract's permissive
  mirror fail. Anyone whose resolver landed on an early 2.x got a broken install and no leg in this
  repository would have gone red.
- The v0.11.0 audit had cleared that same bound by confirming `SerializeAsAny` is exported by
  pydantic 2.0.0. That was correct and it was the wrong question: a symbol existing is not the symbol
  working, and only installing the thing tells them apart.

- `session import --force` renamed the session directory out from under the lock every writer holds,
  so two writers could hold one slug at once (#113). The lock was an fd on `<slug>/.lock` — a claim
  on an **inode** — while every writer under it resolved the session directory and wrote by
  **pathname**. The swap made those two stop describing the same thing.
- Three consequences, all reproduced. A writer inside `save_revision` went on writing into the
  *freshly imported* directory, stamping the replaced session's `session_id` and revision log onto
  the import. A third process opening the lock found a different inode and acquired at once, while
  the first writer still held the old one — which `_swap_in` then unlinked, leaving that lock
  permanently unobservable. And between the swap's two renames `<root>/<slug>` did not exist, so a
  concurrent `save_revision`'s `mkdir` recreated it: the move **and** its rollback then both failed
  on a non-empty destination, the rollback raising a bare `OSError`, and the user's session was
  stranded at a dot-prefixed name every listing skips while the slug was held by a stub, and
  `session list` reported no sessions at all. The step-aside exists to be reversible, and the same
  race defeated it.
- The fix moves the one lock every writer already takes, from `<slug>/.lock` to
  `.requivo/locks/<slug>.lock`. `_swap_in` then holds it like any other compound write (invariant 9),
  because the open handle is no longer inside the directory being renamed — which is what Windows
  refuses and what killed the obvious fix when it was tried. The step-aside, the rollback and the
  atomicity are all unchanged.
- The existence check `session_lock` makes moved *under* the lock, where it is authoritative. It used
  to be closed by accident — the lock file lived inside the session, so `os.open` raised
  `FileNotFoundError` once the directory was gone — and opening a file in `.requivo/locks/`
  establishes nothing about the session. A cheap non-authoritative check stays before the open so a
  slug with no session still refuses without creating a lock file.
- **Compatibility: an older Requivo running in the same workspace at the same time does not
  serialise against a newer one.** It locks `<slug>/.lock` and this one locks
  `.requivo/locks/<slug>.lock`, and two different files do not contend. Bounded — it needs two
  Requivo versions writing one workspace at the same instant — and real. Finish or close the older
  process before running this one against the same workspace.
- Not a format change: `.lock` was already excluded from archives as this machine's coordination, so
  no `format_version` bump and no migration. A `.lock` left inside an existing session by an earlier
  Requivo is inert — nothing opens it, `session export` skips it, `session verify` ignores it — and
  is safe to delete. `docs/compatibility.md` carries both this and the cross-version limitation
  above, because that page is where every other "two versions, one workspace" fact lives and a
  changelog fragment is folded away at the next release.
- Deleting a session by hand now leaves its lock file behind, where the lock used to die with the
  directory. There is no `session delete` verb, so this is the ordinary way a session goes: what
  stays is one empty file under `.requivo/locks/` that claims no slug and is read by nothing.
- `session_unreadable` gains a second condition — the write lock could not be opened — and it is
  documented as such in `docs/compatibility.md` and on the exception. It deliberately does not answer
  `session_not_found`, which is what the old code did only because the lock lived inside the session.
- `requivo doctor` reports the lock root under `workspace.locks` and on its own line. A permission
  fault there fails every verb at once with `could not open the write lock`, and nothing else in the
  workspace names that directory. Additive: `workspace.sessions` is unchanged.
- `docs/session-format.md` and `docs/cli.md` describe the new location and the legacy residue.

- `session import` gave two different answers on two platforms for one stray directory at the target
  slug, and named the wrong cause on both (#114). The free-slug arm claims the slug by renaming the
  extracted directory onto it, and `os.replace` diverges there: POSIX replaces an **empty**
  destination directory silently, while Windows' `MoveFileExW` refuses *any* existing destination
  directory. So a user's `mkdir` at the slug imported on macOS and Linux and failed on Windows.
- The Windows failure arrived as `import_move_failed` — *could not move the imported session into
  place* — which is a fact about a move where the fact is about the destination, and sent the reader
  at their filesystem looking for a fault that is not there.
- Both platforms now refuse it by name, before the rename is attempted: a new code
  `import_destination_occupied` (409, `details` `{slug, path}`), documented in
  `docs/compatibility.md` and `docs/cli.md`. The guard is called on both sides of the rename, because
  neither side alone converges the platforms — a stray already on disk never reaches the `except` on
  POSIX, and one that lands mid-window never reaches the pre-check.
- The rename is still the claim on a free slug (invariant 11). The new guard only ever *refuses*, so
  it cannot authorise an import the rename would have lost, and a probe that cannot look — `exists()`
  re-raises EACCES — says nothing and lets the rename decide rather than reporting the slug free.
- Compatibility: an import that previously succeeded on POSIX against an **empty** stray directory
  now refuses. Deliberate: the alternative converges the other way, on an import that deletes a
  directory the store never created and cannot interpret. No in-code producer leaves an empty
  directory there, so reaching this state takes a `mkdir` or a half-cleaned checkout.
- `requivo doctor` already reported such a directory and marked it `[name taken]`, and its hint closed
  with *which is the only symptom any of this has* — which this change makes false. The hint now names
  both consequences: the hash-suffixed name a new session gets, and the refusal `session import` gives.
  A diagnostic left describing the world before the fix is a defect neither diff review can see, so it
  ships in the same commit.
- No test exercised this path, which is why the divergence was invisible to a suite with a Windows
  leg. Four now do, and the empty-destination row is the one that was silently green.

- `requivo discover` on a session that already carries a model is now refused **before the first API
  call** on the interactive path, not after up to nine of them (#133). `--once` already refused for
  free; the interactive branch met the same gate only at the end, so a user who re-discovered a
  refined session paid for eight turns plus the assessment and got `revision_conflict` with nothing
  to show for it.
- The refusal itself was always correct — its position was not. Invariant 13 says the revision-zero
  gate is taken before the paid call by every entry point, and that was true of the path documenting
  it and false of the path a person uses at a terminal. `DiscoveryService.claim_session` is public
  now, so a surface that owns its own loop takes the gate itself instead of inheriting it at the
  write.
- The cost, stated because it is visible: stopping an interactive discovery early (`q`, an empty
  answer, Ctrl-C) now leaves the session on disk at revision 0 with your request captured, and the
  command prints where. That is the same state `requivo session init` produces — nothing was
  reasoned and nothing was written to the model.

- `requivo estimate` reads its stories and its estimate from **one** session snapshot (#135). It took
  one per provider call, so a write landing between them estimated one model's stories against a
  different model — invariant 12's own "two reads, two instants", in a single terminal output that
  shows both halves and names no revision.
- Nothing is written by that verb, so unlike the case the invariant was written about no provenance
  could become a lie; what drifted was the answer. `DiscoveryService.reason_from` takes a snapshot the
  caller already holds, which also keeps the stories on screen while the estimate is still being
  reasoned — a combined operation would have delayed both until the second call returned.

- `scripts/plugin_cli_drift.py` no longer grades a plugin tree it could only partly read (#139).
  `invocation_sources()` walked with `Path.glob` and `Path.is_file()`, and **both swallow `OSError`**:
  glob skips a subdirectory it cannot descend into and raises nothing, `is_file()` returns `False` on
  EACCES. So a partially readable plugin was graded as a verdict over whatever subset the walk
  happened to manage. The v1.1.0 release audit staged three files, two were walked, and the caller was
  told `resolved`, exit 0, about a plugin containing a verb that does not exist.
- The walk now has three outcomes rather than two — walked, absent, and could-not-look — which is
  invariant 15's third paragraph one directory over: a partition whose predicate can raise has three
  outcomes whether or not its return type says so. `invocation_sources()` returns a `Sources` pair, so
  a caller cannot take the paths without being handed what could not be read alongside them, and the
  run prints `unreadable    : N` even when N is zero: a count that appears only when it is interesting
  cannot be told from a check that stopped running.
- Could-not-look for the *right* reason, which review caught as a separate question. `compare()` is
  never handed the unreadable set, so when the only invocation-bearing file is the one that could not
  be opened it reported *no `requivo` invocations were found in the plugin's files* — blaming the
  plugin for an emptiness the process created — and returned before naming the path at all. The
  unreadable set is now stated before any verdict detail, on every path out of the run. Review also
  found `NotADirectoryError` classified two ways twelve lines apart; both arms now sort it to absent.
- A firm negative still outranks a partial one. Drift found in the part it *could* read keeps the
  drift exit and reports the partial walk beside it, which is the rule invariant 15 already states for
  `session verify`. Only a `resolved` is downgraded.
- **A plugin directory name can no longer break a line and start a workflow command at column 0 of
  the CI log** — one of the two forms the runner parses, and this bullet claimed the whole class
  until #176 corrected it in the same unreleased batch; the legacy `##[name]` form, which is an
  unanchored substring search and needs no newline at all, was still live and is fixed there. Found
  by the audit of this diff, and it has two halves. A filename may legally contain a newline on
  POSIX, and GitHub Actions parses `::command::` at the start of *any* stdout line, not only lines
  routed through `_annotate` — which squashes its message and was never the hole. The bare `print`
  beside it was. Sweeping the file for the class found the second instance in `_label`, which returns
  a skill's *directory name* into every finding the script prints and predates this work entirely.
  Both are squashed at the point the untrusted value enters. This is invariant 14's #40 one script
  over, where a stored context card name spent a release able to forge a line at column 0 of
  `doctor`'s output.
- No user-visible change: `scripts/` is not in the wheel and the CI leg is `continue-on-error`, so
  this was a wrong annotation rather than a wrong build.

- `test_every_generator_drives_a_real_call_without_a_cache_write` now covers every generator the
  provider actually registers (#146). It was parametrized over `_GENERATOR_REPLIES`, a hand-maintained
  dict of six, while `_GENERATORS` held seven: `estimate` joined the registry in 1.1.0 and nothing
  related the two sets, so an eighth generator would have shipped with no cache assertion and nothing
  would have gone red, under a test whose name reads *every generator*. A guard that did not run and a
  guard that found nothing rendered identically.
- `set(_GENERATOR_REPLIES) == set(_GENERATORS)` is asserted once, in both directions: a registry entry
  with no reply is an uncovered generator, and a reply for a name the registry does not hold is a case
  that stopped exercising anything. That turns "somebody remembered" into "the build says so". Shown
  red against the seven-entry registry first, then fixed.
- `estimate` is driven through the parametrization rather than by a test of its own. It is the one
  entry that is not the plain model to contract shape, so the extra `stories` it reads is carried in a
  small per-generator table and passed as a keyword, which is how `AnthropicProvider.generate`
  dispatches it. Its separate test asserted the same two facts and is gone.
- The comment justifying that separate test was false in both clauses and is replaced by an accurate
  one. It said `estimate` was not in `_GENERATORS` and that the CLI called it directly past the
  provider seam; `estimate` has been in the registry since 1.1.0, and `cli.py` has gone through
  `disco.reason_from(snap, "estimate", stories=stories)` since #77 and #135. A reader auditing that
  file was told the opposite of what the code does.
- Tests only. No behaviour change, and no write path was opened by the registry entry this covers:
  `DiscoveryService.generate(slug, "estimate")` still refuses it by name, and the web's `GENERATABLE`
  is built from the service's writers rather than the provider's registry.

- The advisory drift step in `.github/workflows/plugin-validate.yml` no longer echoes a third-party
  binary's output into a CI log that parses it (#147). It captured the combined stdout and stderr of
  `claude plugin validate --strict` and printed it raw, and interpolated `claude --version` into six
  lines beginning at column 0, five of them GitHub Actions workflow commands. `scripts/plugin_cli_drift.py`
  had been hardened against this exact class inside the same feature (#96) and the shell beside it had
  not: one half of a change knew about the hazard and the other half did not.
- The two fixes the issue proposed were both measured against the runner's own parser and neither
  holds on its own. `ActionCommand.TryParseV2` trims leading whitespace **before** it tests for the
  `::` prefix, so indenting untrusted output contains nothing — #147's own measurement leaned on *the
  line is indented two characters*, where what actually saved it was the newline collapsing to a
  space. And `TryParse`, the legacy `##[name]data` form, is an unanchored substring search, so
  collapsing the output onto one line contains nothing either.
- So the captured output is printed **verbatim inside a `::stop-commands::` fence**, which is the
  mechanism GitHub documents for logging untrusted input and the only candidate that covers both
  parser forms at any column. It is also the only one that leaves the log as readable as it was,
  which matters for a step whose own annotation tells the reader to go and read that output. If the
  shell dies inside the fence, commands stay off for the rest of the step: an annotation is lost
  rather than forged.
- `claude --version` cannot be fenced, since it is interpolated into six lines emitted outside the
  fence, so it is sanitised at capture instead and needs both halves: the newline goes, because a
  `::` mid-line is data, and a `##[` is spaced apart, because that form needs no line start at all
  and one of the six sites is the plain `pinned=` echo rather than a command the parser consumes
  first.
- No user-visible change and no change to any build's verdict: the step is `continue-on-error`, ends
  `exit 0`, and the gate steps beside it take their answer from the process exit code rather than
  from the log. What this closes is a run's annotations being able to state something no tool
  concluded.
- The test that can go red here executes the step's shell out of the workflow file against a `claude`
  that forges, because the pytest suite runs no YAML. Two of the four cases are must-fire controls
  that strip one containment back out and assert the forgery reappears — including the indented `::`
  and the mid-line `##[error]`, the two shapes the refused fixes would have missed.
- Two things were reported for filing rather than fixed here, and the first has since landed in the
  same unreleased batch. It was that the same unanchored `##[` form was reachable in
  `scripts/plugin_cli_drift.py`, whose `_one_line` squashes whitespace only, so a skill directory
  named `brief##[error]...` forged an annotation with no newline at all — which also made the Windows
  leg's skip in `_forging_dir` unsound, since that platform refuses the newline and not this vector.
  It needed its own review, because it reopened a heavily-argued docstring, a test helper that only
  checked for a leading `::`, and a claim already written into `changelog.d/139.fixed.md`: that is
  #176. The other two have since landed in the same unreleased batch and are no longer open: this
  workflow had no `permissions:` block and neither did `ci.yml` or `secret-scan.yml`, which is
  `changelog.d/178.fixed.md`; and the two gate steps and the CLI install step ran `claude` unfenced,
  which is `changelog.d/177.fixed.md` — where the fence had to preserve the exit code that *is* the
  gate, and the measurement came back the other way from this one. `claude plugin validate` does
  echo an attacker-controlled manifest field name, in the unanchored `##[` form this issue never
  tested.

- `docs/compatibility.md` no longer undercounts what `requivo.deterministic` exports (#148). Its
  *explicitly not stable* entry said the module's "public job is the single `register(sub)` the CLI
  binds through". It is three names: `__init__.py` re-exports `EXIT_DEGRADED` and `read_user_text`
  beside `register`, `cli.py` imports two of the three, and three test modules import one of them from
  the package root. The page now names all three and says who reads each. (The issue counted two
  importing test modules; `tests/test_unexaminable_entries.py` is the third, found by the audit of
  this diff. The other `requivo.deterministic` imports in the suite reach submodules, which is a
  different thing and not a claim about this surface.)
- This is the one page whose whole job is precision about surface, and the sentence sat in a paragraph
  added to close a gap in that same page.
- The conclusion is unchanged and this is a wording fix, not a version one. `requivo.deterministic`
  was never a promised surface at 1.0.1 and is still explicitly not stable; nothing here reopens the
  1.1.0 number.
- The same undercount was restated in the docstring of
  `test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name`, which quotes the page's
  argument, and is corrected there too. Fixing the page and leaving its restatement standing would
  have been half a fix.

- `requivo brief|prd|criteria|epic|release|stories|estimate` on a session that has never been
  analysed now refuses with a structured error naming the remedy, instead of exiting on a raw
  `AttributeError` (#152). Found by the new type-checking leg on its first run.
- `SessionSnapshot.model` is `None` before the first model — the field says so — and
  `DiscoveryService.generate`/`reason` unpacked it and handed it to the provider unchecked. Nothing
  was lost and nothing was spent: every generator builds its user message as `out.model_dump_json(…)`,
  so it failed while assembling the prompt, before the client was touched.
- The rule existed in exactly one place, and it was a Web route: `if meta.current_revision == 0`
  renders an "offer to run discovery" page. One surface out of three. It is in `DiscoveryService`
  now, as the mirror of `_require_revision_zero` — whose own docstring already says a business rule
  enforced by a hidden button is not enforced. Hiding the button is good on top of an enforced rule
  and is not one.

- `scripts/golden_diff.py` reported "no change above the noise floor" and stopped, before the
  assessment lens ran (#162). A `prompts/brief.md` edit that moved the complexity verdict or the
  challenges without moving a single slot therefore reported as no change — and `--brief` doubles
  that request's calls, so whoever paid for the assessment capture was exactly who was told there
  was nothing to see. A lens that never ran, reported as a lens that ran and found nothing.
- Every lens now runs on every request, and the run's verdict is the **union** of the ones that ran:
  the strongest signal any of them found. The slot section's flat line decides only whether that
  section is a dash. Measured on the committed `leave-approval` baseline with two challenge themes
  dropped and no slot moved: before, one line and a verdict of `flat`; after, both lost themes named
  and a verdict of `moved`.
- A lens that could not look now says so on its own line rather than being folded into a silent
  pass, and moves no verdict. `assessment · not captured — this lens did not look` is a different
  sentence from `assessment · verdict and challenges unchanged`; a baseline that *had* an assessment
  where the new capture has none is louder — `assessment ! … nothing to compare`, because committing
  it would drop a lens — and still grades as nothing measured. A first capture prints its assessment
  consensus, since there is nothing to compare against and the readout is the finding.
- That last state was graded as a strong signal in the first cut of this change, by analogy with the
  interactive lens, and review caught it. The analogy fails: interactivity is declared in
  `requests.md` and reproduced on every capture, while `--brief` is a per-invocation flag no capture
  remembers, and all six single-pass baselines in `fixtures/golden/` carry one. Re-capturing the set
  without `--brief` is the documented workflow for an `engine.md` or context-card change, so the
  grading would have reported six strong signals on a run where nothing moved — the noise this file
  exists to suppress, manufactured by the lens meant to catch it.
- Not tallied in the summary line the way `not re-captured` is: a lens's own state belongs on the
  per-request line, and a counter that fires on nearly every run is one nobody reads.

- `scripts/golden_run.py` and `scripts/golden_diff.py` printed non-ASCII across roughly sixteen
  lines and never reached `streams.py`'s reconfiguration, which ran only from `cli.app()` (#164). On
  a Windows cp1252 console either raised `UnicodeEncodeError` at the print — after the work it was
  reporting had already landed. `golden_run.py` is the script that spends real API calls, so a
  capture that completed fifteen of them and wrote its runs file could still die rendering the
  summary, leaving a traceback standing where a result should be.
- Fixed as the class rather than the instances: both scripts now call `golden_lib.configure_output()`
  first thing in `main()`, which routes them through `streams.py` with `errors="backslashreplace"` —
  never `replace`, because a reader cannot tell a substituted character from one that was never
  there. Sweeping the glyphs out of the two files would have fixed today's strings and left the next
  print to reopen it.
- No `EXIT_RENDER_FAILED` arm for these two, deliberately. `golden_diff.py` neither calls nor writes
  anything, so a guard there could never fire; `golden_run.py` writes each request's baseline before
  any summary print and already catches per-request failure, so its work is durable by the time a
  render could die. What remains is `configure_stream`'s own third state — a stream it could not
  reach is the one that can still crash — and that is reported on stderr as a line somebody can
  read, rather than as an exit code nothing under `scripts/` consumes.
- The other entries under `scripts/` were checked in the same pass. `requivo_cli.py` delegates to
  `cli.app()` and was already covered; `dependency_floor.py` and `plugin_cli_drift.py` print no
  non-ASCII literal and run only on `ubuntu-latest` in CI, so neither was changed.

- The terminal no longer invents a third readiness state (#165). Readiness is one boolean — the Core
  publishes exactly `{"ready": not blockers}` and `docs/requirements-model.md` says outright that
  Requivo does not invent graded "nearly ready" levels — but `render/terminal.py` branched on the
  *length* of the blocker list in two places and produced `Nearly ready` and `⚠ Nearly — N to
  confirm`. A session with two unresolved high-impact topics was told *Nearly ready* by `requivo
  status` and *Not ready to produce a reliable scope* by Requivo Web, off the same `model.json` at
  the same revision. Both splits are gone; the verdict is binary, on the same predicate as
  everywhere else.
- The blocker *count* is gone from the verdict too. It is what the deleted arm branched on, and the
  blocking topics are named on the same line, so it added nothing that was not already there and it
  was the raw material the invented state was made of.
- One boolean is no longer asked as three different questions. `READY FOR IMPLEMENTATION?` in the
  terminal and `Ready to estimate?` in the decision brief both branched on the same blocker list as
  *are we ready?* does everywhere else, which invited a reader to believe they were three
  thresholds. Both now use the wording Requivo Web already ships — the heading *Are we ready?* and
  the two badges *Ready* and *Not ready*. No new readiness dimension was introduced; if *ready to
  estimate* is ever a genuinely different threshold it gets modelled explicitly.
- Inside the decision brief the same boolean is stated as a consequence rather than as a second
  question: **Not ready** now says the brief is a draft and names the topics that can still move the
  solution. Asking *ready for a first decision brief* there would be circular, since the reader is
  holding one.
- The contract is pinned across surfaces for the first time, in `tests/test_readiness_contract.py`.
  It asserts as a property — the verdict a surface renders is a function of `bool(blockers)` and of
  nothing else — over the terminal status block, the per-turn view, the decision brief and the Web
  view model, at 0, 1, 2, 3 and 5 blockers. Nothing pinned it before, which is how the terminal
  contradicted two other surfaces and a documented rule for a whole release without a single leg
  going red.
- Not a compatibility break: terminal output layout is listed under *what is explicitly not stable*
  in `docs/compatibility.md`, and the `--json` payload's `readiness.ready` boolean is unchanged.

- `scripts/plugin_cli_drift.py` no longer lets a plugin directory name forge a GitHub Actions
  workflow command in the CI log (#176). The script runs as a `--github` step in
  `.github/workflows/plugin-validate.yml`, which triggers on `pull_request` with no `paths` filter,
  so a fork pull request controls that name — and a skills entry called
  `brief##[error]title=...`, **with no newline anywhere in it**, reached a bare `print` and produced
  an annotation stating something no tool concluded.
- The runner parses its log twice and this repository had been reasoning about one of the two
  parsers. `ActionCommand.TryParseV2` handles `::name::data` and calls `message.TrimStart()` before
  testing the prefix, so a line start is what matters and indenting contains nothing.
  `ActionCommand.TryParse` handles the legacy `##[name]data` form and is `message.IndexOf("##[")` —
  no anchor at all, needing neither a line start nor a newline. Collapsing whitespace defends the
  first form and does nothing whatever about the second.
- The remedy is sanitising at the point the value enters rather than fencing the region it is
  printed in, which is the opposite call from #147 one file over, for a stated reason: there the
  fenced text was a third-party binary's whole output and the step's own annotation tells a reader
  to go and read it, so readability was the deliverable. Here the untrusted text is a bounded set of
  named values — a directory name, a path in an error, a verb name — that already funnel through one
  chokepoint, and a mangled hostile filename costs nothing. A fence would also have had to be opened
  and closed around five `print` sites interleaved with the script's own annotations, and anything
  leaving the region without closing it turns command processing off for the rest of the step —
  including the `could not look` annotation this script emits when it crashes.
- `_log_safe` is that chokepoint. It collapses whitespace and then spaces both command keys apart,
  `##[` to `## [` and `::` to `: :`, the same spelling the workflow already uses on
  `claude --version`. Breaking `::` as well as `##[` is deliberate: killing newlines already meant a
  value could not *start* a line, but that was a guarantee about the layout of five format strings
  rather than about the value, and it would evaporate the day one of them printed a label first.
- A second instance of the same class, found by sweeping the file rather than by being reported:
  `parse_surface` reads the probe's stdout, and the tree half of that probe introspects
  `requivo.cli._build_parser()` in the **working tree**, which a fork pull request edits as freely as
  it names a directory. A verb or subcommand name carrying a newline put the remainder at column 0 —
  reaching both parsers rather than only the unanchored one. Those names are sanitised where they
  enter now, which costs nothing, since a name this changes could never have matched an
  `INVOCATION_RE` token anyway.
- The guard that should have caught all of it was itself checking the wrong thing.
  `_assert_no_forged_workflow_command` in `tests/test_plugin_cli_drift.py` tested
  `line.startswith("::")` and nothing else, so both end-to-end tests leaning on it ran, checked one
  of the two parsers, and reported a pass over a live vector. It models both now, and it has a
  must-fire control of its own, because a guard nobody can make fail cannot be told from one that
  always passes.
- The Windows skip in `_forging_dir` was unsound and its docstring said why in reverse: it claimed
  the forging vector *is* the newline, so a platform refusing the character refused the vector with
  it. NTFS refuses the newline and does not refuse `brief##[error]...`. The skip is narrower now and
  names only the half it really covers, and the half NTFS cannot refuse has an end-to-end test that
  runs on every platform.
- `changelog.d/139.fixed.md` and `changelog.d/147.fixed.md` are corrected in the same change. Both
  are unreleased, and the first claimed a plugin directory name can no longer forge a line at column
  0 of the CI log — a guarantee the code did not provide. Folding that into a tag would have shipped
  a false guarantee, which is worse than shipping the gap.
- Three more places said the same thing and are corrected too, all found by review of this diff
  rather than by the issue: `tests/test_workflow_untrusted_output.py`'s module docstring said this
  script "had already been hardened against exactly this class", which is the sentence #176
  disproves; and `scripts/golden_lib.py` and `tests/test_golden_lib.py` both cited `_one_line` as
  the validated pattern for a sink a CI runner parses, where it is now only half of one. A neighbour
  claiming a guarantee its neighbour does not provide is how this survived a review the first time.
- No user-visible change and no build's verdict moves: `scripts/` is not packaged, the leg is
  `continue-on-error`, and the gate steps beside it take their answer from the process exit code
  rather than from the log. What this closes is a run's annotations being able to state something no
  tool concluded.

- The two gate steps and the CLI install step in `.github/workflows/plugin-validate.yml` no longer
  run a third-party binary straight into a CI log that parses it (#177). #147 fenced the advisory
  drift step in the same file and left these three, and they are the ones that matter most: the two
  gates are required, branch-protected checks, so a forged annotation lands on a run a reader is
  more likely to believe **because** it is required.
- The vector was measured rather than reasoned, twice — once on the issue and again on this branch,
  against `claude` 2.1.241. `claude plugin validate --strict` echoes an unknown manifest field name
  **verbatim, twice**, in `> <name>: Unknown field '<name>'. Claude Code ignores it at load time.`,
  so a field called `benign##[error]title=X::y` puts `##[` at column 10 of a line the runner reads
  with an unanchored `IndexOf("##[")`. No newline is needed and no line start is needed. The same
  holds for the marketplace catalog on the second gate. A fork pull request edits both manifests.
- The `::` form stays contained for a field name — a newline inside one comes back collapsed to a
  space, which is what #147 measured and it is still true — but that measurement never covered the
  legacy form, which needs neither containment.
- `::stop-commands::` with an unguessable token, the same mechanism #147 used, and deliberately not
  the sanitise-at-capture route #176 took one file over: a gate's output is what a human reads when
  the gate is red, so spacing `##[` apart would trade a forging problem for an unreadable failure.
  Inside the fence the log is byte-identical to what the validator printed.
- **The part that made this its own change rather than a rider on #147 is the exit code.** `echo`
  clobbers `$?`, so a fence written as open / run / close silently reports success for a failed
  gate — trading a log-forging problem for a required check that cannot fail, which is strictly
  worse than the defect. Each step now captures the code with `|| code=$?` while it still exists and
  ends `exit "$code"`.
- That also takes the containment off the shell's flags. GitHub runs a `run:` block with
  `bash -e {0}` when the workflow names no `shell:` key, and this one names none — but nothing here
  rests on that claim about somebody else's runner, and the tests execute each block under `bash -e`
  and under a plain `bash` to show it.
- The guard is over the class rather than the three instances. `tests/test_workflow_untrusted_output.py`
  now enumerates every step in the workflow that starts `claude`, in both the `run: |` and the
  one-line `run:` form, and requires each to fence its output or capture it — so a fourth step added
  later goes red under its own name instead of being found the way these three were. A scanner that
  understood only block scalars would have reported this very defect as absent.
- The extractor that class guard rests on had a blind spot of its own, found by review of this diff
  rather than by anything going red. It matched the block scalar only as the exact string `run: |`,
  so a step written `run: |-` — the ordinary way to drop a trailing newline — fell through to the
  one-line branch, which took `|-` as the whole command and discarded the body. An unfenced step
  written that way reached neither the scan nor the step count, so the guard against *a fourth step
  added later* would have answered cleanly about a step it never saw. Every block-scalar header is
  read now, a nested sequence inside a step no longer clears the step's name, and both are asserted
  against YAML the real workflow does not contain — a guard exercised only on a file that happens
  not to use the form proves nothing about the form.
- Three must-fire controls, because every other assertion here is must-not-fire and passes against a
  harness that never ran: strip the fence and the three forged shapes reappear; strip `exit "$code"`
  and a gate that failed reports success; and the failing-CLI case asserts exit **7** specifically
  rather than merely non-zero, since a fence that lost the code and then died for another reason
  would satisfy `!= 0` and prove nothing.
- No user-visible change and no build's verdict moves. A fork pull request carries a read-only token
  and no secrets, and the gates take their answer from the process exit code rather than from the
  log. What this closes is forged annotations and log-command effects — `##[error]`, or a
  `##[group]` that hides the output beneath it — on a required check.

- Every workflow now declares the token it wants (#178). `plugin-validate.yml`, `ci.yml` and
  `secret-scan.yml` had no `permissions:` block at all, so every job in them took the repository
  default — read/write in a repository whose owner has never changed it — while doing nothing but
  checking out, installing tools and running pytest.
- Weight, honestly: nothing is known to be exploitable through this today. #176 and #177 are where
  the reachable vectors were. This is defence in depth, and what it buys is that a future step
  reaching for a token it should not have fails rather than succeeding quietly.
- Declared at workflow level rather than per job, so a job added later inherits read-only instead of
  falling back to the default. `oss-changelog.yml` already reached that answer and these match it.
- `secret-scan.yml` is the one that is not `contents: read` alone. gitleaks-action's entire use of
  `GITHUB_TOKEN` is `pulls.createReviewComment` and its `GITLEAKS_ENABLE_COMMENTS` defaults to true,
  so a bare `contents: read` would have silently stopped the scanner reporting on the pull request
  it had just scanned. Read out of the action's own README at both `master` and the `v3` tag this
  workflow pins, rather than inferred from the action's name. Turning the comments off instead would
  be a narrower grant and a different product decision, which nobody asked for.
- `publish.yml` already declared `id-token: write` at job level for PyPI Trusted Publishing, and it
  keeps that shape rather than gaining a workflow-level block: naming any permission on a job
  **replaces** the workflow-level set rather than adding to it, so a top-level grant here would be
  discarded by the job's own and read as a guarantee it does not give. What it gains is
  `contents: read` beside the OIDC grant, because `actions/checkout` reads the tag being published
  and nothing else in that job granted it — without it `contents` is implicitly `none` and the
  checkout works only because the repository is public, which is a property of the repository rather
  than of the file, and the wrong thing for a publish to depend on.
- `tests/test_workflow_permissions.py` is the guard, and it is why this is one change over four
  files rather than four edits. Sweeping a class in one file and missing the one beside it inside
  the same feature is exactly the shape #147 was; the edits are a few lines each, and the thing
  worth keeping is that the fifth workflow cannot be added without answering the question.
- The guard checks two things and both directions of each. Every workflow declares permissions
  somewhere, with per-job scoping allowed only for a file that says in `_JOB_SCOPED` why it cannot
  declare them at the top; and every `write` scope granted anywhere has a written reason in
  `_WRITE_GRANTS`, since `permissions: write-all` satisfies the first check while granting more than
  the default it replaced. An entry in either list whose call site is gone goes red as stale prose.
- It has a must-fire control. Three must-not-fire assertions over a tree that is already clean
  cannot tell a working guard from one that returns an empty list whatever it is shown, so the
  offending shapes are put in front of it directly — a silent workflow, one whose only grant is
  inside a job, an unexplained `write`, `write-all`, and a clean one so a guard that flagged
  everything would not read as coverage either.
- No user-visible change and nothing about what any workflow prints; that is #147, #176 and #177.

## [1.1.0] - 2026-08-21

### Highlights

- Every Claude Code skill now checks the `requivo` CLI is installed before it runs anything, and says exactly how to fix it — five of six skills used to fail a new user's first command with a bare shell error.
- The Claude Code plugin README is rewritten as a landing page for someone who installed it from a marketplace and has never cloned the repository.
- `/requivo:discover` now reports the absolute directory a session was written to, not only its slug, so a discovery started from the wrong directory does not silently hide the result.
- The `status`, `prd` and `brief` skills close by naming the next step, matching `discover`, `answer` and `impact`.
- CI now validates both Claude Code manifests with `--strict` and checks the plugin's `requivo` invocations against the released CLI on PyPI, not just the code in the same checkout.

### Added

- A CI leg runs `claude plugin validate --strict` over both manifests on every push and pull request
  (#92). Nothing ran the validator before — in either mode, on either file — so the marketplace
  manifest had been failing it for a whole release with nothing to say so.
- The decision that leg forces is written down in `.github/workflows/plugin-validate.yml`, since #92
  asked for it in writing: **`--strict` is the gate, against a pinned CLI version.** Plain
  `validate` exits 0 on "passed with warnings", and *warning* is precisely the class this check
  exists to catch, so a non-strict leg would have been green throughout the life of the bug it was
  added to find.
- The cost of `--strict` is that an unrecognised field is an error too, so a manifest written against
  a newer plugin spec goes red against an older CLI — a failure on somebody else's release schedule,
  on a pull request that touched no manifest. Pinning the CLI keeps the gate answering a question
  about our files. A second run against the current CLI is advisory, cannot fail the build, and
  annotates the run when it disagrees with the pin, so spec drift is found on an ordinary pull
  request rather than during a marketplace submission.

- Documentation: `docs/plugin-bundling.md` records why the Claude Code plugin does not bundle the `requivo` CLI (#94). It states what the plugin spec measurably supports today — a plugin-root `bin/` really is on the Bash tool's `PATH`, and the mechanisms a first-run bootstrap would need are all documented — and why none of it is shipped: the only hook that fires unattended cannot ask the user, and the one that can ask would turn an inert plugin into one that rewrites shell commands. Installing `requivo` stays a separate step, and the plugin is submitted for Claude Code only.

- A CI leg that resolves the Claude Code plugin's `requivo` invocations against the **released**
  CLI from PyPI, not against the copy in the same checkout (#96). The plugin's existing tests
  compare `plugins/claude-code/` to `src/requivo/` in one working tree, which is green by
  construction; a user installs the plugin from a marketplace pinned to a commit on `main` and the
  CLI from PyPI, and those two artifacts had never been compared. The leg is advisory and never
  fails a build, because this project's normal state between releases is a branch whose plugin uses
  a verb the last release does not have yet.
- The new check reports three states rather than two: every invocation resolves, an invocation does
  not, or the released CLI could not be looked at (PyPI unreachable, nothing published, the install
  failed). The third is annotated as its own outcome and is never rendered as a pass.

- Documentation: the Claude Code plugin now states its native-Windows prerequisite (#121). Claude
  Code's Bash tool is provided by Git Bash on native Windows, and every Requivo skill reaches the
  `requivo` CLI through that tool, so Git for Windows is required there. If Git Bash is installed and
  Claude Code cannot find it, `CLAUDE_CODE_GIT_BASH_PATH` in `settings.json` names the path; under
  WSL nothing extra is needed. The plugin README, the repository README and
  `docs/getting-started.md` all carry it, and the supported-platforms table now says out loud that
  those CI legs test the `requivo` package and that nothing in CI exercises the plugin. What a skill
  actually does on a native Windows machine with no Git Bash was not measured, and none of the three
  pages claims to know. A new check in `tests/test_plugin.py` holds the property the prerequisite
  rests on rather than its wording: every skill declares a `Bash(...)` grant, and none declares a
  second shell tool alongside it, so a skill added later cannot make the sentence wrong in silence.

### Changed

- `src/requivo/deterministic.py` is now the package `src/requivo/deterministic/` (#73). One module of
  1541 lines held six things that change independently: the shared input and output helpers, `doctor`
  together with `schema` and `context`, the seven `session` verbs, `model`, `artifact`, and the
  argparse registration. Each is now its own module, and every line of moved code is byte-identical to
  what it replaced.
- Nothing a user can see has changed. Every verb keeps its output, its `--json` shape and its exit
  codes. That was checked rather than assumed: 21 `--help` screens and 11 verb renderings against a
  fixture session, captured before and after, all byte-for-byte identical.
- `register(sub)` is still the single entry point and still lives at `requivo.deterministic`. It now
  calls four `register_*` functions by name instead of building the whole argparse tree in one place.
  A registry that the modules populated as a side effect of being imported was the alternative, and
  was rejected: dropping a module would then delete its verbs with no error at all and a `--help` one
  group shorter, where naming them makes the same mistake an `ImportError` at startup.
- Two names keep their import path because they are read from outside the package:
  `requivo.deterministic.read_user_text` and `requivo.deterministic.EXIT_DEGRADED`. Internal names
  moved with their modules and are deliberately not re-exported. A re-export is a second binding, so
  rebinding one would not reach the module global the code reads, and a test that patched it would go
  green having patched nothing.

- The interactive `requivo discover` loop now reaches the provider through `DiscoveryService`, like every other surface, instead of calling the engine directly and using the service only for the final write (#77). Same model, same assessment, same session and same provenance; what changes is that a fix to the shared orchestration now reaches the terminal without being applied twice.
- `requivo estimate` goes through the same seam and no longer opens a second API client of its own (#77).
- From the third turn onward, an interactive discovery no longer re-sends the earlier rounds of question and answer: the model being refined already carries what they established, which is how `requivo answer` and the web form have always worked. Turns one and two are unchanged, and later turns send less.
- Compatibility: compatible - no session, artifact or `--json` payload changes shape, and the `ReasoningProvider` protocol gains only a keyword argument with a default.

- The Claude Code plugin README is now a landing page for someone who installed the plugin from a
  marketplace and has no checkout, rather than repo documentation (#95). It states the arc
  (`/requivo:discover` → `status` → `answer` → `brief` → `prd` → `impact`), that sessions are written
  to `.requivo/sessions/<slug>/` under the directory Claude Code is running in — so the directory you
  start a discovery from decides where the work lives — that the reasoning happens in your Claude Code
  session and there is nothing to configure, and which two skills (`status`, `impact`) are pure local
  reads. It also names what Requivo does not do: no automatic relevance routing over context cards,
  and `stories`/`estimate` print rather than save. Every link is absolute, so it resolves without a
  clone.

- The plugin's catalog entry now says what Requivo *is*, not only what it is good for (#118). Both
  manifest descriptions lead with the category — requirements discovery for Claude Code — and name the
  things a reader would search a marketplace for: a requirements model, a decision brief, a PRD,
  acceptance criteria, a tracker epic. The old line opened on a benefit and contained none of those
  words; in a catalog of thousands of entries, `name` and `description` are the whole storefront.
- `.claude-plugin/marketplace.json` gains the top-level `description` a marketplace browser reads —
  what this catalog offers, which is a different sentence from what the plugin does — and a
  `category` of `development` on the entry, the value SchemaStore's own marketplace schema gives as
  an example and the one 104 of the 156 categorised community entries use.
- Both manifests now declare a `$schema`, so an editor validates them as you type and a reader can
  see they were written against the spec rather than copied. The plugin manifest also gains
  `displayName` and `repository`. Giving the source tree a field of its own is what frees `homepage`
  to be the documentation URL the reference describes, rather than a second pointer at the repository
  root. The value `homepage` carries in this release is stated in the `homepage` bullet of this
  section.
- The marketplace `name` is deliberately untouched. It is an immutable slug once published — renaming
  it breaks every existing install with `plugin-not-found` — so `displayName` is the field that
  carries the label from here on.

- The Claude Code plugin's `homepage` is `https://requivo.com` in both the plugin manifest and
  the marketplace entry (#125). The last released value was the repository root. A
  `blob/main/...` link to the plugin's own README was tried in between and ships in no release,
  because a blob URL pinned to a branch breaks as soon as the file moves. A marketplace catalog
  shows one homepage per entry, and the submission already carries the repository in a field of
  its own, so this is the only slot that can point somewhere other than a source tree.
  `repository` is unchanged. The plugin still ships its README, which is what a reader wants
  *after* installing rather than before.

- The plugin `description` in both manifests, the marketplace's own top-level description, and
  `plugins/claude-code/README.md` no longer use the em dash (#127). These are the strings a
  stranger reads in a catalog of 2 281 plugins before deciding whether to trust the project,
  and a dense run of them now reads as machine-written to that audience. Each was rewritten
  rather than substituted: an em dash joins two clauses that wanted a different relationship,
  so swapping the glyph alone leaves worse prose than either version. The description gained a
  claim on the way through, saying that the model *separates* known from assumed from open
  rather than listing the three states in an aside. `docs/`, the root README and the skill
  bodies are deliberately untouched.

- `docs/compatibility.md` now names `requivo.deterministic` in its list of what is explicitly not
  stable, beside `requivo.core`, `requivo.services` and `requivo.providers` (#140). #73 reorganised
  that module into a package and moved most of the names that used to sit at its top level, and the
  page was silent about it, which its own closing rule calls a bug in the page rather than a licence
  to assume. The module's public job is the single `register(sub)` the CLI binds through, so it is
  argparse wiring for the offline verbs. What those verbs promise was already on the page and is
  unchanged: the CLI exit codes, the `--json` payloads and the session format.

### Fixed

- Every Claude Code skill now checks that the `requivo` CLI can be run before it runs anything, and says what to do when it cannot: that the CLI is a separate install rather than a broken plugin, one install command, that the skill can simply be run again — no reinstalling the plugin, no restarting Claude Code — and that nothing was left behind, because the check happens before any mutation. Five of the six skills said nothing at all, so a new user's first command failed with a bare shell error (#93). The statement lives once in the plugin's `REASONING.md` and every skill refers to it.
- `uv tool install requivo` is the one command the skills name, rather than offering two with no guidance (#93). It puts `requivo` on the PATH by construction, in an environment of its own, whichever Python happens to be active in the shell — which is the failure being recovered from. `pipx` and a `pip install` inside an activated virtualenv are named only as fallbacks, and `pip install --user` is called out because it succeeds while leaving the command unfindable.
- The probe is `requivo doctor --json`, read for whether it ran at all rather than for what it reported. `doctor` is also the binary that may be missing, so the question is who is speaking: a message from the shell means the CLI is absent, while anything from Requivo — the JSON report, a structured error envelope, even a traceback — means it is present and the install is not the problem. A healthy install reporting no API key is not a failure at all (#93).
- The `status` and `impact` skills gained the `Read` tool. Without it they could not open the shared statement they point at, and they are the two read-only skills a new user is most likely to try first (#93).
- `tests/test_plugin.py` holds the preflight structurally rather than by wording: every skill must reference it and be able to read it, and the install command must appear in `REASONING.md` and nowhere else — so the next skill added cannot drop it silently (#96, item 1).
- `/requivo:discover` now reports the absolute directory the session was written to, not only its slug. Sessions land under the caller's workspace, so a discovery started from the wrong directory does not fail — it succeeds, produces a valid session, and puts it somewhere the user will not think to look. The path was already in `session init --json`; what was missing was saying it out loud at the one moment it is guaranteed to be on screen.
- The `status`, `prd` and `brief` skills now close by naming the next step, which `discover`, `answer` and `impact` already did. `status` is what a returning user reaches for first, so it was the worst of the three places for the chain to break. A test holds the convention across all six.
- `REASONING.md` no longer calls the skills `requivo-*`, a name that predates the directory rename; the invocations are `/requivo:<skill>`.

## [1.0.1] - 2026-08-20

### Fixed

- **Every error string `deterministic.py` prints goes through `display_token`** (#90). Six sites,
  not the two the issue named: the two on a `doctor` non-session line, `doctor`'s own `schema`,
  `context cards` and `sessions` rows, and `session verify`'s product-context line. Sweeping the
  class rather than the two instances is what the first attempt at this fix did not do, and the
  release audit said so — twice, since the guard added alongside claimed a reach two sites wider
  than it had.
- **The error text on a `doctor` non-session line goes through `display_token`** (#90). Every other
  value interpolated into that line already did — the entry name, each sampled child name — and the
  function's own docstring, three lines above, stated the rule for the *names* on a line that
  carries two classes of value. `error` is `str(e)` from a deliberately wide `except Exception` in
  the store, whose docstring says the set of ways a member can be broken is open, so an open set of
  causes was feeding an unescaped interpolation. That is the shape #40 was.
- Compatibility: compatible - five more printed values are escaped, across `doctor` and `session
  verify`; no payload, code or exit code changes, and ordinary error text is unchanged by the wrap,
  since `display_token` returns its argument byte-for-byte unless it holds a control character
  (#90).
- **Two messages this release does not wrap, and deliberately.** `session verify`'s integrity-problem
  and card-problem lines reach the terminal unwrapped because their values are guarded where they
  are *interpreted* — `normalize_tokens` refuses a control-carrying token before any message text
  exists — which is where invariant 14 says the guard belongs. Wrapping them would add a second
  guard and imply the first is not trusted (#90).
- No reaching instance was constructed and the wrap is not conditional on that. Today the arms are
  effectively the `OSError` family and CPython's `OSError.__str__` already `repr()`s the filename,
  so it misreported rather than forged — but that is a fact about today's exception space, not a
  property of the line. The docstring now states the rule for every value on it rather than for
  half of them, which is why the omission recurred (#90).

- **`requivo session verify` no longer crashes on the row `session list` just told you to look at**
  (#97). `session_exists` used a bare `Path.exists()`, which swallows `ENOENT` into `False` and
  **re-raises everything else** — the same unguarded probe #80 had to remove from the session-root
  scan. #80 made `session list` render a degraded row for an entry it could not examine and print a
  footer pointing at `session verify <slug>`; that verb opened with `session_exists` and answered
  with a bare `PermissionError` traceback.
- Compatibility: compatible - `session verify --json` gains a `session` object and no field is
  renamed or removed. A consumer reading only `slug`, `ok`, `problems` and `context_cards` is
  unaffected on a workspace where every session can be examined (#97).
- **`session verify` exits 4, not 1, when it could not look.** 1 says *I checked and it is broken*;
  nothing checked anything. 4 already means *the work was done and part of the answer was
  unreachable* (#86), which is exactly this. The precedence rule is unchanged: a session with real
  `problems` **and** an unexaminable path exits 1, because a firm negative outranks a partial one.
- **`session verify --json` gains `session: {checked, error}`**, a sibling of `context_cards`
  carrying the same two keys for the same reason: `problems: []` spells both *checked, nothing
  wrong* and *nothing was checked*, and a consumer cannot tell those apart from an empty list.
  **Branch on `session.checked`**, never on the emptiness of `problems`. It is present on every
  payload, reading `{"checked": true, "error": null}` on a session that was examined (#97).
- **`session_exists` raises rather than widening its bool.** A `bool` has two states and the
  question has three, so the third leaves through the error channel as `SessionUnreadableError` —
  #82's code for a fact about the store rather than about the request, already 500 over HTTP.
  Answering `False` was never available: `cli.py` and `session import --force` read this to decide
  whether to create or overwrite, so *I could not tell* becoming *there is nothing here* is a write
  proceeding on an unknown. `ENOENT` still returns `False`, because absent is a real answer and the
  commonest one. `legacy_exists` had the identical shape and is fixed with it (#97).

- **`session import` can no longer destroy a session that was created while it was reading the
  archive** (#111). It decided the collision question twice — `session_exists(slug) and not
  --force` before the extraction, and `replaced = target.exists()` after it — with the whole unzip
  in between. A session that appeared in that window was moved aside and then deleted, by an import
  whose user was never asked for `--force`, because at the moment they would have been asked there
  was nothing to force past.
- Compatibility: compatible - no payload, flag or exit code changes. `replaced` in `session import
  --json` keeps the meaning it always had and is now the guard's own answer rather than a second
  observation; the two could previously disagree, and the disagreement was the bug (#111).
- The answer is asked once and the two outcomes are two code paths, not one flag. A slug that was
  **free** at the guard is claimed by the rename itself — nothing steps aside, and `os.replace`
  refuses a non-empty destination directory, so the concurrent creator's session stops the import
  instead of being destroyed by it. The caller gets `session_exists` / 409 and the same remedy the
  guard would have given them: pass `--force`.
- A slug that was **occupied**, so `--force` was passed for it, swaps as before — reversibly, the
  old directory stepping aside and dying only once the new one is in place. It deliberately does
  **not** take `session_lock`, and the reason is structural: the lock is an open handle on `.lock`
  inside the very directory being renamed, and Windows refuses to rename a directory holding one.
  Taking it there does not serialise the swap, it makes the swap impossible on four of the thirteen
  CI legs. What closes this defect is the single decision above, not a lock (#111).
- The residue is narrow and is what `--force` already means: a concurrent writer part-way through
  the session being replaced loses that work, because the caller asked for the session to be
  replaced. What is no longer possible is losing a session the caller was never asked about (#111).
- Held out of the 1.0.0 tag deliberately rather than missed. The release audit found it, ranked it
  `destroys`, and it was byte-identical in 0.11.0 — outside that release's delta, so holding the tag
  would have removed it from nobody's machine (#111).

- A comment added in 1.0.1's own `session import` fix claimed a narrower residue than actually holds,
  and now states the real one (#113). It said a concurrent writer part-way through a session being
  replaced by `--force` merely *loses that work*. It does worse than that, and the underlying defect
  is filed rather than fixed here because the fix is a change to the swap mechanism.
- Compatibility: compatible - one comment and one changelog sentence. No behaviour, payload, flag or
  exit code changes (#113).
- What actually happens: `save_revision` resolves the session directory once and then writes by
  **pathname**, while `session_lock` holds an fd on an **inode**. A writer inside `save_revision`
  during the swap goes on writing into the newly imported directory, so the import silently inherits
  another session's revision files and identity — and a third process then locks `target/.lock`,
  a different inode from the one that writer holds, and acquires it. Two writers hold the lock for
  one slug, which is invariant 9's own failure mode wearing the shape invariant 9 exists to remove.
- Pre-existing and byte-identical at 1.0.0 and 0.11.0, so nothing in 1.0.1 introduced it. What 1.0.1
  introduced was a sentence asserting it away, which is the part corrected here (#113).

- A changelog fragment and two commit messages shut an issue they were written to keep open (#114).
  GitHub's closing-reference parser matches a `close`/`fix`/`resolve` verb sitting next to a
  hash-number, and it sees **neither negation nor code formatting**: `Also filed, not <verb>: <num>`
  fired, and so did the same pair inside backticks in a sentence explaining that it had.
- Compatibility: compatible - a tracker state and this note. No code (#114).
- The rule that follows is blunt, because two attempts at stating it more carefully both fired:
  **never put those verbs adjacent to a hash-number for an issue you are not closing.** Not in a
  heading, not after a negation, not inside a code span, and not while quoting the mistake. Where a
  number must appear next to such a word, spell one of the two some other way — which is what this
  entry does (#114).
- The subject is a POSIX/Windows divergence in `session import`'s free-slug arm, and nothing in
  `v1.0.0..v1.0.1` touches it. Caught both times by the release audit's composition pass, which
  reads the tracker's *state* rather than the issue text — the tag would otherwise have published a
  tracker asserting a repair that does not exist (#114).

## [1.0.0] - 2026-08-20

### Added

- **Both spellings of the context-card selector now work on every verb that takes one** (#85). The
  same selector — same comma-separated grammar, same `resolve_cards` validator — was spelled
  `--context` on `discover` and `session init` and `--cards` on `context`. Each verb now accepts both.
- **`--context` is the documented primary; `--cards` is a permanent alias.** They are two option
  strings on one argparse action rather than two arguments, so they cannot drift apart and neither can
  silently discard the other when both appear on a command line. The destination is unchanged on each
  verb, so no handler moved and nothing persisted changed.
- One cost worth naming: on these three verbs the single-letter abbreviation `--c` now reports
  `ambiguous option` where argparse used to resolve it. `--co` and `--ca` still resolve, as do both
  full spellings; argparse prefix abbreviation is not a documented part of this CLI.
- Compatibility: compatible - additive, with the single exception named in the bullet above: `--c`
  on those three verbs now reports `ambiguous option` where argparse used to resolve it. Every other
  command line that worked before works unchanged and produces the same output, and the new behaviour
  is that the previously-rejected spelling is now accepted. Adding the aliases **in** 1.0.0 converts
  what would have been a breaking removal into a documentation choice that can be made at any time.

### Changed

- `invalid_session` is now a **family that nothing raises directly** (#82), and each of its nine
  conditions carries a code that names the fact: `unsupported_format_version`, `unsupported_schema_version`,
  `session_unreadable`, `artifact_revision_out_of_range`, `unstated_source_revision`,
  `unreadable_source_revision`, `inconsistent_archive`, `unreadable_archive` and `import_move_failed`.
  It had carried seven facts across eight raise sites with four `details` shapes, and no key was
  present on all eight -- `details["slug"]` raised `KeyError` on three of them. Unlike the
  `cross_site_request` split, this one was never inert: `cli.py` serializes `to_dict()` on every
  `--json` verb, so a consumer could observe the inconsistency, and `docs/compatibility.md` promised a
  condition by code (`invalid_session`, "upgrade requivo") that the code could not tell from a corrupt
  zip -- while the same page says to assert on the code and never on the message.
- `except InvalidSessionError` is unchanged: the base is kept as the family, so nothing that catches
  the class has to enumerate nine names.
- Six of the nine conditions change HTTP status in Requivo Web. The two version frontiers answer
  **409** and the four store-state arms answer **500**, where everything under `invalid_session`
  previously answered 400 -- the misattribution #34 fixed for `context_unreadable`, one condition
  along. **Both counts are history and stay as written**: #101, in this same release, adds a tenth
  arm (`invalid_archive`) and a fourth 400 arm, and restating #82 against the family as it now stands
  would claim something #82 did not do. `docs/compatibility.md` carries the reconciling table.
  The three that keep 400 are the ones genuinely about the request: both archive arms and
  `unstated_source_revision`.
- `unsupported_format_version` carries `{format_version, supported_format_version}`; the second key is
  new, because *newer than what* is half the fact and a reader had no other way to learn which build
  they were holding.
- The arms deliberately do **not** share one `details` shape. Three of them identify no session at
  all -- none has been identified when a zip will not open -- and a `slug: null` there would state a
  fact nobody measured. Branch on the code, then read the shape documented for it.
- Compatibility: breaking - moving a condition from one error code to another, and changing an HTTP
  status, are both listed as breaking in `docs/compatibility.md`. Taken **in** 1.0.0 deliberately:
  this is the release that draws the boundary, so the move costs nothing beyond the tag itself. After
  it, the same move costs a major version, or a promise on that page nobody can keep.

- **`requivo epic --json` is now `requivo epic --export-json`** (#83). On every other verb `--json`
  means *emit the payload on stdout*; on `epic` it meant *also write a second file into `artifacts/`*,
  and nothing reached stdout. It now sits alongside `--github` and `--gitlab` as three flags of one
  kind, each writing an export file. `epic` deliberately gains no stdout `--json`.
- **The rename fixes a second thing the name was hiding: the error channel.** `cli.app()` reads
  `getattr(args, "json", False)` generically and uses it to switch failures from a prose message on
  stderr to a structured JSON envelope on stdout. Because `epic`'s file-writing flag was spelled
  `--json`, passing it silently changed how a failure was reported — the same provider outage printed
  prose under `--github` and a JSON envelope under `--json`. With no `json` attribute on `epic`, that
  `getattr` falls through and all three export flags report a failure identically. Nothing documented
  the old behaviour and nothing asked for it.
- Compatibility: breaking - `requivo epic <slug> --json` is no longer accepted and exits 2 with an
  argparse usage error; use `--export-json`, which writes the same `epic.json`. Breaking on both
  halves named in `docs/compatibility.md`: a flag is removed, and the meaning of passing it changes.
  The failure is loud rather than silent — `--json` is not a prefix of `--export-json`, so argparse
  rejects it outright instead of quietly doing something new. Callers parsing `epic`'s failure output
  as JSON must now read the prose on stderr, as `--github` and `--gitlab` callers already did.

- `requivo session import --json` now prints `{"slug": ..., "path": ..., "replaced": ...}`. It was
  `{"imported": ..., "into": ..., "replaced": ...}` — the one session verb that spelled the session
  and its location differently from all of its siblings, so a consumer looping over the session verbs
  and reading `row["slug"]` got a `KeyError` from the verb that had just put the session there (#84).
- `path` is the **session directory**, not the session root. `into` carried the root; `session init
  --json` has always meant the session directory by `path`, and `session import`'s own human-readable
  line already printed it. Renaming the key over the old value would have given `path` two meanings
  across two verbs of one noun, which is the defect this change closes, back under the harmonised
  name (#84).
- Compatibility: breaking - two keys are removed from a populated public `--json` output and the
  value under the new location key changes. `imported` is `slug` and `into` is `path`, with `path`
  now naming the session directory rather than the directory that holds it. They are not kept as
  duplicates: removing a `--json` key is breaking, so the rename ships **in** the 1.0 tag or never
  (#84).

- `requivo session verify` now exits **4** when it could not read a session's product context at all.
  It answers three different things — the session is inconsistent, its product context was read and
  does not resolve, its product context could not be read — and had two exit codes, so *checked and
  broken* and *could not check* were spelled the same way by the one verb whose whole job is to say
  whether a session is sound. The third state already had a rendering of its own; only the exit code
  collapsed (#86).
- Where both happen at once the **firm negative wins**: a session that is inconsistent *and* whose
  cards were unreadable exits 1. A script gating on *is this usable* wants the definite answer, and
  there is one (#86).
- Exit code 4 is now general. It was `EXIT_DEGRADED_LISTING` and is `EXIT_DEGRADED`: it describes a
  shape of answer — the work was done and part of the answer was unreachable — not a verb. Minting a
  code per verb would rebuild the collapse 4 was introduced to undo. The exit-code table in
  `docs/cli.md` states the general sentence and lists the two commands that reach it (#86).
- `requivo doctor` does **not** move and still exits 0 whatever it finds. `verify` is a gate whose
  exit code is a decision; `doctor` is a report, and a report that exits non-zero is concluding — the
  same directory can be a half-extracted archive or a leftover lock and nothing in it says which. The
  reason is now written down in `docs/cli.md` beside the table, so harmonising the two is a decision
  somebody has to argue with rather than a tidy-up (#86).
- Compatibility: breaking - one condition moves to a different exit code. A session whose product
  context could not be read answered 1 from `session verify` and now answers 4, which is a change a
  script can observe and was never announced. Narrow in practice:
  nothing that exited 0 now exits non-zero and nothing that exited non-zero now exits 0, so
  `verify && deploy` is unaffected — only a script that discriminates on the number sees it. The
  module constant `requivo.deterministic.EXIT_DEGRADED_LISTING` is renamed to `EXIT_DEGRADED` in the
  same change (#86).

- `requivo session list --json` now prints an **object**, not a bare array:
  `{"sessions": [...], "degraded": n, "session_root": "..."}`. The rows are unchanged — the wrap is
  the whole difference — and `degraded` is the count of rows that could not be read, the same
  condition exit code 4 already signals (#87).
- Compatibility: breaking - the top-level type of a populated public `--json` output changes from
  array to object. A `jq '.[] | .slug'` one-liner becomes `jq '.sessions[] | .slug'`; nothing else
  moves, and no row field is renamed or removed. This is deliberately not shimmed: it was the only
  array among the CLI's JSON payloads and an array has no top level, so no field could ever be added
  to it. It ships **in** the 1.0 tag, which is the boundary itself; the next break of this class is
  a 2.0 (#87).
- `degraded` recovers no fact that was missing. Every row has carried `readable` and `error` since
  #62, so the count was always derivable. What it buys is that exit 4 is readable on stdout rather
  than only signalled — the same argument that makes a degraded row name its session instead of
  disappearing (#87).

- `requivo doctor --json` spells the strict-handler stream state `will_crash` rather than
  `will-crash` in `output.streams[].state`. It was the only hyphenated value in any `--json` enum —
  `context.status`, `NonSessionEntry.kind` and `UpdateResult.status` are each one word or
  underscore-joined — so a consumer mapping a state onto an identifier had exactly one value to
  special-case, and one that a naive split on `-` would cut in half (#88).
- The human `doctor` report is unchanged: the state is a wire value, never a printed word.
- Compatibility: breaking - `will-crash` -> `will_crash` renames a value in an enum that v0.11.0
  published to PyPI, so a consumer matching on the old string stops matching and must be updated.
  It ships in 1.0.0, the release that draws the compatibility boundary, so this is the last window
  in which the rename is cheap; after it the hyphen would stand until a 2.0 (#88).

- `docs/compatibility.md` now bounds four surfaces that were in neither column -- neither promised nor
  disclaimed (#89). A 1.0 is only as good as its boundary, and a surface listed nowhere is a promise
  nobody made and everybody may assume.
- **The epic export envelope is stable and versioned.** It carries its own `format` (`requivo-epic`)
  and `version` (1) and exists to be validated outside this repo, so calling it unstable would have
  contradicted the code declaring it stable. The `--github` / `--gitlab` tracker plans are stable in
  the same way, with the asymmetry stated: a change we make is breaking, a change forced by GitHub or
  GitLab moving was never a promise we could make.
- **Environment variables are stable**, under the same rule as a CLI flag: `REQUIVO_WORKSPACE`,
  `REQUIVO_CONTEXT_DIR` and `REQUIVO_WEB_ALLOWED_HOSTS`.
- **Requivo Web's ten routes are stable in path, method and status; their response bodies are not.**
  The bodies are HTML rendered for a browser, HTMX fragments included. `GET /health` and
  `GET /sessions/{slug}/export` are the two that return data and are stable.
- **Artifact filenames are stable and part of the session format**, so renaming one needs a
  `format_version` bump and a migration. The map is written out, which also corrects a detail: `brief`
  is stored as `solution-assessment.md`, not `brief.md`.
- **The `code` on Requivo Web's error banner is presentational and not stable.** Four of its values
  are bare string literals outside the `RequivoError` vocabulary, so the guard that walks that
  vocabulary cannot see them; a caller scripting the Web branches on the HTTP status.
- Compatibility: compatible - nothing in the product changed. This states what the existing behaviour
  promises, in both directions, so a reader can tell which column a surface is in.

- `docs/compatibility.md` now records three breaking `--json` changes that reached no line on the
  contract page (#100): `session list --json` becoming an object and the `session import --json` key
  rename, which both land in 1.0.0, and the `will-crash` -> `will_crash` respelling, which shipped in
  0.11.0 and is corrected here. Each was
  declared breaking in its own changelog fragment and named in `docs/cli.md`; none reached the page
  that says what will not be taken away. `grep -cE '#84|#87|#88|session_root'` returned 0 before this
  change.
- The migrations are stated where a reader needs them: `jq '.sessions[]'` for the first, and the note
  that `path` is the session directory rather than a rename of `into`, which was the sessions root.
- Compatibility: compatible - this records changes already shipped. Nothing in the product moved.

- `session import` refuses a malformed archive as an **archive** (#101). Seven shape conditions --
  no entries, more than `MAX_ARCHIVE_FILES`, expanding past `MAX_ARCHIVE_BYTES`, an entry carrying a
  Windows separator, an entry that is absolute or holds a `.`/`..` segment, an entry not inside a
  session directory, and more than one session directory -- moved from
  `invalid_model` to a new `invalid_archive`. `invalid_model` is documented as *"a proposed model is
  structurally or semantically invalid"* and nobody proposes a model when they hand `session import`
  a zip.
- **An occupied slug is `session_exists` / 409**, not `invalid_model` / 400. The vocabulary already
  had the right code and the right status; the import path was the one caller that did not use them.
- The composition is why this shipped now rather than after 1.0. #82 split `invalid_session` into a
  nine-arm family in the release just gone, on the principle that a code must name its fact, and gave
  `unreadable_archive` and `inconsistent_archive` codes of their own. Those two arms sit *either
  side* of these eight conditions, in the same function, on the same code path. `cli.py` serializes
  `to_dict()` on every `--json` verb, so a consumer scripting `session import --json` read one handle
  for *my zip is too big*, *that slug is taken* and *your proposal is malformed* -- three remedies,
  one code, on the page that tells them to assert on the code and never on the message.
- **One code for the seven, and a discriminator rather than an excuse.** They share a remedy -- give
  me a different archive -- so seven codes would send a reader to one place seven ways, which #82's
  own rule refuses. What a single code owes in exchange is the thing #82 was actually about:
  `details["problem"]` is on **every** arm and is one of `empty`, `too_many_files`, `too_large`,
  `unsafe_entry`, `entry_outside_session_directory`, `multiple_sessions`. Each arm still adds only
  the numbers its own sentence quotes -- `{files, max_files}`, `{bytes, max_bytes}`, `{entry}`,
  `{slugs}` -- because padding them to a common shape would state measurements nobody took.
- `InvalidArchiveError` is a tenth arm of the `InvalidSessionError` family, so `except
  InvalidSessionError` catches it alongside the two archive arms on either side of it. It is no
  longer an `InvalidModelError`, which is the breaking half for anyone who caught the class.
- No HTTP status moves. `invalid_archive` answers **400**, the same number `invalid_model` answered
  and the same one its two siblings on this path already give: the caller handed us this archive,
  nothing has been written to the store, and re-sending the same zip unchanged can never succeed.
  `session_exists` moves 400 -> **409**, which is the correction rather than a side effect.
- `SessionExistsError`'s docstring now names its second raiser. In `create_session` and
  `migrate_legacy` it is raised by the atomic claim itself; in `session import` it is a check, with
  the TOCTOU window that implies. That window predates this change and is not what moved -- it is
  written down so the docstring stops promising a guarantee one of its three raisers does not make.
- **A directory name inside an archive can no longer write a line of the refusal that reports it.**
  Found by this change's own audit, on a line this change edits. `_inspect_archive` interpolated the
  top-level directory names raw when refusing an archive holding more than one -- and those names are
  the one piece of archive text `validate_slug` has not seen yet, because validation runs on the
  single surviving slug after the count check. A directory whose name carried a newline ended the
  line and wrote the next at column 0 of stderr, which `safe_write` does not prevent: it guards
  encoding, not control characters. The two sibling refusals on the same path already rendered an
  entry name with `!r`; this one now renders each name through `display_token`, so a name with
  nothing to escape is unchanged and one that could break the line is quoted rather than dropped.
  `details["slugs"]` stays raw -- `json.dumps` escapes it, so `--json` was never exposed. Same class
  as #40 and #98, one function along.
- Compatibility: breaking - moving a condition from one error code to another is listed as breaking
  in `docs/compatibility.md`, and eight conditions move. Taken **in** 1.0.0 deliberately: this is the
  release that draws the boundary. After it, the same move costs a major version, or a code that
  permanently means eight things in the verb most likely to be scripted.

- **Every `--json` output is public — all fourteen** (#102). The page previously named six and
  justified them as "what the Claude Code plugin drives", which was wrong in both directions by three
  entries each: the plugin drives `session init`, `model validate` and `artifact save`, which were not
  listed, and does not drive `model diff`, `artifact list` or `session list`, which were. Eight
  outputs were in neither column, and #84 made a breaking change to one of them before anyone noticed
  there was no promise to break.
- The promise itself is unchanged and additive: fields get added, a populated field does not change
  meaning without a note. Widening it from six outputs to fourteen costs nothing, and the subset was
  the expensive half -- a subset needs a boundary somebody can check, and the only one ever offered
  was a claim about another artifact's current contents that nothing tested.
- `test_every_json_verb_is_inside_the_promise` is the guard, and it reads the verbs off the built
  argparse tree rather than grepping the source: a grep validates the reader's regex, and what is
  promised is what the command actually accepts. It checks both directions -- a verb with `--json`
  and no row, and a row for a verb that no longer takes one.
- Compatibility: compatible - eight outputs gain a promise; none loses one.

- `requivo artifact list --json` now prints an **envelope**, not the bare map of artifacts:
  `{"slug": "...", "artifacts": {"<type>": {...}}}`. The rows are unchanged — same keys, same order,
  keyed by artifact type as before — and the wrap is the whole difference (#107).
- Compatibility: breaking - the top level of a populated public `--json` output stops being data. A
  `jq '.prd.stale'` one-liner becomes `jq '.artifacts.prd.stale'`; nothing else moves, and no row
  field is renamed or removed. It ships **in** the 1.0 tag, which is the boundary itself; the next
  break of this class is a 2.0 (#107).
- This is #87's argument one shape along, and it is the last of the fourteen `--json` payloads with
  no real top level. #87 moved `session list` off a bare array because "an array has no top level,
  so no field could ever be added to it"; a top-level map keyed by data has that property in
  practice, because the natural consumer read is `for t, info in payload.items()` and any metadata
  key added later is both ambiguous with a future artifact type and breaks that loop. Holding the
  argument for an array and not for a map is not defensible (#107).
- `slug` is the only key the new top level carries. Every sibling session verb answers it and this
  one had nowhere to put it, which is the whole point of gaining a top level; a top level nobody
  needs yet is still worth having, and filling it speculatively is not (#107).
- A session with nothing saved now answers `{"slug": ..., "artifacts": {}}` where it answered `{}`.
  That is the case the old shape served worst: `{}` named neither the session nor the fact that the
  question had been answered, so a consumer could not tell it from a payload that failed to
  serialise (#107).

### Fixed

- One directory under the session root that the process cannot stat into no longer hides every
  healthy session. `requivo session list` exited **1** with an empty stdout and a raw
  `PermissionError` traceback, and every other session in the workspace was invisible: the partition
  that decides whether a name is a session probes `<name>/session.json`, and `Path.exists()` re-raises
  EACCES rather than swallowing it, so one entry aborted the scan for all of them (#80).
- The partition now answers in **three states, not two** — a session, not a session, and *could not
  tell*. An entry whose examination raised belongs in neither of the other two: filed as a non-session
  it would drop out of every listing, which is the invisible-entry defect #67 closed one function
  along; filed as a session it would be claimed to be one, which is exactly what the failed probe did
  not establish (#80).
- `requivo session list` gives such an entry a degraded row and exits **4**. The row names the entry
  and the error, states nothing it could not read — no revision, no provider, no timestamp — and every
  healthy session is still listed in full beside it. Exit 4 already means *the work was done and part
  of the answer was unreachable*, which is what this is (#80).
- `requivo doctor` reports the entry instead of declaring the whole session root unreadable. It said
  `sessions unreadable` with `<root> could not be listed` beneath it, which was a claim broader than
  what had failed — the root *was* listed. The whole-root arm stays for the case that genuinely is the
  whole root: `iterdir()` on the session root itself failing. `--json` gains
  `sessions.unexaminable`, kept out of `non_sessions` because that key states a fact and here nobody
  established one, and kept out of `total`, which stays the count that could be confirmed (#80).
- `session list`'s footer counts **entries** rather than sessions — `1 entry could not be read.`
  where it said `1 session could not be read.`. Every degraded row used to come from
  `list_session_slugs`, so the old word was true of all of them; it is not true of an entry nobody
  could examine, and the footer is the last line a reader takes away. It is also the word `doctor`
  uses for the same entry, so the two surfaces stop describing one thing two ways (#80).
- No traceback on **the three paths above** — `session list`, `doctor` and the web home page, which
  reaches this through the same `list_entries`. A `PermissionError` under someone's workspace is an
  ordinary condition, not a bug in Requivo, and Requivo does not change permissions in a workspace it
  reads. Stated as three paths and not as *any path*, because it is not yet true of any path:
  `session_exists` carries the identical unguarded probe, so `session verify <slug>` and
  `session show <slug>` on such an entry still raise. That is filed separately rather than ridden in
  here — the verdict class and exit code for a session `verify` cannot examine is a design decision,
  and `session_exists` has 17 call sites including write-path guards where answering `False` on
  EACCES would be a worse bug than this one (#80).
- Compatibility: compatible - every observable change is confined to a case that previously produced
  an unhandled traceback. The exit-code policy makes moving a condition from one code to another
  breaking, and 1 → 4 looks like exactly that; it is not, because 1 is documented as `RequivoError`
  and this condition never raised one. There was no working consumer to break: stdout was empty and
  the payload did not exist. `doctor --json`'s `sessions.unexaminable` is additive; `sessions.readable`
  and `total` do change value here, from `false`/`null` to `true`/`N`, and that is a correction rather
  than a repurposing — the old pair asserted a failure of the whole root that the root had not had.
  `session list --json` row keys are untouched; what widens is that a `readable: false` row can now
  name an entry not known to be a session, so `slug` on a degraded row is still not a name to pass to
  another verb. `SessionRepository` gains a required `list_unexaminable`, and `scan_session_root`
  returns a 3-tuple: both fall under *Python internals* in `docs/compatibility.md`, which are
  explicitly not stable, and are named here because that is not the same as nobody noticing (#80).

- `requivo doctor` escapes the name of an inconsistent session before printing it (#98). A directory
  whose name carried a newline and held a `session.json` wrote two further lines of `doctor`'s own
  report at column 0, indented like real rows — the same forgery #40 closed on the card-name half of
  this verb, on the one bucket that had no guard. `_print_non_sessions` and `_print_unexaminable`
  already escaped; the *sessions* bucket did not.
- Reachability was not assumed: the pre-1.0 release audit reasoned it from four code locations and
  said it had not executed it, and the repro is what settled it. A clean session name still renders
  byte-for-byte.
- `doctor --json` was never affected — `json.dumps` escapes a control character before it can reach a
  line of its own.
- Compatibility: compatible - the only output that changes is a name that could previously forge a
  line, which no honest session has.

- `docs/compatibility.md` no longer contradicts itself in three places, all found by the round-2
  release audit and all introduced or carried by this release cycle (#105).
- The bullet on the two provenance refusals said the unreadable arm *"kept `invalid_session`"* and
  then, six lines later, that a test asserts the two codes differ. It carries
  `unreadable_source_revision` since #82; the sibling page `docs/session-format.md` was corrected in
  the same delta and this one was not.
- The `#82` section's arithmetic did not close: *"six of the nine conditions change status"* beside
  *"four conditions keep 400"* sums to ten. Both sentences were true — one counts what #82 did on the
  family as it stood then, the other counts the family as it stands now with #101's tenth arm. The
  split is now visible in the prose rather than only in a commit message, because a reader adding two
  numbers three sentences apart cannot see which tense each is in.
- The `#101` change note was filed under *The other public surfaces*, among stability verdicts, which
  made that section's count read as stale and made it appear to hold a surface with no verdict in a
  section that says each gets one. Moved beside `#82`, its sibling, under the `--json` section. The
  rule that decides which section a subsection belongs in is now written down, since it had already
  been got wrong once.
- `test_every_refusal_on_the_import_path_names_what_it_is_about` asserted six of the seven codes its
  own docstring named as the pin for a table. The seventh, `import_move_failed`, is now driven — by
  patching the one destination under test rather than by arranging a filesystem that refuses a
  rename, because the conditions for that differ per platform and a fixture would test the platform
  on some legs and nothing on others.
- Compatibility: compatible - documentation and a test. Nothing in the product changed.

## [0.11.0] - 2026-08-20

### Added

- Contributing guide now explains the tracked `.claude/` directory, and `tests/test_agent_layer.py` guards what makes it harmless (#2). Issue #2 reported that the `mode: block` jit-context rule over `Read`/`Edit`/`Write`/`Glob`/`Grep` blocks every file operation for a contributor without the maintainer's plugins. Measured, it does not: a jit-context rule is data, the only thing that reads it is a `PreToolUse` hook shipped inside the `claude-jit-context` plugin, and this repository registers no hooks. The barrier existed in the reading of the directory, not in its behaviour — so the fix is the explanation plus a guard that fails if a tracked hook, or a committed hook script, ever makes the barrier real.
- `.gitignore` now excludes `.claude/settings.local.json` (#2). It was never excluded, and only looked excluded on the maintainer's machine, which carries a global ignore entry no contributor has. That file is where a personal hook would be written, and a hook committed by accident would run for everyone who clones — the barrier above, made real.

- CI now runs the test suite on macOS and Windows as well as Linux. Every job in every workflow was
  `ubuntu-latest` and the only matrix axis anywhere was `python-version`, so no leg had ever executed
  this code on the platform most of its users install it on (#3).
- The shape is 9 legs rather than 15, and deliberately so: all five Pythons on Linux, because that is
  where a language-level difference between 3.9 and 3.13 shows, plus the ends of the supported range
  on macOS and Windows, because a path separator, a console codepage or a rename-over-existing does
  not care which minor version it meets (#3).
- The platform legs are a separate `test-platforms` job rather than an `os` axis on the existing one,
  for a reason worth knowing before anyone tidies it: `main`'s branch protection requires the five
  `Test (py3.N)` checks by their exact names, and adding an axis renames all five, so none of the
  required checks would ever report again and no pull request could merge. The four new checks are
  not required yet — the one command that adds them is in a comment at the top of the job (#3).
- README now states which platforms are supported and which are tested, because from outside an
  untested platform and a supported one look identical (#3).

- The first run of the new matrix found three things on Windows that no existing leg could have —
  two product defects and one test-harness bug, listed next. That is the leg paying for itself on
  day one (#3).
- **A concurrent session creation could be rejected as an invalid slug.** `canonical_dir()` checked
  containment by comparing two independently resolved paths, so its verdict depended on what the
  filesystem happened to look like between the two calls — create a directory in that window and they
  disagree. `requivo discover` then failed with *invalid session slug* about a slug that was perfectly
  valid, because something else was creating a session at that moment. Four of twelve concurrent
  creators died this way on the Windows leg. The containment check now resolves only a path that is
  actually there, which is the only case that can fail it (#3).
- The same shape turned up twice more when the class was swept rather than the instance: in the
  artifact path builder, and in `session verify`'s own artifact check, where it would report
  *unsafe artifact filename* about a perfectly ordinary name — the verb whose job is answering
  whether a session is intact, accusing you again. All three are fixed together (#3).
- **And a fourth instance of it, in those same three places, for a different reason.** The check
  decided containment with `Path.resolve()`, which on Windows under Python 3.9 — and only there —
  cannot follow a symlink whose target does not exist, and hands back the link's own location
  instead. A dangling symlink therefore read as living inside the session root however far out it
  pointed: `discover` would accept it as a session directory, and `session verify` reported it as a
  *missing* file rather than an unsafe one, which is the wrong answer from the verb whose job is
  telling you whether a session is intact. All three sites now share one containment function. It
  resolves with `os.path.realpath`, which does read the link itself — and, so that the guarantee does
  not rest on a platform being able to look at all, it refuses any symlink whose resolution comes
  back equal to the link's own location, because that equality is the resolver saying it could not
  follow. Pinned by a test that gives every platform the 3.9 resolver's semantics, rather than by the
  one leg in thirteen that has them natively — and that leg is not a required check, so it was never
  the gate it looked like (#3, #11).
- One more 3.9-only hole closed on the way past, found while checking what else the two resolvers
  disagree about: a **symlink loop** under a session made `Path.resolve()` raise `RuntimeError`,
  which nothing on the path caught, so `session verify` died with a traceback instead of reporting a
  problem. `os.path.realpath` collapses it and returns an answer the check can act on, and the loop
  is then refused like any other path that does not resolve inside the session (#3, #11).
- The refusal messages now say *does not resolve to a path inside …* rather than *resolves outside*.
  There are two ways to fail that check — it points somewhere else, or the platform could not tell us
  where it points — and the old wording asserted the first about both (#3, #11).
- **A write could be lost to an antivirus scanner.** On Windows a rename over an existing file fails
  with *Access is denied* whenever anything holds a handle to the destination — a scanner or the
  Search Indexer, opening the file microseconds after it is written, neither of which Requivo can
  serialise against. `model.json` is the durable product, so it now retries that specific failure a
  few times over a few hundred milliseconds at most. A genuinely unwritable destination still fails at once
  (#3).
- **And one harness bug, fixed as a harness bug rather than as a product one**: the test comparing the
  bundled demo payload with the browsable copy read both files with the platform's codec, so it failed
  on Windows about files the product itself reads correctly. Every read in the suite now names its
  codec, and the guard added for #11 was extended to `tests/` to catch the next one — it had walked
  `src/` and `scripts/` only, so the one directory it did not walk is exactly where the next instance
  turned up (#3, #11).
- One harness bug found and fixed on the way in, before the new legs could report it as a product
  bug: a test fixture wrote a context card containing an em dash with the locale's codec and read it
  back through a product that decodes UTF-8, which disagree on Windows. A narrow guard now catches
  that class — a test writing non-ASCII without naming a codec — because it is the trap this issue
  warned about, a harness rendering an environment limit as a product verdict, and "add more tests
  for that platform" is the wrong lever on it (#3).

### Changed

- Requivo Web's cross-site guard raises **six codes instead of one**. `cross_site_request` carried six
  distinct facts whose `details` payloads had five different shapes between them, against the rule
  `docs/compatibility.md` states in this repository for exactly this reason: a code carries one fact
  and one `details` shape. A consumer matching the code and reading `details["origin"]` got a
  `KeyError` from a payload that correctly carried the code it matched (#52).
- The arms are `undetermined_host`, `host_not_allowed`, `cross_site_fetch`, `opaque_origin`,
  `origin_mismatch` and `missing_request_token`. Each carries one fact and one shape, and the table is
  in `docs/compatibility.md` (#52).
- Compatibility: breaking for anyone matching the string `cross_site_request` on the Web surface —
  nothing raises it any more. It survives as the family base and keeps its 403 status row, and all six
  arms remain `CrossSiteRequestError` subclasses, so catching the class or matching on the 403 is
  unaffected. Only a match on that exact code string needs changing, to the six above.
- **The decision, since the issue asked for one and either answer was defensible.** The alternative was
  an argued exception in the policy for a surface that does not serialize `details` — which is true:
  Requivo Web renders a refusal as HTML, so no consumer could observe the inconsistency today. What
  decided it against the exception is that the cost was already being paid rather than deferred: both
  #43 and #45 had to distinguish their new arm **by message**, and the same policy says never to match
  on the message. The only handle a caller had for the distinction was the one it is told not to use.
  `empty_selector_token` was split for the identical shape one release earlier (#52).
- Read against #57, which asks the same question about `unstated_source_revision` and notes the two may
  want one answer: they do not, and the difference is the point. That code carries **one** fact with a
  `details` shape byte-identical to its sibling raise — the policy is satisfied and the wish there is
  for a more precise *type*. This one violated the policy. #57 is untouched here (#52).
- `opaque_origin` and `origin_mismatch` deliberately share a `details` shape and are still two codes: a
  shared shape is not a shared meaning (#52).

- Requivo Web has a deliberate visual direction. Colour now encodes **state and nothing else** —
  emerald for what is known, amber for what is being assumed, slate for what is open — and the
  primary action carries a blue the triad never uses, so an action can no longer be mistaken for a
  grade. The previous stylesheet spent one indigo accent on buttons, links, focus, the coverage bar
  and the "what changed" panel at once, which left the one distinction this interface exists to make
  as the one its colour system said least about (#64).
- Evidence grade is legible without colour. Each of the three states now has its own mark **shape** in
  the per-topic list — a filled square, a rotated diamond, a hollow circle — in addition to its hue.
  Three coloured dots of identical shape were one state on a monochrome print and to a reader with a
  colour vision deficiency (#64).
- Every foreground/background pair is measured against WCAG AA in both light and dark, and UI
  boundaries against the 3:1 required of a control. Three values were wrong and are corrected: the
  token carrying every label, hint and section caption sat at 3.80:1 on the page, links at 4.48:1, and
  an input border at 1.52:1 against its own field — with a white field on a tinted page, that border
  is the only thing saying where the field is. Control edges now have their own token, held apart from
  the decorative rules they were sharing (#64).
- A session screen states where it is before it is scrolled: readiness, the count of open questions and
  whether a saved document needs updating now sit beside the title. Every value there is already
  computed and already stated in full further down — it is a summary of the page, never a second
  source for it (#64).
- The keyboard focus ring is no longer suppressed on form fields. `outline: none` on `:focus`
  out-specified the global `:focus-visible` rule, which removed the indicator from every input,
  textarea and select on the surface (#64).
- Two rules that had been silently dead since before this change: `ul.clean > li` was declared after
  `.session-list > li` and `ul.tight > li` at equal specificity and overrode both, so neither list ever
  got the spacing it declared. Scoped rather than reordered, so a later addition cannot re-break them
  (#64).
- Nothing persisted, generated or emitted by `--json` changes, and no user-facing term changes:
  `web/viewmodels/labels.py` remains the single definition of what a reader calls things (#64).

### Fixed

- `requivo artifact save` without `--revision` is now refused instead of being recorded as fresh.
  Omitting it used to mean *the session's current revision*, so freshness was computed against a
  revision nobody had claimed to read and the answer was `stale: false` every time — a source
  revision that *is* the current one cannot have moved. The number recorded was a real revision of a
  real session, so no reader downstream could tell the guess from a stated fact: `artifact list`,
  `session show`, `status --json` and the Web's *needs updating* panel all reported a superseded
  document current. Invariant 2 states the prohibition in those words — "never record `stale=False`
  because the caller didn't say otherwise" — and the default was the one thing violating it (#6).
- The refusal names what to pass and the revisions that exist, so the remedy is one flag. It is
  raised before anything is written, so a refused save leaves neither a file under `artifacts/` nor a
  status row in `session.json`.
- Compatibility: breaking - an `artifact save` that omitted `--revision` used to succeed and now
  exits 1 with a structured `unstated_source_revision` envelope (the refusal shipped here under
  `invalid_session`; #57 gave it its own code before either reached a release). Every documented
  invocation already passes it (`plugins/claude-code/REASONING.md`, the `prd` and `brief` skills,
  `docs/cli.md`), and both provider-backed paths in `DiscoveryService` always did, so nothing that
  follows the documentation changes behaviour. Only a caller relying on the undocumented default is
  affected, and that caller was being given a fabricated provenance. No session on disk changes shape
  and `format_version` stays 1.
- An artifact saved against a revision whose file is *present but unreadable* is refused cleanly
  rather than crashing. The guard that turns "I cannot establish freshness" into a refusal caught
  `RequivoError` only, which covers a *missing* revision file and nothing else — a truncated
  `revisions/NNNN-model.json` from an interrupted sync raised pydantic's `ValidationError`, a
  mis-encoded one raised `UnicodeDecodeError`, and a permissions or device fault raised `OSError`.
  None is a `RequivoError`, so the block never ran and a raw traceback came out of the service from
  inside the session lock, past the CLI's own handler. All three are caught now, and the failure's
  type and text are recorded in the error's `details.cause` (#6).
- Both refusals carry the same five `details` keys — `slug`, `type`, `source_revision`,
  `current_revision`, `cause`. They shared a code when this was written, which made the shape
  obligatory; #57 split the codes and the shape was kept anyway, because a key present on one payload
  and absent on the other is what a consumer following the documented advice (match the code, read
  the key) trips over. A test asserts the two key sets against each other rather than each on its own.

- One unreadable session no longer takes the whole listing down in Requivo Web. Invariant 15 — *a
  listing survives its own members* — was enforced one line below where it broke: `session_list`
  guarded `status()` per row, but the rows themselves came from a single-shot comprehension over
  `read_meta`, so a `session.json` this build cannot read raised before any row existed to degrade.
  The source of the rows is now guarded per member (#7).
- Two further ways the same page went down, both outside the old guard. `request_text` was outside the
  `try` entirely; and the `try` named `SessionNotFoundError` alone, so a `model.json` left truncated by
  a crash mid-write raised a pydantic `ValidationError` — not a `RequivoError`, so it missed that catch
  *and* the app's `RequivoError` handler and rendered as a 500 over the whole page. Measured per break
  mode against the unfixed code: a newer `format_version` gave 400, a truncated model 500, an
  unreadable `request.md` 500 — each on a page whose other sessions were all fine (#7).
- **The degraded row names the session.** Neither surface did before, so a user with one bad session
  could see that something was wrong and had no way to learn which — which is most of the cost. The row
  carries the underlying error text too, because *this session was written by a newer Requivo, upgrade*
  is a remedy and a flattened `unreadable` code is not (#7).
- The row states no fact it does not have: no timestamp, no question count, no freshness verdict. A
  plausible `0 open questions` on a session nobody managed to open is the quiet-wrong-answer form of
  the same bug. *Could not be read* and *not analysed yet* are two states and render differently (#7).
- The guard catches bare `Exception`, deliberately and with the reason recorded next to it. An
  aggregate's contract is that one member cannot take the view down, and the set of ways a member can
  be broken is open — naming a family is how a guard ends up nominally on and effectively off for the
  next failure mode, which is what #7 is. `doctor`'s `_session_health` had already made this call for
  the same question; this adopts it rather than re-litigating it (#7).
- Still outstanding, and reported rather than fixed: **`requivo session list` has the same duty and
  still has no guard.** It lives in `deterministic.py`, which was held by another change in the same
  round. The fix is one call — `SessionService.list_entries()` in place of `list_sessions()`, plus a
  degraded line naming the slug — and the service half it needs has shipped here (#7).

- Prompt caching no longer costs money on the verbs that could never benefit from it. The system
  prompt was sent with a `cache_control: ephemeral` breakpoint on every call, and a breakpoint bills
  the block at 1.25x input to write against 0.1x to read — so it only pays from the *second* send of a
  byte-identical prefix. `prd`, `criteria`, `epic`, `release`, `stories` and `estimate` each make one
  call, so each wrote a cache entry that nothing ever read: a flat ~25% surcharge on the largest part
  of the input, on every one of them (#9).
- The comment that justified it claimed the prompt was byte-identical "across the calls of a session".
  That is true across the calls of one *operation* — a golden capture's K runs, `converse()`'s turns,
  each JSON retry — and false across operations, because `build_prompt()` substitutes the shared
  schema and context cards into a **per-operation** template. Nothing failed and nothing warned; the
  rendered cost was correct throughout and simply read as normal, which is why it survived (#9).
- Moving the breakpoint or spending more of the API's four on it would not have helped, and this is
  worth writing down so it is not re-attempted: all eight templates place `{{SCHEMA}}`/`{{CONTEXT}}`
  near their end with an *Output format* section after them, so the shared bulk is a **suffix**.
  Caching is a prefix match, and a suffix has no prefix boundary to cache at (#9).
- Which calls get a breakpoint is now the caller's declaration (`reuse_system`) rather than a constant,
  because the same function is single-call in one caller and multi-call in another: `requivo brief`
  calls `advise()` once, and `scripts/golden_run.py --brief` calls it K times off one prompt. The
  harness passes `reuse_system=True` and keeps its saving; discovery keeps the breakpoint unconditionally,
  since `converse()` re-sends that prompt for up to 8 turns. `_complete` still defaults to caching, so
  a caller that has not thought about it pays the safe answer rather than silently losing a real cache (#9).
- The accepted cost, stated rather than glossed: a one-call verb *can* send twice, when the model
  returns malformed JSON and the retry loop re-sends the identical prompt. Those retries are no longer
  cached, so a generator that retries pays 2.0x the system block where it used to pay 1.35x. Not
  caching is the better bet while a retry is rarer than about one call in four, and it is; caching only
  from the second attempt was considered and rejected, because it costs 2.25x on two attempts — worse
  than 2.0x — and only wins past the same threshold at which caching everywhere would have been right
  to begin with (#9).
- No prompt asset changed, so no engine behaviour changed and no golden-harness cycle was spent: the
  system prompt sent is byte-for-byte what it was, and a test pins that. Making the cache pay *across*
  operations means moving the shared bulk to the front of all eight templates, which is a real change
  to what the model reads and is deliberately left for its own measured pull request (#9).

- Requivo now reads and writes text as UTF-8 everywhere, so a session written on one machine reads
  back byte-identically on another whatever the locale. 29 call sites — 28 reads, plus one write in
  the golden harness — took the platform default instead: UTF-8 on macOS and Linux, cp1252 on
  Windows. A French request round-tripped into mojibake that was still valid JSON, so nothing failed
  and the PRD shipped it (#11).
- `requivo session verify` no longer accuses you of editing a file nobody touched. It recomputed the
  hash from a mis-decoded string, so on Windows every session containing an accent or an em dash
  reported `revision_hash_mismatch` and `session import` refused a perfectly good archive on the same
  evidence (#11).
- `requivo discover`, `demo`, `schema` and `context` no longer die with a raw `UnicodeDecodeError`
  before doing anything. 20 of the bundled assets are not pure ASCII, so on an ASCII locale the
  primary verb could not start at all — observed, not reasoned:
  `LC_ALL=C LANG=C PYTHONUTF8=0 requivo schema` reproduced it on macOS (#11).
- Worth stating precisely, because the issue's own inventory had it the other way round: only 2 of
  those 20 are undecodable as cp1252. The other 18 decode *successfully* into mojibake, so on Windows
  the usual outcome was never a crash — it was a prompt quietly assembled from corrupted product
  context and shipped to the model, billed, looking like it had worked (#11).
- A file *you* name (`requivo discover ./brief.md`) is now refused by name if it is not UTF-8 —
  naming the offending byte and its position — instead of being decoded with the locale's codec into
  text that reads like prose and is wrong. Refusing is the point: mojibake validates (#11).
- A path or slug you supply can no longer forge a line of Requivo's own output. Six error messages
  in `deterministic.py` interpolated one raw — `no such file:`, `archive not found:`, the unreadable
  `.zip` message and three `no canonical session` messages — so a name carrying a newline and an ANSI
  escape could write what reads as a second, authoritative line at column 0. That is #40's class,
  found again by this branch's own guard test after the fix for #11 reintroduced it in a message it
  had just added. All six now go through `display_token`, the helper #40 produced, which is a no-op
  for any ordinary value (#11).
- `tests/test_encoding.py` is the guard that keeps it fixed. Passing `encoding=` at 29 call sites
  leaves the 30th written next week, and this repo has already watched that happen twice, so the
  check is a walk over `src/` and `scripts/` that fails on a bare `read_text()` — and refuses to
  answer at all when its scan set comes back empty (#11).

- A `model.json` written by a **newer** Requivo now loads, and keeps the field it added. Only
  `session.json` was forward-compatible; `model.json` and every `revisions/NNNN-model.json` were read
  through `EngineOutput`, which inherits `extra="forbid"` from the LLM boundary contract — so a key
  added by a later version (which `docs/compatibility.md` explicitly permits without a
  `format_version` bump) made the session unopenable, as a raw Pydantic `ValidationError` rather than
  as anything the surface could phrase. The documented promise and the code disagreed, and the
  document was the half that was right (#14).
- The two rules that collided are now two contracts. `StrictModel` and everything an LLM fills stays
  `extra="forbid"`: a field the model invented must still fail loudly and ride the JSON retry loop,
  because a dropped key makes a drifted prompt read as a clean success. The read path goes through
  `PersistedEngineOutput` instead — a subclass of `EngineOutput`, permissive at every level of the
  model tree, so nothing downstream changes type and every validator the strict tree carries still
  runs. What differs is what an unknown key is evidence *of*: from a provider something is wrong now
  and there is a retry that can fix it; from disk something is newer, there is no retry, and refusing
  costs a session that reads perfectly well (#14).
- `requivo doctor` and `requivo session verify` agree with the loader about the same file. They
  validated `model.json` through the strict contract too, so once the loader carried a newer version's
  field the checker would have reported `invalid_model` on a session that opens fine — a health
  verdict measured against a rule the code no longer follows. Both model checks are permissive now,
  and a model that is actually malformed still reports `invalid_model` / `invalid_revision_model` (#14).
- Reading permissively was only half of it, and the half on its own would have been worse than the
  bug. `ModelProposal.resolve` carries an unstated reasoning collection forward from the model being
  refined (invariant 10), so a decision loaded from a newer Requivo ended up under the *strict*
  tree's annotation — and pydantic serializes by the annotated type, so the unknown key stayed alive
  in memory and disappeared on the very next write. A key at the top level went the same way, since
  a proposal is `extra="forbid"` and cannot speak to a field it has never heard of. Either would
  have converted a refusal you could see into a silent loss on the first ordinary turn. Both are
  fixed in `resolve`, and a test drives a real refinement turn through `SessionService.update_model`
  rather than a re-save, because a re-save never reaches the code that dropped it (#14).
- The question cap is one number again. `ModelProposal` and its persisted mirror each carried a
  hand-written `max_length=6`, and nothing made them agree — a duplication introduced by the mirror
  itself, and the same defect class this change exists to remove, one field along. It fails
  asymmetrically and needs no version skew at all: raise the strict cap, miss the mirror, and a
  session *this build just wrote* with seven questions no longer loads. Both now read
  `MAX_QUESTIONS`, and a second field-graph guard compares the *constraints* of every field the
  mirror restates against the strict tree's, because the existing walk compares `extra` policy and
  cannot see this. The mirror cannot inherit its way out: pydantic drops the parent's `FieldInfo`
  when a subclass re-annotates, so leaving `Field(...)` off would lose the cap and the default and
  quietly make `questions` required — which is why the property is pinned rather than tidied (#14).
- Compatibility: compatible. Nothing that loaded before stops loading, no stored key changes meaning,
  and `format_version` stays 1 — this only widens what a reader accepts and what a writer keeps.
  Two limits are stated rather than left to be found: an apply **replaces** the slots, the summary
  and the questions, so an unknown key inside one of those is superseded by a value this version
  built; and an unknown **slot id** is still refused, because that is `schema_version`'s frontier and
  it already refuses a newer slot schema with a message naming the upgrade.

- Taking the session lock on a slug that has no session no longer creates one. `session_lock` called
  `mkdir(parents=True, exist_ok=True)` on the session directory before opening `.lock` inside it, so a
  lock taken on a name nothing had created left a directory behind holding only that lock file. It is
  invisible to `session list` (no `session.json`) and it is not empty, so `create_session`'s atomic
  rename — the one claim on a slug — lost to a session nobody had made, and reported **session already
  exists** about one neither the reader nor the tool could see or list (#22).
- Which callers reached the lock without a session, stated narrowly because it is narrower than it
  looks. `save_revision` and `save_session_artifact` in `requivo.core.persistence` take the lock
  before the metadata read that raises `session_not_found`, and so does `ArtifactService.mark_stale`,
  which has no preceding existence check. The CLI verbs are **not** among them: `model apply`,
  `artifact save` and `session export` each establish the session exists before the lock is taken.
  What was exposed is the layer underneath them — the one an external consumer calls directly (#22).
- What a leftover directory then did to the CLI is quieter than a refusal, and worth knowing if you
  have one on disk from a previous version. `requivo session init --slug later` does not report the
  clash: `SessionService.create_session` falls back to a hash-suffixed candidate when a slug is taken,
  so it silently creates `later-<hash>` instead. The name you asked for is simply gone, with nothing
  said. The `session_exists` refusal is what a direct `create_session` and `session migrate` see (#22).
- **A guard that creates the thing it guards is a second producer.** Creating a session is one atomic
  claim on its slug — a staging directory renamed into place, which either wins the name or reports
  it taken — and that claim is only decidable while nothing else can make a directory of the same
  name. The lock now refuses a slug with no session rather than materialising one, so a failed or
  released lock leaves the store exactly as it found it (#22).
- Deleting the directory again on the way out was the other repair and is worse: unlinking a `.lock`
  another process is holding is legal on POSIX and silently breaks mutual exclusion, leaving the
  waiter holding a lock on an inode with no name. That trades a misreported refusal for a corrupted
  one, which is the wrong direction (#22).
- `requivo session migrate` was the case where this stopped being cosmetic. It claims its slug through
  the same rename, and the bulk sweep reports a refusal as `skipped_already_present` — so a legacy
  session that had never been migrated was reported as one that was already there. A skip reads as a
  decision, which is worse than an error (#22).
- Compatibility: compatible in what a caller is told, and one step earlier in when. Every path that
  locks before reading the metadata still raises `session_not_found` with the same message and the
  same HTTP status; it is now raised by the lock rather than by the `read_meta` immediately inside it.
  The one message that changes is `ArtifactService.mark_stale` on a session that does not exist, which
  said *has no model yet* and now says there is no such session — the accurate of the two. A session
  that exists is untouched: the lock is still taken, still re-entrant within a thread, still exclusive
  across processes, and `.lock` still lives inside the session it locks (#22).

- A character your console cannot display no longer kills the command that was printing it. Requivo
  configures stdout and stderr once at startup so an unrepresentable glyph is escaped rather than
  raising `UnicodeEncodeError` — and escaped rather than dropped, because a reader cannot tell a
  substituted character from one that was never there (#29).
- The ordering is what made this worth fixing rather than a cosmetic complaint: the crash happened at
  the `print`, *after* the work that print was reporting had already landed. `requivo brief <slug>`
  completed its paid provider call, applied the revision and wrote the artifact, then died in the
  renderer — so the exit code described a crash, and re-running paid for a second call and stacked a
  second revision on the first (#29).
- `requivo doctor` was the worst of them: it died on the check mark of its very first line, having
  already computed the whole diagnosis it exists to report. It now also *reports* your console's
  encoding, with `lossy`, `will-crash` and `unknown` as distinct answers from `safe`, so a stream
  Requivo could not configure is a line you can read rather than an absence (#29).
- `lossy` is a separate verdict from `safe` on purpose. A console set to `errors=replace` or `ignore`
  cannot crash, but it drops the character with no mark — and reporting that as safe would have
  `doctor` endorse the exact quiet hole this fix exists to avoid (#29).
- The API usage line no longer kills a run that already paid for its call. `render_usage` prints a
  middle dot and an em dash, and two of its three call sites sat outside the guard — including the
  one that runs after a *wholly successful* command — so on an unreachable stream a successful
  `requivo brief` still died there, after the provider call was billed and the revision applied.
  It now degrades to a stated absence, which is deliberately not silence: a usage line nobody can
  read is a different thing from a run that made no calls (#29).
- The message printed in that case reads the run's usage ledger instead of asserting. It says a call
  **has** been billed only when one actually was — several verbs never call the provider at all, and
  telling you not to re-run a command that cost nothing would be the same misreport one layer up
  (#29).
- Where a stream cannot be made safe at all, the command exits **3** — a new code meaning *the work
  succeeded and you cannot see the output* — instead of a traceback. Exit 1 would have been a lie in
  the one case that costs money (#29).
- Exercised on every CI platform rather than only where the bug bites: the tests spawn subprocesses
  under `PYTHONIOENCODING=ascii`, which reaches a real console encoder. The previous suite captured to
  `io.StringIO` and so could never have caught this even with a Windows leg (#29).

- A refused submission in Requivo Web no longer costs you what you typed. Refusing an over-long
  request is correct and is unchanged — half a request folded into the model reads exactly like a whole
  one — but the refusal was a full-page error whose only affordance was *Back to sessions*, so a
  26,000-character client email that arrived through the clipboard had to be fetched again from
  wherever it came from. Every refusal on the request form now re-renders the page with the submission
  still in it (#30).
- The answers box was the worse of the two, and for a reason the issue names: it posts as an HTMX swap
  over `#session-body`, the region that *contains* the textarea, so the error fragment did not merely
  fail to preserve the text — it deleted the field the text was typed into, with no Back to return to.
  The whole region now comes back with the answers still in it and the refusal stated on the form
  (#30).
- Four refusals on that page round-trip, not one: the request textarea, both of the session-name
  field's refusals (too long, and not a usable slug), and the answers textarea. Leaving one of a single
  field's two refusals keeping your work and the other throwing it away is a worse state than either,
  because which one you hit is not something you can predict (#30).
- The context-card selection and the *On submit* choice survive a refusal too. A session's identity is
  its request **and** its card selection — the impact estimates are read against them — so handing back
  the textarea while silently clearing the checkboxes would return a form that no longer says what you
  told it (#30).
- Compatibility: compatible. These refusals keep their HTTP status (413 for a length, 400 for an
  unusable name) and their error code, which now rides the banner on the re-rendered form instead of a
  full error page. An unknown context card is deliberately **not** in this set and still raises: those
  boxes are checkboxes over a set the page itself rendered, so an unknown value did not come from a
  reader mistyping something they could correct on a re-render (#30).
- Narrowed from the issue as filed, on the issue's own second comment: the refusal already named the
  limit, and adding the submitted length was judged not to be what this issue is for. What was lost was
  the text, and that is what this restores (#30).
- **Follow-up on the above.** A refused submission no longer fills in a session name the reader never
  typed. `create_session` used one variable for two meanings — the string the reader put in the box,
  and the argument the service takes, where `None` means *derive a slug from the request*. An empty box
  collapsed to `None` before the empty-request arm was reached, and the re-render stringified it, so
  the reader got `value="None"` in a field they had not touched. It also fails that field's own
  `pattern`, so it had to be noticed and cleared before the form could be resubmitted: the refusal path
  built to stop costing the reader work had started adding some. The two meanings now have two names
  (#30).
- Found by review rather than by the tests, and the gap is worth naming: every case covering the
  preserved-input path submitted a session name, so none of them could see a refusal that invents one.
  The regression test is a matrix over each refusal paired with a **blank** name field (#30).

- The two places that still built an artifact path by hand now go through the chokepoint the rest of
  the store goes through (#36). `requivo artifact save` and the line every generator verb prints to say
  where its document went each re-joined `canonical_dir(slug) / "artifacts" / <recorded filename>`
  inline, so the guard #5 put on the writes and #23 extended to the read was closed in three places and
  open in two.
- Neither of the two was exploitable, and that is stated rather than assumed: both only *print* the
  path, and in both the filename reached them from the fixed `ARTIFACT_FILENAMES` table by way of a call
  that had already validated it. What is fixed is the inconsistency — the next person reading
  `deterministic.py` learned the wrong pattern from a file that is otherwise correct (#36).
- Display-only is not the same as harmless, and the reasoning now lives at `artifact_path()` rather
  than being rediscovered a fourth time. A read traversal answers what this code may *disclose* rather
  than what it may create, and a printed path is the plainest disclosure there is; the filename on both
  lines is a plain string off `session.json` that nothing re-validates when it is read back (#36).
- Which door is open is now stated rather than borrowed, because the obvious sentence is wrong.
  Invariant 14 argues that a persisted value is untrusted every time it is read, and it argues it about
  the context cards, which `session import` deliberately cannot resolve. That does not carry over to the
  artifact filename: import pins each one to its known value and to containment and refuses the whole
  archive otherwise — reproduced, for a traversal and for a merely wrong name. The route that is open is
  invariant 14's own, a consumer holding the services over a store that is not this one, which is what
  the tests drive (#36).
- Absence and refusal stay the two different answers #23 made them. A session with nothing generated is
  not newly an error — a name that is not a filename raises `invalid_filename`, and every legitimate one
  prints exactly the path it printed before (#36).
- Compatibility: compatible. No output changed for any name the store can actually write, and both
  lines are covered by a test that pins the legitimate path as well as the refusal — a guard that
  refused everything would satisfy the refusal half on its own (#36).

- Creating a session on an install with **no context cards at all** now says so, instead of blaming
  the name you typed. `resolve_cards` — the validator the CLI, the deterministic verbs, Requivo Web
  and `SessionService.create_session` all run on the way in — was the one card selector the
  empty-install guard was never wired into, so with nothing installed it answered *unknown context
  card(s): pricing. Available:* (an empty list) while the very next call answered *no context cards
  are installed … this install is incomplete*. One condition, two verdicts, and the one sending you
  to check spelling you had got right arrived first — at session creation, which is the first thing
  a fresh install does (#41).
- The three selectors that resolve a card name against the installed vocabulary — `resolve_cards`,
  `load_context` and `check_selection` — now share one guarded read of the card table rather than
  each remembering to call the guard. The original miss had a mechanism: the other two reach the
  table directly and this one reached it through `available_cards()`, so a sweep over the callers of
  the guarded function found two of three and looked complete. `available_cards()` stays deliberately
  outside the guard, because `doctor` reports an empty install as one of its three states and can
  only do that by observing the emptiness rather than raising on it (#41).
- Compatibility: breaking - one condition moves to a different code and across the 4xx/5xx line in
  Requivo Web. `POST /sessions`
  naming a card on a card-less install used to answer `400 unknown_context_card` and now answers
  `500 no_context_cards`. That is the correct side: nothing the caller sent caused an install to
  ship without cards, and the old 400 was the misattribution #34 fixed, one call earlier. Nothing
  changes on an install that has cards, where an unknown name is still `unknown_context_card` and a
  400; and passing no selection at all is untouched, since that is not a selection to validate.
- The unknown-card refusal now lists the available cards from the same read its lookup used. The
  vocabulary you were told to choose from was enumerated by a second `available_cards()` call, so it
  was not guaranteed to be the one your name had just been matched against (#41).

- Requivo Web accepts a form posted from `localhost` to `127.0.0.1` (and every other pairing of the
  three loopback spellings). The cross-site guard compared the two hostnames as strings while its own
  host allowlist treated them as one machine, so the request form returned *this request came from
  another origin* and could not be submitted at all — and switching the address bar to the other
  spelling resubmitted the stale `Origin` and reproduced the identical error, leaving no way forward
  (#43).
- A host you listed in `REQUIVO_WEB_ALLOWED_HOSTS` is deliberately **not** part of that equivalence:
  two real hostnames there must still match each other exactly, because whether they are one trust
  domain is the operator's call rather than something inferred from one comma-separated list.
- Two hostnames that could not be determined at all no longer read as a match. `""` is what the
  parser returns for an absent or unparseable `Host`, or for an origin such as `http:///` that names
  nobody, and two of those compared equal — so the one input where neither side was known produced the
  same verdict as a verified match. No browser can produce it and the request token gated it either
  way, but a check that could not look must say so rather than answer. Found by the audit on the #43
  fix, in the helper that fix introduces.
- `Origin: null` stays refused, now on purpose and with the reason in the code rather than as a
  side effect of parsing the literal string `"null"` into a hostname. An absent origin header stays
  accepted — a browser attaches `Origin` to every POST, so silence means a scripted client, which the
  request token gates. That token remains the load-bearing check for both, and `evil.example` posting
  to a loopback host is still refused (#43).

- Requivo Web refuses a request that does not say which host it was addressed to, instead of skipping
  its host check for that request. The check read `if host and host not in allowed_hosts()`, and the
  parser returns an empty string when it cannot determine a host at all — so an absent or empty `Host`
  header was treated as *no host check needed* rather than as a refusal. That check is the
  DNS-rebinding guard and the only one that also runs on reads, so it was nominally on, effectively
  off, and silent about it. Observed at the socket, not reasoned: `GET / HTTP/1.0` with no `Host`, and
  `GET / HTTP/1.1` with an empty one, both answered 200 (#45).
- Compatibility: breaking - an HTTP/1.0 client that sends no `Host` header now gets a 403 where it
  previously got a response. This is deliberate. HTTP/1.1 requires a `Host`, every browser and every ordinary client
  (`curl`, httpx, requests) sends one, and nothing in Requivo has ever documented HTTP/1.0 support.
  The browser path is unaffected (#45).
- The refusal names its own arm — *this request did not state which host it was addressed to* — rather
  than reusing the wording of a genuine host mismatch. A guard that could not read its input must not
  print what a guard that read it and refused prints; the same correction the opaque-origin arm got in
  #43, one seam over (#45).

- The origin check's stated rationale in `web/security.py` was false, and is corrected. It claimed a
  page at `http://localhost:8765` *"can only have been served by this process — nothing else is
  listening there"*, while the code it justifies discards the port on both sides: the set actually
  accepted is any page served by any process on any loopback port, which on a developer machine is a
  populated one. No behaviour changed — this is a prose fix, and the port-blindness it describes
  predates the loopback-spelling fix in #43 rather than following from it (#46).
- That port-blindness is now written down as a decision rather than left implicit, with its reason:
  the per-process request token is what gates a write, and a page on another loopback port cannot
  obtain one, because the browser's own same-origin policy counts the port and Requivo Web sends no
  CORS headers. Comparing ports in this check would add nothing and would reintroduce exactly the
  false positive #43 fixed, since a default port is elided in an `Origin` but spelled out in a `Host`.
  A test pins the behaviour, so tightening it later has to argue with the rationale instead of
  slipping past it (#46).

- Requivo Web's `Referrer-Policy` is `same-origin` rather than `no-referrer`, which is what made the
  product's entry path unusable in a browser. Under `no-referrer` a browser attaches `Origin: null` —
  the opaque origin — to an ordinary form post, and the cross-site guard refuses that deliberately
  (#43), so creating a session and running discovery answered 403 to a same-origin request carrying a
  valid request token. Both halves were individually correct and individually green; the defect existed
  only in their composition, which is why no per-file test could see it and none did (#47).
- Scope of the failure, narrowed from the report: only the two **plain** form posts were affected —
  *create a session* on the home page and *Analyse request* on a pending session. The HTMX posts
  (answers, generation) travel as XHR, which is CORS-mode, and Fetch consults the referrer policy for
  the `Origin` header only on requests that are *not* CORS-mode. So what broke was precisely the way
  into the product, while everything downstream of it kept working — which is why it read as one broken
  form rather than a broken app (#47).
- Why `same-origin` and not the alternatives, since this replaces a privacy decision with another one:
  it is the strictest value that still leaves the guard an origin to read. Cross-origin destinations
  get nothing at all, exactly as under `no-referrer`, so the whole privacy intent is kept for the only
  case where it ever did anything — navigating away. `strict-origin-when-cross-origin`, the browser
  default, also fixes the bug and hands a third party `http://localhost:8765` on an outbound
  navigation; for a local tool that is a gratuitous disclosure buying nothing. Dropping the header
  entirely would defer to whatever the browser defaults to, and this app states its headers. The cost
  of `same-origin` is that a same-origin `Referer` now carries the full URL, session slug included —
  the reader's own request name, travelling to the server that already holds it (#47).
- Worth recording because it inverts the intuition: a `Referrer-Policy` governs the requests *this
  app's own pages* make. A request some other page sends here is governed by that page's policy, not
  by ours. So this header was never part of the cross-site guard's defence — it could only ever
  constrain us, and it did (#47).
- The guard's refusal of `null` is unchanged and is still the right call. Accepting the opaque origin
  would have made this defect invisible rather than fixing it: the header was the thing that was wrong,
  and a guard loosened to tolerate it would have swallowed the evidence (#43, #47).
- A test now asserts the **composition** rather than either half — the policy the app really emits, fed
  through the Fetch rule for a same-origin form post, and then through the real guard. It carries its
  own limits in writing: neither `TestClient` nor `curl` implements a referrer policy, so the browser's
  half is modelled from the specification rather than executed. An end-to-end check in a real browser
  engine remains the missing coverage, and is the reason this shipped (#47).

- A session that has converged can still be refined. The answer form used to live inside the
  `{% if s.questions %}` arm of the session template, so when the engine returned no question — the
  state the home page presents as *Ready for a first decision brief* — the form disappeared along with
  the question list, and there was no way left to send the model a correction, a constraint that
  arrived late, or scope the client added afterwards. The form is now unconditional and the question
  list is what varies (#49).
- Nothing downstream ever required a question: `DiscoveryService.answer()` takes free text and folds it
  in through the same validated path as any other turn, and has never read the question list. The
  coupling was entirely presentational, which is what made it easy to ship and hard to notice (#49).
- With no questions the section reframes to *Anything to add?* and keeps the notice, so the reader is
  told the engine has nothing further to ask **and** given the box. The engine converging is an answer
  about which questions are worth asking; it is never an answer about what the reader still has to say,
  and collapsing the two presented the product's success state as the end of the conversation (#49).

- One provider call at a time in Requivo Web, enforced page-wide. Every generator under *More
  documents* is its own form posting to the same `#artifacts-region`, and all of them stayed clickable
  while a generation was already running, so a reader could start five. Each is a paid call; at most one
  result ever reached the page, and which one was not the reader's choice. While any request is in
  flight every submit button on the page is now muted, and the count is a counter rather than a flag, so
  the first response finishing does not hand back live buttons while a second call is still running
  (#50).
- The previous behaviour disabled only the submitting form's own button, which left every sibling live.
  That is now reproduced mechanically rather than described: a small DOM harness executes the real
  `static/js/app.js` and the shipped asset fails it with `disabled=[True, False, False]` — the clicked
  button muted, its two siblings ready to buy another call (#50).
- The busy state is re-asserted after each swap, because markup HTMX swaps in carries no `disabled`
  attribute of its own. A bfcache restore now clears the count too; previously a page returned to with
  Back kept a button disabled permanently, which the original report did not mention and the harness
  found (#50).
- **Correction to the mechanism recorded in the issue, which was reasoned rather than measured.** The
  issue states that HTMX 1.9 resolves `hx-target` through the issuing element's root node, so a form
  detached by an earlier swap drops its response silently. Read against the vendored HTMX 1.9.12, that
  is wrong twice. `hx-target` is resolved **once, at request-issue time**, and cached on the request
  context; `getRootNode` appears in that build only inside a shadow-DOM containment helper and is never
  consulted for targeting. And the element whose detachment matters is the **target**, not the form —
  the second response's cached target node is the `#artifacts-region` the first swap replaced, so the
  `outerHTML` swap reads `parentElement` on a detached node, gets `null`, and throws. It fires
  `htmx:swapError` and rethrows, so it is loud in the console and silent only on the page. The
  distinction is not pedantic: under the issue's mechanism, moving the forms outside the swapped region
  would have fixed this, and it would not have (#50).
- Adjacent, folded in because it is the same toolbar and the same symptom: the *Generating…* label was
  dead markup. It is a sibling of the generator forms, and HTMX's default indicator is the requesting
  element, so `.htmx-request .spinner` never matched it. `hx-indicator="closest .toolbar"` puts the
  class on the toolbar that contains both. Verified against the vendored build rather than assumed —
  when `hx-indicator` is present HTMX marks those elements *instead of* the requesting one, which
  nothing here depended on (#50).

- `_hostname` in Requivo Web's cross-site guard now refuses an authority it cannot determine a host
  from, instead of answering about it. `Host: evil.com@127.0.0.1` resolved to `127.0.0.1` and passed
  the allowlist, because `urlsplit` is a URL parser and correctly discards userinfo; `Origin:
  http://evil.com@127.0.0.1` came out same-trust-domain the same way (#51).
- The same fix refuses `Host: 127.0.0.1 evil.com`, which previously came back as that entire string —
  not a hostname, and refused only by happening to miss the allowlist. A parser that returns a non-host
  and leaves a later equality test to reject it is answering where it should be declining (#51).
- Not reachable from a browser, and fixed anyway. No browser serializes userinfo into a `Host`, an
  `Origin` or a `Referer`, and RFC 7231 requires a `Referer` to have it removed — so this closes a hole
  with no attacker who benefits. What earns the fix is that it is the **third** time this module's
  parser answered confidently about an input it should have refused: #43 was the opaque origin parsing
  to the plausible hostname `"null"`, #45 was an undetermined host read as *no host check needed*, and
  this is the same shape again (#51).
- **The refusal is the parser's, not each caller's.** The first two instances were closed with
  caller-side checks, which is a guarantee the next caller inherits without re-checking — and
  `_hostname` already has two callers, on two different headers (#51).
- Known residue, stated rather than implied: an **unbracketed** IPv6 literal (`Host: fe80::1`) still
  parses to `fe80` with the rest read as a port. It is malformed as an authority, no browser emits it,
  and it fails the allowlist — but the parser does answer, so the docstring does not claim the class is
  empty (#51).
- Compatibility: compatible for any caller that sends a hostname. A `Host` or `Origin` carrying
  userinfo, or whitespace inside the authority, now gets a 403 where it previously got a response —
  which is the fix.

- `requivo artifact save` without `--revision` now reports **`unstated_source_revision`** instead of
  `invalid_session` (#57). The refusal added in #6 inherited its sibling's code, because a new code
  needs a row in `web/app.py::_STATUS_BY_CODE` — which
  `tests/web/test_web.py::test_every_error_code_has_an_explicit_http_status` requires of every code in
  the vocabulary — and that file was held by another change in the same round. So the precision lived
  in the exception *type*, which a caller reading a serialized envelope never sees: the one handle it
  had could not tell *you left a flag off* from *this session is broken*. It is a **400**, the same
  status the condition already answered.
- The `details` shape is unchanged and deliberately so (#57). Both refusals still carry all five of
  `{slug, type, source_revision, current_revision, cause}`, with `cause: null` on the unstated arm.
  Sharing it was owed while the code was shared; with the codes split it is a decision, and narrowing
  a payload for nothing would hand a `KeyError` to a consumer reading `details["cause"]` across both
  arms — the failure #35 measured. #52 settled the same question the same way: `opaque_origin` and
  `origin_mismatch` share a shape and are still two codes, because a shared shape is not a shared
  meaning.
- Compatibility: compatible - `invalid_session` never named this condition in a released version. #6
  is still unreleased, so #6 and #57 reach users in the same release and no consumer ever saw the old
  code here. Moving a condition to a new code *is* breaking under `docs/compatibility.md`'s own policy
  and is recorded there as such anyway, because the policy is about the condition rather than about
  who happened to be watching. `UnstatedSourceRevisionError` remains an `InvalidSessionError`
  subclass, so catching the class is unaffected in either direction. This is filed under Fixed rather
  than Changed, which is where #52 filed its code split, for that one reason: `cross_site_request` had
  shipped and this code had not.
- **`artifact save --revision`'s help text no longer advertises a default it does not have** (#57). It
  still read `(default: the session's current revision)` after #6 removed exactly that behaviour — so
  the text a user reads *while deciding whether to pass the flag* was recommending the fabricated
  provenance the refusal exists to stop. Two reviewers on the #6 branch found it independently. It now
  says the flag is required and why: the session's current revision is a different fact, and only the
  caller knows what they read. The flag stays optional to `argparse` on purpose — the omission has to
  arrive as a structured envelope a `--json` caller can parse, not as a usage error and exit 2.
- The rest of the surface was swept rather than assumed (#57). On the help-text half nothing else was
  wrong: `docs/cli.md`, `docs/session-format.md` and `plugins/claude-code/REASONING.md` already state
  that `--revision` is required, and no other option on `artifact save` claims a default. A test now
  reads the rendered help and fails on either form this repository writes a default in.
- The same sweep for the code found two places that named `invalid_session` for this condition and are
  corrected here (#57): `docs/session-format.md`, which now names both codes and says which fact each
  one carries, and the unreleased `changelog.d/6.fixed.md`, which would otherwise have folded the old
  code into `CHANGELOG.md` in the release that removes it. A stale code name in a changelog is worse
  than none — it is the string a consumer would have written their match against.

- `discover`, `answer` and the Web's discovery routes no longer pay a ~25% surcharge on the largest
  part of their input. Every reasoning turn through the provider seam was writing a prompt-cache
  entry — 1.25x input to write, 0.1x to read — that nothing ever read back, because each of those
  operations makes exactly one call. #9 removed this from the six generators and `estimate`; these
  were the two call sites it could not reach that round (#58).
- The breakpoint stays where it is genuinely re-read: `converse()`'s interactive loop sends the same
  engine prompt for up to 8 turns, and the golden harness sends it K times. Both now declare that at
  the call site rather than inheriting it, because it is a per-call-site fact — the same `run()` is
  single-call under the provider seam and multi-call under `converse()` (#58).

- One unreadable session no longer takes `requivo session list` down. Invariant 15 — *a listing
  survives its own members* — was fixed for Requivo Web in #7 and left undone on the CLI, whose
  `deterministic.py` was held by another change that round. A `session.json` written by a newer
  Requivo made the command exit 1 with a single message, **every other session invisible and nothing
  naming which one was the problem** — the exact failure the invariant is about, on the surface that
  did not get the fix. The listing now comes from `list_entries()`, which degrades per member (#62).
- The degraded row names the session and carries the reason, because for the commonest break mode the
  reason *is* the remedy: *this session was written by a newer Requivo, upgrade* is actionable where a
  flattened `unreadable` is not. `requivo session verify <slug>` remains where the full story lives,
  and a footer line points at it (#62).
- It states no fact it could not read — no revision, no provider, no timestamp. A plausible `rev 0` on
  a session nobody managed to open is the quiet-wrong-answer form of the same bug. A session genuinely
  at revision 0 is a normal row and still reads as one: *we could not look* and *we have not looked
  yet* are two answers (#62).
- **A new exit code, 4.** A listing that degraded a row is neither of its neighbours: `0` says nothing
  is wrong and `1` says nothing was listed. It is safe to make non-zero precisely because nothing is
  withheld — the complete listing is still on stdout, so a caller that only wants the rows gets all of
  them. Documented in the exit-code table in `docs/cli.md` (#62).
- Compatibility: compatible. `session list --json` gained `readable` and `error` on every row, which is
  additive; a consumer reading only `slug`, `revision`, `provider` and `updated_at` is unaffected on a
  workspace where every session loads. A **degraded** row keeps the same key set with `null` in the
  three facts it could not read, rather than a shortened dict that would turn `row["revision"]` into a
  `KeyError` on a payload handed over deliberately. Branch on `readable`. The previous behaviour on
  such a workspace was no payload at all and exit 1, so nothing that worked stops working (#62).
- **The issue's own table is corrected, measured rather than assumed.** #62 carried #7's three break
  modes and said all three applied unchanged. They do not: the web row calls `request_text` and
  `status()`, while the CLI row reads nothing but the metadata, so only the `read_meta` mode ever
  reached this command. The other two are pinned as controls asserting they *stay* out of this path —
  a future row that reads the request or the status needs the per-row guard the web viewmodel carries,
  and the control is what will say so (#62).
- The degraded row is a new render site for two pieces of untrusted text, and both go through
  `display_token` (#40). The slug there is the raw directory name — `read_meta` would have refused a
  non-kebab one, but that refusal is why the row is degraded, so it never ran. The error text can be a
  four-line pydantic `ValidationError`, which printed raw turns one session into four rows of listing
  with no way to tell where the row ends (#62).
- **A `session.json` could forge a row of `session list`'s own output, and no longer can.** Found by
  the audit on this branch, and outside the issue as filed. The readable row printed `slug`, `provider`
  and `updated_at` unescaped, and all three are read straight out of the file's body — `read_meta`
  validates the slug it is *called with*, the directory name, and then returns `SessionMeta.slug`, a
  bare `str` with no pattern, from the JSON. Nothing checks the two agree outside `session import`.
  A hand-edited or imported `session.json` whose `slug` carried a newline printed a second, entirely
  fabricated row — `rev 999 (trusted, …)` — into the listing, and the command exited 0. This is
  invariant 14's second door, the same shape as the stored context-card name in #40. All three fields
  now go through `display_token`; a clean value is returned byte-for-byte, so no real session's row
  changes (#62).
- Reported rather than fixed, deliberately: **`requivo session show` has the identical defect in five
  fields** — `session_id`, `created_at`, `updated_at`, `provider` and `model_name` all reach column 0
  unescaped, measured the same way. It is a different verb needing its own tests and its own review,
  so it wants its own change rather than a rider on this one. `--json` is unaffected on both verbs:
  `json.dumps` defaults to `ensure_ascii=True`, which escapes a control character before it can reach
  a line of its own, and there is now a test pinning that this default is load-bearing (#62).

- `requivo doctor` now names what is under `.requivo/sessions/` and is **not** a session. Nothing
  could see one: `list_session_slugs` filters on `session.json`, `doctor` and `session verify` both
  reason over the slugs it returns, and `check_session` answers about a directory it is handed, which
  nobody could hand it a name for. #22 stopped `session_lock` producing these; it could not find the
  ones already on disk from before that fix (#67).
- **The consequence is printed, because it is the only symptom any of this ever had.** The name is
  taken, and `create_session`'s rename is the only claim on a slug (invariant 11) — it loses to
  anything already occupying the name, after which `SessionService` falls through to its
  hash-suffixed candidate. Ask for `leave-approval`, get `leave-approval-a1b2c3`, silently. A finding
  with no remedy is a line people learn to scroll past, so the row carries `[name taken]` and the
  mechanism is stated once beneath it (#67).
- **A report, not a repair.** Nothing is deleted, moved or rewritten, and no field states a
  conclusion — there is no `is_lock_ghost` anywhere. A directory holding only `.lock` is almost
  certainly a leftover lock and *almost certainly* is not a licence to act: a half-extracted archive
  and an interrupted copy are the same shape from outside, and this project's rule is that the
  evidence is the directory and only the directory (invariant 14). Each entry reports `name`, `kind`,
  the first five `entries` it holds and the true `entry_count`. The one derived value, `slug_shaped`,
  is a property of the *name* — whether `create_session` can be asked for it, and so whether the
  entry costs anybody anything — not a guess at where it came from (#67).
- Three things the review on this branch found and are fixed here. **A symlink is no longer reported
  as whatever it points at**: `Path.is_dir()` follows one, so a link at a slug name answered
  `directory` and then listed the *target's* filenames into a report about your workspace. It is
  `kind: "symlink"` now and is not followed. **`slug_shaped` asked the slug pattern alone**, and
  validity is the pattern *and* the length — an 81-character kebab-case name was marked as one a
  session would silently lose, when `canonical_dir` refuses it outright and loudly; it goes through
  `is_slug`, which calls `validate_slug`, so there is one rule rather than two. And **`doctor` takes
  one listing for both halves** (`scan_session_root`) rather than scanning twice: two scans are two
  instants, and a `session.json` landing between them put a name in *neither* answer — the invisible
  state this key exists to end, reintroduced by the key itself (#67).
- `_describe_non_session` never raises, and that is load-bearing rather than defensive. It runs inside
  the one `try` that also holds the session listing, so an exception escaping it discarded a session
  report that had already succeeded and told the reader the whole root was unlistable — a claim
  broader than what failed, invariant 15's shape one layer down. Both arms land in a state the entry
  already has (*could not stat* / *could not list*), so this is not a guard that cannot fire: on Linux
  a filename that is not valid UTF-8 arrives carrying surrogates, and APFS refuses such a name, so it
  could not be constructed here to be ruled out either way (#67).
- **A stray file at a slug name costs exactly the same**, found by sweeping the class rather than
  taking the issue's word for the instance: `rename` onto an existing file fails too, `d.exists()` is
  true, and the caller gets the identical substitution. Reporting only directories would have left an
  identical symptom with an identical remedy invisible, so each entry says what kind of thing it is
  instead of the report assuming they are all directories (#67).
- Three states, at both levels. `sessions.non_sessions` is `null` — never `[]` — when the session
  root could not be listed at all, matching what `sessions.total` already does, because an empty list
  there reads as *we looked and there is nothing else*. Within an entry, `entries: null` with an
  `error` is a directory we could not look inside, which must not render like an empty one — on POSIX
  an empty directory is the single shape that costs nothing at all, because `rename(2)` replaces an
  empty destination. Windows differs (`os.rename` refuses any existing destination), which is why an
  empty directory is still reported and still marked `[name taken]` (#67).
- `doctor` owns this rather than `session verify`, which is per-session and takes a slug — and the
  defining property of one of these is that no listing produces its name, so there is no slug for
  anybody to type. It gets a row of its own rather than a note on the sessions row, because
  `0 in this workspace` stays true: none of this is a session, and folding it in would trade a
  correct count for a vague one. `requivo session list` is unchanged and still lists only sessions
  (#67).
- The listing lives in `core/persistence.py` beside `list_session_slugs`, which owns the store
  layout, and both halves now come out of one predicate over one `iterdir` — a name in neither is
  precisely the state this issue is about, so stating the rule twice is how it comes back. Core
  reading a directory crosses no boundary: invariant 7 forbids importing a provider and touching
  argv, the streams, the environment and process exit, not IO (#67).
- The entry name and the names it holds are read off disk and go through `display_token` (#40).
  Printed bare, one carrying a newline does not merely look odd — it ends the line and starts another
  at column 0 of `doctor`'s own report. `--json` is unaffected and keeps the bytes verbatim (#67).
- Compatibility: compatible. `doctor --json` gains `sessions.non_sessions`, which is additive and is
  `[]` on any workspace Requivo alone has written. No existing field changes meaning, nothing on disk
  is touched, and no new exit code is introduced — the finding is a row, not a failure (#67).

- **A `session.json` could forge a line of `requivo session show`'s own output, and no longer can.**
  The same defect #62 fixed in `session list`, in the other verb: `read_meta` validates the slug it
  is *called with* — the directory name — and then returns every other value straight out of the
  file's body, where they are bare `str` fields with no pattern. A hand-edited or imported
  `session.json` carrying a newline printed **sixteen** lines where eight were real, including its
  own `revision 999` and `provider trusted`, and the command exited 0 (#70).
- **It is eight fields, not the five the issue counted**, and the count is reported rather than made
  to come out right. #62 named the five that are `SessionMeta` scalars — `session_id`, `created_at`,
  `updated_at`, `provider`, `model_name`. It left out `slug`, which is the same bare string here that
  it was on the listing, and the two that are not `SessionMeta` fields at all: the **keys** of
  `artifact_status`, a `dict[str, …]` whose keys are whatever the file says, and each artifact's
  `filename`. `core/integrity.py` already treats that recorded filename as untrusted input, so a
  render site that did not was the exception making the rule unreliable (#70).
- This verb is the sharper of the pair. Every line it prints is one Requivo writes itself, at a fixed
  column, in a fixed shape — so a stored value can print `  revision 0` beneath a session that is at
  revision 12, and nothing in the render tells the two apart. On a listing a forged row at least has
  to imitate a row (#70).
- `current_revision`, an artifact's `revision` and its `stale` flag are deliberately **not** wrapped
  and are named here as such: they are typed `int`, `int` and `bool`, so `read_meta` refuses a string
  in them before the render runs. Wrapping them defensively would say the type bought nothing (#70).
- `session_id` is **sliced before it is escaped**. The other order truncates the escaped form, which
  can cut an escape sequence in half and leave the quote unclosed — a second defect bought with the
  fix for the first (#70).
- **`requivo artifact list` had the same defect and is fixed alongside it, outside the issue's own
  footprint.** Found by sweeping the class rather than the instance: it renders two of the same
  strings — the `artifact_status` key and the `filename` — off the same file at the same fixed
  column, and a forged entry printed a fabricated second artifact row while the command exited 0.
  One line, in the same file, over the same two fields, with the same test fixture. It is called out
  here rather than left to read as scope creep: escaping a stored value in one of the two verbs that
  render it leaves the rule meaning *wherever somebody happened to look* (#70).
- **The reason `--json` is safe is corrected, having been measured rather than repeated.** #62, this
  issue's own text and this repository's docs all said `json.dumps` defaults to `ensure_ascii=True`
  and therefore escapes a control character. That is not what protects a newline — JSON's grammar
  forbids a literal control character below `U+0020` inside a string, so `\n` is escaped whether the
  flag is on or off, and a test probing with a newline is green either way and pins nothing. The
  default **is** load-bearing, for the *non-ASCII* half of the guarded range, `U+007F`–`U+009F`:
  `NEL` (`U+0085`), a line terminator `str.splitlines()` and some terminals honour, and `CSI`
  (`U+009B`), an escape introducer `core/selectors.py` already names. The test now probes both
  halves and fails if the default is turned off to make accented output readable (#70).
- **Where the terminal guard stops is now written down and pinned, rather than assumed to coincide
  with `str.splitlines()`.** Found by the audit on this branch. `core/selectors.py`'s
  `_CONTROL_CHARS` is C0, DEL and C1 — *the class that can move a terminal's cursor or end its line*
  — while `str.splitlines()` also breaks on `U+2028` and `U+2029`, which `display_token` returns
  byte-for-byte. On a terminal that is the right answer (xterm and the VT sequences behind it answer
  to CR and LF, not to Unicode `Zl`/`Zp`), so this is not a forgery on the surface the guard covers;
  it matters to anything reading the human-readable output line by line, and `--json` covers those
  two as well and is therefore the stricter of the two paths. Widening `_CONTROL_CHARS` is
  deliberately **not** done here — it would also change what `normalize_tokens` refuses, i.e. the
  public `unsafe_selector_token` behaviour, which is a decision for that module's owner (#70).
- Compatibility: compatible. A value that is already one safe line comes back byte-for-byte, so no
  session Requivo itself wrote renders differently on any of the three verbs; such a value can only
  have arrived by `session import` or by hand. `--json` is unchanged and was never affected (#70).

## [0.10.0] - 2026-08-18

### Added

- `CONTRIBUTING.md` now states what the changelog gate does **not** cover (#37). The gate triggers on
  `pull_request` only, so direct pushes to `main` are never checked by it — and this is a
  solo-maintained repository whose own working style is to commit straight to `main`. Every release
  cycle so far has included commits that reached `main` without a pull request, so the uncovered
  class is routinely non-empty rather than theoretical. A green board
  therefore means *the changelog gate passed on the commits it was shown*, not that every change in
  the release carries a fragment; those are different claims and nothing on the board distinguished
  them.
- The limit is documented rather than closed, deliberately (#37). Adding a `push:` trigger on `main`
  would fail *after* the fact — a fragment cannot be added retroactively to a commit already pushed —
  which installs a permanently red default branch, a worse lie than the one it fixes. Abandoning
  direct-to-main would cost the workflow this repository chose on purpose. So the honest move is to
  say where the coverage stops, next to the list of checks a change is expected to pass.
- Compatibility: compatible - documentation only; no workflow, code or behaviour changes.
  `.github/workflows/oss-changelog.yml` is deliberately untouched, since it is scaffolded by the
  `oss` plugin and overwritten on every scaffold run — an edit there would be lost silently, which is
  the same class of defect as the one being documented.

### Fixed

- `requivo session migrate` can no longer overwrite a live session (#4). `migrate_legacy()` checked
  only that the *legacy* model existed, so pointed at a slug a real session already occupied it reset
  `session.json` to revision 0 and wrote the legacy model over `revisions/0001-model.json` —
  destroying revision 1 with no copy anywhere, and leaving the session failing its own integrity
  check. It now makes the same atomic claim on the slug that `create_session` does, and refuses with
  `session_exists` before writing anything; the whole migration runs under one session lock instead of
  three, so a concurrent apply cannot interleave with it.

- Artifact filenames are validated like the slug beside them (#5). `write_artifact_file()` and
  `save_session_artifact()` validated `slug` and not `filename`, so a caller passing
  `../../../x.md` wrote outside the session directory entirely — and, on the second of the two,
  persisted that path into `session.json`, where the integrity checker and the artifact-show paths
  read it back. Both now go through one chokepoint, `artifact_path()`, which applies the new
  `validate_filename()` and confirms the resolved path is a genuine child of `artifacts/`. A filename
  must be a bare lowercase name such as `prd.md`; anything else raises `invalid_filename`. No
  in-repo caller could reach this, and none is affected — every one passes a literal or an
  `ARTIFACT_FILENAMES` lookup.

- The request and answer boxes no longer clip a long paste before the server can refuse it (#8).
  Both textareas — and the optional session-name field — carried an HTML `maxlength` set to the same
  number as the server's ceiling, and a browser enforces that attribute by silently dropping the
  overflow: no event, no message, no visual difference. Pasting a 26,000-character email thread
  submitted the first 20,000 of it, which is exactly the length the server admits, so the refusal
  that exists precisely to stop half a request being reasoned over as if it were the whole thing
  could never fire from the browser. The attributes are gone; an over-long paste now submits in full
  and comes back refused, on a page naming the limit it exceeded. The ceilings themselves are
  unchanged, and input at exactly the ceiling is still accepted. **What that refusal costs you today:
  it is a full-page error that preserves none of the submitted text** — a 26,000-character email
  thread that arrived through the clipboard has to be fetched again from wherever it came from, and
  on the answers box the error fragment replaces the region containing the textarea. That is a
  strictly better position than the silent truncation it replaced, and it is not the finished one;
  re-rendering the page with the text intact is tracked in #30.

- The architectural boundary guard (`tests/test_boundaries.py`) no longer passes while scanning
  nothing (#10). `Path.glob` over a directory that does not exist returns an empty list and raises
  nothing, so both boundary tests asserted "no offenders" over an empty set and went green — the
  package has already been renamed once (`product_copilot` -> `requivo`) and the guard survived it by
  luck. The scan set is now asserted non-empty and named, an unscannable root is an error rather than
  an all-clear, and every negative assertion is paired with a fixture the guard must flag.
- The same guard now sees three things it was blind to (#10): relative imports (`from .anthropic
  import ...`, which it skipped outright on `node.level != 0`), dotted absolute imports
  (`import requivo.providers.anthropic`), and anything in a `core/<subpackage>/`, since the walk was
  not recursive.
- The guard reads source with an explicit `encoding="utf-8"` (#10). Every module in core carries an
  em dash, and `read_text()` with no encoding decodes with the locale codepage, so under `LC_ALL=C`
  or a DBCS Windows shell the guard died with `UnicodeDecodeError` rather than running. Its control
  forces the fallback encoding and, on the interpreters where that force cannot be made to take,
  skips loudly naming what went untested instead of passing.
- The half of the core boundary that nothing enforced is now enforced (#10): no `print`, `input` or
  `breakpoint`, no `sys.argv`/`stdout`/`stderr`/`stdin`/`exit`, no `os.environ`/`getenv`/`putenv`, and
  no terminal framework. File IO and `logging` stay allowed, and a test pins that so a later
  tightening which would fail correct code goes red first. Invariant 7 said core was "IO-free", which
  was never true — `persistence.py`, `context.py`, `contracts.py` and `analysis.py` all read files by
  design. Its wording, and the matching lines in `docs/architecture.md` and
  `docs/open-source-strategy.md`, now say what the rule actually means.

- `requivo doctor` no longer renders two of its own failures as green ticks (#12). A context-card
  loading failure was written into the *schema* check's error field, left `schema.ok` true, and was
  printed nowhere — so a wheel or container layer that ships `assets/` but loses `assets/context/`
  showed three green ticks while every impact estimate was made with no product context at all. And
  an unreadable `.requivo/sessions/` was caught and reported as `{"total": 0}`, byte-identical to a
  genuinely empty workspace, so twelve unreachable sessions read as "you have none" and a user
  concludes they were deleted. Both checks now carry a third state: `doctor --json` gains a
  `context` verdict (`status` is `ok`, `empty` or `unreadable`, alongside the existing
  `context_cards` list, which is unchanged), and `sessions` gains `readable`/`error` with `total`
  set to `null` — not `0` — when the directory could not be listed.
- `requivo doctor` and `requivo session verify` now check that a session's saved context cards still
  resolve. Since #13 an unresolvable card selection is refused rather than silently loading nothing,
  so a session that has lost a card is hard-stopped at its next (paid) reasoning turn — and both
  health verbs called it healthy right up to that moment. `session verify --json` gains a
  `context_cards` block (`checked`, `problem`, `error`) and `doctor --json` a
  `sessions.unresolved_cards` map, each naming the missing cards and how to recover.
- This is reported as an *environment* finding and deliberately not as a session-integrity problem:
  a context card lives outside the session directory, so the same session would be "broken" on one
  machine and coherent on another, and `session import` — which refuses an archive on integrity
  problems — would reject a colleague's perfectly good session over a card you do not happen to
  have.
- A context-card directory that exists but **cannot be read** is now reported as `context_unreadable`
  instead of reading as a directory holding no cards. `Path.glob` swallows `PermissionError` and
  yields nothing, so a denied directory was indistinguishable from an empty one: the card vocabulary
  came back quietly short, `doctor` said `ok` at a smaller count when a second root was readable, and
  a session naming a card in the denied directory was told `unknown_context_card` — whose stated
  remedy is to restore a file that was in fact right there. The two conditions have opposite
  remedies, so they are now two errors.
- `session verify` and `session import` no longer stat a path a session names outside itself. A
  recorded artifact's `filename` is an unconstrained string read out of `session.json`, and it was
  joined into the artifacts directory and passed to `.is_file()` without validation — an absolute
  value replaces the prefix entirely under `pathlib`, so `artifacts/` + `/etc/passwd` was
  `/etc/passwd`. Recording a problem for an unknown artifact type or a filename mismatch did not stop
  it: execution fell through to the join either way. No content was ever read, but whether the reply
  carried `missing_artifact_file` answered whether that outside path existed. The name now goes
  through the same bare-filename chokepoint every artifact write uses, and a refused one is reported
  as `unsafe_artifact_filename` rather than probed.
- The Claude Code `discover` skill now confirms `context.ok` as well as `schema.ok` before
  reasoning; `schema.ok` was true in exactly the broken state above, so the plugin proceeded.
- Compatibility: compatible - every existing `doctor --json` and `session verify --json` key keeps
  its name and meaning, with three exceptions that are the fix: `sessions.total` is `null` instead of
  `0` when the session directory cannot be read; `session verify` now exits non-zero (and reports
  `ok: false`) for a session whose context cards no longer resolve, which is a session that cannot
  take another turn; and a session or archive recording an artifact under a name that is not a bare
  filename is now refused as `unsafe_artifact_filename`, where it previously passed whenever the path
  it named happened to exist.

- Selectors no longer widen to everything, or empty to nothing, when a name is blank or no longer
  resolves (#13). An empty slot name — `requivo impact <slug> ""`, which is what an unset shell
  variable expands to — matched every label and reported the whole model as changed with no unmatched
  token to explain it; an empty `--context` name fell through to "every card", the widening
  `resolve_cards` exists to prevent; and a context card that had been renamed, or that lived in
  `REQUIVO_CONTEXT_DIR` on another machine, silently replaced `{{CONTEXT}}` with the empty string on
  every later turn, so the engine reasoned with no product context at all and nothing said so. All
  three are now refusals carrying a structured error (`empty_selector_token`, `unknown_context_card`),
  from one shared rule in `requivo.core.selectors` rather than three local ones.
- Compatibility: breaking - a session whose `context_cards` no longer resolve now fails its next turn
  with a named card instead of quietly reasoning without product context, and `--context "a,"` is
  refused rather than read as `a`. Both were previously silent; neither has a persisted-format change,
  so an unaffected session is untouched.

- The `ReasoningProvider` protocol now declares `name`, the member `DiscoveryService` reads first
  (#19). It reaches for `provider.name` when it claims the session, before any reasoning happens, but
  the protocol declared only `analyze`, `generate`, `model_name` and `provenance` — so a second
  implementation satisfied the published contract, satisfied `isinstance`, and then failed with an
  `AttributeError` on the first `discover`. The seam reported conformance it had not checked.
  `isinstance(p, ReasoningProvider)` now catches that provider, because `@runtime_checkable` does
  cover non-method members; `docs/providers.md` lists `name` alongside the four methods, and states
  what that check can and cannot tell you.
- Compatibility: breaking - only for code calling `issubclass(X, ReasoningProvider)`, which Python
  refuses for any protocol carrying a non-method member and now raises `TypeError`; `isinstance` is
  the replacement and is stricter than before. Nothing in Requivo calls either, `AnthropicProvider`
  already carried `name`, and no persisted format or CLI output changes.

- Artifact filenames are validated on the way **out** as well as in (#23). #5 closed the traversal on
  the two mutating paths; `FileSessionRepository.load_artifact` still joined
  `canonical_dir(slug) / "artifacts" / filename` inline, one layer above the chokepoint, so
  `load_artifact(slug, "../../../../secret.md")` read and returned a file outside the session
  directory. The two are separate exposures — a write target decides what this code may create, a read
  target decides what it may disclose — so closing one did not close the other. Reads now go through
  the same `artifact_path()` chokepoint, which is where they should have been: the read side is the
  proof of the rule `artifact_path()` exists for, that a guard applied per-caller is a guard the next
  caller forgets.
- A refused read raises `invalid_filename`; only a genuinely missing artifact returns nothing (#23).
  Returning nothing for both was the tempting shape and the quiet one — a rejected traversal would
  have been indistinguishable from an artifact nobody has generated yet, and a caller that cannot tell
  a refusal from an absence has been handed the wrong answer in the more dangerous direction.
- Artifact content is decoded as UTF-8 explicitly, matching how it was written (#23). The same read
  took the locale's encoding, so under `LC_ALL=C` or a DBCS Windows shell an artifact died on its
  first em-dash — and every artifact this engine generates has them.
- Compatibility: compatible - no in-repo caller and no valid filename changes behaviour; every caller
  reaches this through `ArtifactService.show` with an `ARTIFACT_FILENAMES` lookup, and those still
  load byte for byte. The only behaviour that moved is for a filename that was previously a traversal,
  where an exception replaces a silently-wrong answer.

- The files that declare the project version are now checked against each other on every test run
  (#32). Four of them declare it — `pyproject.toml`, `src/requivo/__init__.py`, the Claude Code
  plugin manifest and the marketplace catalog — and nothing compared them; a release edits them by
  hand, one at a time. The two expensive drifts are both silent on both ends: a stale
  `plugins/claude-code/.claude-plugin/plugin.json` is the version the Claude Code updater compares,
  so a release that leaves it behind uploads to PyPI, announces itself correctly and is never
  offered to plugin users at all; a stale `src/requivo/__init__.py` is what `requivo doctor` prints
  as `requivo_version`, so the diagnostic whose job is answering *is anything wrong* becomes the
  thing that is wrong, and every bug report from that install cites the wrong version.
- The guard derives its site list by scanning for a version at a known structural position, rather
  than reading the registered list in `.oss.json` (#32). That distinction was not academic: the
  registry was swept by hand at `aed734c` specifically to catch unregistered sites, and that sweep
  still missed `src/requivo/__init__.py` — so a guard reading the registry would have certified
  agreement across the files somebody remembered while the one they forgot sat unchecked, which is
  the failure it exists to close. `src/requivo/__init__.py` is now registered, and the registry is
  cross-checked rather than trusted: a derived site missing from `version_sites` is itself a
  failure, since it is a site a release will not know to update.
- Scanning by structural position is also what keeps `CHANGELOG.md` out of it (#32). A history file
  names every version the project has ever had, and it is in `version_sites` because a release must
  *edit* it — not because it declares anything. The cross-check is therefore one-directional:
  registered-but-not-declaring is fine, declaring-but-unregistered is a finding.
- The guard has three states rather than two, and refuses on the third (#32). An unreadable
  manifest, a known site that has moved, and an empty scan are each a failure worded as *could not
  check* and distinguishable from drift — because a version guard that skips a file it could not
  read and passes anyway certifies an agreement it never looked for, converting "nobody checked"
  into "checked and fine". That is strictly worse than having no guard.
- Compatibility: compatible - a test-only addition plus one new entry in `.oss.json`. No product
  code, no public output and no on-disk format changes, and no version number moves.

- An install with no context cards at all is now refused instead of reasoning without them (#33).
  `load_context(None)` — what every session with no card selection sends on each turn — comprehended
  over an empty card directory and returned the empty string, and `build_prompt` substituted that into
  `{{CONTEXT}}` with no check. A wheel or container layer that shipped `assets/` but lost
  `assets/context/` therefore reasoned with no product context at all, on calls that cost money, for
  as long as the install lasted: `information_value = uncertainty x impact` is the engine's central
  idea and it runs entirely on those cards. Two earlier fixes had closed the narrow instances — a
  selection that resolves to nothing, and `doctor` learning to tell `ok` from `empty` from
  `unreadable` — but both are about a *selection*, and `None` is the absence of one. The new
  `no_context_cards` error names where it looked, `check_selection` reports it so `doctor` and
  `session verify` still answer for free and in advance, and the test that used to assert
  `load_context() == ""` asserted the defect as the contract.

- A server-side fault is no longer reported to the browser as your bad request (#34). Requivo Web
  mapped error codes to HTTP statuses through a table that defaulted to `400` for anything unlisted,
  so a code added anywhere else arrived wearing a plausible, wrong status. `context_unreadable` — the
  server unable to read its own context-card directory, entirely the operator's environment — told
  the reader they had done something wrong. Three more codes were sitting on that same default
  unnoticed: `provider_output_invalid` and `session_locked`, and `session_exists`. Every code now has
  an explicit status (`500`, `502`, `503` and `409` respectively), an unrecognised code is a `500`
  rather than a `400` because "we could not classify this" is not evidence the caller erred, and a
  test walks the error vocabulary so the next code added is a red build instead of a wrong answer to
  a user. A `5xx` is now also logged for the operator, who otherwise had no record of it.

- `empty_selector_token` no longer carries two different facts behind two different payload shapes
  (#35). One code covered both an empty *token inside* a selection (`details: {selector, position}`)
  and a selection that was *itself* empty (`details: {selector, tokens}`) — a distinction the
  selector's own docstring had already argued for, and then not honoured seventy lines later in the
  same change. Since the documented advice for consumers is to assert on the code, anyone following
  it and reading `details["position"]` got a `KeyError` from a payload that correctly carried the
  code they had matched. The second case is now `empty_selection`, a sibling rather than a subclass
  so the two cannot be re-conflated by an `except`, `position` is guaranteed on every
  `empty_selector_token` payload, and `docs/compatibility.md` carries the mapping for anyone matching
  the old code.

### Security

- A session's stored context-card names can no longer forge a line of `doctor` or `session verify`
  (#40). `context_cards` is an unconstrained list of strings in `session.json`, `session import`
  passes it through intact, and both health verbs rendered those names into their output bare. A
  name containing a newline therefore did not merely look odd — it ended the line and started a new
  one at whatever column it chose, so a session could print `doctor`'s own `sessions` row,
  byte-identical in shape and column, answering *all clear* directly beneath the row reporting it.
  `session verify`, the verb whose entire job is to say whether a session is telling the truth, was
  forged the same way and still exited 1, so its text and its exit code disagreed.
- The fix refuses rather than escapes, at `normalize_tokens` — the one function every selector
  passes through — so a hostile name cannot reach a render site at all, rather than being made safe
  at the two sites that exist today. The new refusal is `unsafe_selector_token`, with
  `details: {selector, position}`; it is reported by `check_selection` rather than raised, so one
  tampered session degrades its own row instead of taking the whole listing down. `session show`,
  a third render site the issue does not name, prints stored names through a one-line display rule
  instead, because nothing there is selecting and no refusal can run.
- Also fixed, and outside the issue's footprint: `_SLUG_RE` and `_FILENAME_RE` were anchored with
  `$`, which in Python matches at the end of the string *or just before a trailing newline*. Both
  guards therefore accepted one trailing newline, which is what made `integrity.py`'s
  `artifacts/<name> is missing` line — the one place that renders a recorded filename without `!r` —
  reachable with a name that splits it in two. Both are now anchored with `\Z`.
- Found in review of this fix and fixed here: `resolve_slots` echoed the **unstripped** token into
  its unmatched list, where `resolve_cards` and `_selection_keys` both echo `raw.strip()`. Because
  the new guard inspects the stripped token — `str.strip()` removes the control characters Python
  classifies as whitespace — a slot token with a *leading* newline passed the guard and then broke
  the line `requivo impact` prints. Lower severity than the card path (a slot token is a live argv
  value the same user typed, not persisted data), but `core/selectors.py` claimed the value could
  never reach a render site, so the claim had to be made true. The docstrings now state the guard's
  actual scope rather than the loose version of it.
- Compatibility: compatible - no name Requivo writes is affected, and `--json` output is unchanged
  and still carries the bytes verbatim. A `session.json` hand-edited or imported with a control
  character in `context_cards` now reports `unsafe_selector_token` where it previously printed the
  name; `docs/compatibility.md` carries the new code.

## [0.9.10] - 2026-08-04

A documentation release. No contract, no session format, no prompt, no generator and no engine
behaviour changed — 0.9.10 reasons exactly as 0.9.9 did. It exists because 0.9.9 was tagged but never
published, and because the quickstart it shipped with did not work on a machine without `uv`.

### Fixed

- **The install instructions assumed a tool the reader may not have.** Every line in the README and in
  getting-started began with `uv`, and neither said how to obtain it or offered an alternative. `uv`
  keeps the lead and now carries the one line that installs it; `pipx` is named as the equivalent, and
  getting-started gained an *Installing* section with a virtualenv route for pip-only setups, tested
  end to end on a clean Python 3.9. `pip install --user` is called out as the one to avoid: it
  succeeds while leaving `requivo` off the PATH, which reads as a broken package rather than a PATH
  problem.
- **`requivo demo` no longer asks you to clone the repository.** The payload ships in the wheel, so it
  runs straight after any install; the clone is now the alternative rather than the instruction.

### Changed

- **The README is an orientation again.** 250 → 160 lines. Requivo Web now opens the document instead
  of being the second of three equally-weighted quickstarts — the hierarchy is in the space each
  surface occupies, not only in its heading. *Core concepts* moved to
  [`docs/requirements-model.md`](docs/requirements-model.md), which gained the product ↔ engine
  vocabulary table it never had in writing; *What Requivo produces* folded into *How it works*; the
  13-row documentation table dropped to 5, the rest already being indexed in `docs/`.
- **The Web install is two lines.** `export ANTHROPIC_API_KEY` left the install block: `cli.py` loads a
  `.env` and `web/config.py` already reports a missing key in the interface, so presenting it as a
  prerequisite overstated it — the interface opens and reads existing sessions without one.

## [0.9.9] - 2026-08-04

The product release. Nothing about the engine changed — no contract, no session format, no prompt, no
generator. What changed is that the useful part is now the visible part: Requivo Web is built around
one workflow instead of around the model, and the three interfaces are no longer presented as three
equal choices. **Web is the product experience, Claude Code is an integration, the CLI is
infrastructure** — a difference in weight, never in capability.

### Added
- **"What changed"**, after every answer. The page now leads with what those answers moved: which parts
  of the solution changed, which decisions and contested premises need re-examining, which documents
  need updating. All of it is a projection of the `UpdateResult` the Core already returned — computed
  from the dependency graph, never generated. This was the product's differentiator and it had been
  rendering as a one-line notice.
- **`web/viewmodels/labels.py`** — the user-facing vocabulary in one table, so a term that appears in
  six templates cannot drift in six directions. *What we know*, *what we are assuming*, *open
  question*, *needs updating*, *are we ready?*, *decision brief*. A translation layer only: nothing
  stored, emitted by `--json`, or named in a contract changed.
- **`docs/product-validation.md`** — the manual protocol for the question the test suite cannot answer:
  is this better than a strong prompt to a capable model? It isolates the two moments where the answer
  actually lies — coming back to a session two days later, and changing an answer you already gave.
  Deliberately not folded into the golden harness, which would lend a measurement's precision to a
  judgment.
- **Traceability details** — one disclosure on the session page holding everything the engine knows:
  per-topic understanding, coverage, every open question, decisions, contested premises, provenance,
  raw model export. Hiding is presentational; the counts are always stated, so a short list can never
  be mistaken for the whole list.
- `core.analysis.slot_labels()` — the public form of the internal `_label`, so an interface translates
  slot ids through the schema instead of inventing its own names.

### Changed
- **The request box is the home page.** `/sessions/new` is retired (it redirects); the provider is
  resolved by the server rather than asked of the reader, and joins the session name and the product
  context cards under *Advanced settings*.
- **The session page reads in one order**: the request, what Requivo understood, at most five questions
  (each with *why it matters* and its likely area of impact), the answer form, *Are we ready?* as one
  action state with its reasons, then the decision brief. Everything else moved behind traceability.
- **One primary document action.** "Generate decision brief" leads; PRD, acceptance criteria, epic and
  release notes stay available under *More documents*. Six buttons of equal weight is not six options,
  it is no recommendation.
- **The decision brief is half deterministic.** `brief_markdown` now opens with *What is confirmed* and
  *Important assumptions*, read straight off the model rather than restated by the provider — a
  restatement can drift from what it restates; a projection cannot. The contract, the prompt and the
  filename (`solution-assessment.md`) are unchanged, so no session, script or golden baseline moves.
- **The engine's own `summary` is finally shown.** `scope`, `assumptions` and `blind_spot` were being
  produced on every turn and thrown away; they are the paragraph a reader needs to judge whether the
  engine understood them at all.
- The answers turn swaps the whole session body rather than the questions alone — a partial swap left
  the "needs updating" badges describing the previous revision, under the reader's eyes.
- README, `docs/`, the Claude Code plugin and the CLI help all speak the product's vocabulary and the
  Web → Claude Code → CLI hierarchy. `examples/leave-approval/` is the canonical example, and now ends
  with the change-impact walkthrough.

### Fixed
- **One un-analysed session no longer 404s the whole home page.** `session_list` asked every session
  for a `status()`, which needs a model; a session created through "save the request only" has none, and
  the exception took the entire listing with it — hiding every *other* session behind one that had
  simply not been analysed yet. A listing has to survive its own members (invariant 15).
- Opportunities rendered their leverage as `Leverage.high`: the view model dumped without
  `mode="json"`, and Jinja renders an enum by its repr.

## [0.9.8] - 2026-08-02

The clean-up release. The pre-store architecture is gone, sessions can be checked against themselves,
and the artifact contracts hold their own references. Nothing here changes what Requivo does — it
removes the ways it could be wrong about what it has.

### Removed
- **The legacy flag CLI** (`python src/engine.py "…" --prd`) and its `src/engine.py` shim, deprecated
  since 0.9.2. It wrote the pre-versioned `out/<slug>/` layout: no revisions, no provenance, no
  staleness — everything the subcommand CLI exists to provide.
- **The `pc` command alias**, deprecated since the 0.7.0 rename. `requivo` is the command.
- **The implicit `out/<slug>/` fallback.** Every read of every session used to fall back to that
  layout, and a mutation migrated one in place — so old sessions kept working without the user
  knowing, which is also what was wrong with it: the fallback ran on every read for a layout nothing
  had written since 0.8.0, and "where does this session live?" had two answers throughout the code.
  Migration is explicit (`requivo session migrate`, unchanged, still copying rather than moving) and
  all that remains is detection: a session found only in `out/` is reported as missing *with that
  command named in the error*. The three removals were scheduled for 1.1.0, which would have meant
  carrying them through 1.0.
- The legacy write path in `persistence` (`save_model`, `save_session`, `write_artifact`, …) and
  `stale_on_disk`, which answered "which files in `out/<slug>/` are stale?" — a question the session
  store answers from `artifact_status`. Nothing but the flag CLI had called them.

### Added
- **`requivo session verify <slug>`** and `core/integrity.py` behind it. A session is several files
  that have to agree — the revision count, one file per revision, a current model that *is* the last
  of them, artifacts pointing back at revisions that exist — and every one of those claims can be
  false while each individual file is valid JSON. Validating shapes cannot see any of it. The checker
  reports every broken relationship with a stable code rather than raising on the first, so `verify`
  can list them, `import` can refuse an archive, and `doctor` can name the sessions worth looking at.

### Fixed
- **`session import` accepted archives that did not add up.** It checked that `session.json` parsed,
  that its slug agreed and that a claimed revision had a `model.json` — shape, not truth. An archive
  announcing revision 2 with no `revisions/` at all imported cleanly, and so did one whose `model.json`
  had been swapped for a different model. Import now runs the same integrity check as `session verify`.
- **A structurally invalid `session.json`, a corrupt zip and an I/O failure reached the user as
  tracebacks.** Every way a supplied archive can be wrong now arrives as a Requivo error.
- **`import --force` deleted before it replaced.** `rmtree` then rename leaves nothing at all if the
  rename fails: the archive refused *and* the session the user already had gone. The existing session
  now steps aside and is deleted only once the new one is in place, with a rollback if it is not.
- **`session export` read the session without its lock**, so an archive could combine an old
  `session.json` with a newer `model.json` — internally inconsistent, and only discovered on import.
  It now reads under the same lock every writer takes, writes to a temp archive renamed into place,
  and excludes `.lock`, which is this machine's coordination and meant nothing in an archive.
- **Artifact contracts held references that pointed at nothing.** A story could be traced to a slot
  the schema does not define; an epic issue could `depends_on` an id the epic did not contain (and be
  exported as a real tracker link); an estimate could run 5 to 1 days, dragging the project's low
  bound above its high bound; a PRD requirement could be an empty numbered row; ids could repeat, which
  downstream reads as one item rather than two. Each of these is a pointer something follows, so each
  is now checked — structural coherence only, never a judgment about content. The generator prompts
  state the same rules, so the model self-corrects rather than burning retries.

### Changed
- Dependencies carry upper bounds on the majors (`pydantic>=2,<3`, `anthropic>=0.40,<1`, …). A
  dependency's next major is by definition allowed to break us, and without a ceiling it does so on a
  fresh install of an *unchanged* Requivo — the one failure a user cannot correlate with anything.
- The Claude Code `discover` skill passes the request on stdin instead of interpolating it into a
  shell command. A client request is untrusted text; quotes, newlines and `$(…)` are ordinary in prose.
- `ARTIFACT_FILENAMES` moved to Core, where the service that saves, the CLI's `--type` choices and the
  integrity checker read one vocabulary instead of two.

## [0.9.7] - 2026-08-02

The 0.9.6 review: the two seams where a provider call meets a session that can move under it, and the
service layer becoming the integrity boundary it has to be before anything external calls it directly.

### Fixed
- **A first discovery could still overwrite a refined model.** 0.9.6 gave `run_discovery` the revision
  it read as an optimistic-lock precondition, which is the wrong instrument for this: discovery reasons
  from the request *alone* — it never sees the current model — so on a session at revision 2 it reads
  2, writes against 2, satisfies the precondition perfectly, and replaces two turns of refinement with
  a naive first analysis. `POST /sessions/{slug}/discover` reaches it directly; the Web only offers
  the button at revision 0, but a business rule enforced by a hidden button is not enforced. The
  revision itself is now the rule (`_require_revision_zero`), shared by every entry point, and checked
  *before* the provider call rather than after — a repeat `discover` used to buy a discovery turn, and
  an assessment when finalizing, purely to throw both away.
- **A generation's revision and its model were two separate reads.** A write landing between them gave
  revision N with the model of N+1: the artifact was generated from the newer model and filed against
  the older revision. Nothing downstream could catch it, because the recorded number is perfectly
  plausible — it just describes a different model than the document was written from, which is exactly
  the claim traceability cannot get wrong. `SessionService.snapshot()` returns revision, model, request
  and cards from one read under the session lock, and every provider-backed operation takes one.
- **An unestablishable freshness was reported as "fresh".** `_stale_since` swallowed a `RequivoError`
  from an unreadable history and returned `False`, on the reasoning that an unanswerable question must
  not manufacture a stale flag. But `False` is not the absence of an answer — it is the claim that the
  artifact is up to date, made about a session whose history could not be read at all. The save is now
  refused: the provenance it would record cannot be verified.
- **The service trusted its context cards.** The CLI and the Web both resolve them first, which made
  the service look safe; it is not a boundary until it holds the rule itself. An unknown card recorded
  on a session is read back by every later turn, and an empty resolved selection means *every* card, so
  a bad name silently widened the context instead of narrowing it. `create_session` resolves.
- **`DiscoveryService` could split its storage.** The artifact service defaulted to the process
  repository rather than the session service's, so `DiscoveryService(sessions=SessionService(postgres))`
  — the shape an external deployment constructs — wrote sessions to Postgres and artifacts to the local
  filesystem, with every call succeeding. It now follows the session service, and takes a `repo=`
  argument that configures both at once.

## [0.9.6] - 2026-08-02

The 0.9.5 review. Two correctness bugs sat where the product's own
promise lives — one erased the reasoning layer during an ordinary turn, the other replaced a whole
model with a fragment of it — plus the preconditions and the session identity that a second writer
makes matter.

### Fixed
- **A refinement turn silently erased every decision, challenge and opportunity.** `engine.md` asks a
  turn for `model`/`questions`/`summary` and nothing else — a refinement answers a question, it does
  not re-derive the brief. That reply was read as a complete `EngineOutput`, whose reasoning fields
  default to empty lists, and the apply path stored it verbatim: one ordinary `requivo answer` after a
  brief deleted the entire reasoning layer. It was silent in both directions, because
  `diff_reasoning` deliberately absorbed the populated → empty case to stop exactly this turn from
  marking everything stale — so the apply reported no reasoning change and left the PRD marked fresh
  over a model whose decisions no longer existed. The two defects had been hiding each other. A
  proposal is now its own contract (`ModelProposal`) in which the three collections are tri-state:
  absent keeps what is established, `[]` deletes it, a list replaces it. `resolve()` collapses them
  against the model being refined, once, for every surface — and with omission resolved before the
  diff, the diff is symmetric again, so a real deletion is reported and does invalidate what rested
  on it.
- **`model apply --allow-partial` replaced the model with the fragment.** The name read as a patch; it
  merged nothing. It only relaxed the completeness check, and the partial model then replaced the
  complete one — applying a single slot left a one-slot model where fifteen had been, reported as
  fourteen changed slots. The flag is gone from `apply` and from `diff` (its dry run). Checking a
  projection is still `model validate --allow-partial`, which is what the flag always actually meant.
- **A first discovery had no precondition.** `run_discovery` and `finalize_discovery` reasoned from a
  session and applied without stating the revision they read, the one gap left after 0.9.4 closed
  `answer` and `generate`. A write that landed during the provider call was overwritten by a model
  reasoned from the older state. Both now carry it — and because creation is idempotent, re-running
  `discover` on a request whose session already holds a model is a `revision_conflict` naming
  `requivo answer`, rather than a naive first-turn model quietly replacing a refined one.
- **Session creation ignored its context cards, and was not atomic.** Two creations of the same request
  returned the same session even when they asked for different cards — but the cards are the provenance
  of every impact estimate the session will make, so the same request read against `b2b-platform` and
  against `event-ops` is not the same discovery; the caller got a session with cards it had not asked
  for and no way to notice. Identity is now the request *and* its card selection. Creation itself was a
  `has_meta` check followed by a write, which a dozen concurrent callers all passed: each wrote its own
  `session.json` over the last, so the session's id, provider and cards were whichever writer finished
  last. A session is now assembled in a staging directory and renamed into place — the rename is the
  claim on the slug, so exactly one caller creates it and the rest are handed what exists.
- **An empty objective was complete on one surface and not the other.** The provider's retry hook
  required `summary.objective`; the deterministic apply path required only the slots. The same model
  was therefore acceptable from Claude Code and refused from Anthropic. Both boundaries now read one
  definition (`completeness_gap`), which also keeps a session of fifteen filled slots from rendering a
  blank heading in every view.
- **The brief skill offered an enum the contract rejects** — `"leverage": "low|medium|high"`, where
  `Leverage` is `high|medium|future`. A skill's JSON template is an instruction, so the failure landed
  one step later as a schema error on an apply. Fixed, and a static test now holds every skill's enum
  placeholders to the contracts' vocabulary.

### Changed
- `plugins/claude-code/REASONING.md` states the proposal contract the skills work against: complete
  slots, a real objective, and the tri-state reasoning layer. The `answer` skill is explicit that
  leaving the three collections out is the normal case.

## [0.9.5] - 2026-08-02

The second half of the 0.9.3 review: untrusted input, output that is worth saving, and the Claude Code
surface reaching parity with the provider path.

### Fixed
- **`session import` wrote before it checked.** It called `extractall` straight into the session store
  and then reported success, so a bad archive was already unpacked by the time anything could object.
  Its traversal guard compared path *strings* (`str(target).startswith(str(root))`), which is not a
  containment test — `/…/sessions-evil` starts with `/…/sessions`. And an archive whose folder was
  named `bad slug` imported happily and then broke every later `session list`. Import is now
  inspect → extract to scratch → validate → move: exactly one session directory, its name validated as
  a slug, file-count and expanded-size ceilings, every entry decomposed into path components rather
  than string-matched, and the extracted directory confirmed to be a real session (its `session.json`
  parses, its slug agrees with the directory, a claimed revision has a `model.json`) before it is moved
  into place. A collision is refused unless `--force`. A refused import leaves nothing behind.
- **The Claude Code brief produced prose and dropped its reasoning.** The provider path absorbs the
  assessment's decisions, challenges and opportunities into `model.json` so every later generator
  inherits them; the skill only wrote Markdown. A PRD generated after a brief in Claude Code therefore
  could not build on it. The skill now folds the structured reasoning back through `model apply`
  first — no CLI change was needed, the apply path already accepted it — and saves the document
  against the revision that created.
- **Skills staged content in `/tmp`.** One shared path, so two sessions working at once overwrote each
  other; `/tmp/requivo:prd.md` is not a legal filename on Windows; and cleanup needed `rm`, which the
  plugin does not grant itself. Every command that takes a document now accepts `-` for stdin, and no
  skill writes a file at all — the `Write` grant is gone with the need for it.
- **Skills read every context card, whatever the session was created with.** A session's card selection
  is held constant across its turns because it is what the impact estimates were made against; a later
  turn reading all of them reasons from a wider context than the model was built on, which the golden
  harness has measured as a real cost. `requivo context --session <slug>` prints exactly that
  session's cards, and the skills use it.
- **`schema_version` was decorative** — recorded on every session, read by nothing. A session authored
  against a newer slot vocabulary is now refused as clearly as a newer `format_version`.
- **Two identical reasoning items collided on one id.** Ids are content-derived, so a repeated decision
  produces a duplicate key — and the id is what a diff keys on and what a user cites a decision by. It
  is now a validation error that rides the retry loop, rather than one of the pair going invisible.
- **htmx injected a `<style>` block the CSP blocked** on every page load. Nothing uses
  `.htmx-indicator`, so the styles were pure cost and the violation was pure noise — and a CSP that
  cries wolf is one nobody reads. Disabled via htmx's config meta tag.
- Docstring and comment corrections: the package no longer describes Claude Code and the Web as
  future work, and `ArtifactService` no longer claims the assessment is exempt from staleness.

### Changed
- **Artifact contracts require what makes each artifact *be* that artifact.** A PRD with an empty
  `title` or no `problem`, a Gherkin scenario with no `when` or no `then`, an epic that decomposes into
  zero issues, a nameless story — all were structurally valid and none were usable. These fields are
  now non-empty by contract, so a degraded generation fails loudly and retries instead of being saved.
  The bar is deliberately low: only what is definitionally required, never a judgment about whether an
  artifact is *good enough*. The generator prompts state the same requirements, so the model is told
  the rule rather than discovering it through a retry. `engine.md` and `brief.md` are untouched — the
  golden baselines still apply.

### Added
- `-` reads a document from stdin on `model validate`, `model apply`, `model diff`,
  `artifact save --file`, and `session init`.
- `requivo context --session <slug>` — the cards that session was created with.
- `session import --force` — replace a session of the same slug.

## [0.9.4] - 2026-08-02

Integrity: the session store is now trustworthy under concurrent writers, and freshness and forward
compatibility are guarantees rather than intentions. From an external review of 0.9.3 — everything
here is a case where the store could lose a change or report something it could not know.

### Fixed
- **Two writers could both win.** `save_revision` checked `expected_revision` and *then* performed its
  writes, with nothing holding the two together, so two processes reading the same revision both
  passed the check and the second silently overwrote the first. Worse, they also shared one scratch
  filename (`.model.json.tmp`), so the usual symptom was not a lost update but a `FileNotFoundError`
  from `Path.replace` — a conflict presented as a bug in Requivo. Every compound mutation now runs
  under a per-session OS lock (`.lock`, re-entrant per thread, released by the kernel on crash) and
  every atomic write uses a temp name private to the writer. The loser gets `revision_conflict`, which
  is a real answer. Reproduced by a twelve-thread regression test.
- **An artifact saved against an older revision was recorded fresh.** `ArtifactService.save` took the
  caller's `source_revision` and wrote `stale=False` beside it, so a PRD reasoned from revision 1 and
  saved once the session had reached 3 sat on disk marked current. Reasoning and saving are not the
  same moment — a provider call takes minutes, and Claude Code may save a document from several turns
  ago — and the answer is knowable: the source revision is now diffed against the current model and
  the artifact is recorded stale if its dependencies moved. `artifact save --json` returns the `stale`
  it recorded, rather than making the caller ask again.
- **A change to the reasoning layer alone left every artifact fresh.** Staleness was computed from
  `diff_models`, which compares slots. But every generator is prompted with the complete model,
  reasoning included, so a rewritten design decision can change a PRD with no slot touched — and the
  apply reported `changed_slots: []` and marked nothing stale. `diff_reasoning` now covers decisions,
  challenges and opportunities, comparing content rather than only ids (an id derives from a subset of
  each item's fields, so an edited rationale kept its id and was invisible). Reasoning a turn merely
  *omits* is deliberately not a removal: a refinement turn replies without re-stating the brief, and
  reading that silence as a deletion would mark everything stale on nearly every turn.
- **An older Requivo destroyed a field a newer one had added.** `SessionMeta` used `extra="ignore"`,
  so an unknown key loaded fine and was then dropped the moment the older version wrote the file back
  — turning the documented "adding a field is compatible" into "the first mutation by an older reader
  deletes it". Persisted metadata is now `extra="allow"`, matching `RevisionRecord`, which had made
  this choice for exactly this reason. Keys Requivo has genuinely retired are dropped explicitly, in
  `_RETIRED_KEYS`, so forward compatibility does not mean carrying dead keys forever.
- **A long request produced a slug the filesystem refused.** `_slug` took the first five words with no
  length bound, so a single 300-character token became a 300-character directory name and the write
  failed deep inside with a bare `OSError`. Slugs are now capped (80 characters, enforced in
  `validate_slug` so an explicit `--slug` is bounded too) with deterministic truncation plus a content
  hash, so two different long requests cannot collapse onto one session.
- **The provider raised a bare `RuntimeError` after exhausting its retries.** Every surface catches
  `RequivoError`, so this one reached the user as a traceback. It is now `ProviderOutputError`
  (`provider_output_invalid`), carrying the contract, the attempt count and the last failure.
- **The Claude Code skills ignored the locking primitives the CLI already had.** `model apply` accepts
  `--expected-revision` and `artifact save` accepts `--revision`, and no skill passed either — so a
  Claude Code turn could overwrite a change made in the Web while it was reasoning, with no error. The
  revision contract is now stated once in `REASONING.md` and followed by every skill: read the
  revision, reason from it, hand it back on apply and on save.
- **The plugin version had drifted a release behind** because it was also written out in prose. The
  prose no longer restates it, and a test pins the manifest to the package version.

### Added
- `session init --json` reports `revision`. Init is idempotent, so it can hand back an *existing*
  session that already carries a model; a caller about to apply needs to know which of the two it got.
- `model apply --json` reports `changed_decisions`, `changed_challenges` and `changed_opportunities`
  alongside `changed_slots`. The slots say the facts moved; these say the judgment over them moved.
- `SessionRepository.lock(slug)` — the storage seam gains the one operation the service needs to make
  a compound update atomic. The file backing maps it to an OS file lock; a Postgres backing maps it to
  the row lock of the enclosing transaction.

## [0.9.3] - 2026-08-01

Pre-1.0 consolidation: the session format is declared public and pinned by a test, the deprecations
are written down, and the Web catches up with what the shared service can already do.

### Added
- **The session format is a published contract.** [`docs/compatibility.md`](docs/compatibility.md)
  states what `.requivo/sessions/` guarantees, what may change without a `format_version` bump (adding
  a field, retiring an unpopulated one), and what requires one (renaming or repurposing a populated
  field, changing the layout). The `--json` outputs and error `code` values are covered by the same
  rule. A frozen 0.8.2 `session.json` now lives in the test suite and must keep loading verbatim —
  including a key that has since been removed — and a session claiming a newer `format_version` must
  still be refused rather than half-understood.
- **A written deprecation policy**, with the current list: the legacy flag CLI (removal 1.1.0), the
  `pc` alias, legacy `out/` sessions, and the old `/requivo-<skill>` plugin names. Anything deprecated
  keeps working for at least one minor version and names its replacement; nothing is removed in a patch.
- **The Web generates every artifact the service can produce** — acceptance criteria, delivery epic and
  release notes join the solution assessment and the PRD. The buttons are built from the service's own
  `GENERATABLE` vocabulary rather than a list kept in the Web, so a generator registered once appears
  on every surface instead of each surface keeping its own list and drifting.

### Fixed
- **`discover` on a file whose name is not already a slug.** The filename stem was passed through as
  the session slug, so an ordinary input file — `Leave Approval v2.md` — died on `invalid_slug`. A
  filename is a suggestion; it is now slugified like any other.
- **`discover` on a directory path.** The file check used `exists()`, which a directory satisfies, and
  the next line called `read_text()` on it — a traceback instead of treating the argument as a request.
- **`model validate --session` was declared and read by nothing.** A flag that parses and changes
  nothing is worse than a missing one: the caller believes a check ran. Removed; `model diff` is the
  command that actually validates a proposal against a session.
- **Unexpected web errors are logged.** The handler correctly kept tracebacks away from the browser
  but sent them nowhere else, so a genuine failure left the operator with a generic page and no trace.
  Method and path are logged; the request body deliberately is not.

## [0.9.2] - 2026-08-01

The second half of the 0.9.0 review: consistency between surfaces, and the identity/provenance
decisions that have to be made before anything else writes sessions. No breaking change to the session
format — a 0.9.x session is read and written unchanged.

### Changed
- **Every interface produces the same artifact.** Generation moved behind `DiscoveryService.generate()`
  for all of `brief` / `prd` / `criteria` / `epic` / `release`, so a document asked for from the
  terminal is produced, saved, versioned and tracked exactly as the same document asked for from the
  browser or from Claude Code. The terminal used to render the solution assessment and keep it — it now
  saves it like everywhere else. `stories` and `estimate` stay deliberately terminal-only (analyses
  feeding the estimate, not deliverables with a file) via `DiscoveryService.reason()`.
- **The provider seam is real, not decorative.** `DiscoveryService` now talks to the
  `ReasoningProvider` protocol only — `analyze` / `generate` / `model_name` / `provenance` — instead of
  importing the Anthropic functions directly. Swapping the reasoning backend is a constructor argument;
  a test runs a whole discovery through a provider with no vendor behind it.
- **Provenance is populated, not just declared.** Each revision records the provider, the model, the
  surface, and `prompt_version` — a hash of the exact system prompt (prompt file + schema + the context
  cards actually selected). Behaviour here is tuned by editing assets, so that hash is half the answer
  to "what produced this revision". The never-written session-level `prompt_versions` map is gone.
- **Boundary contracts are strict.** Everything an LLM fills now inherits a `StrictModel` base
  (`extra="forbid"`): a field the model invents fails loudly and rides the retry loop instead of being
  silently dropped. Text that must say something (a question's `q`/`why`, a challenge's premise,
  alternative, consequence and recommendation) is rejected when empty, and a discovery reply must carry
  a non-empty objective.
- **Design decisions, challenges and opportunities carry stable ids** (`dec_…`, `chl_…`, `opp_…`),
  derived from their own content and recomputed on every validation — identical across revisions,
  surfaces and machines while the statement is unchanged, and impossible to forge, since a supplied id
  is always overwritten. A consumer needs to refer back to a decision; text is a poor handle.
- **The legacy flag CLI is deprecated** and moved to `requivo/legacy.py`, frozen, with a notice on use
  and removal scheduled for **1.1.0**. It writes the old `out/` layout — no revisions, no provenance,
  no staleness — and deleting that one file is now the whole removal.
- **`CLAUDE.md` rewritten** (327 → 261 lines). It described `out/<slug>/model.json` as the store, `pc`
  as the modern CLI, an 8k token ceiling, and two modules that no longer exist — and it is the file an
  agent reads before changing this repo, so its drift was a live risk of re-introducing what had just
  been removed. It now leads with the invariants that must not be broken.

### Fixed
- **The Claude Code skills were documented under names nobody could type.** Claude Code namespaces
  plugin skills as `/<plugin>:<skill>`, so `skills/requivo-discover/` in a plugin named `requivo` was
  really `/requivo:requivo-discover`. The skills are renamed to `discover`, `answer`, `status`,
  `brief`, `prd`, `impact` — invoked as `/requivo:discover` — and a test now checks the README against
  the actual namespacing.
- **`requivo discover --context` accepted an unknown card with a warning** and carried on with *all*
  cards, which is the opposite of narrowing. It now uses the same Core resolver as the deterministic
  verbs and the Web, and refuses. (0.9.1 fixed this everywhere except the main discovery path.)

### Added
- **The repository is a plugin marketplace** (`.claude-plugin/marketplace.json`), so the plugin install
  is two exact commands — `/plugin marketplace add jbkkz/requivo` then `/plugin install requivo@requivo`
  — instead of the previous "point Claude Code at this directory". The plugin version now tracks the
  Requivo release it was tested against.
- **`THIRD-PARTY-NOTICES.md`**, shipped in the wheel's `dist-info`: the vendored htmx copy, its version,
  its upstream and its licence. 0BSD requires no attribution; a redistributed file should still be
  traceable.
- **The publish workflow gates on the things that make a bad release permanent**: the tag must exist and
  agree with both `pyproject.toml` and `__version__`, the tagged commit is what gets built, and lint,
  tests, `twine check` and an outside-the-repo wheel smoke test (including the web assets) all run
  before the upload. A manual dispatch now requires a tag instead of publishing whatever is on main.

## [0.9.1] - 2026-08-01

Correctness and web-security fixes from an external review of 0.9.0. No new surface, no format change:
a 0.9.0 session is read and written identically.

### Fixed
- **Generation no longer races a concurrent write.** A provider call runs for seconds to minutes, and
  the session can move underneath it (a second browser tab, a CLI apply, a Claude Code turn). The
  revision the model was read at is now captured before the call and carried through both writes: as
  the optimistic-lock precondition on any apply — so a concurrent change surfaces as a clean
  `revision_conflict` instead of being silently overwritten — and as the artifact's recorded source, so
  a document written from revision 1 is never filed as if it came from revision 2. An artifact whose
  inputs moved while it was being generated is now born stale rather than inheriting the newer
  revision's freshness. An answers turn now defaults to the same precondition (the revision it read),
  so the CLI inherits the protection the Web already had from its form.
- **The saved solution assessment goes stale when the model does.** It was excluded from the
  artifact→slot map as "the live analysis layer"; that stopped being true when it became a saved
  artifact, and the result was an assessment still marked fresh after the problem statement under it
  had been rewritten. It now maps to every slot — it is a judgment over the whole model — so any
  material change reaches it.
- **`session show` and `artifact list` agree on freshness.** `session show` treated any artifact behind
  the current revision as stale, contradicting every other view in the same binary. The explicit stale
  flag (set from the dependency graph) is the rule; the source revision is provenance, not a verdict.
  The Claude Code skills said the same wrong thing and have been corrected.
- **Impossible artifact provenance is refused.** An artifact could be recorded against a revision that
  does not exist (or against revision 0). Every freshness answer downstream is read off that number,
  so it is now validated against the session's history at the write.
- **Sonnet 5 launch pricing.** The cost estimate billed the default model at the standard $3/$15 while
  launch pricing ($2/$10, through 2026-08-31) was live, overstating cost by a third. Rates that expire
  now carry their end date, so the estimate is right on both sides of it without another edit.

### Security
- **Cross-site request protection on Requivo Web.** Binding to `127.0.0.1` is not a boundary: any page
  open in the same browser could post to a known local port without a preflight, creating sessions and
  spending the server's Anthropic key — the attacker never needs to read a response to do that damage.
  Writes now require a per-process request token (rendered into every form), and are checked against
  the browser's `Sec-Fetch-Site` hint and an `Origin`/`Referer` host match. A host allowlist (loopback,
  plus `REQUIVO_WEB_ALLOWED_HOSTS` for a deliberate non-local bind) runs on reads too — it is the guard
  against DNS rebinding, where the attacker's page *would* be able to read the token. `requivo web
  --host` records its own bind address, so an intentional non-local run keeps working.
- **Over-long input is refused, not truncated.** A request or answers block past its ceiling was cut
  silently and reasoned over as if whole. Request bodies are also capped before being parsed.
- **An unknown context card is an error.** It used to be filtered out, which left an empty selection —
  and an empty selection means *all* cards, so a typo widened the context instead of narrowing it. The
  CLI already refused; the resolver now lives in Core and both surfaces share it.

## [0.9.0] - 2026-08-01

**Requivo Web — a third interface.** A local, single-user, self-hostable browser UI over the same Core,
services and session format as the CLI and Claude Code. It exists for people less comfortable in a
terminal; it is deliberately bounded — no accounts, auth, database, remote storage, or telemetry
(see `docs/web.md`).

### Added
- **`requivo web`** — launches a local FastAPI + Jinja2 + HTMX interface (the optional `[web]` extra:
  `uv tool install "requivo[web]"`). Binds to `127.0.0.1` by default, opens a browser, prints the URL,
  and warns if bound to a non-local host. The Anthropic key is read from the server environment (never
  the browser) and only needed for provider actions. Options: `--host --port --workspace --no-open
  --reload`.
- **The web interface** (`requivo.web`): home + session list, new discovery (run now or *create session
  only*), a session screen (understanding split with a *partial* coverage marker, readiness + blockers,
  priority questions with a single answers form, persisted decisions/challenges/opportunities,
  artifacts), an answers turn (optimistic-locked, HTMX status refresh reporting changed slots /
  unseated reasoning / stale artifacts), and generation of the **solution assessment** and **PRD**
  (saved with source revision, marked *Draft* when blocking unknowns remain, viewable + downloadable).
  Templates, CSS and a vendored HTMX ship in the wheel — no CDN, works offline.
- **UI aligned to the Requivo landing** — indigo accent, warm off-white, soft-shadow cards, monospace
  meta labels, dot-coded understanding rows (FACT / ASSUM / UNKWN) and a segmented readiness bar. A
  visible loading signal on every action (a top progress bar + an in-button spinner) covering both HTMX
  swaps and full-page submits, so it is always clear that something is happening. Degrades without JS.
- **`DiscoveryService`** (`services/discovery.py`) — the provider-backed orchestration (start / answer /
  generate) extracted so the CLI and Web share exactly one pipeline; neither re-orchestrates "call the
  provider, then apply". `brief_markdown()` renders the assessment as a saveable/downloadable artifact.
- **Optional `web` extra** and web package-data (templates + static) in the wheel; a CI job installs the
  wheel with `[web]`, verifies the assets ship, and hits `/health`.

### Security
- Local by default: localhost bind, structured `RequivoError`s rendered as clean pages (never a
  traceback), every slug validated in Core (no path traversal), only the package `static/` served
  (never the workspace / `.requivo` / `.env` / `.git`), API key never in HTML or logs, all content
  HTML-escaped, bounded input sizes, and conservative headers (`X-Content-Type-Options`,
  `Referrer-Policy`, a same-origin `Content-Security-Policy`).

### Docs
- **Editorial pass: README as orientation, `docs/` as depth.** The README is rewritten as an
  activation guide (434 → 223 lines) — hero, why, a three-interface table, three quickstarts, core
  concepts, a docs index — with the depth moved to ten specialized documents under `docs/`
  (`getting-started`, `cli`, `architecture`, `requirements-model`, `session-format`, `providers`,
  `context-cards`, `evaluations`, `roadmap`, plus an index). Fixed stale/incorrect references (a
  non-existent `discover --provider` flag in the plugin README, "two interfaces" → three, `out/` →
  `.requivo/`) and added a local-Web exposure note to `SECURITY.md`. No behaviour change.

## [0.8.2] - 2026-08-01

Correctness at the session boundary — the layer any external consumer sits on. From the same
external review's session-boundary list.

### Added
- **`SessionRepository` storage seam.** `SessionService` and `ArtifactService` no longer touch the
  filesystem directly — storage is injected as a `SessionRepository` (in `services/repository.py`),
  with `FileSessionRepository` (the default) delegating to `core.persistence`. The canonical-vs-legacy
  `out/` handling now lives inside the file repository, where it belongs. A deployment can supply a
  `PostgresSessionRepository` with the same protocol and reuse the service orchestration verbatim,
  instead of bypassing the service or faking a filesystem. Proven by an in-memory repository the full
  service cycle (create → apply → stale-flag → status → provenance → locking) runs against with zero
  filesystem.
- **Optimistic locking.** `SessionService.update_model` / `save_revision` take an optional
  `expected_revision`; a write whose expectation is stale raises `RevisionConflictError`
  (`revision_conflict`, with `expected`/`actual`) instead of silently landing on top of a concurrent
  update. The single-user CLI omits it; `requivo model apply --expected-revision N` exposes it. Harmless
  locally, required for a concurrent Web service.
- **Per-revision provenance.** Each applied revision now records who produced it — `RevisionRecord`
  (revision, created_at, previous_revision, provider, model_name, surface, model_hash) appended to a
  `revisions` log in `session.json`. Provenance belongs to the revision, not just session creation,
  because a model is moved by more than one surface (Anthropic provider, Claude Code, CLI, later Web)
  over its life. `discover` / `answer` / `model apply` each stamp their surface.
- **Richer `status --json`.** The payload now carries the full picture — `understanding` (per-slot,
  grouped by state, with pillar/completeness/impact and a `thin` flag), priority `questions` (labelled),
  `summary`, `remaining_gaps`, and `context_cards` — so Claude Code and a future Web client render it
  without rebuilding the presentation logic. Built from one shared `model_status` projection used by
  both the CLI and `SessionService.status` (no second implementation).

### Fixed
- **Reasoning references are validated.** `DesignDecision.derived_from` and `Challenge.contests` could
  name a slot the schema doesn't define, letting the dependency graph look rigorous while pointing at
  nothing. The `EngineOutput` contract now rejects unknown slot references, same as the model and the
  questions.
- **First apply no longer invalidates its own reasoning.** A first model carrying decisions/challenges
  reported them all as invalidated on apply (the impact was computed over the *new* model when there
  was no prior). Invalidation is now computed strictly against the prior established reasoning; on a
  first apply nothing is invalidated.
- **Third copy of the artifact-staleness bug.** `cli._status_payload` (what `status --json` actually
  used) still carried the `revision != current` invalidation the 0.8.1 fix removed from
  `ArtifactService.list` and `SessionService.status`. Unifying the two status paths onto `model_status`
  eliminated it.

## [0.8.1] - 2026-08-01

Correctness pass at the surface boundaries, from a full external review of the 0.8.0 snapshot. No
model-format change; the fixes are about *when* an artifact is stale, *when* a session is ready,
*where* a session can be written, and keeping the docs honest. All six pre-release findings from the
review are addressed.

### Fixed
- **Artifact freshness now respects the dependency graph.** `ArtifactService.list` (and `status`)
  flagged *every* artifact stale on *any* revision bump (`revision != current_revision`), defeating the
  selective blast-radius calculation. Freshness is now the explicit `stale` flag, set by
  `update_model`/`mark_stale` for exactly the artifacts a change reaches — an unrelated or
  completeness-only change leaves an artifact fresh. Revision is provenance, not an invalidation rule.
- **Readiness gates on coverage, not just provenance.** A high-impact slot could read as confirmed on
  `confidence == explicit` alone, even at completeness 5. `_readiness_blockers` now requires both
  `explicit` **and** completeness at/above the soft boundary, so a stated-but-thin high-impact
  dimension still blocks.
- **Session slugs are validated in Core (directory-traversal guard).** An explicit `--slug` was joined
  onto the session root unchecked, so `--slug ../../escaped` could write outside `.requivo/sessions/`.
  `validate_slug()` now enforces a strict kebab-case token (and confirms the resolved path stays under
  the root) at the two path constructors, so every surface — CLI, provider, a future web service —
  inherits the guard. New error: `invalid_slug`.
- **CI wheel-install job no longer imports a dead module.** It imported `requivo.core.llm`
  (`build_prompt`, `available_cards`), which the refactor moved to `requivo.core.context`.

### Changed
- **Install-free launcher moved to `scripts/requivo_cli.py`.** A root-level `requivo.py` shadowed the
  `requivo` package on `import requivo` from a checkout (a footgun for editable installs). The root
  `requivo.py` and `pc.py` launchers are removed; use `uv run requivo`, the installed `requivo`, or
  `python scripts/requivo_cli.py` from a bare clone.
- **Documentation reconciled with the shipped 0.8 surface.** README/`CLAUDE.md`/`SECURITY.md` now name
  the canonical `.requivo/sessions/<slug>/` store (not `out/`), the `/requivo-*` plugin commands (not
  `/pc-*`), and the `pip install '.[anthropic]'` / `uv run --extra anthropic` path that discovery
  actually needs. The stale `.claude/commands/pc-*` command files (which called the removed `pc.py`)
  are deleted in favour of the `plugins/claude-code/` plugin.

## [0.8.0] - 2026-08-01

Architectural refactor into **three surfaces over one engine** — Core, CLI, and Claude Code — in
preparation for a future Web UI, plus the formalized **open-source strategy** (the public / private
boundary). The model format is unchanged and the license stays MIT; the refactor itself changed no
behaviour, but this release also ships a robustness fix and a first round of discovery-quality tuning
from the first end-to-end usage test (below).

### Added
- **Requivo for Claude Code** — a plugin (`plugins/claude-code/`) with six skills (`/requivo-discover`,
  `/requivo-answer`, `/requivo-status`, `/requivo-brief`, `/requivo-prd`, `/requivo-impact`). Claude Code
  does the reasoning with your existing session; the deterministic CLI validates and applies. **No
  Anthropic API key required.**
- **Deterministic CLI surface** (no LLM, no key): `requivo doctor`, `requivo schema`, `requivo context`,
  `requivo session init|list|show|migrate|export|import`, `requivo model show|validate|apply|diff`,
  `requivo artifact save|list|show`, plus `--json` on the machine-readable verbs and a `--workspace`
  global. `status` and `impact` now accept a session slug as well as a model path.
- **Versioned session format** at `.requivo/sessions/<slug>/` — `session.json` (metadata + provenance +
  artifact status), `model.json`, `revisions/NNNN-model.json` (history), `request.md`, and `artifacts/`.
  Writes are atomic; a `migrate_session()` version frontier guards forward compatibility.
- **Structured error hierarchy** (`RequivoError` + `code`/`message`/`path`/`details`), serialized as a
  JSON envelope on `--json` failures so Claude Code and the future Web can act on the `code`.
- **Application services** (`SessionService`, `ArtifactService`) — the single validated apply path shared
  by the CLI, the Anthropic provider, and Claude Code. A proposal from any source flows through the same
  validate → diff → propagate → revision → stale-flag pipeline.
- **Reasoning invalidation.** `propagate()` now also reports the **challenges** whose premise a changed
  slot contests (via `contests`), symmetrically to decisions (`derived_from`). When a change unseats a
  decision or premise the saved **assessment** rests on, that assessment is flagged stale — `model apply`
  reports `invalidated_decisions`/`invalidated_challenges`, and `impact` shows *Premises to re-examine*.
- **Open-source governance & distribution boundary.** `docs/open-source-strategy.md` (the Core / CLI /
  Claude Code / Community Web surface map, and the public-vs-private data boundary),
  `CONTRIBUTING.md`, `TRADEMARKS.md`, `GOVERNANCE.md`, and `examples/README.md`. New GitHub templates
  (feature request, pull request, issue-template `config.yml` routing security reports to private
  advisories) and a Gitleaks secret-scan workflow. The README gains **Open source** and **Data and
  privacy** sections; `.gitignore` and `.env.example` are hardened. The generator prompts
  (stories/estimate/prd/criteria/epic/release) now carry the same untrusted-data framing already used
  in discovery and the assessment. The license stays **MIT**.
- **Discovery-quality tuning** (from the first end-to-end usage test): the engine now ranks
  primary-object *lifecycle* questions first (where an object is created / owned / updated / completed
  / sent), asks the stakeholder to confirm expected **behaviour** rather than choose a technical
  mechanism, and no longer asserts unsourced industry consensus ("many teams do X") in the assessment.
  The assessment is titled **Draft Solution Assessment** while a blocking decision remains.

### Fixed
- **Discovery truncation on rich requests.** The per-call output ceiling was 8k tokens; the discovery
  JSON for a messy multi-feature request exceeded it and the whole reply was discarded as truncated.
  Raised to 16k — the non-streaming-safe ceiling — which fits a rich run with headroom and never
  changes an output that already fit.

### Changed
- **`requivo.core` is now provider-free** (guarded by a test): the Anthropic client, the single-call
  loop, the usage ledger, and all discovery/generation moved to `requivo.providers.anthropic`.
- **`anthropic` is now an optional extra.** Core, the deterministic CLI, and the Claude Code plugin
  install and run without the SDK; `pip install 'requivo[anthropic]'` adds the API-powered mode.
- The modern `requivo`/`pc` subcommand CLI (discover, answer, generators) now writes the canonical
  `.requivo/sessions/` store through the services; legacy `out/<slug>/` sessions are read-only and
  migrated on first change (or in bulk via `requivo session migrate`).

### Compatibility
- The legacy flag CLI (`python src/engine.py "…" --prd`) and the `src.engine` re-export shim are
  preserved. The `pc` alias is unchanged. `model.json` format is unchanged.

## [0.7.0] - 2026-07-31

**First release under the name Requivo, and the first published to PyPI** (`pip install requivo`).
Versions 0.1.0–0.6.3 were developed pre-publication under the name Product Copilot.

### Changed
- Renamed **Product Copilot to Requivo**. New positioning: *turn vague requests into validated product
  decisions*. The engine, model format and business behaviour are unchanged — this is a rename only.
- Renamed the Python package from `product_copilot` to `requivo` (no compatibility shim — the project
  has no published distribution yet, so a clean rename is preferred).
- Added `requivo` as the **primary CLI command**; kept `pc` as a temporary backward-compatible alias
  (same entry point) that may be removed in a future major version.
- Renamed the environment variables `PC_OUTPUT_DIR` → `REQUIVO_OUTPUT_DIR` and `PC_CONTEXT_DIR` →
  `REQUIVO_CONTEXT_DIR`, the default user-context directory to `~/.config/requivo/context`, the tracker
  idempotency label to `requivo-epic:<slug>`, and the `session.json` provenance key to `requivo_version`.
- Updated project metadata (name, description, URLs) and documentation to the Requivo identity.

## [0.6.3] - 2026-07-31

Closes the UX gap the 0.6.2 packaging move introduced — pip-installed users can now bring their own
context, the last thing standing between the wheel and a first PyPI release.

### Added
- **User-level context directory (`REQUIVO_CONTEXT_DIR`).** A pip-installed setup can be extended without a
  source checkout: drop cards in `REQUIVO_CONTEXT_DIR` (default `~/.config/requivo/context`) and
  they merge with the bundled cards. A user card whose stem matches a built-in **overrides** it, so a
  bundled card can be tweaked without editing the package. Both feed the same `--context` selector and
  `load_context()`; with no user directory present, behaviour is byte-identical to before (so golden
  baselines are untouched).

## [0.6.2] - 2026-07-31

Packaging: the engine is now a self-contained, pip-installable wheel, and generated output no longer
lands inside the install. This closes the review's top remaining gap — that installing from a wheel
(rather than a clone) would break, because the assets lived outside the package.

### Fixed
- **Assets ship inside the wheel.** The prompts, the framework schema, the context cards and the demo
  payload moved into the package at `src/requivo/assets/` and are declared as package data, so
  a `pip install` outside the clone has everything it needs. Before, they lived at the repo root and a
  wheel install had no prompts or schema — every command that builds a prompt would fail. Git tracked
  the move as renames, so history is preserved.
- **Read-only assets vs writable output are separated.** `paths.py` now exposes `ASSETS` (resolved
  from the package location, read-only — works identically from an editable checkout or a wheel) and
  `output_root()` (`./out` under the working directory, overridable via `REQUIVO_OUTPUT_DIR`). Generated
  models/artifacts are never written inside a possibly read-only install.

### Added
- **Wheel-install CI job.** Builds the wheel, installs it into a clean venv, and drives `pc demo` plus
  a schema load and all eight prompt builds from a directory that is *not* the repo — so the packaging
  invariant (assets resolve from the installed package, not the source tree) is guarded on every push.
- **Frozen demo payload** (`assets/demo/`) so `pc demo` runs from a wheel with no clone. A test asserts
  it stays byte-identical to the browsable `examples/event-checkin-reconciliation/` copy, killing drift.

### Changed
- **README leads with the proof.** Reordered to open on a real before/after — a rambling client email
  and what the engine caught in it (two systems conflated, a disguised-employment exposure, an offline
  constraint, a buried deadline) — followed immediately by `pc demo`, before the theory and diagrams.

## [0.6.1] - 2026-07-31

A boundary-hardening pass from a second external review: durable writes, run provenance, clean
handling of API failures, and context continuity across commands. No engine-logic changes — the core
is unchanged; this hardens what happens at the edges (disk, network, untrusted input).

### Fixed
- **Atomic model/artifact writes.** Every write (`save_model`, `write_artifact`, `session.json`) now
  goes through a temp file + atomic rename, so an interruption can never leave a half-written JSON in
  place of a good one. model.json is the durable product — a truncated write would be unrecoverable.
- **API failures surface as clean messages, not tracebacks.** `client.messages.create()` was called
  outside the retry loop's `try`, so a network drop, timeout, rate limit, or provider outage escaped
  as a raw traceback. `_complete()` now translates any `anthropic.APIError` into an `EngineError` the
  CLI prints as one actionable line ("… The model on disk was not modified. Retry the command."), and
  exits non-zero. The saved model is never touched by a failed call.
- **The output-token ceiling is raised from 4k to 8k.** A rich discovery output (full slot model +
  questions + summary) runs right up against 4k — a simple request already spends ~3.6k output tokens
  — so multi-feature requests were one variance spike away from silent truncation. 8k gives ~2x
  headroom; you pay only for tokens generated, not the ceiling, so smaller outputs cost the same. (A
  per-generator budget is a later refinement.)
- **Genuinely truncated replies fail cleanly instead of feeding the parser garbage.** When a reply is
  cut off at the ceiling (`stop_reason == "max_tokens"`) *and* its JSON won't parse, it's reported
  ("narrow the request, or split it into fewer features per run") rather than retried — the same
  ceiling would truncate again. A reply flagged `max_tokens` whose JSON is nonetheless complete still
  succeeds (the check is parse-first), so outputs sitting right at the boundary aren't wrongly rejected.
- **All text blocks are read, not just the first.** `_response_text()` concatenates every text block
  of a response (skipping thinking/tool_use), so a reply split across blocks isn't silently truncated
  to its opening fragment before JSON extraction.
- **The `--context` selection now persists across commands.** A discovery run with a card subset saved
  its selection nowhere, so `pc answer` and every generator (`prd`, `stories`, `brief`, …) silently
  widened back to all cards — breaking reproducibility and re-diluting the context the run had trimmed.
  The selection is recorded in `session.json` and threaded through `answer_turn()` and all generators.

### Added
- **Run provenance (`session.json`).** Each discovery now writes a sidecar next to `model.json`
  recording the engine version, the Claude model, the context cards used, a SHA-256 of the request,
  and a timestamp — so a run is reproducible and traceable, matching the "the model is a durable
  product" thesis. `model.json` stays a clean `EngineOutput`; readers tolerate the sidecar's absence
  (pre-0.6.1 models simply mean "all cards").
- **Trust boundary against prompt injection.** The engine and assessment prompts now state explicitly
  that the client request, answers, and context cards are *untrusted business data* — material to
  model, never instructions to obey. A new `SECURITY.md` documents what leaves the machine (Anthropic
  API only, no telemetry), the injection posture, and how to report a vulnerability.
- **GitHub issue templates**: a *Real-world discovery feedback* template (the field signal we most
  want — was each question useful, useless, or missing?) and a *Bug report* template.

## [0.6.0] - 2026-07-31

A robustness-and-packaging pass, closing gaps an external code review surfaced.

### Fixed
- **The model's slot set is now guaranteed, closing a readiness blind spot.** `EngineOutput` rejected
  nothing about *which* slots it carried, and readiness inspected only the slots the model returned —
  so a required slot the engine omitted became invisible and a high-impact gap could pass as "ready".
  Now the contract rejects unknown slot ids everywhere, the discovery boundary requires the full
  required set (self-healing through the existing retry loop), readiness reasons over the schema (a
  missing high-impact slot is a blocker, not invisible), and `diff_models()` walks the union of keys so
  a removed slot registers as a change.
- **Output invariants the prompt only suggested are now enforced in the contract.** `EngineOutput`
  caps `questions` at 6 (the prompt asks for 3–6) and rejects any question that targets a slot the
  schema doesn't define — both self-healing through the discovery retry loop.

### Added
- **`pc discover --context <cards>`** — load a chosen subset of `context/*.md` for a discovery instead
  of all of them, so irrelevant cards can't dilute impact estimation. Selection is per-session (held
  constant across the run's turns), so the cached system prefix survives; unknown card names are warned
  and ignored. Partially mitigates the "every card is loaded for every request" known limit.
- **Per-run API usage reporting.** Every `pc` command that hits the API now prints its footprint when
  it finishes — calls, tokens (with the cached share), latency, and an estimated cost. `_complete()`
  records each call into a session-scoped `UsageLedger`; tokens are exact, cost is a labelled estimate
  from a dated rate table (never presented as a bill). Offline verbs (`demo`, `status`, `impact`) print
  nothing.
- **`pc demo`** — a no-API-key, no-argument, no-network walkthrough that replays the event-check-in
  example from its saved outputs: the messy request, the questions the engine raised (rendered live
  from the saved model), and the solution assessment it produced. The zero-friction way to feel the
  product before installing a key.
- **README "Before you rely on it"** section: what leaves your machine, cost shape, models tested,
  known limits (non-determinism, all-cards-loaded), and an explicit no-professional-advice note. The
  quickstart now leads with `pc demo`.
- **Continuous integration** (`.github/workflows/ci.yml`): `ruff` lint plus the test suite across
  Python 3.9–3.13 on every push and pull request.
- **Ruff configuration** and richer packaging metadata (keywords, classifiers, `dev` extra now includes
  `ruff`) in `pyproject.toml`.

### Changed
- **Discovery no longer silently overwrites a colliding slug.** The five-word `out/<slug>/` folder is
  kept when it's free or belongs to the same request (a re-run), but a *different* request that maps to
  the same slug now gets a short deterministic hash suffix (`leave-approval-a3f82c`) instead of
  clobbering the first.
- **Markdown tables escape cell content.** The PRD requirements table now escapes `|` and flattens
  newlines in its cells, so a requirement containing a pipe no longer breaks the table.

## [0.5.0] - 2026-07-31

A hardening-and-proof milestone: the reasoning was validated end to end on real, messy input, the
robustness holes that real input exposes were closed, and the regression lens and docs were finished.

### Added
- A second worked example, `examples/event-checkin-reconciliation/` — a rambling, multi-feature client
  email taken end to end (request → model → assessment → epic → acceptance criteria). The assessment
  refuses the "tie this together" conflation, catches a disguised-employment (*salariat déguisé*)
  exposure the request never mentions, and sequences the two builds against a fixed deadline.
- `golden_diff.py --questions` now prints each challenge's `alternative` and `recommendation`, not just
  the headline — the half of a challenge that separates an architect's pushback from a bare observation.
- Complete `--brief` assessment baselines across all six golden request forms (previously only two), so
  the challenge block can be measured on every problem shape before it is tuned.

### Fixed
- `pc discover` no longer crashes on a real-length request. `Path.exists()` raises above the OS filename
  limit, so any request longer than a tidy sentence — i.e. any real client email — died before reaching
  the engine.
- `pc discover ""` (empty or whitespace request) now fails fast with a usage message instead of crashing
  on `Path("")` resolving to the current directory.

### Changed
- The engine's `system` prompt (prompt + schema + all context cards) is sent as a single
  `cache_control: ephemeral` block, so its prefix is cached across the calls of a session — the K runs of
  a golden capture, the up-to-eight turns of `converse()`, and each JSON retry — cutting repeated-call
  input cost to roughly a tenth.

## [0.4.0] - impact calibration + the dependency DAG

### Added
- `core/dependencies.py` — the dependency DAG made explicit: `propagate()` (blast radius of a change),
  `diff_models()` (material change between two model versions), and `stale_on_disk()`.
- `pc impact` — an offline query for the decisions to re-validate and artifacts to regenerate when a slot
  changes; `pc answer` now runs the diff each turn and warns which generated files no longer match.
- A release-notes generator (`pc release`).

### Changed
- Impact calibration: `impact_default` is a baseline, not a ceiling — a compliance/audit/traceability
  need named anywhere in the request escalates the relevant slots to high impact.

## [0.3.0]

- Repository cleanup: removed the demo GIF cluster and the redundant `requirements.txt` in favour of the
  `pyproject.toml` single source of truth.

## [0.2.0]

- README polish and structure per review feedback.

## [0.1.0]

- Initial public release: the requirements engine and discovery loop, the solution assessment (the
  differentiator — a judgment that contests the request's premises, not a recap), the artifact
  generators (PRD, user stories, estimate, acceptance criteria, delivery epic with GitHub/GitLab
  exports), and the MIT license.

[Unreleased]: https://github.com/jbkkz/requivo/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/jbkkz/requivo/releases/tag/v3.2.0
[3.1.0]: https://github.com/jbkkz/requivo/releases/tag/v3.1.0
[3.0.0]: https://github.com/jbkkz/requivo/releases/tag/v3.0.0
[2.0.0]: https://github.com/jbkkz/requivo/releases/tag/v2.0.0
[1.3.0]: https://github.com/jbkkz/requivo/releases/tag/v1.3.0
[1.2.0]: https://github.com/jbkkz/requivo/releases/tag/v1.2.0
[1.1.0]: https://github.com/jbkkz/requivo/releases/tag/v1.1.0
[1.0.1]: https://github.com/jbkkz/requivo/releases/tag/v1.0.1
[1.0.0]: https://github.com/jbkkz/requivo/releases/tag/v1.0.0
[0.11.0]: https://github.com/jbkkz/requivo/releases/tag/v0.11.0
[0.10.0]: https://github.com/jbkkz/requivo/releases/tag/v0.10.0
[0.9.10]: https://github.com/jbkkz/requivo/releases/tag/v0.9.10
[0.9.9]: https://github.com/jbkkz/requivo/releases/tag/v0.9.9
[0.9.8]: https://github.com/jbkkz/requivo/releases/tag/v0.9.8
[0.9.7]: https://github.com/jbkkz/requivo/releases/tag/v0.9.7
[0.9.6]: https://github.com/jbkkz/requivo/releases/tag/v0.9.6
[0.9.5]: https://github.com/jbkkz/requivo/releases/tag/v0.9.5
[0.9.4]: https://github.com/jbkkz/requivo/releases/tag/v0.9.4
[0.9.3]: https://github.com/jbkkz/requivo/releases/tag/v0.9.3
[0.9.2]: https://github.com/jbkkz/requivo/releases/tag/v0.9.2
[0.9.1]: https://github.com/jbkkz/requivo/releases/tag/v0.9.1
[0.9.0]: https://github.com/jbkkz/requivo/releases/tag/v0.9.0
[0.8.2]: https://github.com/jbkkz/requivo/releases/tag/v0.8.2
[0.8.1]: https://github.com/jbkkz/requivo/releases/tag/v0.8.1
[0.8.0]: https://github.com/jbkkz/requivo/releases/tag/v0.8.0
[0.7.0]: https://github.com/jbkkz/requivo/releases/tag/v0.7.0
[0.6.3]: https://github.com/jbkkz/requivo/releases/tag/v0.6.3
[0.6.2]: https://github.com/jbkkz/requivo/releases/tag/v0.6.2
[0.6.0]: https://github.com/jbkkz/requivo/releases/tag/v0.6.0
[0.5.0]: https://github.com/jbkkz/requivo/releases/tag/v0.5.0
[0.4.0]: https://github.com/jbkkz/requivo/releases/tag/v0.4.0
[0.3.0]: https://github.com/jbkkz/requivo/releases/tag/v0.3.0
[0.2.0]: https://github.com/jbkkz/requivo/releases/tag/v0.2.0
[0.1.0]: https://github.com/jbkkz/requivo/releases/tag/v0.1.0
