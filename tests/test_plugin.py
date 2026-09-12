"""Static validation of the Claude Code plugin.

No Claude, no runtime — these assert the plugin's shape and that its skills honour the contract:
they drive the deterministic CLI, never require an API key, and never hand-edit model.json.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
SKILLS = PLUGIN / "skills"
# Claude Code namespaces plugin skills as `/<plugin>:<skill>`, so the directory name must NOT repeat
# the plugin name — `skills/requivo-discover/` in a plugin called `requivo` is invoked as
# `/requivo:requivo-discover`, which is not what any of the docs said.
EXPECTED_SKILLS = {"discover", "answer", "status", "brief", "prd", "impact",
                   "stories", "estimate", "criteria", "epic", "release"}
# One preferred install command, named in the shared preflight and nowhere else in the skills. The
# plugin's own README may name it too — that file is a reader's document, not an instruction Claude
# follows, and it is not walked here. Its verbs are not unchecked, though (#138): they are checked
# by `test_the_plugin_readme_names_only_verbs_this_checkout_has`, which reads that page's code spans
# and never its prose.
PREFERRED_INSTALL = "uv tool install requivo"


def _cli_commands() -> set[str]:
    """The real top-level `requivo` subcommands, from the argparse tree — the source of truth the
    skills' command references are checked against."""
    from requivo.cli import _build_parser
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def _skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


# ── manifest ─────────────────────────────────────────────────────────────────────


def test_manifest_present_and_valid():
    manifest = PLUGIN / ".claude-plugin" / "plugin.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"] == "requivo"
    assert data["version"] and data["description"]


def test_readme_present():
    assert (PLUGIN / "README.md").is_file()
    assert (PLUGIN / "REASONING.md").is_file()


def test_repo_is_a_marketplace_pointing_at_this_plugin():
    # `/plugin marketplace add jbkkz/requivo` is the documented install path, and it only works if the
    # repo root carries a catalog whose `source` actually resolves to the plugin directory.
    catalog = PLUGIN.parents[1] / ".claude-plugin" / "marketplace.json"
    assert catalog.is_file(), "the repo root must carry a marketplace catalog"
    data = json.loads(catalog.read_text(encoding="utf-8"))
    entry = next(p for p in data["plugins"] if p["name"] == "requivo")
    assert (catalog.parent.parent / entry["source"]).resolve() == PLUGIN.resolve()
    # The catalog and the manifest are edited in different files; drift makes the install lie.
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert entry["version"] == manifest["version"]
    # The label, the description and the homepage are the storefront (#118) — the catalog line is
    # what a reader scans in a list of thousands, the manifest line is what they land on after.
    # One sentence, two hand-edited files, which is exactly the shape the version above drifted in.
    for field in ("displayName", "description", "homepage"):
        # Non-empty as well as equal. Equality alone is satisfied by two blanks, and a
        # synchronised blank is exactly the shape a careless edit of both files produces —
        # measured: emptying `displayName` and `homepage` in both files passed all 15 tests
        # in this module before this line existed.
        assert entry[field], f"{field!r} is empty in the catalog entry"
        assert entry[field] == manifest[field], f"catalog/manifest drift on {field!r}"
    # The marketplace's own description is a different sentence: what this catalog offers, not
    # what the plugin does. `claude plugin validate --strict .` refuses the manifest without one
    # (#92) — this is the other half, that the cheap fix of copying the plugin's line is not it.
    assert data["description"] and data["description"] != entry["description"]


def test_the_plugin_version_tracks_the_package_version():
    """Four files declare a version — pyproject, the package, the plugin manifest, the marketplace
    catalog — and each release edits them by hand. The plugin's had silently fallen a release behind,
    which matters because the skills call CLI verbs and the version is the only thing telling a user
    which CLI they were tested against. A hand-edited number needs a test, not a convention."""
    from requivo import __version__

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    # And the prose must not restate it: the copy in the README is exactly what drifted before.
    assert __version__ not in (PLUGIN / "README.md").read_text(encoding="utf-8")


