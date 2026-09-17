"""Untrusted CLI output cannot forge a GitHub Actions workflow command (#147, #176, #177): `TryParseV2` trims
leading whitespace before matching `::name::`, the legacy `TryParse` matches `##[` anywhere in a line, and a
`::stop-commands::<token>` fence contains both. Every step that runs `claude` is fenced, and a gate step still
carries its exit code out past the fence."""
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

# Every command the runner registers, not just what this workflow emits.
_REGISTERED = frozenset({
    "set-env", "set-output", "add-mask", "add-path", "debug", "warning", "error", "notice",
    "group", "endgroup", "save-state", "echo", "add-matcher", "remove-matcher", "stop-commands",
})
# The command lines this workflow authors; anything else the runner would act on is forged.
_AUTHORED = ("::stop-commands::", "::warning title=Drift check could not look::", "::warning title=Plugin spec drift::")
_MARK = "FORGED"  # carried by every line the fake `claude` prints: tells "the guard held" from "never reached"
# Must-fire controls strip these keys; each matches only the effective line, never a comment about it.
_SQUASH_KEY = "$latest"
_FENCE_KEYS = ("::stop-commands::", "::${fence}::")
_FENCE_OPEN, _FENCE_CLOSE = _FENCE_KEYS

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
    """A `claude` that forges in both parser forms and exits `exit_code` (#177)."""
    return _STUB_TEMPLATE.replace("@EXIT@", str(exit_code))


_CLAUDE_STUB = _claude_stub()
_NPM_STUB = "#!/bin/sh\nexit 0\n"
# Every block-scalar header a `run:` can carry; matching only `run: |` hid an unfenced step (#177).
_BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")
# A `claude` at command position: the install step's package name is a string, not a process.
_CLAUDE_INVOCATION = re.compile(r"(?:^|[;&|(\s])claude\s", re.M)
_GATE_STEPS = ("Install the pinned Claude Code CLI", "Validate the plugin manifest (gate)", "Validate the marketplace catalog (gate)")


def _run_steps(text):
    """Every `run:` step in `text` as `(name, script)` pairs, in file order, without a YAML parser (#177)."""
    lines = text.splitlines()
    steps, name, name_indent, i = [], None, -1, 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith("- ") and (name is None or indent <= name_indent):
            name = stripped[len("- name:"):].strip() if stripped.startswith("- name:") else None
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
        i = k
    return steps


def _all_run_steps():
    steps = _run_steps(WORKFLOW.read_text(encoding="utf-8"))
    if not steps:
        pytest.fail(f"no `run:` step found in {WORKFLOW} at all -- an empty scan set is an all-clear nobody earned")
    return steps


def _step_script(step_name: str = STEP_NAME) -> str:
    """One named step's shell; every way this can fail to find the block is a hard failure."""
    found = [script for name, script in _all_run_steps() if name == step_name]
    if len(found) != 1:
        pytest.fail(f"expected exactly one step named {step_name!r} in {WORKFLOW}, found {len(found)}")
    assert "claude" in found[0], "extracted the wrong block:\n" + found[0]
    return found[0]


