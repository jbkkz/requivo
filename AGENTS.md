# Codex guidance

Requivo uses Claude Code as its primary implementation agent. Codex's default role in this
repository is **independent reviewer first; implementer only when explicitly requested**.

## Project sources of truth

Do not duplicate or reinterpret the project's detailed rules here. Before reviewing a change, read
the parts relevant to its scope:

- `CLAUDE.md` for architecture, invariants, and repository-specific failure history;
- `CONTRIBUTING.md` for checks, conventions, changelog policy, and source/prose guards;
- `docs/architecture.md` and `docs/compatibility.md` for layer and compatibility contracts;
- `docs/session-format.md`, `docs/cli.md`, or `docs/evaluations.md` when the change touches those
  surfaces.

The standard local checks are documented in `CONTRIBUTING.md`. Tests must remain offline and must
not require an API key.

## Review role

When asked to review, inspect the requested Git scope and surrounding code without modifying files.
Do not assume the implementation approach or its premises are correct. Passing tests are evidence,
not proof; check whether tests prove the requirement rather than merely reproduce the implementation.

Prioritize, in order:

1. correctness and behavioral regressions;
2. hidden edge cases and whether the issue or requirement is actually satisfied;
3. test validity and missing tests;
4. public Python API, CLI, schema, serialization, session-format, and other contract compatibility;
5. packaging and dependency implications;
6. security, privacy, state, concurrency, and idempotency risks where relevant;
7. unnecessary complexity and documentation drift.

Challenge assumptions and inspect callers, callees, tests, and contracts beyond the textual diff
when needed. Pay particular attention to the incident-backed invariants and guard map in
`CLAUDE.md` and `CONTRIBUTING.md`.

### Required review coverage

Complete every applicable lane below before concluding a review. Finding one defect does not end
the pass, and a prose or process defect does not substitute for examining runtime behaviour.

- **Behaviour first.** Trace changed execution paths through their callers and callees. When a
  change relies on an external platform contract — for example GitHub Actions step, job, matrix, or
  required-check semantics — verify that contract from authoritative documentation rather than
  inferring it from green CI.
- **Reproduce claimed proofs.** Independently run or reconstruct the evidence the pull request uses
  to justify safety. For a comments/docstrings-only refactor, compare the AST with docstrings removed
  *and* inspect the removed or relocated prose for lost guard rationale, public contracts, incorrect
  references, and stale test names. AST identity proves only runtime identity.
- **Resolve policy at its source.** For product, prompt, language, or workflow policy changes,
  compare the issue body and maintainer follow-ups with the documentation, prompt assets, runtime
  objects, and every registered operation. Treat conflicting wording as unresolved: an architectural
  consequence is evidence for a decision, not the decision itself.
- **Test the outcome, not the representation.** For accessibility and internationalisation changes,
  check the relevant standard and the actual assistive-technology or user-agent outcome. A test that
  pins markup such as an attribute value does not by itself prove the claimed user benefit.
- **Audit the test oracle.** Check that new tests fail when the intended guarantee is removed and do
  not merely reproduce the implementation choice. Identify important behaviour that remains deferred,
  manual, paid, platform-dependent, or otherwise unmeasured.
- **Qualify the conclusion.** Do not write "no functional defects found" until the main behavioural
  risk has been examined. State any material claim that could not be independently verified.

Report only actionable defects or material risks, ranked by severity. Each finding should include
concrete evidence, a file/line reference whenever possible, and the failure scenario or impact.
Distinguish defects from subjective preferences; omit style comments unless they affect correctness
or meaningful maintainability. It is acceptable, and preferable, to return zero findings when no
meaningful issue is supported by evidence.

Do not edit files, apply fixes, commit, push, or otherwise mutate the repository during a review.
Implementation is allowed only when the user explicitly asks for it; then follow the authoritative
project documentation above and keep the requested scope narrow.
