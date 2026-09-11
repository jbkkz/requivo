# Governance

Requivo is a young project, and its governance is intentionally light.

## Today

- Requivo is **maintained by its author**, who has final say on what is merged and on the project's
  direction.
- Decisions are made in the interest of the **coherence and long-term health of the project** —
  keeping the architecture clean (the Core / provider / service boundaries in
  [docs/architecture.md](docs/architecture.md)) and the reasoning quality measurable (the golden harness).
- **Technical discussion happens in the open**, in GitHub issues and pull requests. Proposing a
  change or challenging a decision there is welcome.
- There is **no formal SLA**. This is a solo-maintained project; issues and security reports are
  handled on a best-effort basis, with security prioritised over features.
- **A release is cut because of what is in it**, never on a schedule or a merge count: a user-visible
  capability, a fix somebody is waiting on, or a blocking-class security finding — which ships
  immediately, alone if need be. Breaking changes batch and ride the next release that has a reason
  to happen. The argument and the alternatives it rejects are in
  [docs/decisions/0010-a-release-is-justified-by-its-contents.md](docs/decisions/0010-a-release-is-justified-by-its-contents.md);
  the consequence for an integrator is one sentence — pin exactly, `requivo==X.Y.Z`, and bump it as a
  routine chore gated by your own tests — and [docs/compatibility.md](docs/compatibility.md) is the
  list of what that pin protects.
- Behaviour is covered by the [Code of Conduct](CODE_OF_CONDUCT.md), which is in force today.

## Later

If a real contributor community forms, a more formal model (maintainer roles, a written decision
process) can be introduced. This document will be updated when that happens. No foundation, steering
committee, or formal structure exists today — and none is implied.

## Related

- Contribution process: [CONTRIBUTING.md](CONTRIBUTING.md)
- Behaviour: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Security reports: [SECURITY.md](SECURITY.md)
- Name and identity: [TRADEMARKS.md](TRADEMARKS.md)
- Distribution boundary: [docs/open-source-strategy.md](docs/open-source-strategy.md)
