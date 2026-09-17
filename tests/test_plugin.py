"""Static validation of the Claude Code plugin: manifest, skills, the public copy (#542), the pinned-CLI cache in
`plugin-validate.yml` (#299) and plugin/CLI version-skew detection for the shared preflight (#251)."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "claude-code"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
REASONING = PLUGIN / "REASONING.md"
WORKFLOW = REPO / ".github" / "workflows" / "plugin-validate.yml"
sys.path.insert(0, str(PLUGIN / "scripts"))

import version_skew  # noqa: E402
from version_skew import BEHIND, COULD_NOT_LOOK, IN_STEP, check, compare  # noqa: E402
from version_skew import tested_against_version as read_tested_against_version  # noqa: E402

# Claude Code namespaces plugin skills as `/<plugin>:<skill>`.
EXPECTED_SKILLS = {"run", "status", "docs", "brief", "prd", "stories", "estimate", "criteria", "epic", "release"}
# One preferred install command, named in the shared preflight and nowhere else in the skills (#138).
PREFERRED_INSTALL = "uv tool install requivo"
# The generators the CLI's own optional API mode can produce (#542), and what each skill mirrors.
CLI_API_MODE_GENERATORS = ("criteria", "epic", "release", "stories", "estimate")
GENERATOR_PROMPTS = {"stories": ["stories.md"], "estimate": ["stories.md", "estimate.md"],
                     "criteria": ["criteria.md"], "epic": ["epic.md"], "release": ["release.md"]}
ARTIFACT_SKILLS = ("brief", "prd", *CLI_API_MODE_GENERATORS)
SKILLS = {p.parent.name: p.read_text(encoding="utf-8") for p in sorted((PLUGIN / "skills").glob("*/SKILL.md"))}
assert SKILLS, "no skills found -- a guard over them would otherwise pass by having nothing to check"
README = (PLUGIN / "README.md").read_text(encoding="utf-8")


def _cli_commands() -> set[str]:
    """The real top-level `requivo` subcommands, from the argparse tree."""
    from requivo.cli import _build_parser
    return next(set(a.choices) for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction))


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in m.group(1).splitlines()) if _}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _catalog_entry():
    catalog = REPO / ".claude-plugin" / "marketplace.json"
    assert catalog.is_file(), "the repo root must carry a marketplace catalog"
    data = json.loads(catalog.read_text(encoding="utf-8"))
    return catalog, data, next(p for p in data["plugins"] if p["name"] == "requivo")


def _section(text: str, heading: str, flags=re.MULTILINE) -> str:
    """The body under the first `##` heading matching `heading`, up to the next `##` (subsections stay inside)."""
    head = re.search(heading, text, flags)
    assert head, f"no section matches {heading!r}"
    rest = text[head.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


# ── manifest and the public copy (#542) ──────────────────────────────────────────


def test_repo_is_a_marketplace_pointing_at_this_plugin():
    """`/plugin marketplace add jbkkz/requivo` is the documented install path; catalog, manifest and package agree (#118, #92)."""
    from requivo import __version__
    catalog, data, entry = _catalog_entry()
    manifest = _manifest()
    assert manifest["name"] == "requivo" and manifest["description"] and manifest["version"] == __version__
    assert REASONING.is_file() and __version__ not in README, "the README prose must not restate the version"
    assert (catalog.parent.parent / entry["source"]).resolve() == PLUGIN.resolve()
    assert entry["version"] == manifest["version"]
    for field in ("displayName", "description", "homepage"):
        assert entry[field] and entry[field] == manifest[field], f"catalog/manifest drift on {field!r}"
    assert data["description"] and data["description"] != entry["description"]


def test_the_plugin_readme_documents_the_namespaced_skills_and_what_the_generators_need():
    """The landing page, where the reader decides to run the command."""
    for name in EXPECTED_SKILLS:
        assert f"/requivo:{name}" in README, f"{name}: README must document the namespaced invocation"
    assert "/requivo-" not in README
    heading = "## The generators"
    assert heading in README, "the section naming what the CLI's own API mode needs is gone or renamed"
    section = README.split(heading, 1)[1].split("\n## ", 1)[0]
    named = [v for v in CLI_API_MODE_GENERATORS if v in section.lower()]
    assert named, f"{heading!r} names none of the CLI's API-mode generators; is this still that section?"
    assert "ANTHROPIC_API_KEY" in section and "requivo[anthropic]" in section, f"{heading!r} offers {named} without naming the key and extra"


@pytest.mark.parametrize("site", ["plugin.json", "marketplace.json"])
def test_a_description_offering_the_cli_generators_says_they_need_a_key(site):
    text = _manifest()["description"] if site == "plugin.json" else _catalog_entry()[2]["description"]
    if "no API key" not in text:
        pytest.skip(f"{site} makes no keyless claim, so there is nothing to qualify")
    named = [v for v in CLI_API_MODE_GENERATORS if v in text.lower()]
    assert not named or "API mode" in text, f"{site} claims 'no API key' and offers the CLI's {named} without saying those run in API mode"


# ── skills ───────────────────────────────────────────────────────────────────────


def test_exactly_the_expected_skills_exist():
    assert set(SKILLS) == EXPECTED_SKILLS, f"skill set drifted: {set(SKILLS) ^ EXPECTED_SKILLS}"


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_every_skill_meets_the_static_rules(name):
    """Frontmatter, no key, no temp file, the preflight (#93, #138, #512), Bash only (#121), the arc (#539)."""
    text = SKILLS[name]
    fm, body = _frontmatter(text), text.split("---", 2)[2]
    assert fm.get("name") == name and fm.get("description") and "allowed-tools" in fm, f"{name}: frontmatter"
    assert "ANTHROPIC_API_KEY" not in text or "not need" in text.lower() or "no api key" in text.lower(), f"{name}: must not require an API key"
    assert "--provider anthropic" not in text, f"{name}: must not call the Anthropic provider"
    assert not re.search(r"(edit|write)\s+[^\n]*model\.json", text, re.IGNORECASE), f"{name}: must not hand-edit model.json"
    assert "/tmp" not in text and not re.search(r"^\s*rm\s", text, re.MULTILINE), f"{name}: must not stage content in /tmp or need `rm`"
    assert "Write" not in text.split("---")[1], f"{name}: no skill needs the Write tool now"
    assert re.search(r"preflight", body, re.IGNORECASE) and "REASONING.md" in body, f"{name}: must run the shared preflight and point at REASONING.md"
    if re.search(r"Read `\$\{CLAUDE_PLUGIN_ROOT\}/REASONING\.md`", body):
        assert "unless you already hold it" in body, f"{name}: instructs a read of REASONING.md without the once-per-session condition"
    tools = fm.get("allowed-tools", "")
    assert "Read" in tools, f"{name}: allowed-tools must include Read, or it cannot open REASONING.md"
    stray = re.search(r"\b(pip[\d.]*|pipx|uv(\s+\w+)?)\s+install\b", text)
    assert not stray, f"{name}: states an install command of its own ({stray.group(0)!r}); REASONING.md names the one"
    assert re.search(r"\bBash\(", tools), f"{name}: declares no Bash grant ({tools!r}); revisit the README prerequisite with it"
    assert not re.search(r"\b(PowerShell|Shell)\b", tools), f"{name}: declares a second route to the CLI beside Bash"
    others = {re.sub(r"[^a-z]", "", m) for m in re.findall(r"/requivo:([a-z]+)", body)} - {name}
    assert others, f"{name}: body names no other skill; its own `# /requivo:{name}` heading does not count"


def test_the_preflight_names_its_probe_and_one_install_command():
    section = _section(REASONING.read_text(encoding="utf-8"), r"^##\s+.*preflight.*$", re.IGNORECASE | re.MULTILINE)
    assert "requivo doctor" in section and PREFERRED_INSTALL in section, "the preflight must name the probe and one install command"


def test_skills_reference_only_real_cli_commands():
    commands = _cli_commands()
    assert commands, "could not introspect CLI commands"
    for name, text in SKILLS.items():
        for cmd in re.findall(r"requivo (\w[\w-]*)", text):
            assert cmd in commands, f"{name}: references unknown `requivo {cmd}`"


def test_mutating_skills_apply_through_the_cli_and_state_a_recovery_path():
    """`run` changes the model: it MUST apply through the CLI on stdin, and emit the proposal once (#511)."""
    text = SKILLS["run"]
    assert "model apply <slug> - --expected-revision" in text, "run: must pass the proposal on stdin under the optimistic lock"
    assert re.search(r"`code`\s*/\s*`details`", text), "run: must name the structured error fields a refused apply is fixed from"
    assert "revision_conflict" in text, "run: must name the one refusal that is not about the proposal"
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        assert "requivo model validate" not in block, "run: a dry run ahead of the apply costs a second emission of the model"


def test_session_scoped_skills_read_the_session_s_context_cards():
    """A session's card selection is held constant across its turns (#539)."""
    for name in ("brief", "run"):
        assert "context --session" in SKILLS[name], f"{name}: must read context scoped to the session"


@pytest.mark.parametrize("name", ARTIFACT_SKILLS)
def test_artifact_saving_skills_state_the_revision_they_reasoned_from(name):
    """Every artifact skill saves via the CLI, and every `artifact save` line states `--revision` (#6, #519, #542)."""
    lines = [ln for ln in SKILLS[name].splitlines() if "artifact save" in ln]
    assert lines, f"{name}: must save via `requivo artifact save`"
    for ln in lines:
        assert "--revision" in ln, f"{name}: `artifact save` must state the revision it reasoned from: {ln.strip()}"


def test_the_pages_that_build_a_proposal_name_every_field_a_question_is_made_of():
    """A page that asks Claude to emit `questions` must spell out the fields one is made of (#489)."""
    from requivo.core.contracts import Question
    required = sorted(n for n, f in Question.model_fields.items() if f.is_required())
    assert "q" in required, "the Question contract no longer has a `q` field -- update this guard"
    pages = {**SKILLS, "REASONING.md": REASONING.read_text(encoding="utf-8")}
    checked = [n for n, t in pages.items() if "`questions`" in t and re.search(r"model (validate|apply)", t)]
    for name in checked:
        missing = [f for f in required if not re.search(rf'[`"]{f}[`"]', pages[name])]   # as a code span or a JSON key
        assert not missing, f"{name} asks for `questions` and never names {missing} (#489)"
    assert "run" in checked, f"only {checked} were found to build a proposal -- this guard is watching almost nothing"


def test_skill_enum_placeholders_name_values_the_contracts_accept():
    """A `"field": "a|b|c"` placeholder is a prompt the deterministic CLI validates the answer to, so a wrong alternative is not a typo."""
    from requivo.core.contracts import Complexity, Confidence, Impact, Level, Leverage, Priority, ScenarioKind
    enums = {"leverage": (Leverage,), "confidence": (Confidence,), "impact": (Impact,), "priority": (Priority,),
             "kind": (ScenarioKind,), "complexity": (Complexity, Level)}   # a field can be backed by two enums
    for name, text in SKILLS.items():
        for field, value in re.findall(r'"(\w+)"\s*:\s*"([a-zA-Z_]+(?:\|[a-zA-Z_]+)+)"', text):
            if field in enums:
                allowed = {m.value for e in enums[field] for m in e}
                bad = sorted(set(value.split("|")) - allowed)
                assert not bad, f"{name}: \"{field}\" offers {bad}, but the contract accepts {sorted(allowed)}"


def test_generator_skills_name_the_prompt_they_mirror_and_at_which_commit():
    """The Watch-for in #542: each generator skill states which prompt it mirrors and at which commit."""
    for name, prompts in GENERATOR_PROMPTS.items():
        text = SKILLS[name]
        for prompt in prompts:
            assert prompt in text, f"{name}: does not name the prompt file it mirrors ({prompt})"
        assert re.search(r"\bcommit\b", text, re.IGNORECASE) and re.search(r"`[0-9a-f]{7,40}`", text), f"{name}: names no commit"


def test_run_pins_its_three_stop_conditions_and_never_asks_mid_loop():
    """`/requivo:run` (#539) is one continuous conversation with three stop conditions and no hand-back (#538, #545)."""
    text = SKILLS["run"]
    section = _section(text, r"^##\s*8\.\s*Stop.*$")
    assert "readiness.ready" in section and "/requivo:docs" in section, "run: the stop section names `readiness.ready` and ends on /requivo:docs"
    for pattern, what in ((r"questions[\s\S]{0,40}empty|empty[\s\S]{0,40}questions", "an empty `questions` list"),
                          (r"user says stop|says to stop", "the user saying stop"), (r"say which", "saying which condition ended the loop"),
                          (r"[Nn]ever suggest running[\s\S]{0,20}/requivo:run", "never handing back into the loop")):
        assert re.search(pattern, section, re.IGNORECASE), f"run: the stop section must name {what}"
    assert re.search(r"[Nn]ever ask.{0,80}slug", text), "run: must state it never asks the user for a slug mid-loop"
    assert re.search(r"never.{0,120}/requivo:\*", text) or re.search(r"never.{0,120}another `/requivo:", text), \
        "run: must state it never tells the user to run another /requivo:* command mid-loop"


# ── the pinned Claude Code CLI install in `plugin-validate.yml` is cached (#299) ────────────────


def _job_block(job_key: str, next_job_key: str | None, comments: bool = True) -> str:
    """One top-level job's YAML, from ` <job_key>:` to the next top-level job key (or EOF)."""
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"\n  {job_key}:\n")
    block = text[start:] if next_job_key is None else text[start:text.index(f"\n  {next_job_key}:\n", start + 1)]
    return block if comments else "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))


