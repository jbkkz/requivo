"""Every file that declares the project version must declare the same one (#32).

Hand-written in several places with nothing to compare them, so a release can half-land silently: PyPI ships while `plugin.json` (what the Claude Code updater reads) or `__init__.py` (what `doctor` reports) go stale -- `v0.9.9` shipped nothing and nothing caught it. Sites are DERIVED by scanning known
structural positions (`[project] version`, `__version__`, a manifest's `version`), never a version-shaped string match, and cross-checked one-directionally against `.oss.json`'s `version_sites`. Three verdicts, never two -- `ok`, `disagree`, `could not check` -- so an unreadable site or empty scan fails loud
rather than passing as agreement. Not caught here, stated rather than read as clean: an unwatched file kind, a marketplace entry with no `version` of its own, and agreement with the git tag (checked instead at publish time by `.github/workflows/publish.yml`)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Anchors, not the full listing (same split as test_boundaries.py's CORE_ANCHORS): adding a site must not fail this
# file, but MOVING one of these must -- else a rename turns every reader into a no-op.
ANCHOR_SITES = (
    "pyproject.toml", "src/requivo/__init__.py", ".claude-plugin/marketplace.json",
    "plugins/claude-code/.claude-plugin/plugin.json",
)

# Directories a derived scan must not wander into: a version declaration inside an installed dependency or a build
# artifact is not this project declaring anything.
_PRUNED = frozenset({
    ".venv", "venv", ".git", ".tox", ".nox", "node_modules", "build", "dist", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".eggs",
})


def _is_nested_checkout(directory: Path) -> bool:
    """Is this directory a git worktree or clone of its own, sitting inside the tree? `_PRUNED` prunes by name and cannot see one -- `git worktree add` under the repo root left six strays, each a duplicate of an already-registered site. So this prunes by what a directory IS: a worktree's `.git` is a file (parent
    gitdir), a nested clone's is a directory; `exists()` not `is_dir()` catches both."""
    return (directory / ".git").exists()

