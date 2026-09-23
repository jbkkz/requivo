# Adopting the DCO

**Slug:** `adopting-the-dco`

## Context

`CONTRIBUTING.md` said a CLA or DCO "may be introduced before accepting large external
contributions". The MIT → Apache-2.0 relicense was possible only because sole authorship was
provable from history, and the first two external contributions arrived that same week (#439).
Every added contributor raises the cost of the question; waiting for the *large* contribution means
imposing it on a pull request somebody has already written.

## Decision

**Adopt the Developer Certificate of Origin 1.1 now.** Every commit in a pull request carries a
`Signed-off-by:` trailer matching the commit's author email; `.github/workflows/dco.yml` and
`scripts/check_dco.py` check it, and `CONTRIBUTING.md` states what is certified. **A CLA is refused**:
the hosted product consumes `requivo` as an ordinary Apache-2.0 dependency, so there is no
relicensing right the project needs, and a CLA is a signing ceremony against a per-commit trailer.

Deliberately not: a required check on day one (a context that never ran blocks every open PR —
`decision: appending-a-required-check`); signing history already on `main`; checking a PR body or
squash message (the DCO is a per-commit assertion). #343, opened under the MIT-era wording and
merged hours after the relicense, is a few *de minimis* lines, and MIT-inbound is redistributable
under Apache-2.0 — a maintainer note, **not a legal conclusion**.

## What breaking it cost

Nothing yet: the cost is asymmetric in time. It protects against a licensing question answerable
only by reading git history and guessing intent. **Revisit** when a corporate contribution needs a
written grant — the case a CLA exists for.

## Alternatives rejected

- **Inbound=outbound under Apache-2.0 §5 alone** — free today, but leaves provenance as git metadata
  nobody asserted; the last licensing change was unambiguous by luck.
- **A full CLA** — grants a right nobody will use, in front of a first-time contributor.
- **A third-party DCO action or App** — supply-chain posture (#433); the check is forty lines of
  `git log` with no token.
- **Required immediately** — rejected on ordering, not merit.