def test_the_gate_job_caches_the_pinned_cli_install():
    """The cache exists, keyed on the pin, before the install; a hit reads back; the `@latest` job stays uncached."""
    gate = _job_block("validate", "drift")
    assert "actions/cache" in gate, "the gate job's pinned CLI install has no actions/cache step (#299)"
    rest = gate[gate.index("actions/cache"):]
    next_step = rest.find("\n      - ", 1)
    cache_block = rest[:next_step] if next_step != -1 else rest
    assert "CLAUDE_CLI_VERSION" in cache_block, "the cache key does not reference CLAUDE_CLI_VERSION:\n" + cache_block
    assert gate.index("actions/cache") < gate.index("Install the pinned Claude Code CLI"), "the cache step must precede the install"
    assert "steps.cache-claude-cli-npm.outputs.cache-hit" in _job_block("validate", "drift", comments=False), \
        "nothing in the gate job's step bodies reads back the cache-hit output, so a caching regression is invisible"
    assert "actions/cache" not in _job_block("drift", None), "the advisory `@latest` job must stay uncached"


def test_the_cache_hit_guard_fires_when_the_readback_is_removed():
    """The must-fire half for the test above: strip the functional reference while leaving the neighbouring comment."""
    gate = _job_block("validate", "drift", comments=False)
    reverted = gate.replace('if [ "${{ steps.cache-claude-cli-npm.outputs.cache-hit }}" = "true" ]; then',
                            'echo "cache status unknown -- not actually read back"')
    assert "steps.cache-claude-cli-npm.outputs.cache-hit" not in reverted, "the fixture did not remove the reference"