def _pinned_version() -> str:
    match = re.search(r'^\s*CLAUDE_CLI_VERSION:\s*"([^"]+)"', WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "the workflow no longer sets CLAUDE_CLI_VERSION; this harness supplies it"
    return match.group(1)


def _parse(line, resume_token):
    """What `TryParseV2` then `TryParse` make of one log line: V2 trims leading whitespace, V1 is unanchored."""
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
    """The lines the runner would act on, and the fence token still open at the end (None when closed)."""
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
    return [line for line in acted_on if not any(line.lstrip().startswith(prefix) for prefix in _AUTHORED)]


def _exec(script, tmp_path, claude_stub=None, shell=("bash", "-e")):
    """Run an extracted step with a `claude` that forges and an `npm` that does nothing; `bash -e` is what GitHub runs."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this platform. UNTESTED HERE: the end-to-end shell half; the structural tests assert theirs on every leg.")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    for name, body in (("claude", _CLAUDE_STUB), ("npm", _NPM_STUB)):
        path = stub_dir / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env["CLAUDE_CLI_VERSION"] = _pinned_version()
    # Probed with the exit-0 stub, so a failing-stub caller cannot get a manufactured skip.
    probe = subprocess.run(["bash", "-e"], input="claude --version\n", env=env, cwd=str(tmp_path), capture_output=True, text=True)
    if probe.returncode != 0 or _MARK not in probe.stdout:
        pytest.skip(f"this platform's bash cannot reach a staged stub by name (exit {probe.returncode}, stderr {probe.stderr[:200]!r}). "
                    "UNTESTED HERE: the end-to-end shell half.")
    if claude_stub is not None:
        (stub_dir / "claude").write_text(claude_stub, encoding="utf-8")
        os.chmod(stub_dir / "claude", 0o755)
    return subprocess.run(list(shell), input=script, env=env, cwd=str(tmp_path), capture_output=True, text=True)


def _run(script, tmp_path):
    """`_exec` for the advisory step, whose contract is that it always exits 0."""
    proc = _exec(script, tmp_path)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, "the step is meant to exit 0 always:\n" + log
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    return log


def _without_comments(script):
    """Whole-line `#` comments blanked to spaces, so offsets hold and prose about `claude` is not an invocation."""
    return "\n".join(" " * len(line) if line.lstrip().startswith("#") else line for line in script.splitlines())


def _steps_running_claude():
    return [(name, script) for name, script in _all_run_steps() if _CLAUDE_INVOCATION.search(_without_comments(script))]


def _assert_three_shapes_forged(forged):
    assert any("FORGED-AT-COLUMN-0" in line for line in forged), forged
    assert any("FORGED-BEHIND-AN-INDENT" in line for line in forged), "an indented `::` must count: the runner trims before it matches"
    assert any("FORGED-IN-THE-LEGACY-FORM" in line for line in forged), "`##[error]` mid-line must count: the legacy parser is unanchored"


def test_the_step_still_carries_both_containments():
    """The structural half, on every leg: the fence brackets the output and the version string is squashed."""
    script = _step_script()
    fence_open = script.index("::stop-commands::")
    loop = script.index("claude plugin validate")
    assert fence_open < loop, "the fence opens after the output it contains:\n" + script
    assert script.index("::${fence}::", fence_open) > loop, "the fence closes before the output it contains:\n" + script
    assert script.count(_SQUASH_KEY) == 1, "the version string is no longer squashed at capture:\n" + script


def test_the_validator_output_cannot_forge_a_workflow_command(tmp_path):
    """End to end through the real shell: nothing forged, the fence closed, the log still readable."""
    log = _run(_step_script(), tmp_path)
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log
    assert any(line.lstrip().startswith("::stop-commands::") for line in acted_on), "the fence never opened:\n" + log
    for expected in ("Checking manifest", "FORGED-AT-COLUMN-0", "FORGED-BEHIND-AN-INDENT", "FORGED-IN-THE-LEGACY-FORM"):
        assert expected in log, "the hardening ate the output it was meant to contain:\n" + log


@pytest.mark.parametrize("step_name, count", [(STEP_NAME, 6), ("Validate the marketplace catalog (gate)", 3)], ids=["advisory", "gate"])
def test_removing_the_fence_lets_the_validator_forge_one(tmp_path, step_name, count):
    """MUST-FIRE for the fence: without it every forged line is acted on (the advisory step loops over two manifests)."""
    script = _step_script(step_name)
    stripped = "\n".join(line for line in script.splitlines() if not any(key in line for key in _FENCE_KEYS)) + "\n"
    assert len(script.splitlines()) - len(stripped.splitlines()) == 2, "expected to strip exactly the two fence lines"
    proc = _exec(stripped, tmp_path)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    forged = _forged(_processed(log)[0])
    assert len(forged) == count, f"expected all {count} forged lines unfenced, got: {forged}"
    _assert_three_shapes_forged(forged)


def test_removing_the_version_squash_lets_the_version_string_forge_one(tmp_path):
    """MUST-FIRE for the other containment: the version string is interpolated outside the fence, at column 0."""
    script = _step_script()
    stripped = "\n".join(line for line in script.splitlines() if _SQUASH_KEY not in line) + "\n"
    assert len(script.splitlines()) - len(stripped.splitlines()) == 1, "expected to strip exactly the squash line"
    forged = _forged(_processed(_run(stripped, tmp_path))[0])
    assert any("FORGED-VIA-VERSION::" in line for line in forged), f"the version string could not forge a `::` line: {forged}"
    assert any("FORGED-VIA-VERSION-LEGACY" in line for line in forged), f"the version string could not forge a `##[` line: {forged}"


def test_the_step_extractor_reads_every_run_form():
    """MUST-FIRE for the extractor everything rests on: every block-scalar header, the one-liner, a nested sequence."""
    text = (
        "jobs:\n  j:\n    steps:\n"
        "      - name: Literal\n        run: |\n          claude plugin validate --strict .\n"
        "      - name: Stripped\n        run: |-\n          claude plugin validate --strict .\n"
        "      - name: Kept\n        run: |+\n          claude plugin validate --strict .\n"
        "      - name: Folded\n        run: >-\n          claude plugin validate --strict .\n"
        "      - name: Explicit indent\n        run: |2\n          claude plugin validate --strict .\n"
        "      - name: Commented header\n        run: | # why this is a block\n          claude plugin validate --strict .\n"
        "      - name: One line\n        run: claude plugin validate --strict .\n"
        "      - name: Nested sequence first\n        with:\n          args:\n            - --strict\n"
        "        run: |\n          claude plugin validate --strict .\n"
        "      - uses: actions/checkout@v7\n"
    )
    steps = _run_steps(text)
    assert [name for name, _ in steps] == ["Literal", "Stripped", "Kept", "Folded", "Explicit indent", "Commented header",
                                           "One line", "Nested sequence first"], steps
    for name, script in steps:
        assert script.strip() == "claude plugin validate --strict .", (name, script)
        assert _CLAUDE_INVOCATION.search(_without_comments(script)), f"{name}: the body was dropped"
    assert _run_steps("      - name: Nothing\n        uses: actions/checkout@v7\n") == []


def test_every_step_that_runs_the_cli_contains_what_it_prints():
    """The class guard (#177): every step that starts `claude` fences its output or captures it, whatever it is named."""
    steps = _steps_running_claude()
    assert len(steps) == 4, ("the set of steps that run the Claude CLI changed; each needs a fence or a capture, and a gate "
                             f"carries its exit code past the fence. Add it to _GATE_STEPS if it is one. Found: {[n for n, _ in steps]}")
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
            if "$(claude" in line:  # capture into a variable is the other accepted containment
                continue
            offenders.append((name, line.strip()))
    assert offenders == [], ("a step runs the Claude CLI straight into the log, outside any fence (#177):\n"
                             + "\n".join(f"  {name}: {detail}" for name, detail in offenders))


@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_carries_its_exit_code_past_the_fence(step_name):
    """`echo` clobbers `$?`, so closing the fence destroys the verdict unless it was captured first."""
    script = _step_script(step_name)
    assert "|| code=$?" in script, f"{step_name!r} does not capture the CLI's exit code:\n" + script
    assert script.rstrip().endswith('exit "$code"'), f"{step_name!r} does not re-raise the captured exit code as its own:\n" + script


@pytest.mark.parametrize("step_name", [name for name, _ in _steps_running_claude()])
def test_no_step_lets_the_cli_forge_a_workflow_command(step_name, tmp_path):
    """The behavioural half of the class guard, through the real shell."""
    proc = _exec(_step_script(step_name), tmp_path)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log


@pytest.mark.parametrize("step_name", _GATE_STEPS)
@pytest.mark.parametrize("shell", [("bash", "-e"), ("bash",)], ids=["errexit", "no-errexit"])
def test_a_gate_step_reports_the_cli_failure_through_its_fence(step_name, shell, tmp_path):
    """A `claude` that forges AND exits 7 comes back exactly 7, with nothing forged, under either shell mode."""
    proc = _exec(_step_script(step_name), tmp_path, claude_stub=_claude_stub(7), shell=shell)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    assert proc.returncode == 7, f"the gate's verdict did not survive its fence: expected exit 7, got {proc.returncode}\n" + log
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [] and open_token is None, "a third-party binary forged a workflow command, or the fence stayed open:\n" + log


@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_passes_when_the_cli_passes(step_name, tmp_path):
    """A fence that always fails is not a gate either."""
    proc = _exec(_step_script(step_name), tmp_path)
    assert proc.returncode == 0, f"the step failed against a CLI that exited 0: {proc.returncode}\n" + proc.stdout + proc.stderr


def test_removing_the_exit_line_lets_a_failing_gate_report_success(tmp_path):
    """MUST-FIRE for the exit-code half: with the re-raise removed, a CLI that exits 7 comes back 0."""
    script = _step_script("Validate the plugin manifest (gate)")
    stripped = "\n".join(line for line in script.splitlines() if 'exit "$code"' not in line) + "\n"
    assert len(script.splitlines()) - len(stripped.splitlines()) == 1, "expected to strip exactly the re-raise line"
    proc = _exec(stripped, tmp_path, claude_stub=_claude_stub(7))
    assert proc.returncode == 0, f"the control removed nothing: the step failed anyway (exit {proc.returncode})\n" + proc.stdout + proc.stderr
