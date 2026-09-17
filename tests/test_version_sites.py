"""Every file that declares the project version declares the same one (#32). Sites are derived by scanning
structural positions, cross-checked against `.oss.json`'s `version_sites`, with three verdicts: ok, disagree,
could not check."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Anchors, not the listing: adding a site must not fail this file, moving one of these must.
ANCHOR_SITES = (
    "pyproject.toml", "src/requivo/__init__.py", ".claude-plugin/marketplace.json",
    "plugins/claude-code/.claude-plugin/plugin.json",
)
# A version declaration inside a dependency or a build artifact is not this project declaring anything.
_PRUNED = frozenset({
    ".venv", "venv", ".git", ".tox", ".nox", "node_modules", "build", "dist", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".eggs",
})


def _is_nested_checkout(directory: Path) -> bool:
    """A worktree's `.git` is a file, a nested clone's a directory: `exists()` catches both."""
    return (directory / ".git").exists()


# Anchored at line start and key so a dependency pin or `requires-python` is never read as a declaration.
_VERSION_KEY_RE = re.compile(r"""^[ \t]*version[ \t]*=[ \t]*["']([^"']+)["']""", re.MULTILINE)
_DUNDER_RE = re.compile(r"""^__version__[ \t]*=[ \t]*["']([^"']+)["']""", re.MULTILINE)
_PROJECT_TABLE_RE = re.compile(r"^\[project\][^\n]*\n(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)


class Unreadable(Exception):
    """A file this guard knows how to read and could not: never silently skipped."""


@dataclass(frozen=True)
class Declaration:
    site: str     # repo-relative, POSIX separators
    where: str    # the structural position inside the file
    version: str


@dataclass(frozen=True)
class Survey:
    declarations: tuple
    problems: tuple    # (site, reason): every "could not look"

    @property
    def versions(self) -> set:
        return {d.version for d in self.declarations}

    def describe(self) -> str:
        return "\n".join(f"  {d.site} ({d.where}) = {d.version}" for d in sorted(self.declarations, key=lambda d: d.site))

    def describe_problems(self) -> str:
        return "\n".join(f"  {site}: {reason}" for site, reason in sorted(self.problems))


def _text(path: Path) -> str:
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
    """`[project] version` by a section-scoped regex: the floor is 3.9 and `tomllib` is 3.11."""
    table = _PROJECT_TABLE_RE.search(_text(path))
    if not table:
        raise Unreadable("no [project] table")
    found = _VERSION_KEY_RE.search(table.group(1))
    if not found:
        raise Unreadable("[project] declares no version")
    return [Declaration(site, "[project] version", found.group(1))]


def _read_dunder(site: str, path: Path) -> list:
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
    return [Declaration(site, f"plugins[{entry.get('name', '?')}].version", str(entry["version"]))
            for entry in data["plugins"] if isinstance(entry, dict) and "version" in entry]


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
            # `relative.parents` stops short of root itself, which carries a `.git` too.
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
    """Derived sites absent from `.oss.json`'s `version_sites`; one-directional, since CHANGELOG.md declares nothing."""
    config = root / ".oss.json"
    if not config.is_file():
        raise Unreadable(f"{config} is missing -- the registry cross-check could not be made")
    data = _json(config)
    registered = set(data.get("version_sites") or ())
    if not registered:
        raise Unreadable(".oss.json declares no `version_sites` -- nothing to cross-check against")
    return sorted({d.site for d in found.declarations} - registered)

# ---- the real tree ----


def test_no_site_was_unreadable():
    """`could not check` is its own verdict and never rides inside a green agreement."""
    found = survey(REPO_ROOT)
    assert not found.problems, "COULD NOT CHECK -- a version site this guard knows how to read could not be read:\n" + found.describe_problems()


def test_the_scan_reached_every_known_declaration_site():
    found = survey(REPO_ROOT)
    assert found.declarations, "COULD NOT CHECK -- the scan derived no version declarations at all"
    missing = missing_anchors(found)
    assert not missing, f"COULD NOT CHECK -- these known sites yielded no declaration: {missing}. Found:\n" + found.describe()


def test_every_declared_version_agrees():
    found = survey(REPO_ROOT)
    assert len(found.versions) == 1, "VERSION DRIFT -- these files declare the project version and disagree:\n" + found.describe()


def test_every_declaration_site_is_registered():
    """A site missing from `version_sites` is a site a release skips."""
    strays = unregistered(survey(REPO_ROOT), REPO_ROOT)
    assert not strays, f"UNREGISTERED VERSION SITE -- not in `.oss.json`'s `version_sites`: {strays}"

# ---- positive controls: every must-not-fire above has a must-fire below ----


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
    found = survey(_tree(tmp_path, _agreeing()))
    assert not found.problems
    assert found.versions == {"1.2.3"}
    assert len(found.declarations) == 4
    assert unregistered(found, tmp_path) == []  # CHANGELOG.md is registered and declares nothing: fine


