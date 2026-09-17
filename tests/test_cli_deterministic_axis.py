"""#296: `cli.py` hosts three no-LLM verbs alongside the provider ones, and the split's own prose has to keep
saying so or it drifts back to being wrong the way the issue found it."""
from __future__ import annotations

from pathlib import Path

from requivo import cli, deterministic

# The three journey verbs that never construct a provider client.
NO_LLM_JOURNEY_VERBS = ("_cmd_status", "_cmd_demo", "_cmd_impact")


def test_the_three_no_llm_journey_verbs_still_live_in_cli_py():
    """The claim is a fact about *where the code is*, checked directly."""
    for name in NO_LLM_JOURNEY_VERBS:
        func = getattr(cli, name)
        assert func.__module__ == "requivo.cli", (
            f"{name} moved out of cli.py -- the split's documented axis (CLAUDE.md's tree, "
            f"deterministic/__init__.py's docstring) needs to move with it, or be rewritten (#296)"
        )


def test_claude_md_names_the_three_exceptions_in_the_repo_tree():
    """The tree entries for `cli.py` and `deterministic/` are where CLAUDE.md states the split's axis."""
    page = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    start = page.index("\nrequivo/\n")
    tree = page[start:page.index("\n```", start)]
    for verb in ("status", "demo", "impact"):
        assert verb in tree, (
            f"CLAUDE.md's repo tree no longer names {verb!r} as a no-LLM verb kept in cli.py (#296)"
        )


def test_the_deterministic_docstring_names_the_three_exceptions():
    doc = deterministic.__doc__ or ""
    for verb in ("status", "demo", "impact"):
        assert verb in doc, (
            f"deterministic/__init__.py's docstring no longer names {verb!r} as a journey verb kept "
            f"in cli.py rather than moved here (#296)"
        )
