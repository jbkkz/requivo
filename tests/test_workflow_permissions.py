"""Every workflow states the token it wants, and every `write` it wants is written down (#178)."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Workflows that scope permissions per job instead of at the top.
_JOB_SCOPED = {
    "publish.yml": "the publish job needs `id-token: write` for PyPI Trusted Publishing, and a "
                   "workflow-level block would be replaced wholesale by that job's own",
}

# Every `write` scope granted anywhere, with why.
_WRITE_GRANTS = {
    ("codeql.yml", "security-events"): "uploads the analysis to code scanning, which is what the "
                                       "required `CodeQL` check reads",
    ("publish.yml", "id-token"): "mints the short-lived OIDC token PyPI Trusted Publishing "
                                 "verifies; it is what replaces a stored API token",
    ("secret-scan.yml", "pull-requests"): "gitleaks-action's only use of GITHUB_TOKEN is "
                                          "`pulls.createReviewComment`, and its "
                                          "GITLEAKS_ENABLE_COMMENTS defaults to true",
}


def _workflow_files():
    files = sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml")))
    # A glob over a moved directory returns [], and every assertion would pass over nothing.
    assert files, f"no workflow files under {WORKFLOWS} -- the scan set is empty"
    return files


def _permission_blocks(text):
    """Every `permissions:` block in one workflow, as `(indent, {scope: value})`."""
    lines = text.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped.startswith("permissions:"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        inline = stripped[len("permissions:"):].strip()
        if inline:
            # `permissions: read-all`, `permissions: write-all`, `permissions: {}`.
            blocks.append((indent, {"<inline>": inline}))
            i += 1
            continue
        grants, j = {}, i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip() or line.lstrip().startswith("#"):
                j += 1
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            scope, _, value = line.strip().partition(":")
            grants[scope.strip()] = value.split("#")[0].strip()
            j += 1
        blocks.append((indent, grants))
        i = j
    return blocks


def test_the_job_scoped_exception_list_names_files_that_exist():
    """An entry naming a renamed or deleted workflow exempts nothing."""
    present = {p.name for p in _workflow_files()}
    stale = sorted(set(_JOB_SCOPED) - present)
    assert stale == [], f"_JOB_SCOPED names workflows that no longer exist: {stale}"


def _declaration_offence(name, text):
    """Why `name` fails the declaration rule, or None."""
    blocks = _permission_blocks(text)
    if not blocks:
        return (f"{name}: no `permissions:` block, so every job in it takes the repository "
                f"default -- read/write here. Add `permissions:` with `contents: read`, or scope "
                f"it per job and record the reason in _JOB_SCOPED.")
    if name in _JOB_SCOPED:
        if not any(indent > 0 for indent, _ in blocks):
            return (f"{name}: _JOB_SCOPED says its grant is per job ({_JOB_SCOPED[name]}) but "
                    f"every `permissions:` block in it is at workflow level.")
        return None
    if not any(indent == 0 for indent, _ in blocks):
        return (f"{name}: declares permissions per job only. A job added later still falls back "
                f"to the repository default. Either declare one at workflow level, or record in "
                f"_JOB_SCOPED why this file cannot.")
    return None


def _write_scopes(name, text):
    """`(granted, offences)` for one workflow: which `<scope>: write` it grants."""
    granted, offences = set(), []
    for _, grants in _permission_blocks(text):
        for scope, value in grants.items():
            if scope == "<inline>":
                if "write" in value:
                    offences.append(
                        f"{name}: `permissions: {value}` grants write across every scope. List "
                        f"the scopes it actually needs instead.")
                continue
            if value != "write":
                continue
            granted.add((name, scope))
            if (name, scope) not in _WRITE_GRANTS:
                offences.append(
                    f"{name}: grants `{scope}: write` with no entry in _WRITE_GRANTS. Say what "
                    f"needs it and why, or drop the scope.")
    return granted, offences


def test_every_workflow_declares_its_own_permissions():
    offenders = []
    for path in _workflow_files():
        offence = _declaration_offence(path.name, path.read_text(encoding="utf-8"))
        if offence:
            offenders.append(offence)
    assert offenders == [], "\n".join(offenders)


def test_every_write_scope_a_workflow_grants_is_written_down():
    """Declaring `permissions:` is not the same as declaring a narrow one."""
    granted, offenders = set(), []
    for path in _workflow_files():
        seen, offences = _write_scopes(path.name, path.read_text(encoding="utf-8"))
        granted |= seen
        offenders += offences
    assert offenders == [], "\n".join(offenders)

    stale = sorted(set(_WRITE_GRANTS) - granted)
    assert stale == [], (
        f"_WRITE_GRANTS explains write scopes no workflow grants any more: {stale}. Remove the "
        f"entries -- an explanation with no call site reads as a live decision.")


def test_the_permission_guards_fire_on_a_workflow_that_offends():
    """The must-fire half, and the reason the two checks above are worth having."""
    silent = "name: X\non:\n  pull_request:\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
    assert _declaration_offence("silent.yml", silent), (
        "a workflow with no `permissions:` at all has to be caught")

    per_job_only = silent.replace(
        "    runs-on: ubuntu-latest\n",
        "    permissions:\n      contents: read\n    runs-on: ubuntu-latest\n")
    assert _declaration_offence("per-job.yml", per_job_only), (
        "a workflow whose only grant is inside one job leaves the next job on the default")

    assert _declaration_offence("publish.yml", per_job_only) is None, (
        "_JOB_SCOPED must actually exempt the file it names, or the entry is decoration")
    assert _declaration_offence("publish.yml", "permissions:\n  contents: read\njobs:\n"), (
        "a _JOB_SCOPED workflow that moved its grant to the top no longer matches its own reason")

    top_level = "permissions:\n  contents: read\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
    assert _declaration_offence("fine.yml", top_level) is None, top_level

    _, blanket = _write_scopes("blanket.yml", "permissions: write-all\njobs:\n")
    assert blanket, "`permissions: write-all` grants more than the default it replaced"

    _, undeclared = _write_scopes("new.yml", "permissions:\n  contents: write\njobs:\n")
    assert undeclared, "a write scope with no entry in _WRITE_GRANTS has to be caught"

    assert _write_scopes("fine.yml", top_level) == (set(), []), (
        "a read-only workflow must come back clean, or the guard above flags everything")