# Anchored at line start and key name so a dependency pin or `requires-python` is never read as this project's own
# version; `[ \t]*` (not `\s*`) keeps `^` and the key on the same line.
_VERSION_KEY_RE = re.compile(r"""^[ \t]*version[ \t]*=[ \t]*["']([^"']+)["']""", re.MULTILINE)
_DUNDER_RE = re.compile(r"""^__version__[ \t]*=[ \t]*["']([^"']+)["']""", re.MULTILINE)
# The `[project]` table only, up to the next table header -- `[project.optional-dependencies]` is a different table and
# `[tool.ruff] target-version` must never be mistaken for a declaration.
_PROJECT_TABLE_RE = re.compile(r"^\[project\][^\n]*\n(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)


class Unreadable(Exception):
    """A file this guard knows how to read, that it could not read. Never silently skipped."""


@dataclass(frozen=True)
class Declaration:
    site: str     # repo-relative, POSIX separators -- comparable with .oss.json on any platform
    where: str    # the structural position inside the file, for the failure message
    version: str


@dataclass(frozen=True)
class Survey:
    declarations: tuple
    problems: tuple    # (site, reason) -- every "could not look", never dropped

    @property
    def versions(self) -> set:
        return {d.version for d in self.declarations}

    def describe(self) -> str:
        rows = sorted(self.declarations, key=lambda d: d.site)
        return "\n".join(f"  {d.site} ({d.where}) = {d.version}" for d in rows)

    def describe_problems(self) -> str:
        return "\n".join(f"  {site}: {reason}" for site, reason in sorted(self.problems))


def _text(path: Path) -> str:
    # encoding pinned to utf-8 (files carry em-dashes; the locale default is cp1252 on Windows), and both OSError
    # and UnicodeDecodeError become `Unreadable` rather than an escaping traceback.
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unreadable(f"could not read the file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise Unreadable(f"not valid UTF-8: {exc}") from exc


def _json(path: Path):
    try:
        return json.loads(_text(path))
    except json.JSONDecodeError as exc:
        raise Unreadable(f"not valid JSON: {exc}") from exc


def _read_pyproject(site: str, path: Path) -> list:
    """`[project] version`, by a section-scoped regex rather than a TOML parse: this suite's floor is Python 3.9, `tomllib` landed in 3.11, and there is no `tomli` in the dev extras."""
    table = _PROJECT_TABLE_RE.search(_text(path))
    if not table:
        raise Unreadable("no [project] table")
    found = _VERSION_KEY_RE.search(table.group(1))
    if not found:
        raise Unreadable("[project] declares no version")
    return [Declaration(site, "[project] version", found.group(1))]


def _read_dunder(site: str, path: Path) -> list:
    """`__version__` in a top-level package. A package without one declares nothing and is skipped; the anchor is what makes DELETING ours a failure rather than a quietly shorter scan."""
    found = _DUNDER_RE.search(_text(path))
    return [Declaration(site, "__version__", found.group(1))] if found else []


def _read_plugin_manifest(site: str, path: Path) -> list:
    data = _json(path)
    if not isinstance(data, dict):
        raise Unreadable("the manifest is not a JSON object")
    if "version" not in data:
        raise Unreadable("the plugin manifest declares no version -- the Claude Code updater reads this key")
    return [Declaration(site, "version", str(data["version"]))]


def _read_marketplace(site: str, path: Path) -> list:
    data = _json(path)
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise Unreadable("the catalog has no `plugins` list")
    out = []
    for entry in data["plugins"]:
        if isinstance(entry, dict) and "version" in entry:
            out.append(Declaration(site, f"plugins[{entry.get('name', '?')}].version", str(entry["version"])))
    return out

_SITE_READERS = (
    ("pyproject.toml", _read_pyproject), ("src/*/__init__.py", _read_dunder),
    ("**/.claude-plugin/plugin.json", _read_plugin_manifest), ("**/.claude-plugin/marketplace.json", _read_marketplace),
)


def survey(root: Path) -> Survey:
    """Every version declaration under `root`, plus every site that could not be read."""
    if not root.is_dir():
        return Survey((), ((str(root), "no such directory -- this is 'could not look'"),))
    declarations: list = []
    problems: list = []
    for pattern, reader in _SITE_READERS:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root)
            if _PRUNED.intersection(relative.parts):
                continue
            # `relative.parents` walks up to `root` itself, excluded deliberately -- root carries a `.git` too and
            # pruning on it would empty the scan set and pass by not looking.
            if any(_is_nested_checkout(root / parent) for parent in relative.parents if parent.parts):
                continue
            site = relative.as_posix()
            try:
                declarations.extend(reader(site, path))
            except Unreadable as exc:
                problems.append((site, str(exc)))
    return Survey(tuple(declarations), tuple(problems))


def missing_anchors(found: Survey) -> list:
    sites = {d.site for d in found.declarations}
    return [a for a in ANCHOR_SITES if a not in sites]


def unregistered(found: Survey, root: Path) -> list:
    """Derived sites absent from `.oss.json`'s `version_sites`.

    One-directional on purpose: the registry legitimately names files a release must EDIT but which declare nothing (CHANGELOG.md), so registered-but-not-derived is not a finding."""
    config = root / ".oss.json"
    if not config.is_file():
        raise Unreadable(f"{config} is missing -- the registry cross-check could not be made")
    data = _json(config)
    registered = set(data.get("version_sites") or ())
    if not registered:
        raise Unreadable(".oss.json declares no `version_sites` -- nothing to cross-check against")
    return sorted({d.site for d in found.declarations} - registered)

# -- the real tree ---------------------------------------------------------------------------


def test_no_site_was_unreadable():
    """`could not check` is its own verdict and must never ride along inside a green agreement."""
    found = survey(REPO_ROOT)
    assert not found.problems, (
        "COULD NOT CHECK -- this is not drift, and it is not agreement. A version site exists that this guard "
        "knows how to read and could not:\n" + found.describe_problems()
    )


def test_the_scan_reached_every_known_declaration_site():
    """An empty or shortened scan is 'could not look', never 'looked and found nothing'."""
    found = survey(REPO_ROOT)
    assert found.declarations, (
        "COULD NOT CHECK -- the scan derived no version declarations at all. An empty scan set cannot support an " "'everything agrees' verdict."
    )
    missing = missing_anchors(found)
    assert not missing, (
        "COULD NOT CHECK -- these known version sites yielded no declaration, so the scan is no longer looking " f"at this project: {missing}. Fix the reader or the anchor, never the assertion.\nWhat it did find:\n"
        + found.describe()
    )


def test_every_declared_version_agrees():
    found = survey(REPO_ROOT)
    assert len(found.versions) == 1, (
        "VERSION DRIFT -- these files declare the project version and they disagree. A release that half-lands " "is invisible: PyPI gets the new version while plugin users are never offered it and `requivo doctor` "
        "reports the wrong one.\n" + found.describe()
    )


def test_every_declaration_site_is_registered():
    """The release process edits `version_sites`; a site missing from it is a site a release skips."""
    found = survey(REPO_ROOT)
    strays = unregistered(found, REPO_ROOT)
    assert not strays, (
        "UNREGISTERED VERSION SITE -- these files declare the version but are not in `.oss.json`'s " f"`version_sites`, so a release will not know to update them: {strays}"
    )

# Positive controls: every "must not fire" above is paired with a "must fire" below, so this file cannot be asserting
# nothing while looking identical.


def _tree(root: Path, files: dict) -> Path:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def _agreeing(version: str = "1.2.3") -> dict:
    return {
        "pyproject.toml": f'[project]\nname = "x"\nversion = "{version}"\n\n[tool.ruff]\ntarget-version = "py39"\n',
        "src/x/__init__.py": f'__version__ = "{version}"\n',
        ".claude-plugin/marketplace.json": json.dumps({"plugins": [{"name": "x", "version": version}]}),
        "plugins/x/.claude-plugin/plugin.json": json.dumps({"name": "x", "version": version}),
        ".oss.json": json.dumps({"version_sites": [
            "pyproject.toml", "CHANGELOG.md", "src/x/__init__.py",
            ".claude-plugin/marketplace.json", "plugins/x/.claude-plugin/plugin.json",
        ]}),
    }


def test_an_agreeing_tree_is_read_as_agreeing(tmp_path):
    """The must-fire control's partner: if this failed, every red below would prove nothing."""
    found = survey(_tree(tmp_path, _agreeing()))
    assert not found.problems
    assert found.versions == {"1.2.3"}
    assert len(found.declarations) == 4


@pytest.mark.parametrize("site", [
    "pyproject.toml", "src/x/__init__.py", ".claude-plugin/marketplace.json", "plugins/x/.claude-plugin/plugin.json",
])
def test_a_drift_at_any_single_site_is_caught(tmp_path, site):
    """Each site drifted on its own -- so no site is carried green by its neighbours."""
    files = _agreeing()
    files[site] = files[site].replace("1.2.3", "9.9.9")
    found = survey(_tree(tmp_path, files))
    assert not found.problems, "a drifted file must still be READABLE -- otherwise this proves the wrong thing"
    assert found.versions == {"1.2.3", "9.9.9"}, f"{site}: drift went unnoticed"


def test_a_nested_checkout_is_pruned_and_the_prune_is_what_did_it(tmp_path):
    """A `git worktree` under the repo root duplicates every site; `_PRUNED` prunes by name and missed six strays. Asserted both ways: with the `.git` marker the nested tree is invisible, without it the identical tree is found -- proving the prune, not a scan gone blind."""
    root = _tree(tmp_path, _agreeing())
    nested = root / ".claude" / "worktrees" / "agent-1"
    for site, body in _agreeing().items():
        p = nested / site
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.replace("1.2.3", "9.9.9"), encoding="utf-8")

    # A worktree's `.git` is a FILE, not a directory -- the case `is_dir()` would miss.
    (nested / ".git").write_text("gitdir: /elsewhere/.git/worktrees/agent-1\n", encoding="utf-8")
    found = survey(root)
    assert found.versions == {"1.2.3"}, "the nested checkout's version leaked into the survey"
    # Matched on the worktree path, not on ".claude" -- two of the four legitimate sites are `.claude-plugin/...`,
    # so the looser substring catches exactly what must survive.
    assert not [d for d in found.declarations if d.site.startswith(".claude/worktrees/")]
    assert len(found.declarations) == 4, "the four real sites must still be there"

    # must fire: without the marker the same four files ARE found, so the prune is what removed them and not a scan
    # that stopped looking.
    (nested / ".git").unlink()
    assert survey(root).versions == {"1.2.3", "9.9.9"}


