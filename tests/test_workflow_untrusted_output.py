"""Untrusted CLI output can forge GitHub Actions workflow commands: `TryParseV2` trims leading whitespace before matching `::name::` (indenting doesn't defeat it), and the legacy `TryParse` matches `##[` unanchored anywhere in a line (collapsing to one line doesn't defeat it either). `::stop-commands::<token>`
fences both while leaving the log readable -- added to the advisory step by #147. #96 hardened a sibling form in scripts/plugin_cli_drift.py but not this one; #176 closed the gap where squashing covered `TryParseV2` but not the unanchored `TryParse` (`_log_safe` now breaks both keys at the value). #177 found
three more unfenced `claude`-running steps -- an install step and two required gates whose verdict IS the exit code -- and added the fence-plus-captured-exit-code pattern the gate tests below check."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plugin-validate.yml"
STEP_NAME = "Spec drift against the current CLI (advisory, never fails)"

# Every command the runner registers (`ActionCommandManager`) -- the full vocabulary, not just what this workflow
# emits, same argument as `_assert_no_forged_workflow_command` in test_plugin_cli_drift.py.
_REGISTERED = frozenset({
    "set-env", "set-output", "add-mask", "add-path", "debug", "warning", "error", "notice",
    "group", "endgroup", "save-state", "echo", "add-matcher", "remove-matcher", "stop-commands",
})

# The three command lines this step authors. Anything else the runner would act on is forged.
_AUTHORED = (
    "::stop-commands::",
    "::warning title=Drift check could not look::",
    "::warning title=Plugin spec drift::",
)

# A marker no line of this workflow contains, carried by every line the fake `claude` prints -- what tells "the guard
# held" from "the harness never reached the hostile output".
_MARK = "FORGED"

# Must-fire controls strip these keys to re-forge the output, so each must match only the *effective* line, never a
# comment describing it. `$latest` (bare) matches only the squash re-read -- the six interpolation sites spell it
# `${latest}`.
_SQUASH_KEY = "$latest"
_FENCE_KEYS = ("::stop-commands::", "::${fence}::")

_STUB_TEMPLATE = """#!/bin/sh
if [ "$1" = "--version" ]; then
cat <<'VER'
2.1.238 ##[error]FORGED-VIA-VERSION-LEGACY in the middle of the first line
::error title=FORGED-VIA-VERSION::a version string that broke its own line
VER
exit @EXIT@
fi
cat <<'OUT'
Checking manifest
::error title=FORGED-AT-COLUMN-0::validator output starting at column 0
  ::error title=FORGED-BEHIND-AN-INDENT::the runner trims leading space before matching
