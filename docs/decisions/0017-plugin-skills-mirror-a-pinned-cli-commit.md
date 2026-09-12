# Plugin skills mirror a pinned CLI commit

**Slug:** `plugin-skills-mirror-a-pinned-cli-commit`

## Context

`brief` and `prd` were the only two generators the plugin could produce keylessly: their skills
restate the rules `assets/prompts/brief.md`/`prd.md` and their contracts give the CLI's own reasoning
call, because a Claude Code skill cannot `import` a Markdown prompt asset at runtime — it is prose in
a different process, installed from a different channel, on a different release cadence. #542 adds
the same shape for the five remaining artifact types (`stories`, `estimate`, `criteria`, `epic`,
`release`), so the duplication that was two instances is now seven: each `SKILL.md` restates a set of
rules that also lives, in the same repository, as a prompt asset and a Pydantic contract.

Two copies of one set of rules drift. `tests/test_plugin_cli_drift.py` (#96) already measures the
*invocation* half of that gap — a skill calling a CLI verb a released Requivo does not have — because
a marketplace listing pins the plugin to a commit on `main` while `uv tool install requivo` gets the
last PyPI release, and the two artifacts drift by construction between releases. It has no way to
measure the *rule* half: whether a skill's Voice section, its certainty calibration, or its Output
format still says what the prompt file it was copied from says, because that is a semantic
comparison no static check can make without re-deriving the natural-language content itself.

## Decision

**The duplication is accepted, not solved.** The plugin is a second reasoning provider by design —
the same category as `providers/anthropic/`, which also carries its own copy of "how to turn this
model into a PRD" baked into a prompt string — and it is asked to produce the same document with no
LLM call and no dependency on the installed CLI's *version* of that prompt, only its *contract*
(`artifact save --type <t>` accepting Markdown on stdin, which is stable per `docs/compatibility.md`).
It cannot import `assets/prompts/*.md` from inside a Claude Code session; even if it could, the CLI on
a user's machine may be an older or newer release than the plugin commit the marketplace pinned.

So each of the seven generator skills now states, in its own body, which prompt file and which
contract it mirrors, and the commit its rules were read from (`969cfd9` for the five #542 added).
That is a citation for a human reviewing the pair at release time, not a live binding — the plugin
does not, and cannot, verify at runtime that the CLI installed still agrees. `tests/test_plugin.py`'s
`test_generator_skills_name_the_prompt_they_mirror_and_at_which_commit` (#542) holds the citation
itself to exist; it cannot and does not check that the two sets of rules still agree in substance,
which stays a manual diff a maintainer runs when either side changes.

Extending the *invocation* drift guard (`plugin_cli_drift.py`) to catch *rule* drift was considered
and is not tractable at the bar this repository applies to a new guard tier (CLAUDE.md, "A new
source-scanning guard tier is not free"): it would require parsing two independent English prose
documents and asserting semantic equivalence, which is not a property regex or an AST walk can state.
What is tractable, and now guarded, is the citation convention above.

## What breaking it cost

Nothing has yet gone red for the rule half — this is a decision made ahead of an incident, the same
shape `docs/decisions/README.md`'s "Tense" section describes. The invocation half already had one
real cost before it had a guard: #93, six skills that could each meet a shell with no `requivo`
installed and mostly said nothing about it, found only once a script measured the released surface
directly rather than trusting the checkout the two artifacts were compared inside.

## Alternatives rejected

- **Ship the prompt Markdown as a plugin asset the skill reads at runtime.** The plugin bundles no
  Python and executes nothing but the CLI through Bash; reading a file out of `src/requivo/assets/`
  presupposes a specific, matching CLI checkout is what's installed, which is exactly the assumption
  `plugin_cli_drift.py` exists to stop making.
- **A generation script that renders both the prompt asset and the skill body from one shared data
  structure.** Tractable in principle, and overengineering for seven files whose prose differs in
  more than substitution — a skill states the revision contract and the artifact-save step, which a
  CLI prompt never needs to. The two-instance bar this repo applies to a new guard tier or a new
  shared-generation mechanism (CLAUDE.md) has not been met: nothing has drifted yet.
- **Have the skill shell out to `requivo <verb>` in the CLI's own API mode instead of reasoning
  in-session.** This is the keylessness the issue exists to close (#538: "a document menu where five
  of seven rows say *needs an API key* is not the product") — it would remove the duplication by
  removing the point of the change.
