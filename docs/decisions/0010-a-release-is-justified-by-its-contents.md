# A release is justified by its contents

**Slug:** `a-release-is-justified-by-its-contents`

## Context

v1.0.0 → v2.0.0 → v3.0.0 in thirteen days, each major correctly forced by `docs/compatibility.md`'s
breaking rule; a fourth was proposed a day later, and the loop's `merged_prs: 10` trigger fired on a
morning of five merges. Every release was right under the rules, and nothing said what a release is
*for* (#440). The project is maintained when its author has time — a cadence is a promise it will
not make — and its one consumer of consequence pins exactly, so a major costs a chore and the
reader's trust that the number means something.

## Decision

**A release is cut because of what is in it; no PR count or elapsed time is a reason to tag.**

1. **What justifies one:** a user-visible capability (verb, surface, artifact type, skill); a fix
   somebody is waiting on; or a **blocking-class finding** (the loop's `destroys`, `discloses`,
   `executes`, `forges`, `ships-local-state`, containment rows), which ships immediately and alone.
   Anything else waits for the next release that qualifies.
2. **The count and time triggers are removed from `.oss.json`**, not raised. An absent trigger reads
   as *not met, none declared* in `release_trigger.py`; the loop keeps tag authority for the
   blocking class, which must not wait on a keyboard.
3. **A major means correct code stops working.** A fragment is `breaking` only when correct usage
   breaks; an observable that moved on a path no correct code was on is `compatible`, with the
   moved observable named. Both fragments behind the `4.0.0` proposal grade `compatible` under this.
4. **Breaking changes batch**: never held back from a release that is happening, never its reason.
5. **Integrators pin exactly, `requivo==X.Y.Z`,** and bump it as a chore gated by their own tests.

## What breaking it cost

Three majors in thirteen days, and a consumer's `<2.0.0` pin stale within a day of 2.0.0 with
nothing red on either side — a ceiling that read as prudence and starved it of every later fix.
**Revisit** with a second maintainer or a consumer that cannot pin exactly.

## Alternatives rejected

- **Fast majors as policy** — the number becomes a changelog index and signals nothing.
- **A monthly cadence** — still a clock: an empty month releases nothing meaningful, a day-two
  security fix waits.
- **Higher thresholds** — the same policy with a longer fuse.
- **A third fragment grade** — the right diagnosis, but the vocabulary belongs to the vendored
  assembler; rule 3 reaches the outcome by definition.
- **Authority to `maintainer`** — the one remaining trigger is where speed matters most.
