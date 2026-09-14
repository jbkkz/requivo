"""A grouped runtime bump must not be titled `chore(deps-dev)` (#347).

`.github/dependabot.yml` groups the `pip` ecosystem into `runtime` and `dev-tooling` (#346), but
grouping does not change dependabot's own production/development CLASSIFICATION -- for pip, that
classification is "outside `[project.dependencies]` == development", and it is applied per
dependency, not per group. Left with no `commit-message` block, dependabot falls back to its
default prefixes, `chore(deps)` for a dependency it calls production and `chore(deps-dev)` for one
it calls development -- so a bump to `anthropic`, grouped into `runtime` by #346, would still land
titled `chore(deps-dev): ...`, exactly the symptom #315 first observed for the ungrouped case.

The choice recorded in `.github/dependabot.yml`'s own comment (read it, don't duplicate it here):
ONE prefix for every pip bump (`commit-message.prefix: "chore(deps)"`, `prefix-development`
deliberately unset), so the GROUP NAME in the title is what tells runtime from dev-tooling apart.
Dependabot's prefixes are per ecosystem, not per group, so this is the only way to stop a wrong
prefix without inventing a right one (the issue's own "What would settle it").

Text, not YAML -- PyYAML is not a dependency of this project, the same call
`tests/test_workflow_permissions.py` makes for the same reason. Scoped to the `pip` block only:
`github-actions` has no production/development split to get wrong.

The must-fire half is the point of this file, not the must-not-fire half: a `commit-message` key
existing somewhere is easy and proves nothing (#347's whole defect was one classification rule
applying underneath a change that looked complete). What has to be caught is `prefix-development`
reappearing under `pip` with a value that would split the title again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# Anchored at a line's own key position (after stripping indentation), and never inside a comment
# -- otherwise this file's own prose, which has to SAY "prefix-development" to explain why it is
# unset, would trip its own guard. That happened on the first draft of this file: the real fix's
# comment mentions the key by name and the substring check fired on the comment, not on any YAML.
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
    """The YAML text of one `- package-ecosystem: <ecosystem>` entry, up to the next top-level
    `- package-ecosystem:` entry or EOF. Both entries in this file are at the same two-space
    `updates:` list indent, so this is a plain slice -- no YAML parser needed."""
    marker = f"- package-ecosystem: {ecosystem}"
    start = text.index(marker)
    rest = text[start + len(marker):]
    next_at = rest.find("\n  - package-ecosystem:")
    return rest if next_at == -1 else rest[:next_at]


def _pip_block() -> str:
    return _ecosystem_block(_text(), "pip")


def _prefix_offence(block: str) -> str | None:
    """Why a pip-ecosystem block reintroduces #347, or None. Split out so the must-fire test below
    can hand it a synthetic offender without needing the real file to be broken.

    Comments are stripped before either regex runs -- this file's own fix has to SAY
    "prefix-development" in prose to explain why it is unset, and a bare substring check fires on
    that prose rather than on any YAML key. See the module-level comment on the regexes."""
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


# The three cases a guard over `_prefix_offence` has to get right, merged from three
# near-identical functions (#555): two must-fire halves (a reintroduced `prefix-development`,
# and no `commit-message` block at all -- dependabot's own unmodified default) and the positive
# control -- a detector flagging every input, including a correct one, would satisfy both
# must-fire cases and say nothing true about the real file.
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
    """This is a pip-ecosystem fix. `github-actions` has no production/development classification
    to mislabel, and #347 draws that scope line explicitly -- a guard that silently expected the
    same commit-message block there would fail the day this file's github-actions entry is edited
    for an unrelated reason."""
    block = _ecosystem_block(_text(), "github-actions")
    assert "commit-message" not in block, (
        "the github-actions block gained a commit-message key -- #347 is scoped to pip; if this "
        "is deliberate, update this guard's expectation"
    )
