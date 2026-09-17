"""A grouped runtime bump must not be titled `chore(deps-dev)` (#347)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# Anchored at a line's own key position (after stripping indentation), and never inside a comment
# -- otherwise this file's own prose, which has to SAY "prefix-development" to explain why it is
# unset, would trip its own guard.
_PREFIX_DEV_RE = re.compile(r'^[ \t]*prefix-development[ \t]*:', re.MULTILINE)
_PREFIX_RE = re.compile(r'^[ \t]*prefix[ \t]*:[ \t]*["\']chore\(deps\)["\']', re.MULTILINE)


def _strip_comments(block: str) -> str:
    lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _text() -> str:
    assert DEPENDABOT.is_file(), f"missing: {DEPENDABOT}"
    return DEPENDABOT.read_text(encoding="utf-8")


def _ecosystem_block(text: str, ecosystem: str) -> str:
    """The YAML text of one `- package-ecosystem: <ecosystem>` entry."""
    marker = f"- package-ecosystem: {ecosystem}"
    start = text.index(marker)
    rest = text[start + len(marker):]
    next_at = rest.find("\n  - package-ecosystem:")
    return rest if next_at == -1 else rest[:next_at]


def _pip_block() -> str:
    return _ecosystem_block(_text(), "pip")


def _prefix_offence(block: str) -> str | None:
    """Why a pip-ecosystem block reintroduces #347, or None."""
    code = _strip_comments(block)
    if "commit-message:" not in code:
        return ("no `commit-message:` block under the pip ecosystem -- dependabot falls back to "
                "its own production/development split, which is #347")
    if _PREFIX_DEV_RE.search(code):
        return ("`prefix-development` is set under the pip ecosystem -- that reintroduces the "
                "split #347 is about: a group is not what dependabot uses to write the prefix, "
                "the production/development classification is, and this key is how that "
                "classification reaches the title again")
    if not _PREFIX_RE.search(code):
        return ("the pip ecosystem's commit-message prefix is not `chore(deps)` -- if it changed, "
                "update this guard's expectation deliberately rather than letting it drift")
    return None


def test_the_pip_block_sets_one_prefix_and_no_development_split():
    offence = _prefix_offence(_pip_block())
    assert offence is None, offence


# The three cases a guard over `_prefix_offence` has to get right (#555).
@pytest.mark.parametrize("block, flagged, why", [
    (
        "  - package-ecosystem: pip\n"
        "    commit-message:\n"
        "      prefix: \"chore(deps)\"\n"
        "      prefix-development: \"chore(deps-dev)\"\n"
        "    groups:\n",
        True,
        "a pip block that sets prefix-development alongside prefix must be caught",
    ),
    (
        "  - package-ecosystem: pip\n    groups:\n",
        True,
        "a pip block with no commit-message key at all must be caught -- that is dependabot's "
        "default split, unmodified",
    ),
    (
        "  - package-ecosystem: pip\n"
        "    commit-message:\n"
        "      prefix: \"chore(deps)\"\n"
        "    groups:\n",
        False,
        "a clean single-prefix block must not be flagged",
    ),
], ids=["#347-reintroduced-split", "#347-no-commit-message-block", "#347-clean-block"])
def test_the_guard_classifies_a_pip_block_correctly(block, flagged, why):
    offence = _prefix_offence(block)
    assert (offence is not None) == flagged, why


def test_the_github_actions_block_is_untouched():
    """This is a pip-ecosystem fix. `github-actions` has no production/development classification to mislabel,
    and #347 draws that scope line explicitly."""
    block = _ecosystem_block(_text(), "github-actions")
    assert "commit-message" not in block, (
        "the github-actions block gained a commit-message key -- #347 is scoped to pip; if this "
        "is deliberate, update this guard's expectation"
    )
