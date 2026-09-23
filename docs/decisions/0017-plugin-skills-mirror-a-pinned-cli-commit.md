# Plugin skills mirror a pinned CLI commit

**Slug:** `plugin-skills-mirror-a-pinned-cli-commit`

## Context

A Claude Code skill cannot import a prompt asset: it is prose in another process, installed from
another channel, on another release cadence. So the plugin's generator skills restate the rules of
`assets/prompts/<type>.md` and its contract. #542 extended keyless generation from `brief`/`prd` to
`stories`, `estimate`, `criteria`, `epic` and `release` — seven copies of rules that also live as a
prompt and a Pydantic contract. `tests/test_plugin_cli_drift.py` (#96) measures the *invocation*
half of the gap (a skill calling a verb the released CLI lacks — a marketplace pins a `main` commit
while `uv tool install` gets the last release). The *rule* half — does a skill's Voice section or
Output format still say what its prompt says — is a semantic comparison no static check can make.

## Decision

**The duplication is accepted, not solved.** The plugin is a second reasoning provider by design,
bound to the CLI only through its stable contract (`artifact save --type <t>` taking Markdown on
stdin). Each generator skill names the prompt file and contract it mirrors and the commit its rules
were read from (`969cfd9` for #542's five) — a citation for a reviewer at release time, not a live
binding. `test_generator_skills_name_the_prompt_they_mirror_and_at_which_commit` holds the citation
to exist; agreement in substance stays a manual diff when either side changes.

## What breaking it cost

Nothing yet on the rule half. The invocation half cost #93 before it had a guard: six skills that
could meet a shell with no `requivo` and mostly said nothing about it.

## Alternatives rejected

- **Ship the prompts as plugin assets** — presumes a matching CLI checkout is installed, the
  assumption `plugin_cli_drift.py` exists to stop making.
- **Generate skill and prompt from one source** — overengineering for seven files whose prose
  differs beyond substitution (a skill also states the revision contract and the save step); nothing
  has drifted yet.
- **Shell out to the CLI's API mode** — removes the duplication by removing keylessness, the point
  of the change (#538).