def test_documented_skill_invocations_are_namespaced():
    # Claude Code always namespaces plugin skills as `/<plugin>:<skill>`. The README documented
    # `/requivo-discover`, which no user could ever type successfully.
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    for name in EXPECTED_SKILLS:
        assert f"/requivo:{name}" in readme, f"{name}: README must document the namespaced invocation"
    assert "/requivo-" not in readme


# ── skills ───────────────────────────────────────────────────────────────────────


def test_exactly_the_expected_skills_exist():
    found = {p.parent.name for p in _skill_files()}
    assert found == EXPECTED_SKILLS, f"skill set drifted: {found ^ EXPECTED_SKILLS}"


def test_each_skill_frontmatter_name_matches_dir():
    for p in _skill_files():
        fm = _frontmatter(p.read_text(encoding="utf-8"))
        assert fm.get("name") == p.parent.name, f"{p.parent.name}: frontmatter name mismatch"
        assert fm.get("description"), f"{p.parent.name}: missing description"
        assert "allowed-tools" in fm, f"{p.parent.name}: must declare allowed-tools"


def test_no_skill_requires_an_api_key_or_the_anthropic_provider():
    for p in _skill_files():
        text = p.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY" not in text or "not need" in text.lower() or "no api key" in text.lower(), \
            f"{p.parent.name}: must not require an API key"
        assert "--provider anthropic" not in text, f"{p.parent.name}: must not call the Anthropic provider"


def test_skills_reference_only_real_cli_commands():
    commands = _cli_commands()
    assert commands, "could not introspect CLI commands"
    for p in _skill_files():
        for cmd in re.findall(r"requivo (\w[\w-]*)", p.read_text(encoding="utf-8")):
            assert cmd in commands, f"{p.parent.name}: references unknown `requivo {cmd}`"