@pytest.mark.parametrize("site", [
    "pyproject.toml", "src/x/__init__.py", ".claude-plugin/marketplace.json", "plugins/x/.claude-plugin/plugin.json",
])
def test_a_drift_at_any_single_site_is_caught(tmp_path, site):
    """MUST-FIRE: no site is carried green by its neighbours."""
    files = _agreeing()
    files[site] = files[site].replace("1.2.3", "9.9.9")
    found = survey(_tree(tmp_path, files))
    assert not found.problems, "a drifted file must still be readable"
    assert found.versions == {"1.2.3", "9.9.9"}, f"{site}: drift went unnoticed"


def test_a_nested_checkout_is_pruned_and_the_prune_is_what_did_it(tmp_path):
    """A worktree under the repo root duplicates every site; with the `.git` marker it is invisible, without it found."""
    root = _tree(tmp_path, _agreeing())
    nested = root / ".claude" / "worktrees" / "agent-1"
    for site, body in _agreeing().items():
        p = nested / site
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.replace("1.2.3", "9.9.9"), encoding="utf-8")
    (nested / ".git").write_text("gitdir: /elsewhere/.git/worktrees/agent-1\n", encoding="utf-8")  # a file, not a dir
    found = survey(root)
    assert found.versions == {"1.2.3"}, "the nested checkout's version leaked into the survey"
    assert not [d for d in found.declarations if d.site.startswith(".claude/worktrees/")]
    assert len(found.declarations) == 4, "the four real sites must still be there"
    (nested / ".git").unlink()
    assert survey(root).versions == {"1.2.3", "9.9.9"}


@pytest.mark.parametrize("site, body, reason", [
    ("plugins/x/.claude-plugin/plugin.json", "{ this is not json", "not valid JSON"),
    ("plugins/x/.claude-plugin/plugin.json", json.dumps({"name": "x"}), "declares no version"),
    ("src/x/__init__.py", b'__version__ = "\xff\xfe1.2.3"\n', "not valid UTF-8"),
], ids=["unparseable", "no-version", "not-utf8"])
def test_an_unreadable_site_is_could_not_check_and_not_drift(tmp_path, site, body, reason):
    """Two reds that must stay distinguishable: a problem is reported, and the readable sites still agree."""
    root = _tree(tmp_path, _agreeing())
    if isinstance(body, bytes):
        (root / site).write_bytes(body)
    else:
        (root / site).write_text(body, encoding="utf-8")
    found = survey(root)
    assert [s for s, _ in found.problems] == [site], "an unreadable site must be reported, never skipped into a green pass"
    assert reason in found.describe_problems()
    assert len(found.versions) == 1


def test_a_missing_anchor_is_could_not_check(tmp_path):
    """A site that moved must not read as a shorter, still-agreeing scan."""
    files = _agreeing()
    del files["pyproject.toml"]
    found = survey(_tree(tmp_path, files))
    assert len(found.versions) == 1, "the remaining sites agree -- which is exactly the trap"
    assert "pyproject.toml" in missing_anchors(found)


def test_an_empty_or_missing_root_is_refused_rather_than_called_clean(tmp_path):
    found = survey(tmp_path)
    assert not found.declarations and missing_anchors(found) == list(ANCHOR_SITES)
    found = survey(tmp_path / "nope")
    assert found.problems and "no such directory" in found.describe_problems()


@pytest.mark.parametrize("extra", [
    {"CHANGELOG.md": '# Changelog\n\n## [0.1.0]\nversion = "0.0.1"\n\n## [9.9.9]\n'},
    {"pyproject.toml": '[project]\nname = "x"\nversion = "1.2.3"\nrequires-python = ">=3.9"\ndependencies = ["pydantic>=2.0,<3"]\n'
                       '\n[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n\n[tool.ruff]\ntarget-version = "py39"\n'},
    {".venv/lib/site-packages/other/.claude-plugin/plugin.json": json.dumps({"name": "o", "version": "0.0.1"})},
], ids=["changelog", "dependency-pin", "installed-dependency"])
def test_a_version_shaped_string_elsewhere_is_never_read_as_a_declaration(tmp_path, extra):
    files = {**_agreeing(), **extra}
    found = survey(_tree(tmp_path, files))
    assert found.versions == {"1.2.3"}
    assert "CHANGELOG.md" not in {d.site for d in found.declarations}


def test_an_unregistered_site_is_reported(tmp_path):
    """The failure that happened: a real site nobody added to `version_sites`."""
    files = _agreeing()
    config = json.loads(files[".oss.json"])
    config["version_sites"].remove("src/x/__init__.py")
    files[".oss.json"] = json.dumps(config)
    root = _tree(tmp_path, files)
    found = survey(root)
    assert len(found.versions) == 1, "everything agrees today -- the registry gap is the finding"
    assert unregistered(found, root) == ["src/x/__init__.py"]


@pytest.mark.parametrize("config, match", [
    (None, "registry cross-check could not be made"),
    (json.dumps({"repo": "x/y"}), "no `version_sites`"),
], ids=["missing", "no-version-sites"])
def test_a_registry_that_cannot_be_read_is_could_not_check_rather_than_clean(tmp_path, config, match):
    files = _agreeing()
    del files[".oss.json"]
    if config is not None:
        files[".oss.json"] = config
    root = _tree(tmp_path, files)
    with pytest.raises(Unreadable, match=match):
        unregistered(survey(root), root)