# ── plugin/CLI version skew for the shared preflight (#251) ─────────────────────────────────────


def _doctor_json(version: str) -> str:
    return json.dumps({"requivo_version": version, "python_version": "3.12.0"})


@pytest.mark.parametrize(("cli", "plugin", "state"), [
    ("1.4.0", "1.3.0", IN_STEP), ("1.3.0", "1.3.0", IN_STEP), ("1.2.0", "1.3.0", BEHIND), ("1.3", "1.3.0", IN_STEP),
], ids=["newer-cli-is-in-step", "equal-is-in-step", "older-cli-is-behind", "differing-precision-is-not-behind"])
def test_an_older_cli_is_behind_and_warns(cli, plugin, state):
    """Both directions, so `IN_STEP` cannot be returned no matter what; a true prefix is not smaller; behind never refuses."""
    result = compare(cli, plugin)
    assert result.state == state, result
    assert cli in result.message or plugin in result.message
    if state == BEHIND:
        assert cli in result.message and plugin in result.message
        assert "refuse" not in result.message.lower() and "stop" not in result.message.lower()


@pytest.mark.parametrize(("stdout", "error"), [
    (None, "the `requivo` command was not found on PATH"), ("", None), ("not json at all {{{", None),
    (json.dumps({"python_version": "3.12.0"}), None), (_doctor_json("unknown"), None),
], ids=["doctor-call-failed-outright", "empty-doctor-output", "unparseable-doctor-json",
        "doctor-json-missing-requivo-version", "non-version-shaped-cli-version"])