def test_mutating_skills_apply_through_the_cli_and_state_a_recovery_path():
    """discover/answer change the model — they MUST go through the CLI on stdin, never by editing
    `model.json`, and they must say what to do when the CLI refuses.

    This used to also assert `"model validate" in text`, i.e. that the dry run was mandatory. That is
    stronger than the rule it was written for, and it cost a second full emission of the model on the
    way to every revision — the largest block of context a plugin turn spends (#511). `model apply`
    runs the identical validation before it writes and a refusal leaves the session untouched, so the
    dry run buys nothing on the happy path and costs one extra emission on the unhappy one;
    `test_a_refused_apply_writes_nothing_and_answers_like_validate` is what holds that up.

    What is load-bearing is pinned instead: apply through the CLI, on stdin, under the optimistic-lock
    precondition, with a stated route out of a refusal. A skill that emits a proposal and says nothing
    about `code`/`details` sends the reasoning session into a retry loop with no error to read."""
    for name in ("discover", "answer"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert "model apply" in text, f"{name}: must apply via the CLI"
        assert "model apply <slug> - --expected-revision" in text, (
            f"{name}: must pass the proposal on stdin under the optimistic-lock precondition")
        assert re.search(r"`code`\s*/\s*`details`", text), (
            f"{name}: must name the structured error fields a refused apply is fixed from")
        assert "revision_conflict" in text, (
            f"{name}: must name the one refusal that is not about the proposal")
        # And the proposal is emitted *once*. A `requivo model validate` inside a fenced block is the
        # dry-run-then-apply shape returning: two heredocs carrying the same model, one of which
        # decides nothing. Checked against the command blocks rather than the prose, because both
        # skills legitimately still *discuss* `model validate` — the narrow cases it remains right
        # for are named in REASONING.md and pointed at from here.
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
            assert "requivo model validate" not in block, (
                f"{name}: runs `model validate` as a command — the mutating skills apply directly, "
                "and a dry run ahead of the apply costs a second emission of the whole model")
    # No skill should instruct writing/editing model.json directly.
    for p in _skill_files():
        assert not re.search(r"(edit|write)\s+[^\n]*model\.json", p.read_text(encoding="utf-8"), re.IGNORECASE), \
            f"{p.parent.name}: must not hand-edit model.json"


def test_no_skill_stages_content_through_a_temp_file():
    """Temp files cost more than they looked. `/tmp/requivo-proposal.json` was one shared path, so two
    sessions working at once overwrote each other; `/tmp/requivo:prd.md` is not even a legal filename on
    Windows; and cleanup needed `rm`, which the plugin does not grant itself. Content the skill already
    holds goes in on stdin — so the convention is pinned here rather than left to habit."""
    for p in _skill_files():
        text = p.read_text(encoding="utf-8")
        assert "/tmp" not in text, f"{p.parent.name}: must not stage content in /tmp"
        assert not re.search(r"^\s*rm\s", text, re.MULTILINE), \
            f"{p.parent.name}: must not need `rm` — it is not in allowed-tools"
    # And the grant should not outlive the need: nothing writes files any more.
    for p in _skill_files():
        front = p.read_text(encoding="utf-8").split("---")[1]
        assert "Write" not in front, f"{p.parent.name}: no skill needs the Write tool now"


def test_session_scoped_skills_read_the_session_s_context_cards():
    """A session's card selection is held constant across its turns — it is what the impact estimates
    were made against. A later turn calling bare `requivo context` reads every card and reasons from a
    wider context than the model was built on, which the golden harness has measured as a real cost."""
    for name in ("answer", "brief"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert "context --session" in text, f"{name}: must read context scoped to the session"


def test_artifact_saving_skills_use_the_cli():
    for name in ("brief", "prd"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert "artifact save" in text, f"{name}: must save via `requivo artifact save`"


def test_artifact_saving_skills_state_the_revision_they_reasoned_from():
    """`--revision` is the one fact only the skill holds, and since #6 a save without it is refused
    rather than filled in with the session's current revision.

    Both skills already pass it — this is here so they cannot stop. A skill is prose, so nothing else
    would notice: the drift would land on a user as a refused save several turns after the edit, and
    the composition (a service that refuses, a skill that does not state it) is invisible to a review
    of either one on its own.
    """
    for name in ("brief", "prd"):
        lines = [ln for ln in (SKILLS / name / "SKILL.md").read_text(encoding="utf-8").splitlines()
                 if "artifact save" in ln]
        assert lines, f"{name}: no `artifact save` line to check — the scan found nothing to speak for"
        for ln in lines:
            assert "--revision" in ln, (
                f"{name}: `artifact save` must state the revision it reasoned from: {ln.strip()}")


def test_every_skill_has_an_answer_for_an_unavailable_requivo():
    """The plugin ships skills; the `requivo` CLI is a separate PyPI install. So the first Bash call of
    any skill can meet a shell that has never heard of the command, and five of the six skills used to
    say nothing about it — `status` and `impact` among them, which are the read-only ones a new user
    tries first (#93).

    Held structurally rather than by wording, because the prose next to it is rewritten often and a
    test that pins sentences makes documentation expensive to write (#96). What is pinned is the
    shape: the shared statement exists in REASONING.md, it names the probe and exactly one preferred
    install command, every skill points at it, and every skill is *able* to read it. The last two are
    what stop the next skill added from dropping the preflight silently.

    The install command is asserted to live in REASONING.md and **nowhere else** in the plugin's
    skills. Six copies of an install line is how one of them ends up naming a command that has been
    superseded, and `discover` already carried a second copy that had drifted to `pip install`.
    """
    reasoning = (PLUGIN / "REASONING.md").read_text(encoding="utf-8")
    head = re.search(r"^##\s+.*preflight.*$", reasoning, re.IGNORECASE | re.MULTILINE)
    assert head, "REASONING.md must carry the shared preflight section"
    rest = reasoning[head.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)          # `###` subsections stay inside the section
    section = rest[: nxt.start()] if nxt else rest

    assert "requivo doctor" in section, "the preflight must name the probe it runs"
    assert PREFERRED_INSTALL in section, (
        f"the preflight must name one preferred install command ({PREFERRED_INSTALL!r}); offering two "
        "with no guidance is the failure #93 names")

    files = _skill_files()
    assert files, "no skills found — this test would otherwise pass by having nothing to check"
    for p in files:
        text = p.read_text(encoding="utf-8")
        fm = _frontmatter(text)
        body = text.split("---", 2)[2]
        name = p.parent.name
        assert re.search(r"preflight", body, re.IGNORECASE), \
            f"{name}: must run the shared preflight before its first `requivo` call"
        assert "REASONING.md" in body, f"{name}: must point at the shared statement"
        # ...and a skill that instructs a *read* must make it conditional. REASONING.md has always
        # opened with "Read this once per session"; four skills then told Claude to read it
        # unconditionally, so a discover → answer → answer → brief flow re-read the longest document
        # the plugin ships four times, into a context that keeps every copy (#512). The two skills
        # that only point at the preflight section are untouched — they never asked for a read.
        # Matched on the imperative rather than on the sentence, so the prose stays rewritable.
        if re.search(r"Read `\$\{CLAUDE_PLUGIN_ROOT\}/REASONING\.md`", body):
            assert "unless you already hold it" in body, (
                f"{name}: instructs a read of REASONING.md without the once-per-session condition "
                "that file's own opening rule states")
        # A pointer a skill cannot follow is not a pointer. `status` and `impact` shipped without the
        # Read tool, so `${CLAUDE_PLUGIN_ROOT}/REASONING.md` was unreachable from exactly the two
        # skills a new user reaches first.
        assert "Read" in fm.get("allowed-tools", ""), \
            f"{name}: allowed-tools must include Read, or it cannot open REASONING.md"
        # Stated once. A skill that grows its own install line is how the two drift apart. The
        # pattern is wider than the three spellings REASONING.md uses, because the drift arrives as a
        # *helpful* variant — `pip3 install`, `uv pip install`, `python -m pip install` — and a guard
        # that only knows the sanctioned wording cannot see the unsanctioned one.
        stray = re.search(r"\b(pip[\d.]*|pipx|uv(\s+\w+)?)\s+install\b", text)
        assert not stray, (
            f"{name}: states an install command of its own ({stray.group(0)!r}) — the preflight in "
            "REASONING.md is the single place that names one")


def test_every_skill_body_points_at_another_skill():
    """The six skills are one arc — discover, answer, brief, prd, with status and impact alongside —
    and a user only walks it if each step says where the next one is. Three bodies carried a forward
    pointer and three did not, and the three that did not included `status`, which is what a returning
    user reaches for first: the worst place for the chain to break.

    Checked on the **body**, deliberately not on the frontmatter. A `description` naming another skill
    would satisfy a whole-file scan while doing nothing for the reader, and the descriptions are the
    routing signal a model matches on when choosing between skills — six of them each reciting the
    same six-verb arc makes them more similar to one another, which spends the most valuable text in
    the plugin on the least discriminating content. So the pointer belongs in the body and the
    assertion has to look there.

    Its own H1 does not count. That is the trap this would otherwise fall into: every SKILL.md opens
    with `# /requivo:<name>`, so a naive scan for the invocation form passes on all six whatever the
    bodies say — a guard that cannot fail, on the exact convention it claims to hold.
    """
    files = _skill_files()
    assert files, "no skills found — this test would otherwise pass by having nothing to check"
    for p in files:
        name = p.parent.name
        body = p.read_text(encoding="utf-8").split("---", 2)[2]
        others = {re.sub(r"[^a-z]", "", m) for m in re.findall(r"/requivo:([a-z]+)", body)} - {name}
        assert others, (
            f"{name}: body names no other skill — a user finishing here is told nothing about where "
            f"to go next. Its own `# /requivo:{name}` heading does not count.")


def test_the_pages_that_build_a_proposal_name_every_field_a_question_is_made_of():
    """A page that asks Claude to emit `questions` must spell out the fields one is made of — `q`
    above all, because `question` is the natural guess and nothing in the prose said otherwise.

    Sibling of the enum test below, and the same defect class approached from the other side: that
    one is a placeholder offering values the contract rejects, this one is a *required field the
    prose never names at all*, which leaves the reader to guess. `Question` is `extra="forbid"`, so
    the guess costs a whole validate cycle -- two errors per question (`questions.N.q Field
    required`, `questions.N.question Extra inputs are not permitted`), 12 for a six-question turn.
    Reproduced against the real CLI, and spent for real on a plugin session reported by an external
    contributor (#489). Recoverable, since the first error of each pair names `q`; avoidable
    entirely by writing the field down.

    Scoped to the pages that actually build a proposal -- the ones naming `model validate` or `model
    apply` -- rather than to every mention of the word, because `status` merely *reports* open
    questions and owes the reader nothing about their shape. Held as a property and not as a
    sentence, per #96: it asserts the field names are somewhere on the page a reader is following,
    never where or in what words, so these pages stay cheap to rewrite.
    """
    from requivo.core.contracts import Question

    required = sorted(n for n, f in Question.model_fields.items() if f.is_required())
    assert "q" in required, "the Question contract no longer has a `q` field — update this guard"

    checked = []
    for path in [*_skill_files(), PLUGIN / "REASONING.md"]:
        text = path.read_text(encoding="utf-8")
        if "`questions`" not in text or not re.search(r"model (validate|apply)", text):
            continue
        checked.append(path.parent.name)
        # Named as a code span (`q`) or as a JSON key ("q"), because both put the exact string in
        # front of the reader. Which one a page uses is formatting, and formatting is not the rule.
        missing = [f for f in required if not re.search(rf'[`"]{f}[`"]', text)]
        assert not missing, (
            f"{path.parent.name}/{path.name} asks for `questions` and never names {missing}. A "
            f'required field the prose leaves out is guessed at, and `extra="forbid"` turns the '
            f"guess into a wasted validate cycle (#489).")
    assert len(checked) >= 2, (
        f"only {checked} were found to build a proposal — this guard is watching almost nothing")


def test_skill_enum_placeholders_name_values_the_contracts_accept():
    """A skill's JSON template is a prompt: Claude fills it in and the deterministic CLI validates the
    result. So a wrong alternative in a `"field": "a|b|c"` placeholder is not a typo in a comment — it
    is an instruction to produce output the contract rejects, and the failure lands one step later, on
    an apply, as a schema error the reader has no reason to connect to the skill.

    The brief skill offered `"leverage": "low|medium|high"`; `Leverage` is high|medium|future. The
    provider's own prompt was right, which is exactly why this drifted unnoticed — the second surface
    had no test holding it to the same vocabulary."""
    from requivo.core.contracts import Complexity, Confidence, Impact, Level, Leverage, Priority, ScenarioKind

    # A field name can be backed by more than one enum across the contracts (`complexity` is S/M/L on
    # an estimate item and low/medium/high on the brief), so a placeholder is checked against the union.
    enums = {
        "leverage": (Leverage,), "confidence": (Confidence,), "impact": (Impact,),
        "priority": (Priority,), "kind": (ScenarioKind,), "complexity": (Complexity, Level),
    }
    for p in _skill_files():
        for field, value in re.findall(r'"(\w+)"\s*:\s*"([a-zA-Z_]+(?:\|[a-zA-Z_]+)+)"', p.read_text(encoding="utf-8")):
            if field not in enums:
                continue
            allowed = {m.value for e in enums[field] for m in e}
            bad = sorted(set(value.split("|")) - allowed)
            assert not bad, (f"{p.parent.name}: \"{field}\" offers {bad}, "
                             f"but the contract accepts {sorted(allowed)}")


def test_every_skill_reaches_the_cli_through_the_bash_tool():
    """The plugin README states a native-Windows prerequisite — Git for Windows — because Claude
    Code's Bash tool on that platform is Git Bash, and every skill drives the deterministic CLI
    through it (#121).

    That claim is true because of a property of these six files, and nothing was holding it true. A
    skill added tomorrow that declares no Bash grant makes the prerequisite over-broad; a skill that
    reached a shell some other way makes it incomplete. Either way the README goes wrong in silence,
    because prose has no failing build.

    Pinned as the property, never the wording. #96 is explicit that the Markdown must not be
    asserted verbatim: a test that quotes a sentence makes the page expensive to rewrite, and this
    page is rewritten often. So neither assertion below reads the README at all — they read what the
    README is *about*, and each message names the sentence that has to move if it fires.
    """
    files = _skill_files()
    assert files, "no skills found — this test would otherwise pass by having nothing to check"
    for p in files:
        name = p.parent.name
        tools = _frontmatter(p.read_text(encoding="utf-8")).get("allowed-tools", "")
        assert re.search(r"\bBash\(", tools), (
            f"{name}: declares no Bash grant ({tools!r}). Every skill reaches the `requivo` CLI "
            "through the Bash tool, and the plugin README states Git for Windows as a native-"
            "Windows prerequisite on exactly that basis. If this skill genuinely needs no shell, "
            "the prerequisite needs revisiting in the same change.")
        # The other direction, and the harder one to notice: a second shell tool would leave the
        # prerequisite over-broad rather than false, so the page would still read as correct.
        # Nothing here forbids adding one. It asks that the README move in the same commit.
        other = re.search(r"\b(PowerShell|Shell)\b", tools)
        assert not other, (
            f"{name}: declares {other.group(0)!r} alongside Bash — a second route to the CLI, so "
            "the README's blanket Git-for-Windows prerequisite is no longer the whole answer for "
            "this skill.")


# ── generator skills (#542: keyless parity for stories/estimate/criteria/epic/release) ────────────

GENERATOR_SKILLS = {"stories", "estimate", "criteria", "epic", "release"}
# What each mirrors, per the issue's own table (#542).
GENERATOR_PROMPTS = {
    "stories": ["stories.md"], "estimate": ["stories.md", "estimate.md"],
    "criteria": ["criteria.md"], "epic": ["epic.md"], "release": ["release.md"],
}


def test_generator_skills_use_the_cli_and_state_the_revision():
    """Same contract as brief/prd (#519, #6): save via the CLI, and every `artifact save` line
    states `--revision`, never left implicit. Its own function so a rebase alongside #539 stays
    a one-line addition rather than a merge on a shared assertion (#542)."""
    for name in GENERATOR_SKILLS:
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert "artifact save" in text, f"{name}: must save via `requivo artifact save`"
        lines = [ln for ln in text.splitlines() if "artifact save" in ln]
        for ln in lines:
            assert "--revision" in ln, f"{name}: must state the revision on the same line: {ln!r}"


def test_generator_skills_name_the_prompt_they_mirror_and_at_which_commit():
    """The Watch-for in #542 asks each skill to state which prompt it mirrors and at which commit,
    since a skill and a prompt asset are two copies of one set of rules that will drift (`decision:
    plugin-skills-mirror-a-pinned-cli-commit`). Checked mechanically: the filename and a commit-like
    hash must both appear, never that the two agree in substance — that half stays a human review."""
    for name, prompts in GENERATOR_PROMPTS.items():
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for prompt in prompts:
            assert prompt in text, f"{name}: does not name the prompt file it mirrors ({prompt})"
        assert re.search(r"\bcommit\b", text, re.IGNORECASE), f"{name}: does not say 'commit'"
        assert re.search(r"`[0-9a-f]{7,40}`", text), f"{name}: names no commit-like hash"
