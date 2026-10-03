"""#419: the suite's hermeticity is a guarantee of `tests/conftest.py`, not a property of the machine it runs
on."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_ambient_credential_reaches_a_test():
    """The probe: inside a test body, no credential variable survives and the wire points at the sinkhole."""
    for var in _CREDENTIAL_ENV:
        assert var not in os.environ, (
            f"{var} survived into a test body — the autouse net in tests/conftest.py is not running"
        )
    assert os.environ.get("ANTHROPIC_BASE_URL") == SINKHOLE_BASE_URL, (
        "an escaped provider call would reach the real API instead of dying on the sinkhole"
    )


def test_the_net_fires_when_a_credential_is_ambient():
    """The must-fire half: run the probe in a child pytest whose environment carries a planted key (#419)."""
    proc = _child(_REPO_ROOT, _PYTEST + ["tests/test_suite_hermeticity.py::test_no_ambient_credential_reaches_a_test"],
                  ANTHROPIC_API_KEY="sk-test-ambient-should-never-survive")
    assert proc.returncode == 0, (
        "the probe failed under a planted ambient key — the net no longer scrubs:\n"
        + proc.stdout + proc.stderr
    )


def test_no_personal_context_card_reaches_a_test(tmp_path):
    """#711: a card in the developer's own `~/.config/requivo/context` is invisible inside a test; the probe's
    must-fire half removes the net and finds it, so the planted card is real."""
    home = tmp_path / "home"
    (home / ".config" / "requivo" / "context").mkdir(parents=True)
    (home / ".config" / "requivo" / "context" / "planted-card.md").write_text("# Planted\n", encoding="utf-8")
    proc = _workspace_probe(tmp_path, (
        "import os\n"
        "from requivo.core.context import available_cards\n"
        "assert 'planted-card' not in available_cards(), 'a personal card reached a test'\n"
        "os.environ.pop('REQUIVO_CONTEXT_DIR', None)\n"
        "assert 'planted-card' in available_cards(), 'the control: the planted card was not found'\n"
    ), HOME=str(home), USERPROFILE=str(home))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_importing_the_cli_leaves_the_environment_alone(tmp_path):
    """#419's first mechanism, closed: importing `requivo.cli` from a directory holding a `.env` must not load
    it."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    script = (
        "import os, sys\n"
        "import requivo.cli\n"
        "sys.exit(1 if 'REQUIVO_HERMETICITY_CANARY' in os.environ else 0)\n"
    )
    proc = _run_in(tmp_path, script)
    assert proc.returncode == 0, (
        "importing requivo.cli loaded the cwd's .env into os.environ:\n" + proc.stdout + proc.stderr
    )


def _canary_script(argv: list[str]) -> str:
    return (
        "import io, os, sys\n"
        "from contextlib import redirect_stdout\n"
        "import requivo.cli\n"
        "buf = io.StringIO()\n"
        "with redirect_stdout(buf):\n"
        f"    requivo.cli.app({argv!r})\n"
        "sys.exit(0 if os.environ.get('REQUIVO_HERMETICITY_CANARY') == 'from-dotenv' else 1)\n"
    )


def _run_file_in(cwd, script, **env_extra):
    """From a script *file*, as a console script runs: under `python -c` python-dotenv falls back to the cwd,
    which hid #687 (a bare `load_dotenv()` searches from the installed `cli.py`, never the user's directory)."""
    probe = cwd / "probe.py"
    probe.write_text(script, encoding="utf-8")
    return _child(cwd, [sys.executable, str(probe)], **env_extra)


def test_a_verb_still_reads_the_dotenv_file(tmp_path):
    """The contract's other half: the `.env` of the directory the user runs from (#419, #687)."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    proc = _run_file_in(tmp_path, _canary_script(["schema"]))
    assert proc.returncode == 0, (
        "app() does not read the .env of the directory it runs from:\n" + proc.stdout + proc.stderr
    )


def test_a_dotenv_above_the_directory_the_user_runs_from_is_never_read(tmp_path):
    """Only the cwd's own `.env`: a parent's could set ANTHROPIC_BASE_URL and send the key elsewhere (#687 audit)."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    below = tmp_path / "project"
    below.mkdir()
    proc = _run_file_in(below, _canary_script(["schema"]))
    assert proc.returncode == 1, "app() read a .env from a directory above the one it runs from"


@pytest.mark.parametrize("named_by", ["flag", "environment"])
def test_a_named_workspace_reads_only_its_own_dotenv(tmp_path, named_by):
    """A workspace named by `--workspace` or `REQUIVO_WORKSPACE` reads its own `.env` and only it: the directory
    an MCP host launched from is not the user's project, and its `.env` must not win (#687, release audit)."""
    workspace, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    (elsewhere / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-the-launch-directory\n", encoding="utf-8")
    if named_by == "flag":
        proc = _run_file_in(elsewhere, _canary_script(["--workspace", str(workspace), "schema"]))
    else:
        proc = _run_file_in(elsewhere, _canary_script(["schema"]), REQUIVO_WORKSPACE=str(workspace))
    assert proc.returncode == 0, "the workspace's .env was not the one read:\n" + proc.stdout + proc.stderr


_PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def _child(cwd, argv, *, tests_on_path=False, **env_extra):
    """A child interpreter on this checkout's `src` (#420), with the canary, the workspace and
    `REQUIVO_CONTEXT_DIR` (#711) scrubbed."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("REQUIVO_HERMETICITY_CANARY", "REQUIVO_WORKSPACE", "REQUIVO_CONTEXT_DIR")}
    names = ("src", "tests") if tests_on_path else ("src",)
    env["PYTHONPATH"] = os.pathsep.join(str(_REPO_ROOT / name) for name in names)
    env.update(env_extra)
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)


def _run_in(cwd, script):
    return _child(cwd, [sys.executable, "-c", script])


def test_the_incomplete_model_test_leaves_the_callers_workspace_untouched(tmp_path):
    """#432: a fake reply missing required slots exhausted retries without isolating its workspace."""
    name = "test_run_rejects_a_model_missing_required_slots"
    target = next(p for p in sorted((_REPO_ROOT / "tests").glob("test_*.py"))
                  if f"def {name}(" in p.read_text(encoding="utf-8"))   # found by name: the file may move
    proc = _workspace_pytest(tmp_path, f"{target}::{name}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (tmp_path / ".requivo").exists(), (
        "the incomplete-model test wrote into its caller's workspace instead of its own tmp_path"
    )


@pytest.mark.parametrize("existing", [False, True])
def test_the_workspace_guard_accepts_an_untouched_workspace(tmp_path, existing):
    if existing:
        debug = tmp_path / ".requivo" / "debug"
        debug.mkdir(parents=True)
        (debug / "old.txt").write_text("existing diagnostic", encoding="utf-8")
    proc = _workspace_probe(tmp_path, "pass\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("retained", [0, 1, 20])
def test_the_workspace_guard_catches_a_real_unisolated_dump(tmp_path, retained):
    """The must-fire half: exercise the shipped writer, including a directory at its retention cap (#432)."""
    debug = tmp_path / ".requivo" / "debug"
    before = set()
    if retained:
        debug.mkdir(parents=True)
        before = {f"20000101-{i:02d}.txt" for i in range(retained)}
        for name in before:
            (debug / name).write_text("existing diagnostic", encoding="utf-8")
    proc = _workspace_probe(tmp_path, (
        "from requivo.providers.anthropic.completion import _save_failed_reply\n"
        "assert _save_failed_reply('fake reply', 'EngineOutput') is not None\n"
    ))
    after = {path.name for path in debug.iterdir()}
    new = after - before
    assert len(new) == 1, "the positive control must actually write one new dump"
    if retained == 20:
        assert len(after) == len(before), "the fixture must exercise unchanged-count retention"
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, "the probe must pass; the session guard must be what fails"
    assert str(tmp_path / ".requivo") in proc.stdout
    assert next(iter(new)) in proc.stdout


def test_the_workspace_guard_keeps_watching_the_starting_directory(tmp_path):
    proc = _workspace_probe(tmp_path, (
        "import os\n"
        "from pathlib import Path\n"
        "from requivo.providers.anthropic.completion import _save_failed_reply\n"
        "assert _save_failed_reply('fake reply', 'EngineOutput') is not None\n"
        "elsewhere = Path('elsewhere')\n"
        "elsewhere.mkdir()\n"
        "os.chdir(elsewhere)\n"
    ))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
    assert str(tmp_path / ".requivo") in proc.stdout
    assert "EngineOutput" in proc.stdout


def test_the_workspace_guard_does_not_redirect_unrelated_tests(tmp_path):
    proc = _workspace_probe(tmp_path, (
        "import os\n"
        "from pathlib import Path\n"
        "from requivo.paths import workspace_root\n"
        "assert 'REQUIVO_WORKSPACE' not in os.environ\n"
        "assert workspace_root() == Path.cwd()\n"
    ))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_workspace_guard_does_not_hide_a_listing_error(tmp_path):
    watched = tmp_path / ".requivo"
    watched.mkdir()
    proc = _workspace_probe(tmp_path, (
        "import os\n"
        "from pathlib import Path\n"
        "watched = Path.cwd() / '.requivo'\n"
        "original = os.scandir\n"
        "def denied(path):\n"
        "    if Path(path) == watched:\n"
        "        raise PermissionError('cannot inspect watched workspace')\n"
        "    return original(path)\n"
        "os.scandir = denied\n"
    ))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
    assert "cannot inspect watched workspace" in proc.stdout


def _workspace_probe(cwd, body, **env_extra):
    # Copy the real net, not a stand-in.
    (cwd / "conftest.py").write_text(
        (_REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    probe = "test_" + "probe.py"  # assembled so the reference guard does not read the fixture as a citation (#581)
    (cwd / probe).write_text(
        "def test_probe():\n" + textwrap.indent(body, "    "), encoding="utf-8",
    )
    return _workspace_pytest(cwd, probe, **env_extra)


def _workspace_pytest(cwd, target, **env_extra):
    return _child(cwd, _PYTEST + [target], tests_on_path=True, **env_extra)