def test_doctor_input_that_cannot_be_read_is_could_not_look(stdout, error):
    """Could-not-read must never render as 'versions match', and the message must say so."""
    result = check(stdout, error)
    assert result.state == COULD_NOT_LOOK and result.state != IN_STEP
    assert "not" in result.message.lower() or "could" in result.message.lower()


def test_check_reads_the_manifest_end_to_end():
    """The positive control and the whole flow: doctor output in, the real manifest read live, a verdict out."""
    assert check(_doctor_json("1.3.0"), None).state != COULD_NOT_LOOK
    assert read_tested_against_version(MANIFEST) == _manifest()["version"]
    assert check(_doctor_json("0.1.0"), None, manifest_path=MANIFEST).state == BEHIND


@pytest.mark.parametrize("manifest", ["{not json", json.dumps({"version": "unreleased"})], ids=["unparseable", "non-version-shaped"])
def test_a_bad_manifest_is_could_not_look_not_in_step(tmp_path, manifest):
    """`_parse_version` tolerates a non-numeric TRAILING component (`.dev0`, `-rc1`), not a bare word (self-review)."""
    bad = tmp_path / "plugin.json"
    bad.write_text(manifest, encoding="utf-8")
    if manifest.startswith("{not"):
        with pytest.raises((ValueError, OSError)):
            read_tested_against_version(bad)
    result = check(_doctor_json("1.3.0"), None, manifest_path=bad)
    assert result.state == COULD_NOT_LOOK, f"got state={result.state} message={result.message!r}"


