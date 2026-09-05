"""#419: the suite's hermeticity is a guarantee of `tests/conftest.py`, not a property of the
machine it runs on.

Before the net existed, "no API calls, no network" was true exactly where no credential was
resolvable: `cli.py` loaded the repo's `.env` at import time, `client=None` meant "build the
default client", and `test_the_real_session_is_still_reachable_by_its_own_slug[answer]` made a
real paid Anthropic call and then went red — on every keyed machine, green in keyless CI, ~$0.07
per full-suite run. These tests are the must-fire pair the net's docstring names, plus the two
halves of the `.env` contract the fix moved.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_ambient_credential_reaches_a_test():
    """The probe: inside a test body, no credential variable survives and the wire points at the
    sinkhole. On a keyless machine this passes vacuously — `test_the_net_fires_when_a_credential_is_ambient`
    is the half that makes it fire everywhere."""
    for var in _CREDENTIAL_ENV:
        assert var not in os.environ, (
            f"{var} survived into a test body — the autouse net in tests/conftest.py is not running"
        )
    assert os.environ.get("ANTHROPIC_BASE_URL") == SINKHOLE_BASE_URL, (
        "an escaped provider call would reach the real API instead of dying on the sinkhole"
    )


def test_the_net_fires_when_a_credential_is_ambient():
    """The must-fire half: run the probe in a child pytest whose environment carries a planted key,
    the exact shape of the developer machine #419 billed. Red if the autouse net is removed —
    which is what makes the probe above more than a keyless-CI tautology."""
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = "sk-test-ambient-should-never-survive"
    # Same pin as `_run_in`, for the same reason: in a worktree the venv's editable install still
    # resolves `requivo` to the main checkout, and the child's conftest must import THIS tree's
    # `requivo.cli` or the probe fails for a reason no diff explains (found in review of #420).
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_suite_hermeticity.py::test_no_ambient_credential_reaches_a_test"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, (
        "the probe failed under a planted ambient key — the net no longer scrubs:\n"
        + proc.stdout + proc.stderr
    )


def test_importing_the_cli_leaves_the_environment_alone(tmp_path):
    """#419's first mechanism, closed: importing `requivo.cli` from a directory holding a `.env`
    must not load it. A canary lands in `os.environ` only when `app()` runs (the test below)."""
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


def test_a_verb_still_reads_the_dotenv_file(tmp_path):
    """The contract's other half, unchanged for every CLI user: `app()` itself still honours a
    `.env` in the directory the command runs from. In-process suite runs never see this — the net
    no-ops `load_dotenv` there precisely so the developer's real key cannot come back mid-test —
    so a subprocess, owning its own environment, is where the promise is checked."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    script = (
        "import io, os, sys\n"
        "from contextlib import redirect_stdout\n"
        "import requivo.cli\n"
        "buf = io.StringIO()\n"
        "with redirect_stdout(buf):\n"
        "    requivo.cli.app(['schema'])\n"
        "sys.exit(0 if os.environ.get('REQUIVO_HERMETICITY_CANARY') == 'from-dotenv' else 1)\n"
    )
    proc = _run_in(tmp_path, script)
    assert proc.returncode == 0, (
        "app() no longer reads the cwd's .env — the move out of import time went too far:\n"
        + proc.stdout + proc.stderr
    )


def _run_in(cwd, script):
    """A child interpreter running this checkout's `requivo`, wherever the venv's install points.

    `PYTHONPATH` pins the import to this tree's `src/` — in a worktree the venv's editable install
    resolves to the main checkout, which is exactly the wrong tree to assert about."""
    env = {k: v for k, v in os.environ.items() if k != "REQUIVO_HERMETICITY_CANARY"}
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def test_the_incomplete_model_test_leaves_the_callers_workspace_untouched(tmp_path):
    """#432: a fake reply missing required slots exhausted retries without isolating its workspace.
    Run the actual test from a disposable cwd, not the developer's checkout: the un-fixed test
    passes its own assertions while leaving a fake debug dump. This assertion must fail on that
    version even if the session guard is also absent."""
    target = _REPO_ROOT / "tests" / "test_provider.py"
    proc = _workspace_pytest(tmp_path, f"{target}::test_run_rejects_a_model_missing_required_slots")
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
    """The must-fire half: exercise the shipped writer, including a directory at its retention
    cap. At the cap the count stays at 20, but the new name must still be reported (#432)."""
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


def _workspace_probe(cwd, body):
    # Copy the real net, not a stand-in. No repository fixture redirects this child's cwd.
    (cwd / "conftest.py").write_text(
        (_REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (cwd / "test_probe.py").write_text(
        "def test_probe():\n" + textwrap.indent(body, "    "), encoding="utf-8",
    )
    return _workspace_pytest(cwd, "test_probe.py")


def _workspace_pytest(cwd, target):
    env = dict(os.environ)
    env.pop("REQUIVO_WORKSPACE", None)
    env["PYTHONPATH"] = os.pathsep.join(str(_REPO_ROOT / name) for name in ("src", "tests"))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
