"""The pinned Claude Code CLI install in `plugin-validate.yml` is cached (#299).

The gate job's "Install the pinned Claude Code CLI" step resolves a ~320MB platform-native ELF as
one of `@anthropic-ai/claude-code`'s ordinary `optionalDependencies` -- see the workflow's own
header, "Notes on the install and the platform". That download is repeated, byte-identical, on
every single pull request, because the version is pinned by `CLAUDE_CLI_VERSION` and nothing
before this cached it.

Text, not YAML -- PyYAML is not a dependency of this project, the same call
`tests/test_workflow_permissions.py` and `tests/test_workflow_untrusted_output.py` make for the
same reason. This guard is scoped to the one workflow the issue names rather than folded into
either of those two directory-wide guards: caching-key derivation is not a rule every workflow in
this repo needs to obey, only this one job.

The judgment call the issue names as the whole point: a cache that can serve a STALE CLI turns a
validation leg into one that passes against a version nobody ships, which is worse than the 320MB
it saves. So this does not merely assert "a cache step exists" -- it asserts the key is DERIVED
from `CLAUDE_CLI_VERSION`, the same env var the gate is already pinned to, so a version bump misses
the cache by construction rather than by luck. And it asserts the advisory `@latest` job stays
UNCACHED on purpose (the issue's own "out of scope": that job's whole point is currency, and a
cache would work directly against it).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plugin-validate.yml"


def _text() -> str:
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def _job_block(text: str, job_key: str, next_job_key: str | None) -> str:
    """The YAML text of one top-level job, from its `  <job_key>:` line to the next top-level job
    key (or EOF). Simple slicing on the two-space top-level job indent -- this file's own jobs are
    `validate:` and `drift:`, both at that indent, so this is enough without a YAML parser."""
    start_marker = f"\n  {job_key}:\n"
    start = text.index(start_marker)
    if next_job_key is None:
        return text[start:]
    end_marker = f"\n  {next_job_key}:\n"
    end = text.index(end_marker, start + len(start_marker))
    return text[start:end]


def _gate_job() -> str:
    return _job_block(_text(), "validate", "drift")


def _advisory_job() -> str:
    return _job_block(_text(), "drift", None)


def test_the_gate_job_caches_the_pinned_cli_install():
    gate = _gate_job()
    assert "actions/cache" in gate, (
        "the gate job's pinned CLI install has no actions/cache step -- #299 asks for one so this "
        "~320MB download is not repeated on every pull request"
    )


def test_the_cache_key_is_derived_from_the_version_pin_not_hardcoded():
    """The judgment call the issue is actually about: a key that does not reference
    CLAUDE_CLI_VERSION could serve a stale CLI on a version bump and nobody would notice, because
    the leg would still go green -- just green about the wrong thing."""
    gate = _gate_job()
    cache_start = gate.index("actions/cache")
    # The `with:` block immediately below the `uses:` line, up to the next step ("      - name:" or
    # "      - uses:" at the same two-level step indent).
    rest = gate[cache_start:]
    next_step = rest.find("\n      - ", 1)
    cache_block = rest[:next_step] if next_step != -1 else rest
    assert "CLAUDE_CLI_VERSION" in cache_block, (
        "the cache step's key does not reference CLAUDE_CLI_VERSION -- a version bump would not "
        "miss the cache by construction, only by luck:\n" + cache_block
    )


def test_the_cache_step_precedes_the_install_step():
    """A cache step that exists but runs AFTER the install it is meant to speed up caches nothing
    -- this is the must-fire half of the ordering claim."""
    gate = _gate_job()
    cache_at = gate.index("actions/cache")
    install_at = gate.index("Install the pinned Claude Code CLI")
    assert cache_at < install_at, (
        "the actions/cache step must run BEFORE the install step it is meant to speed up"
    )


def _without_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def test_a_cache_hit_and_a_cache_miss_are_distinguishable_in_the_log():
    """Second, smaller judgment call from the issue: a leg that silently fell back to a full
    install and one that restored must not look the same in the run log. Found tautological in
    self-review: a bare `"cache-hit" in gate` substring check also matched an explanatory
    COMMENT, not just a functional readback -- comments are stripped first, and the check is for
    the FUNCTIONAL reference (`steps.<id>.outputs.cache-hit`), not the bare word."""
    gate = _without_comments(_gate_job())
    assert "steps.cache-claude-cli-npm.outputs.cache-hit" in gate, (
        "nothing in the gate job's step BODIES (comments excluded) reads back "
        "steps.cache-claude-cli-npm.outputs.cache-hit -- a miss and a restore render identically "
        "in the log, so a caching regression would never be noticed"
    )


def test_the_cache_hit_guard_fires_when_the_readback_is_removed():
    """The must-fire half for the test above: strip the functional reference (as if the readback
    step were reverted to an unconditional echo) while leaving the neighbouring comment -- which
    still says "cache-hit" in prose -- untouched, and confirm the comment-stripped check still
    catches it."""
    gate = _without_comments(_gate_job())
    reverted = gate.replace(
        'if [ "${{ steps.cache-claude-cli-npm.outputs.cache-hit }}" = "true" ]; then',
        'echo "cache status unknown -- not actually read back"',
    )
    assert "steps.cache-claude-cli-npm.outputs.cache-hit" not in reverted, (
        "the fixture did not actually remove the functional reference -- fix the fixture"
    )


def test_the_advisory_latest_job_is_deliberately_not_cached():
    """The negative control: caching the `@latest` job would work directly against its whole
    point, which is currency (the issue's own 'Out of scope'). If this guard only asserted the
    gate job's cache and never checked the advisory job, a change that accidentally cached
    `@latest` too would go unnoticed."""
    advisory = _advisory_job()
    assert "actions/cache" not in advisory, (
        "the advisory `@latest` job must stay uncached -- its whole point is currency, and a cache "
        "would work directly against that"
    )
