"""The 'Reproduce it' block of each committed example is executable, and this runs it (#222)."""
from __future__ import annotations

import io
import shlex
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import app
from requivo.core import persistence as store

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = ("leave-approval", "event-checkin-reconciliation")


class ReachedProvider(Exception):
    """Raised in place of the API call."""


class _SentinelClient:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise ReachedProvider


def _reproduce_commands(example: str) -> list[list[str]]:
    """Every `requivo …` line in the example README's 'Reproduce it' section, in order."""
    text = (REPO / "examples" / example / "README.md").read_text(encoding="utf-8")
    section = text.split("## Reproduce it", 1)
    assert len(section) == 2, f"{example}/README.md has no 'Reproduce it' section"
    body = section[1].split("\n## ", 1)[0]
    cmds = []
    in_block = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_block = line.startswith("```bash")
            continue
        if in_block and line.strip().startswith("requivo "):
            # `comments=True` strips the trailing `# what this does` the blocks align on.
            cmds.append(shlex.split(line, comments=True)[1:])
    assert cmds, f"{example}/README.md documents no commands"
    return cmds


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # cwd stays at the repo root, because the documented commands name `examples/<name>/…` relative to it.
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.chdir(REPO)
    return tmp_path


@pytest.mark.parametrize("example", EXAMPLES)
def test_the_documented_reproduce_sequence_runs_on_a_fresh_workspace(example, workspace):
    """Each command in order: the offline ones complete, the paid ones reach the provider (#222)."""
    reached = 0
    for argv in _reproduce_commands(example):
        try:
            with redirect_stdout(io.StringIO()):
                app(argv, client=_SentinelClient())
        except ReachedProvider:
            reached += 1
        except SystemExit as e:  # pragma: no cover - only on a real failure
            pytest.fail(f"`requivo {' '.join(argv)}` exited {e.code}")
    assert reached, f"{example}'s block documents no provider-backed command"
    assert store.session_exists(example), (
        f"{example}'s block never created the session its generators are run against"
    )


@pytest.mark.parametrize("example", EXAMPLES)
def test_no_example_documents_a_generator_against_a_bare_model_file(example, workspace):
    """The regression in its own words, so a red run names the defect rather than a stack (#222)."""
    generators = {"brief", "prd", "criteria", "epic", "release", "stories", "estimate"}
    for argv in _reproduce_commands(example):
        if argv and argv[0] in generators:
            assert not argv[1].endswith(".json"), (
                f"`requivo {' '.join(argv)}` passes a model file to a generator; generators resolve "
                f"a session, so this raises SessionNotFoundError on a fresh clone"
            )


def test_no_example_still_promises_the_retired_output_root():
    """`out/<slug>/` was retired in 0.9.8 and is opened by nothing but `session migrate`."""
    for example in EXAMPLES:
        text = (REPO / "examples" / example / "README.md").read_text(encoding="utf-8")
        assert "out/" not in text, f"{example}/README.md names the retired out/ root"