def test_reasoning_md_names_the_skew_check_without_hardcoding_a_version():
    """REASONING.md is what Claude actually reads at runtime; a bare X.Y.Z in it is the duplicated literal."""
    text = REASONING.read_text(encoding="utf-8")
    assert "version_skew.py" in text or "requivo_version" in text, "REASONING.md's preflight does not mention the skew check"
    assert ".claude-plugin/plugin.json" in text, "REASONING.md must point at the manifest as the source of the version"
    stray = re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])", text)
    assert not stray, f"REASONING.md hardcodes what looks like a version number: {stray}"


# `subprocess.TimeoutExpired` is a `SubprocessError`, not an `OSError` (#251); reachable since #263's 30s lock timeout.
_TIMEOUT = subprocess.TimeoutExpired(cmd=["requivo", "doctor", "--json"], timeout=30)


def _main_after(monkeypatch, capsys, exc):
    """`version_skew.main()` with `subprocess.run` raising `exc`: the exit code and what was printed."""
    def run(*args, **kwargs):
        raise exc
    monkeypatch.setattr(version_skew.subprocess, "run", run)
    return version_skew.main(), capsys.readouterr().out


@pytest.mark.parametrize(("exc", "expected"), [
    (_TIMEOUT, "30"), (OSError("permission denied"), "permission denied"), (FileNotFoundError("requivo"), "PATH"),
], ids=["subprocess-timeout", "plain-os-error", "missing-binary"])
def test_main_reports_could_not_look_on_subprocess_timeout(monkeypatch, capsys, exc, expected):
    """Each arm is COULD_NOT_LOOK (exit 3) with a message naming its cause, never an uncaught exception (#363)."""
    code, message = _main_after(monkeypatch, capsys, exc)
    assert code == COULD_NOT_LOOK and "traceback" not in message.lower()
    assert expected in message, message


def test_main_distinguishes_a_timeout_from_a_missing_binary(monkeypatch, capsys):
    """A timeout and a missing binary both land in COULD_NOT_LOOK, and read differently (#263)."""
    _, timeout_message = _main_after(monkeypatch, capsys, _TIMEOUT)
    _, missing_message = _main_after(monkeypatch, capsys, FileNotFoundError("requivo"))
    assert timeout_message != missing_message
    assert "PATH" in missing_message and "PATH" not in timeout_message


def test_main_must_fire_control_a_genuine_skew_still_reports_skew(monkeypatch, capsys):
    """The must-fire twin, paired with the three could-not-look arms above."""
    old_version = "0.0.1"
    assert old_version != _manifest()["version"]  # guard the fixture's own assumption
    monkeypatch.setattr(version_skew.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=_doctor_json(old_version)))
    assert version_skew.main() == BEHIND
    assert old_version in capsys.readouterr().out