def test_an_unreadable_site_is_could_not_check_and_not_drift(tmp_path):
    """The two reds must be distinguishable: this one reports a problem and NO disagreement."""
    files = _agreeing()
    files["plugins/x/.claude-plugin/plugin.json"] = "{ this is not json"
    found = survey(_tree(tmp_path, files))
    assert found.problems, "an unparseable manifest must be reported, never skipped into a green pass"
    assert [s for s, _ in found.problems] == ["plugins/x/.claude-plugin/plugin.json"]
    assert "not valid JSON" in found.describe_problems()
    # The decisive half: the surviving sites still agree, so agreement alone would have passed.
    assert len(found.versions) == 1


def test_a_manifest_that_declares_no_version_is_could_not_check(tmp_path):
    files = _agreeing()
    files["plugins/x/.claude-plugin/plugin.json"] = json.dumps({"name": "x"})
    found = survey(_tree(tmp_path, files))
    assert "declares no version" in found.describe_problems()


def test_a_site_that_is_not_utf8_is_could_not_check(tmp_path):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError` -- catching only the latter let a mis-encoded site escape the survey as a traceback instead of a `could not look` verdict."""
    root = _tree(tmp_path, _agreeing())
    (root / "src" / "x" / "__init__.py").write_bytes(b'__version__ = "\xff\xfe1.2.3"\n')
    found = survey(root)
    assert [s for s, _ in found.problems] == ["src/x/__init__.py"]
    assert "not valid UTF-8" in found.describe_problems()
    # And it must not be mistaken for agreement: the readable sites still agree.
    assert len(found.versions) == 1


def test_a_missing_anchor_is_could_not_check(tmp_path):
    """A site that MOVED must not read as a shorter, still-agreeing scan."""
    files = _agreeing()
    del files["pyproject.toml"]
    found = survey(_tree(tmp_path, files))
    assert len(found.versions) == 1, "the remaining sites agree -- which is exactly the trap"
    assert "pyproject.toml" in missing_anchors(found)


def test_an_empty_tree_is_refused_rather_than_called_clean(tmp_path):
    found = survey(tmp_path)
    assert not found.declarations
    assert missing_anchors(found) == list(ANCHOR_SITES)


def test_a_root_that_does_not_exist_is_could_not_check(tmp_path):
    found = survey(tmp_path / "nope")
    assert found.problems and "no such directory" in found.describe_problems()


def test_a_changelog_is_never_read_as_a_declaration(tmp_path):
    """CHANGELOG.md names every past version. A history file is not a declaration."""
    files = _agreeing()
    files["CHANGELOG.md"] = '# Changelog\n\n## [0.1.0]\nversion = "0.0.1"\n\n## [9.9.9]\n'
    found = survey(_tree(tmp_path, files))
    assert found.versions == {"1.2.3"}
    assert "CHANGELOG.md" not in {d.site for d in found.declarations}


def test_a_dependency_pin_is_never_read_as_a_declaration(tmp_path):
    files = _agreeing()
    files["pyproject.toml"] = (
        '[project]\nname = "x"\nversion = "1.2.3"\nrequires-python = ">=3.9"\ndependencies = ["pydantic>=2.0,<3"]\n'
        '\n[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n\n[tool.ruff]\ntarget-version = "py39"\n'
    )
    found = survey(_tree(tmp_path, files))
    assert found.versions == {"1.2.3"}


def test_an_installed_dependency_is_not_scanned(tmp_path):
    files = _agreeing()
    files[".venv/lib/site-packages/other/.claude-plugin/plugin.json"] = json.dumps({"name": "o", "version": "0.0.1"})
    found = survey(_tree(tmp_path, files))
    assert found.versions == {"1.2.3"}, "a vendored/installed tree must be pruned, not compared"


def test_an_unregistered_site_is_reported(tmp_path):
    """The failure that actually happened: a real site nobody added to `version_sites`."""
    files = _agreeing()
    config = json.loads(files[".oss.json"])
    config["version_sites"].remove("src/x/__init__.py")
    files[".oss.json"] = json.dumps(config)
    root = _tree(tmp_path, files)
    found = survey(root)
    assert len(found.versions) == 1, "everything agrees today -- the registry gap is the finding"
    assert unregistered(found, root) == ["src/x/__init__.py"]


def test_a_registry_naming_a_non_declaring_file_is_not_a_finding(tmp_path):
    """CHANGELOG.md is registered and declares nothing. One-directional, so that is fine."""
    root = _tree(tmp_path, _agreeing())
    assert unregistered(survey(root), root) == []


def test_a_missing_registry_is_could_not_check_rather_than_clean(tmp_path):
    files = _agreeing()
    del files[".oss.json"]
    root = _tree(tmp_path, files)
    with pytest.raises(Unreadable, match="registry cross-check could not be made"):
        unregistered(survey(root), root)


def test_a_registry_with_no_version_sites_is_could_not_check(tmp_path):
    files = _agreeing()
    files[".oss.json"] = json.dumps({"repo": "x/y"})
    root = _tree(tmp_path, files)
    with pytest.raises(Unreadable, match="no `version_sites`"):
        unregistered(survey(root), root)