plain ##[error]FORGED-IN-THE-LEGACY-FORM which is matched anywhere in a line
OUT
exit @EXIT@
"""

def _claude_stub(exit_code: int = 0) -> str:
    """A `claude` that forges in both parser forms and exits `exit_code`. Parametrized (#177): a stub that only ever exits 0 can't tell a fence that preserved the exit code from one that swallowed it."""
    return _STUB_TEMPLATE.replace("@EXIT@", str(exit_code))

_CLAUDE_STUB = _claude_stub()

_NPM_STUB = "#!/bin/sh\nexit 0\n"

# Every YAML block-scalar header a `run:` can carry (`|`/`>`, chomping + indent indicators). Matching only exact
# `run: |` missed `run: |-` and hid an unfenced step from every check here (#177); over-matching is the safe direction.
_BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")

def _run_steps(text):
    """Every step in `text` that carries a `run:`, as `(name, script)` pairs, in file order. No YAML parser: handles every `run:` form, since matching only exact `run: |` hid an unfenced step from every check here (#177). Takes text, not the workflow, so the control below can feed it synthetic YAML."""
    lines = text.splitlines()
    steps, name, name_indent, i = [], None, -1, 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith("- "):
            # A new step begins here, unless nested *inside* one -- compare indent rather than clear the name on
            # every `- ` line.
            if name is None or indent <= name_indent:
                name = (stripped[len("- name:"):].strip()
                        if stripped.startswith("- name:") else None)
                name_indent = indent
        key = stripped[2:].strip() if stripped.startswith("- ") else stripped
        label = name or f"<unnamed step at line {i + 1}>"
        if not key.startswith("run:"):
            i += 1
            continue
        value = key[len("run:"):].strip().split(" #", 1)[0].strip()
        if value and not _BLOCK_SCALAR.match(value):
            steps.append((label, value + "\n"))
            i += 1
            continue
        body, k = [], i + 1
        while k < len(lines):
            if not lines[k].strip():
                body.append("")
            elif len(lines[k]) - len(lines[k].lstrip()) <= indent:
                break
            else:
                body.append(lines[k])
            k += 1
        steps.append((label, textwrap.dedent("\n".join(body)).rstrip() + "\n"))
        i = k                         # past the block, so a `run:` inside a heredoc is not a step
    return steps

def _all_run_steps():
    steps = _run_steps(WORKFLOW.read_text(encoding="utf-8"))
    if not steps:
        pytest.fail(f"no `run:` step found in {WORKFLOW} at all -- the extractor is broken, and an empty scan set is "
                    f"an all-clear nobody earned")
    return steps

def _step_script(step_name: str = STEP_NAME) -> str:
    """One named step's shell, dedented. Every way this can fail to find the block is a hard failure rather than an empty string: an extractor that quietly returns nothing turns every test below green."""
    found = [script for name, script in _all_run_steps() if name == step_name]
    if len(found) != 1:
        pytest.fail(f"expected exactly one step named {step_name!r} in {WORKFLOW}, found {len(found)}")
    script = found[0]
    # Weaker than testing for `claude plugin validate` since #177 added a `claude --version`-only step; still catches
    # an extractor that grabbed the wrong block entirely.
    assert "claude" in script, "extracted the wrong block:\n" + script
    return script

def _pinned_version() -> str:
    match = re.search(r'^\s*CLAUDE_CLI_VERSION:\s*"([^"]+)"',
                      WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "the workflow no longer sets CLAUDE_CLI_VERSION; this harness supplies it"
    return match.group(1)

def _parse(line, resume_token):
    """What `ActionCommand.TryParseV2` and then `TryParse` would make of one log line: models both asymmetries that make the obvious fixes insufficient -- V2 trims leading whitespace first, and V1 is unanchored."""
    known = _REGISTERED | ({resume_token} if resume_token else set())
    stripped = line.lstrip()
    if stripped.startswith("::"):
        end = stripped.find("::", 2)
        if end >= 0:
            name = stripped[2:end].split(" ", 1)[0]
            if name in known:
                return name, stripped[end + 2:]
    start = line.find("##[")
    if start >= 0:
        closing = line.find("]", start)
        if closing >= 0:
            name = line[start + 3:closing].split(" ", 1)[0]
            if name in known:
                return name, line[closing + 1:]
    return None

def _processed(log):
    """The lines the runner would act on, and the fence token still open at the end (None when closed). A token left open is its own finding: annotations after it are suppressed."""
    acted_on, token = [], None
    for line in log.splitlines():
        parsed = _parse(line, token)
        if parsed is None:
            continue
        name, data = parsed
        if token is not None:
            if name == token:
                token = None
            continue
        acted_on.append(line)
        if name == "stop-commands":
            token = data.strip()
    return acted_on, token

def _forged(acted_on):
    return [line for line in acted_on
            if not any(line.lstrip().startswith(prefix) for prefix in _AUTHORED)]

def _exec(script, tmp_path, claude_stub=None, shell=("bash", "-e")):
    """Run an extracted step with a `claude` that forges and an `npm` that does nothing; return the whole `CompletedProcess` (exit code included -- that's what a gate is judged on). `bash -e` on stdin is what GitHub runs a shell-less `run:` block with, sidestepping path translation under Git Bash; `shell` also
    lets the exit-code tests run under plain `bash`, since the #177 fences carry their verdict via an explicit `exit` rather than assumed errexit."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this platform. UNTESTED HERE: that the workflow's own shell contains the validator's "
                    "output end to end. The workflow itself runs on ubuntu-latest only, and the structural tests "
                    "assert their half on every leg.")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    for name, body in (("claude", _CLAUDE_STUB), ("npm", _NPM_STUB)):
        path = stub_dir / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env["CLAUDE_CLI_VERSION"] = _pinned_version()

    # Probe before asserting anything -- staging an extensionless shell script reachable by name off PATH is the
    # platform-dependent part (Git Bash on Windows); probed with the exit-0 stub so a failing-stub caller can't get
    # a manufactured skip instead of a real check.
    probe = subprocess.run(["bash", "-e"], input="claude --version\n", env=env,
                           cwd=str(tmp_path), capture_output=True, text=True)
    if probe.returncode != 0 or _MARK not in probe.stdout:
        pytest.skip(
            f"this platform's bash cannot reach a staged stub by name (exit {probe.returncode}, stderr " f"{probe.stderr[:200]!r}). UNTESTED HERE: that the workflow's own shell contains the validator's output " "end to end. The workflow runs on ubuntu-latest only, and the structural tests assert their half "
            "everywhere.")

    if claude_stub is not None:
        (stub_dir / "claude").write_text(claude_stub, encoding="utf-8")
        os.chmod(stub_dir / "claude", 0o755)

    return subprocess.run(list(shell), input=script, env=env, cwd=str(tmp_path),
                          capture_output=True, text=True)

def _run(script, tmp_path):
    """`_exec` for the advisory step, whose contract is that it always exits 0."""
    proc = _exec(script, tmp_path)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, "the step is meant to exit 0 always:\n" + log
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    return log

def test_the_step_still_carries_both_containments():
    """The half that runs on every leg, including one with no bash. Structural rather than behavioural on purpose: says the two containments are still in the file and still bracket the output, and the three tests below say what they do."""
    script = _step_script()
    assert "::stop-commands::" in script, script
    fence_open = script.index("::stop-commands::")
    loop = script.index("claude plugin validate")
    assert fence_open < loop, "the fence opens after the output it contains:\n" + script
    resume = script.index("::${fence}::", fence_open)
    assert resume > loop, "the fence closes before the output it contains:\n" + script
    assert script.count(_SQUASH_KEY) == 1, (
        "the version string is no longer squashed at capture, or is squashed somewhere the "
        "controls below cannot find:\n" + script)

def test_the_validator_output_cannot_forge_a_workflow_command(tmp_path):
    """The must-not-fire half, end to end through the real shell in the real workflow file."""
    log = _run(_step_script(), tmp_path)
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log
    assert any(line.lstrip().startswith("::stop-commands::") for line in acted_on), \
        "the fence never opened, so the assertion above passed for the wrong reason:\n" + log
    # And the log still reads as it did: every line the validator printed survives, verbatim.
    for expected in ("Checking manifest", "FORGED-AT-COLUMN-0", "FORGED-BEHIND-AN-INDENT",
                     "FORGED-IN-THE-LEGACY-FORM"):
        assert expected in log, "the hardening ate the output it was meant to contain:\n" + log

def test_removing_the_fence_lets_the_validator_forge_one(tmp_path):
    """Must-fire control for the fence: without it, the assertion above would pass against a runner model that parses nothing, a stub that printed nothing, or an extractor that returned nothing."""
    script = _step_script()
    stripped = "\n".join(line for line in script.splitlines()
                         if not any(key in line for key in _FENCE_KEYS)) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 2, f"expected to strip exactly the two fence lines, stripped {removed}"

    acted_on, _ = _processed(_run(stripped, tmp_path))
    forged = _forged(acted_on)
    # Three shapes, and the loop runs the validator over both manifests, so six lines.
    assert len(forged) == 6, f"expected all six forged lines unfenced, got: {forged}"
    assert any("FORGED-AT-COLUMN-0" in line for line in forged), forged
    assert any("FORGED-BEHIND-AN-INDENT" in line for line in forged), \
        "an indented `::` must count: the runner trims before it matches"
    assert any("FORGED-IN-THE-LEGACY-FORM" in line for line in forged), \
        "`##[error]` mid-line must count: the legacy parser is unanchored"

def test_removing_the_version_squash_lets_the_version_string_forge_one(tmp_path):
    """Must-fire control for the other containment: `claude --version` is interpolated into six lines that begin at column 0, all outside the fence, so the fence cannot cover it."""
    script = _step_script()
    stripped = "\n".join(line for line in script.splitlines() if _SQUASH_KEY not in line) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 1, f"expected to strip exactly the squash line, stripped {removed}"

    acted_on, _ = _processed(_run(stripped, tmp_path))
    forged = _forged(acted_on)
    # Both forms: the `pinned=` echo is the one unwrapped column-0 line this value reaches, and a newline gives `::`
    # a line start while `##[` needs none at all.
    assert any("FORGED-VIA-VERSION::" in line for line in forged), \
        f"the version string could not forge a `::` line even unsquashed: {forged}"
    assert any("FORGED-VIA-VERSION-LEGACY" in line for line in forged), \
        f"the version string could not forge a `##[` line even unsquashed: {forged}"

# -- The three steps that are NOT advisory (#177) ---------------------------------------------
# The install step and the two gate steps run `claude` straight into the log, unfenced -- required checks whose
# verdict IS the exit code. A fork PR's plugin-manifest field name forges via the unanchored legacy form (read-only
# token, no secrets at risk). Fenced rather than sanitised like the version string, since a gate's output is what a
# human reads when it's red.

_GATE_STEPS = (
    "Install the pinned Claude Code CLI",
    "Validate the plugin manifest (gate)",
    "Validate the marketplace catalog (gate)",
)

_FENCE_OPEN, _FENCE_CLOSE = _FENCE_KEYS

# A `claude` invocation at command position, not `"claude" in line`: the install step's package name
# `@anthropic-ai/claude-code@...` is a string this workflow wrote, not a process it starts.
_CLAUDE_INVOCATION = re.compile(r"(?:^|[;&|(\s])claude\s", re.M)

def _without_comments(script):
    """The script with whole-line `#` comments blanked to spaces, so every offset is unchanged. A prose line inside a `run:` block that says `claude ...` is not an invocation -- without this the guard below would report the paragraph explaining the fence as the thing the fence misses."""
    return "\n".join(" " * len(line) if line.lstrip().startswith("#") else line
                     for line in script.splitlines())

def _steps_running_claude():
    return [(name, script) for name, script in _all_run_steps()
            if _CLAUDE_INVOCATION.search(_without_comments(script))]

def test_the_step_extractor_reads_every_run_form():
    """The extractor everything else here rests on, so its blind spots become theirs. #177 found `_run_steps` matched the block scalar only as exact `run: |`, losing an unfenced `claude` step's body -- invisible to every check. Asserted against synthetic text, since a guard exercised only on a form the file
    doesn't use proves nothing."""
    text = (
        "jobs:\n" "  j:\n" "    steps:\n" "      - name: Literal\n" "        run: |\n"
        "          claude plugin validate --strict .\n" "      - name: Stripped\n" "        run: |-\n"
        "          claude plugin validate --strict .\n" "      - name: Kept\n" "        run: |+\n"
        "          claude plugin validate --strict .\n" "      - name: Folded\n" "        run: >-\n"
        "          claude plugin validate --strict .\n" "      - name: Explicit indent\n" "        run: |2\n"
        "          claude plugin validate --strict .\n" "      - name: Commented header\n"
        "        run: | # why this is a block\n" "          claude plugin validate --strict .\n"
        "      - name: One line\n" "        run: claude plugin validate --strict .\n"
        "      - name: Nested sequence first\n" "        with:\n" "          args:\n" "            - --strict\n"
        "        run: |\n" "          claude plugin validate --strict .\n"
        "      - uses: actions/checkout@v7\n"
    )
    steps = _run_steps(text)
    assert [name for name, _ in steps] == [
        "Literal", "Stripped", "Kept", "Folded", "Explicit indent", "Commented header",
        "One line", "Nested sequence first"], steps
    for name, script in steps:
        assert script.strip() == "claude plugin validate --strict .", (name, script)
        assert _CLAUDE_INVOCATION.search(_without_comments(script)), (
            f"{name}: the body was dropped, so every check in this module would look straight past an unfenced step "
            "written this way")

    # And the must-not-fire half: a step that runs no CLI must not be conjured into the set.
    assert _run_steps("      - name: Nothing\n        uses: actions/checkout@v7\n") == []

def test_every_step_that_runs_the_cli_contains_what_it_prints():
    """The class guard, not three per-step assertions -- #147 and #176 were both one hardened half and one missed, so this checks every step that starts `claude`, whatever it's named: fence or capture its output, or go red under its own name. #177 is why: naming the three steps it found would leave a fourth to
    be discovered the same way."""
    steps = _steps_running_claude()
    assert len(steps) == 4, (
        "the set of steps that run the Claude CLI changed. Every one of them needs a decision: " "fence its output, or capture and sanitise it, and if it is a gate, carry its exit code out "
        f"past the fence. Add it to _GATE_STEPS if it is one. Found: {[n for n, _ in steps]}")

    offenders = []
    for name, script in steps:
        code = _without_comments(script)
        if _FENCE_OPEN not in code:
            offenders.append((name, "runs the CLI with no ::stop-commands:: fence anywhere"))
            continue
        open_at = code.index(_FENCE_OPEN)
        close_at = code.find(_FENCE_CLOSE, open_at)
        if close_at < 0:
            offenders.append((name, "opens a fence and never closes it"))
            continue
        for match in _CLAUDE_INVOCATION.finditer(code):
            if open_at < match.start() < close_at:
                continue
            line_start = code.rfind("\n", 0, match.start()) + 1
            line_end = code.find("\n", match.start())
            line = code[line_start:line_end if line_end != -1 else len(code)]
            # Outside the fence, capture-into-a-variable is the only other accepted containment -- the version
            # string is the one instance; see test_removing_the_version_squash_lets_the_version_string_forge_one.
            if "$(claude" in line:
                continue
            offenders.append((name, line.strip()))
    assert offenders == [], (
        "a step runs the Claude CLI straight into the log, outside any fence (#177). Wrap it: open " "::stop-commands:: with an unguessable token, run the command, echo the token back.\n"
        + "\n".join(f"  {name}: {detail}" for name, detail in offenders))

@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_carries_its_exit_code_past_the_fence(step_name):
    """The structural half of the exit-code question, so it runs on every leg. `echo` clobbers `$?`, so closing the fence destroys the verdict unless it was captured first; a fence written without the two lines below turns a required check into one that always passes -- worse than the defect it was fixing."""
    script = _step_script(step_name)
    assert "|| code=$?" in script, (
        f"{step_name!r} does not capture the CLI's exit code, so closing the fence loses it:\n" + script)
    assert script.rstrip().endswith('exit "$code"'), (
        f"{step_name!r} does not re-raise the captured exit code as its own, so the step reports the exit status "
        f"of the echo that closed the fence:\n" + script)

@pytest.mark.parametrize("step_name", [name for name, _ in _steps_running_claude()])
def test_no_step_lets_the_cli_forge_a_workflow_command(step_name, tmp_path):
    """The behavioural half of the class guard: every step that runs the CLI, through the real shell, against a `claude` that forges in both parser forms."""
    proc = _exec(_step_script(step_name), tmp_path)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log

@pytest.mark.parametrize("step_name", _GATE_STEPS)
@pytest.mark.parametrize("shell", [("bash", "-e"), ("bash",)], ids=["errexit", "no-errexit"])
def test_a_gate_step_reports_the_cli_failure_through_its_fence(step_name, shell, tmp_path):
    """The must-fire half deciding whether this fix was worth making: a `claude` that forges AND exits 7. The step must come back exactly 7, not merely non-zero (a lost code failing for some other reason would satisfy `!= 0` and prove nothing), with nothing forged. Run under both `bash -e` and plain `bash` --
    neither fence assumes GitHub's shell."""
    proc = _exec(_step_script(step_name), tmp_path, claude_stub=_claude_stub(7), shell=shell)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    assert proc.returncode == 7, (
        f"the gate's verdict did not survive its fence: expected exit 7, got {proc.returncode}. A required check "
        f"that cannot fail is worse than one that can be forged.\n" + log)
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed:\n" + log

@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_passes_when_the_cli_passes(step_name, tmp_path):
    """The other half of the same question: a fence that always fails is not a gate either, and the test above cannot tell one from a fence that works."""
    proc = _exec(_step_script(step_name), tmp_path)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"the step failed against a CLI that exited 0: {proc.returncode}\n" + log)

def test_removing_the_exit_line_lets_a_failing_gate_report_success(tmp_path):
    """Must-fire control for the exit-code half: with the re-raise removed, an identical run against a CLI that exits 7 has to come back 0 -- otherwise the assertions above were passing for some other reason."""
    script = _step_script("Validate the plugin manifest (gate)")
    stripped = "\n".join(line for line in script.splitlines()
                         if 'exit "$code"' not in line) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 1, f"expected to strip exactly the re-raise line, stripped {removed}"

    proc = _exec(stripped, tmp_path, claude_stub=_claude_stub(7))
    assert proc.returncode == 0, (
        "the control removed nothing: the step failed anyway, so the exit-code assertions above prove nothing " f"about the line they name (exit {proc.returncode})\n"
        + proc.stdout + proc.stderr)

def test_removing_the_fence_lets_a_gate_step_forge_one(tmp_path):
    """Must-fire control for the fence on a gate step: same control as the advisory step's above, on the step whose output nobody had contained."""
    script = _step_script("Validate the marketplace catalog (gate)")
    stripped = "\n".join(line for line in script.splitlines()
                         if not any(key in line for key in _FENCE_KEYS)) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 2, f"expected to strip exactly the two fence lines, stripped {removed}"

    proc = _exec(stripped, tmp_path)
    acted_on, _ = _processed(proc.stdout + proc.stderr)
    forged = _forged(acted_on)
    # One manifest, three forged shapes -- unlike the advisory step, which loops over two.
    assert len(forged) == 3, f"expected all three forged lines unfenced, got: {forged}"
    assert any("FORGED-AT-COLUMN-0" in line for line in forged), forged
    assert any("FORGED-BEHIND-AN-INDENT" in line for line in forged), \
        "an indented `::` must count: the runner trims before it matches"
    assert any("FORGED-IN-THE-LEGACY-FORM" in line for line in forged), \
        "`##[error]` mid-line must count: the legacy parser is unanchored"
