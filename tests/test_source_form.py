"""Source-form guards (#551): the boundaries (invariant 7; #77, #76), the encoding rule (invariant 16;
#11, #29), the dependency floor (#469), the reference guard (#75) and the lean ratchet (#553)."""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _scan import list_files, list_python_files, parse_utf8, write_tree

from requivo import cli, cli_support, streams
from requivo.core.errors import InvalidModelError
from requivo.deterministic import read_user_text

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "requivo"
_parse = parse_utf8
_write_tree = write_tree


def test_the_scan_helpers_refuse_a_root_they_could_not_look_at(tmp_path):
    """#10: an empty or missing scan root is "could not look", never "looked and found nothing"."""
    missing, empty = tmp_path / "missing", tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no such directory"):
        list_python_files(missing, label="boundary guard")
    with pytest.raises(AssertionError, match="no Python files"):
        list_python_files(empty, label="boundary guard")
    with pytest.raises(AssertionError, match="could not look"):
        list_files((empty,), suffixes=(".py",), label="the reference guard")
    with pytest.raises(AssertionError, match="no such file"):
        subject_module(missing / "cli.py")

# ---- SECTION 1: boundaries. core stays provider- and process-free; a surface reaches the provider ----
# ---- and the store only through the named seam (invariant 7; #77, #76, #167, #183, #355, #425). ----

CORE = SRC / "core"
CORE_PACKAGE = "requivo.core"
CORE_ANCHORS = ("__init__.py", "contracts.py")  # anchors, not a listing: a renamed package must fail

_PROVIDER_IMPORTS = {
    "anthropic": "the Anthropic SDK -- core must work with no API key and no SDK installed",
    "providers": "requivo.providers -- the dependency arrow points provider -> core, never the reverse",
}
_TERMINAL_IMPORTS = {
    "argparse": "an argument parser -- core is called with parameters; cli.py owns argv",
    "click": "a terminal CLI framework -- see argparse",
    "typer": "a terminal CLI framework -- see argparse",
    "curses": "a terminal UI library -- core renders nothing",
}
_FORBIDDEN_CALLS = {
    "print": "writes to stdout -- core returns data and a caller decides whether to display it",
    "input": "reads stdin -- core takes arguments, it never prompts",
    "breakpoint": "opens an interactive prompt on whatever terminal happens to be attached",
}
_FORBIDDEN_ATTRIBUTES = {
    ("sys", "argv"): "reads the process arguments -- cli.py owns argv",
    ("sys", "stdout"): "writes to stdout -- render/ turns data into strings, interfaces print them",
    ("sys", "stderr"): "writes to stderr -- see sys.stdout",
    ("sys", "stdin"): "reads stdin -- see input()",
    ("sys", "exit"): "kills the process -- an engine raises, an interface decides to exit",
    ("os", "environ"): "reads ambient process state -- paths.py, which is not core, owns the environment",
    ("os", "getenv"): "reads ambient process state -- see os.environ",
    ("os", "putenv"): "writes ambient process state -- see os.environ",
}


def _package_for(path: Path, root: Path, root_package: str) -> str:
    parts = path.relative_to(root).parts[:-1]
    return ".".join((root_package, *parts))


def _resolve_relative(package: str, level: int) -> str:
    """The dotted base a relative import of depth `level` is measured from; level 0 is absolute."""
    if level == 0:
        return ""
    parts = package.split(".")
    return ".".join(parts[: max(0, len(parts) - (level - 1))])


def scan(root: Path, package: str) -> list[tuple[Path, str]]:
    """Every Python file under `root`, paired with the package its relative imports resolve against."""
    return [(p, _package_for(p, root, package)) for p in list_python_files(root, label="boundary guard")]


def imported_modules(path: Path, package: str) -> set[str]:
    """Every module `path` imports as a dotted absolute name, relative imports resolved against `package`."""
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            if node.module:
                names.add(f"{base}.{node.module}" if base else node.module)
            else:
                names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def import_hits(modules: set[str], table: dict) -> dict:
    hits = {}
    for module in sorted(modules):
        for part in module.split("."):
            if part in table:
                hits[module] = table[part]
                break
    return hits


def process_violations(path: Path, package: str) -> list[str]:
    """Every place `path` talks to the process rather than to its caller."""
    out: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            out.append(f"line {node.lineno}: {node.func.id}() -- {_FORBIDDEN_CALLS[node.func.id]}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            key = (node.value.id, node.attr)
            if key in _FORBIDDEN_ATTRIBUTES:
                out.append(f"line {node.lineno}: {key[0]}.{key[1]} -- {_FORBIDDEN_ATTRIBUTES[key]}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in ("sys", "os"):
            # `from sys import argv` never produces an Attribute node, so the import is where it shows.
            for alias in node.names:
                key = (node.module, alias.name)
                if key in _FORBIDDEN_ATTRIBUTES:
                    out.append(f"line {node.lineno}: from {node.module} import {alias.name} -- {_FORBIDDEN_ATTRIBUTES[key]}")
    for module, reason in sorted(import_hits(imported_modules(path, package), _TERMINAL_IMPORTS).items()):
        out.append(f"imports {module} -- {reason}")
    return sorted(out)


def _core_offenders(table: dict) -> dict:
    offenders: dict = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        hits = import_hits(imported_modules(path, package), table)
        if hits:
            offenders[path.relative_to(CORE).as_posix()] = sorted(hits)
    return offenders


def test_the_guard_scans_the_real_core_package():
    """Name what was scanned: everything below is a negative assertion over this set."""
    names = sorted(p.relative_to(CORE).as_posix() for p, _ in scan(CORE, CORE_PACKAGE))
    missing = [anchor for anchor in CORE_ANCHORS if anchor not in names]
    assert not missing, f"the boundary guard scanned {CORE} and did not find {missing}; scanned: {names}"


def test_core_never_imports_a_provider():
    """Invariant 7: no module in core imports the SDK or `requivo.providers`."""
    offenders = _core_offenders(_PROVIDER_IMPORTS)
    assert not offenders, f"requivo.core must not import a provider; offenders: {offenders}"


_IMPORT_VIOLATIONS = {
    "absolute_sdk.py": "import anthropic\n",
    "absolute_provider.py": "from requivo.providers.anthropic import AnthropicProvider\n",
    "dotted_import.py": "import requivo.providers.anthropic\n",
    "relative_sibling.py": "from .anthropic import Client\n",
    "relative_parent.py": "from ..providers import anthropic\n",
    "relative_bare.py": "from .. import providers\n",
    "relative_aliased.py": "from ..providers.anthropic import AnthropicProvider as P\n",
    "sub/buried.py": "from ...providers import anthropic\n",  # the walk must be recursive
}


def test_the_import_guard_sees_every_way_of_writing_the_violation(tmp_path):
    """MUST-FIRE: each forbidden import shape, the three relative forms and a buried subpackage included."""
    root = tmp_path / "core"
    _write_tree(root, {**_IMPORT_VIOLATIONS, "__init__.py": "", "sub/__init__.py": ""})
    missed = [path.relative_to(root).as_posix() for path, package in scan(root, CORE_PACKAGE)
              if path.name != "__init__.py" and not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)]
    assert not missed, f"the import guard is blind to these: {missed}"


def test_core_never_touches_the_process():
    """Invariant 7's other half: no argv, standard streams, environment or exit in core (#10)."""
    offenders = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        found = process_violations(path, package)
        if found:
            offenders[path.relative_to(CORE).as_posix()] = found
    assert not offenders, f"requivo.core must not talk to the process; offenders: {offenders}"


_PROCESS_VIOLATIONS = {
    "prints.py": "def show(model):\n    print(model)\n",
    "prompts.py": 'def ask():\n    return input("slug? ")\n',
    "breakpoints.py": "def debug():\n    breakpoint()\n",
    "reads_argv.py": "import sys\n\ndef slug():\n    return sys.argv[1]\n",
    "reads_argv_by_name.py": "from sys import argv\n\ndef slug():\n    return argv[1]\n",
    "writes_stdout.py": "import sys\n\ndef show(text):\n    sys.stdout.write(text)\n",
    "writes_stderr.py": "import sys\n\ndef warn(text):\n    sys.stderr.write(text)\n",
    "reads_stdin.py": "import sys\n\ndef read():\n    return sys.stdin.read()\n",
    "exits.py": "import sys\n\ndef stop():\n    sys.exit(1)\n",
    "reads_env.py": 'import os\n\ndef root():\n    return os.environ["REQUIVO_WORKSPACE"]\n',
    "getenvs.py": 'import os\n\ndef root():\n    return os.getenv("REQUIVO_WORKSPACE")\n',
    "getenv_by_name.py": 'from os import getenv\n\ndef root():\n    return getenv("REQUIVO_WORKSPACE")\n',
    "parses_args.py": "import argparse\n\ndef parser():\n    return argparse.ArgumentParser()\n",
    "buried/deeper.py": "import sys\n\ndef slug():\n    return sys.argv[1]\n",
}


def test_the_process_guard_sees_each_forbidden_construct(tmp_path):
    """MUST-FIRE: one fixture per construct, so a narrowing of the guard shows up as itself."""
    root = tmp_path / "core"
    _write_tree(root, _PROCESS_VIOLATIONS)
    missed = [path.relative_to(root).as_posix() for path, package in scan(root, CORE_PACKAGE)
              if not process_violations(path, package)]
    assert not missed, f"the process guard is blind to these: {missed}"

# ---- the other end of the arrow: a surface reaches the provider only through the services (#77) ----

CLI_PACKAGE = "requivo"
RENDER = SRC / "render"
RENDER_PACKAGE = "requivo.render"


def top_level_modules() -> list[Path]:
    """Every module directly under `src/requivo/`, derived rather than listed (#587)."""
    found = sorted(SRC.glob("*.py"))
    if not found:
        raise AssertionError("scan of src/requivo found no top-level modules -- fix the path")
    return found


# Keyed by (file, name) so one file's argument cannot license another's; add an entry only with a
# reason a reader can argue with. `track_usage` left at #167: the concept moved out of providers/.
_SURFACE_PROVIDER_ALLOWLIST = {
    ("cli.py", "new_client"): "builds the SDK client from the environment; DiscoveryService decides when to reason with it.",
    ("cli.py", "EngineError"): "an exception type, isinstance-caught by app()/_cmd_web; its provider_unavailable code is public --json (#135). From providers.errors since #167.",
    ("api/app.py", "EngineError"): "the identical shape one surface over (#425): create_api() raises it when the [api] extra's fastapi import fails.",
    ("web/config.py", "Anthropic"): "the SDK handle, probed in a try/except to answer one boolean -- is it installed? -- never built into a client.",
    ("web/config.py", "credential_present"): "answers is a credential visible?, via the SDK's own resolution chain since #334 (a transient client, built and discarded, #374) rather than re-deriving env-var names, which drifted at #332.",
    ("http.py", "EngineError"): "the same exception-type shape as cli.py's entry, moved out of web/app.py by #422; touches none of argv/stdout/HTTP, so it is a top-level module the scan sweeps rather than a surface.",
    ("web/routes/sessions.py", "EngineError"): "caught for a routing decision the HTTP boundary can't make: session page rather than a bare 500 (#207); paired with ProviderOutputError since #253.",
    ("deterministic/doctor.py", "current_model_name"): "reads the provider's own model id for the row `requivo doctor` prints (#247); no client is built, no call is made. Drifted once already (#364, #268).",
    ("deterministic/doctor.py", "credential_diagnosis"): "same reasoning as current_model_name, for api_key_present/credential_problem (#365, the drift #332 found one function along); also builds a transient client since #334 (#374).",
}

PROVIDER_TREES = (
    (RENDER, RENDER_PACKAGE),
    (SRC / "web", "requivo.web"),
    (SRC / "deterministic", "requivo.deterministic"),
    (SRC / "api", "requivo.api"),
)
# A whole-module import contributes this marker instead of a name: unspellable as an allowlist key.
_WHOLE_MODULE = "(the whole module)"


def subject_module(path: Path) -> Path:
    """`scan` for a guard whose subject is one named module: a missing file is 'could not look' (#10)."""
    if not path.is_file():
        raise AssertionError(f"boundary guard could not read {path}: no such file -- fix the path, never the assertion.")
    return path


def provider_subjects() -> list[tuple[Path, str, str]]:
    """Every surface the provider guard watches, as (path, package, label): the allowlist key."""
    subjects = [(subject_module(m), CLI_PACKAGE, m.name) for m in top_level_modules()]
    for root, package in PROVIDER_TREES:
        subjects.extend((p, pkg, p.relative_to(SRC).as_posix()) for p, pkg in scan(root, package))
    return subjects


def _crosses_providers(module: str) -> bool:
    return "providers" in module.split(".")


def provider_names(path: Path, package: str) -> set[str]:
    """Every name `path` can reach inside `requivo.providers`; a module import yields `_WHOLE_MODULE`."""
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(f"{a.name} {_WHOLE_MODULE}" for a in node.names if _crosses_providers(a.name))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            module = f"{base}.{node.module}" if base and node.module else (node.module or base)
            if node.module is None:
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    if _crosses_providers(full):
                        names.add(f"{full} {_WHOLE_MODULE}")
            elif _crosses_providers(module):
                # `from requivo.providers import anthropic` names a submodule; anything deeper names a symbol.
                submodules = module.split(".")[-1] == "providers"
                for alias in node.names:
                    names.add(f"{module}.{alias.name} {_WHOLE_MODULE}" if submodules else alias.name)
    return names


def test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns():
    """#77, #167: no second orchestration; asserted in both directions so a stale allowlist entry fails too."""
    reached = {(label, name) for path, package, label in provider_subjects() for name in provider_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_PROVIDER_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past the service seam to {unexpected}. Route it through DiscoveryService, move a "
        f"provider-neutral name out of `providers/`, or add it to _SURFACE_PROVIDER_ALLOWLIST with its reason."
    )
    stale = sorted(set(_SURFACE_PROVIDER_ALLOWLIST) - reached)
    assert not stale, f"_SURFACE_PROVIDER_ALLOWLIST still names {stale}, which no surface imports any more -- delete it."


_SURFACE_PROVIDER_IMPORTS = {
    "from_module.py": "from requivo.providers.anthropic import advise\n",
    "aliased_symbol.py": "from requivo.providers.anthropic import advise as a\n",
    "dotted.py": "import requivo.providers.anthropic\n",
    "aliased_module.py": "import requivo.providers.anthropic as prov\n",
    "submodule.py": "from requivo.providers import anthropic\n",
    "relative_package.py": "from .providers.anthropic import run\n",
    "relative_bare.py": "from . import providers\n",
}


def test_the_surface_guard_sees_every_way_of_reaching_the_provider(tmp_path):
    """MUST-FIRE: each import shape is seen, and a whole-module import can never match an allowlist name."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_PROVIDER_IMPORTS)
    missed = [path.name for path, package in scan(root, CLI_PACKAGE) if not provider_names(path, package)]
    assert not missed, f"the surface guard is blind to these: {missed}"
    reached = provider_names(root / "dotted.py", CLI_PACKAGE)
    assert reached == {f"requivo.providers.anthropic {_WHOLE_MODULE}"}
    assert not reached & {name for _, name in _SURFACE_PROVIDER_ALLOWLIST}, "the marker no longer separates them"

# ---- the storage half: a surface reaches the store only through SessionRepository (#76) ----

SURFACE_TREES = (
    (SRC / "deterministic", "requivo.deterministic"),
    (SRC / "web", "requivo.web"),
    (SRC / "providers", "requivo.providers"),  # #355
    (SRC / "api", "requivo.api"),  # #425
)
PERSISTENCE_MODULE = "requivo.core.persistence"

# Keyed by (file, name): the target is zero *unjustified* direct calls, and each entry is its argument.
_SURFACE_STORAGE_ALLOWLIST = {
    ("cli.py", "canonical_dir"): "prints where a session landed after discover/answer -- no repository path exists to route it through.",
    ("cli_support.py", "artifact_path"): "prints a generated artifact's path (moved out of cli.py by #550); a printed path is a disclosure like any other (#36).",
    ("cli.py", "write_artifact_file"): "writes the three neutral epic exports -- untracked extra views of an already-saved artifact, not a second artifact-list row. Carries its own source_revision since #274.",
    ("cli.py", "load_model"): "reads a bare model.json the user named on the command line -- not a session, no repository method can reach it.",
    ("deterministic/sessions/lifecycle.py", "canonical_dir"): "session init reports where the session landed. See the cli.py entry.",
    ("deterministic/sessions/archives.py", "canonical_dir"): "export/restore/import each report or need the directory they are about to zip, copy into or move over.",
    ("deterministic/sessions/verify.py", "canonical_dir"): "the restore remedy line searches this session's own revisions/ before telling the reader session restore would do anything.",
    ("deterministic/sessions/archives.py", "ensure_store_dir"): "creates .requivo/sessions/ before import moves a session into it, and writes the privacy .gitignore (#211).",
    ("deterministic/sessions/lifecycle.py", "migrate_legacy"): "converts a session from the retired out/ layout -- a statement about two filesystem layouts, which is what the verb is.",
    ("deterministic/sessions/archives.py", "validate_slug"): "checks a directory name inside an uploaded archive is slug-shaped, before anything is extracted -- before any session exists.",
    ("deterministic/sessions/archives.py", "_replace_with_retry"): "the transient-PermissionError retry export/restore share with _atomic_write (invariant 18), moved to core/persistence/atomic.py by #550.",
    ("deterministic/doctor.py", "scan_session_root"): "the one caller needing all three parts of one partition -- two separate scans is two instants, and a session.json landing between them lands in no answer at all.",
    ("deterministic/doctor.py", "scan_lock_root"): "the lock-root residue check (#180) -- a fact about the filesystem backing, with no backing-neutral form.",
    ("deterministic/artifacts.py", "artifact_path"): "prints where artifact save put the file. See the cli.py entry.",
    ("web/dependencies.py", "_slug_shape"): "refuses a slug-shaped path segment at the HTTP boundary before any service -- the shape half of what used to be one validate_slug call (#396).",
    ("web/dependencies.py", "_refuse_new_reserved_slug"): "the other half of #396's split: every {slug} route addresses a session that must already exist, so the refusal is conditional (#372's read-time form).",
    ("web/routes/sessions.py", "validate_slug"): "the creation-time half, at the route that takes a slug from a form field -- unconditional, since POST /sessions can bring a slug into existence (#221).",
    ("api/dependencies.py", "_slug_shape"): "the identical call as web/dependencies.py's entry, one surface over (#425); slice 1 has no creation route, so only the read-time half exists here.",
    ("api/dependencies.py", "_refuse_new_reserved_slug"): "mirrors web/dependencies.py's pair, for the same reason (#372, #425).",
    ("providers/anthropic/completion.py", "_atomic_write"): "writes the JSON retry loop's final malformed reply into .requivo/debug/ so a bug report is one paste (#283); the private name is deliberate (#355) -- the alternative is a second atomic-write implementation, which invariant 16 exists to prevent.",
    ("providers/anthropic/completion.py", "ensure_store_dir"): "creates .requivo/debug/ before the first failed reply is written into it. Same reasoning as the archives.py ensure_store_dir entry above.",
    ("deterministic/sessions/lifecycle.py", "UnexaminableEntry"): "a plain dataclass, not a call -- reused vocabulary for the legacy out/ root's own scan (#411), which has no repository method of its own.",
}


def surface_subjects() -> list[tuple[Path, str, str]]:
    """Every surface file, as (path, package, label): the allowlist key."""
    subjects = [(subject_module(m), "requivo", m.name) for m in top_level_modules()]
    for root, package in SURFACE_TREES:
        subjects.extend((p, pkg, p.relative_to(SRC).as_posix()) for p, pkg in scan(root, package))
    return subjects


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def persistence_names(path: Path, package: str) -> set[str]:
    """Every name `path` reaches inside `core.persistence`, module aliases resolved so the key is per-function."""
    tree = _parse(path)
    aliases: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PERSISTENCE_MODULE:
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            module = f"{base}.{node.module}" if base and node.module else (node.module or base)
            if module == PERSISTENCE_MODULE:
                names.update(alias.name for alias in node.names)
            elif module == "requivo.core":
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "persistence")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted is None:
                continue
            head, _, attr = dotted.rpartition(".")
            if head in aliases:
                names.add(attr)
    return names


def test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns():
    """#76, in both directions: an entry naming a call no longer made fails too."""
    reached = {(label, name) for path, package, label in surface_subjects() for name in persistence_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_STORAGE_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past SessionRepository to core.persistence: {unexpected}. Route it through the "
        f"repository, or add it to _SURFACE_STORAGE_ALLOWLIST with the reason no backing-neutral form is possible."
    )
    stale = sorted(set(_SURFACE_STORAGE_ALLOWLIST) - reached)
    assert not stale, f"_SURFACE_STORAGE_ALLOWLIST still names {stale}, which the surfaces no longer reach -- delete it."


_SURFACE_STORAGE_IMPORTS = {
    "aliased.py": "from requivo.core import persistence as store\nstore.canonical_dir('s')\n",
    "bare.py": "from requivo.core import persistence\npersistence.canonical_dir('s')\n",
    "symbol.py": "from requivo.core.persistence import canonical_dir\n",
    "dotted.py": "import requivo.core.persistence as p\np.canonical_dir('s')\n",
    "relative.py": "from .core import persistence as store\nstore.canonical_dir('s')\n",
    "relative_symbol.py": "from .core.persistence import canonical_dir\n",
    "two.py": "from requivo.core import persistence as store\nstore.canonical_dir('s')\nstore.session_lock('s')\n",
}


def test_the_storage_guard_sees_every_way_of_reaching_the_store(tmp_path):
    """MUST-FIRE: every alias shape resolves, and two calls behind one import are two keys."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_STORAGE_IMPORTS)
    missed = [path.name for path, package in scan(root, "requivo") if "canonical_dir" not in persistence_names(path, package)]
    assert not missed, f"the storage guard is blind to these: {missed}"
    assert persistence_names(root / "two.py", "requivo") == {"canonical_dir", "session_lock"}


def test_the_surface_guards_name_what_they_scanned():
    """#10 for both surface scan sets: the packages #167, #183, #355 and #425 each found unguarded."""
    provider_labels = sorted(label for _, _, label in provider_subjects())
    for expected in ("cli.py", "http.py", "render/terminal.py", "web/config.py", "deterministic/sessions/lifecycle.py", "api/app.py"):
        assert expected in provider_labels, f"the provider guard did not scan {expected}; it scanned {provider_labels}"
    storage_labels = sorted(label for _, _, label in surface_subjects())
    for expected in ("cli.py", "deterministic/doctor.py", "web/dependencies.py", "providers/anthropic/completion.py", "api/dependencies.py"):
        assert expected in storage_labels, f"the storage guard did not scan {expected}; it scanned {storage_labels}"

# ---- SECTION 2: encoding. Every text read/write names its codec (#11); a console that cannot ----
# ---- encode a glyph degrades rather than crashing after the work has landed (#29). ----

SCAN_ROOTS = {"src/requivo": "the package", "scripts": "the golden harness and the install-free launcher"}
SCAN_ANCHORS = {"src/requivo": ("core/persistence/store.py", "deterministic/__init__.py"), "scripts": ("golden_lib.py",)}
_TEXT_METHODS = {
    "read_text": "Path.read_text() with no encoding decodes with the locale codepage, not the file's",
    "write_text": "Path.write_text() with no encoding encodes with the locale codepage, not UTF-8",
    "open": "Path.open() in text mode with no encoding uses the locale codepage in both directions",
}
_EXEMPT_RECEIVERS = {
    "os": "os.open returns a raw file descriptor -- there is no text layer and no codec to declare",
    "webbrowser": "webbrowser.open takes a URL and opens a browser, not a file",
    "zipfile": "zipfile members are opened as bytes unless wrapped explicitly",
    "tarfile": "tarfile members are opened as bytes unless wrapped explicitly",
    "shutil": "shutil has no text-mode open",
    "subprocess": "subprocess streams take their codec from the Popen call, not from open()",
}


def encoding_scan(root: Path) -> list[Path]:
    return list_python_files(root, label="the encoding guard")


def _has_keyword(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


def _mode_of(node: ast.Call, positional_index: int) -> tuple[str | None, bool]:
    """The literal `mode` of an open() call and whether it could be read; a runtime mode is a finding."""
    for kw in node.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value, True
            return None, False
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value, True
        return None, False
    return "r", True


_UNKNOWN_MODE = "whose mode is not a literal -- this guard cannot tell text from binary here, so pass encoding= explicitly or read bytes"


def encoding_violations(path: Path) -> list[str]:
    """Every text read or write in `path` that takes whatever codec the locale happens to offer."""
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            if _has_keyword(node, "encoding"):
                continue
            mode, known = _mode_of(node, positional_index=1)
            if not known:
                out.append(f"line {node.lineno}: open() {_UNKNOWN_MODE}")
            elif "b" not in mode:
                out.append(f"line {node.lineno}: open() in text mode -- {_TEXT_METHODS['open']}")
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _TEXT_METHODS:
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EXEMPT_RECEIVERS:
            continue
        if _has_keyword(node, "encoding"):
            continue
        if node.func.attr == "open":
            mode, known = _mode_of(node, positional_index=0)
            if known and "b" in mode:
                continue
            if not known:
                out.append(f"line {node.lineno}: .open() {_UNKNOWN_MODE}")
                continue
        out.append(f"line {node.lineno}: .{node.func.attr}() -- {_TEXT_METHODS[node.func.attr]}")
    return sorted(out)


def _nonascii_literals(node: ast.AST) -> list:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and any(ord(c) > 127 for c in n.value)]


def fixture_violations(path: Path) -> list:
    """Encoding-less text IO in a test whose content is not ASCII: the harness half of #3."""
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in ("read_text", "write_text"):
            continue
        if _has_keyword(node, "encoding"):
            continue
        found = [s for arg in node.args for s in _nonascii_literals(arg)]
        if found:
            out.append(f"line {node.lineno}: .{node.func.attr}() with non-ASCII content and no encoding -- "
                       f"written with the locale's codec, read back as UTF-8. Content: {found[0][:60]!r}")
    return sorted(out)


# Reads with the locale default *on purpose*, to measure what the default does; keyed by file and function.
_LOCALE_DEFAULT_BY_DESIGN = {
    "test_source_form.py": {
        "_force_default_encoding": "probes whether the ambient default could be forced at all; encoding= here would make the probe measure nothing",
    },
    "test_persistence.py": {
        "test_an_artifact_round_trips_non_ascii_content": "the bare read performs the defect under a forced ASCII default so `raises` can catch it",
    },
}


def _force_default_encoding(monkeypatch, tmp_path: Path, encoding: str) -> bool:
    """Force what an encoding-less `open()` falls back to and report whether the force took (3.9 only)."""
    for module_name, attr in (("_bootlocale", "getpreferredencoding"), ("locale", "getencoding"), ("locale", "getpreferredencoding")):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        monkeypatch.setattr(module, attr, lambda *args, **kwargs: encoding, raising=False)
    probe = tmp_path / "_probe_encoding.py"
    probe.write_bytes(b"# \xe2\x80\x94\n")
    try:
        probe.read_text()
    except UnicodeDecodeError:
        return True
    return False


def _enclosing_function_names(tree: ast.Module) -> dict:
    out: dict = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                out.setdefault(id(node), fn.name)
    return out


def read_violations_in_test(path: Path) -> list:
    """Encoding-less reads in a test, minus the ones deliberately measuring the default."""
    tree = _parse(path)
    try:
        key = path.relative_to(REPO_ROOT / "tests").as_posix()
    except ValueError:
        key = ""
    exempt = _LOCALE_DEFAULT_BY_DESIGN.get(key, {})
    enclosing = _enclosing_function_names(tree)
    out: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "read_text":
            continue
        if _has_keyword(node, "encoding"):
            continue
        if enclosing.get(id(node)) in exempt:
            continue
        out.append(f"line {node.lineno}: .read_text() with no encoding -- the file may hold characters the locale's codec cannot decode")
    return sorted(out)


@pytest.mark.parametrize("relative", sorted(SCAN_ROOTS))
def test_the_guard_scans_the_real_trees(relative):
    """Name what was scanned: everything below is a negative assertion over this set."""
    root = REPO_ROOT / relative
    names = sorted(p.relative_to(root).as_posix() for p in encoding_scan(root))
    missing = [a for a in SCAN_ANCHORS[relative] if a not in names]
    assert not missing, f"the encoding guard scanned {root} and did not find {missing}; scanned: {names}"


def test_every_text_read_declares_its_encoding():
    """#11: every text read/write in the shipped trees names its codec, as a class rather than an instance list."""
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in encoding_scan(REPO_ROOT / relative):
            found = encoding_violations(path)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, "every text read and write must pass encoding= explicitly (#11). Offenders: " + repr(offenders)


def test_every_read_in_the_suite_declares_its_encoding():
    """The 30th bare-read site was in the one directory the guard did not walk: tests/."""
    offenders: dict = {}
    for path in encoding_scan(REPO_ROOT / "tests"):
        found = read_violations_in_test(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, "a test reading text without its codec passes on Linux and fails on Windows: " + repr(offenders)


def test_no_test_fixture_writes_non_ascii_with_the_locale_codec():
    """The harness half of #3, kept honest by the same walk that keeps the product honest."""
    offenders: dict = {}
    for path in encoding_scan(REPO_ROOT / "tests"):
        found = fixture_violations(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, "a test fixture carrying non-ASCII text must name its codec: " + repr(offenders)


_ENCODING_VIOLATIONS = {
    "bare_read.py": "from pathlib import Path\n\ndef load(p: Path) -> str:\n    return p.read_text()\n",
    "bare_write.py": "from pathlib import Path\n\ndef save(p: Path, text: str) -> None:\n    p.write_text(text)\n",
    "bare_builtin_open.py": "def load(name):\n    with open(name) as f:\n        return f.read()\n",
    "builtin_open_explicit_text_mode.py": 'def load(name):\n    with open(name, "r") as f:\n        return f.read()\n',
    "path_open_text.py": "from pathlib import Path\n\ndef load(p: Path) -> str:\n    with p.open() as f:\n        return f.read()\n",
    "open_with_computed_mode.py": "def load(name, mode):\n    with open(name, mode) as f:\n        return f.read()\n",
    "chained_read.py": 'import json\nfrom pathlib import Path\n\ndef load(p: Path) -> dict:\n    return json.loads((p / "session.json").read_text())\n',
    "buried/deeper.py": "from pathlib import Path\n\ndef load(p: Path) -> str:\n    return p.read_text()\n",
}
_FIXTURE_CASES = {
    "must_fire.py": 'from pathlib import Path\n\ndef fixture(root: Path) -> None:\n    (root / "card.md").write_text("ACME \\u2014 the context")\n',
    "must_not_fire_ascii.py": 'from pathlib import Path\n\ndef fixture(root: Path) -> None:\n    (root / "card.md").write_text("ACME - the context")\n',
    "must_not_fire_explicit.py": 'from pathlib import Path\n\ndef fixture(root: Path) -> None:\n    (root / "card.md").write_text("ACME \\u2014 the context", encoding="utf-8")\n',
}


def test_the_guard_sees_each_way_of_writing_the_violation(tmp_path):
    """MUST-FIRE for the three detectors: one fixture per shape, the buried one proving the walk is recursive."""
    root = tmp_path / "src"
    _write_tree(root, _ENCODING_VIOLATIONS)
    missed = [p.relative_to(root).as_posix() for p in encoding_scan(root) if not encoding_violations(p)]
    assert not missed, f"the encoding guard is blind to these: {missed}"
    assert len(read_violations_in_test(root / "bare_read.py")) == 1
    assert read_violations_in_test(root / "bare_write.py") == []
    fixtures = tmp_path / "tests"
    _write_tree(fixtures, _FIXTURE_CASES)
    verdicts = {p.name: bool(fixture_violations(p)) for p in encoding_scan(fixtures)}
    assert verdicts == {"must_fire.py": True, "must_not_fire_ascii.py": False, "must_not_fire_explicit.py": False}, verdicts

# ---- a keyword the floor does not have (#464, #469); pyright cannot see it, typeshed does not version-gate it ----
_KEYWORDS_YOUNGER_THAN_THE_FLOOR = {("write_text", "newline"): (3, 10), ("read_text", "newline"): (3, 13)}


def _declared_floor() -> tuple:
    """The `requires-python` floor, read out of pyproject rather than restated here."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*"[><=~^ ]*(\d+)\.(\d+)', text, re.MULTILINE)
    assert m, "could not read requires-python out of pyproject.toml -- a floor this guard cannot read is not one it may guess at"
    return (int(m.group(1)), int(m.group(2)))


def floor_violations(path: Path, floor: tuple) -> list:
    """Every text call in `path` passing a keyword the declared floor's interpreter does not accept."""
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EXEMPT_RECEIVERS:
            continue
        for kw in node.keywords:
            if kw.arg is None:
                continue
            added = _KEYWORDS_YOUNGER_THAN_THE_FLOOR.get((node.func.attr, kw.arg))
            if added is not None and added > floor:
                out.append(f"line {node.lineno}: .{node.func.attr}({kw.arg}=...) -- reached {node.func.attr} in Python "
                           f"{added[0]}.{added[1]}; the floor is {floor[0]}.{floor[1]}. Write it through .open(..., {kw.arg}=...) instead.")
    return sorted(out)


def test_no_text_call_passes_a_keyword_the_declared_floor_rejects():
    """#469: one keyword on the one function every persistence path goes through cost 518 failures per 3.9 leg."""
    floor = _declared_floor()
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in encoding_scan(REPO_ROOT / relative):
            found = floor_violations(path, floor)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, f"a text call passes a keyword the declared {floor[0]}.{floor[1]} floor rejects: " + repr(offenders)


def test_the_floor_guard_fires_on_the_exact_call_that_shipped_red(tmp_path):
    """MUST-FIRE, written as the line #469 shipped; the `.open()` fix and a caught-up floor must both pass."""
    root = tmp_path / "src"
    _write_tree(root, {"as_shipped.py": 'from pathlib import Path\n\ndef write(tmp: Path, content: str) -> None:\n    tmp.write_text(content, encoding="utf-8", newline="")\n',
                       "fixed.py": 'from pathlib import Path\n\ndef write(tmp: Path, content: str) -> None:\n    with tmp.open("w", encoding="utf-8", newline="") as fh:\n        fh.write(content)\n'})
    found = floor_violations(root / "as_shipped.py", (3, 9))
    assert len(found) == 1 and "write_text(newline=...)" in found[0], found
    assert floor_violations(root / "fixed.py", (3, 9)) == []
    assert floor_violations(root / "as_shipped.py", (3, 10)) == []


def test_the_floor_is_read_from_pyproject_and_matches_what_ci_runs():
    """The lever control: a floor CI does not run guards a version nothing observes."""
    floor = _declared_floor()
    matrix = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'"{floor[0]}.{floor[1]}"' in matrix or f"'{floor[0]}.{floor[1]}'" in matrix, (
        f"pyproject declares a {floor[0]}.{floor[1]} floor and no CI leg runs it")

# ---- the runtime half (#29): only a subprocess reaches the console encoder ----


def _clean_env(extra: dict) -> dict:
    env = dict(os.environ)
    for name in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONWARNDEFAULTENCODING"):
        env.pop(name, None)  # an inherited value would override the levers below silently
    env.update(extra)
    return env


def _cli(args: list, extra_env: dict, cwd: Path, python_args: list = None):
    env = _clean_env(extra_env)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run([sys.executable, *(python_args or []), "-m", "requivo", *args],
                          cwd=str(cwd), env=env, capture_output=True, timeout=300)


_ASCII_CONSOLE = {"PYTHONIOENCODING": "ascii"}
_CHECK_MARK = chr(0x2705)  # named by codepoint so this file stays pure ASCII
_WARNING_SIGN = chr(0x26A0)


def test_the_ascii_console_lever_actually_bites():
    """The lever control for the console tests below: measured, not assumed."""
    probe = subprocess.run([sys.executable, "-c", "print(chr(0x2705))"], env=_clean_env(_ASCII_CONSOLE),
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode != 0, "PYTHONIOENCODING=ascii did not take; the console controls below cannot fire"
    assert "UnicodeEncodeError" in probe.stderr, probe.stderr


@pytest.mark.parametrize("verb", [["doctor"], ["schema"], ["demo"]])
def test_the_cli_survives_a_console_that_cannot_encode_its_glyphs(verb, tmp_path):
    """#29: a verb must not die in its own renderer after the work it reports is done."""
    r = _cli(verb, _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, f"`requivo {' '.join(verb)}` died in its own renderer: {detail}"
    assert r.returncode == 0, detail
    assert r.stdout.strip(), "the command exited 0 and printed nothing at all"


def test_the_console_chokepoint_degrades_rather_than_dropping_the_glyph(tmp_path):
    """An escape is ugly and honest; a hole where a glyph was is neither."""
    r = _cli(["doctor"], _ASCII_CONSOLE, tmp_path)
    assert r.returncode == 0, r.stderr.decode("ascii", "backslashreplace")
    text = r.stdout.decode("ascii", "strict")
    escaped = [g.encode("ascii", "backslashreplace").decode("ascii") for g in (_CHECK_MARK, _WARNING_SIGN)]
    assert any(e in text for e in escaped), "the glyphs were dropped rather than escaped. Expected one of " + repr(escaped) + " in: " + text


def _seed_a_usage_bearing_session(tmp_path, monkeypatch, *, priced_as_of):
    """A session with revisions carrying token/rate provenance (#292); `priced_as_of=None` leaves it unpriced."""
    from _fakes import out as build_model
    from _fakes import slot

    from requivo.core import persistence as store

    monkeypatch.chdir(tmp_path)
    slug = "usage-bearing"
    store.create_session(slug, "a leave approval system")
    engine_output = build_model({"problem": slot(80, "explicit", "high")})
    if priced_as_of is None:
        store.save_revision(slug, engine_output, provenance={
            "usage_input_tokens": 1000, "usage_output_tokens": 200,
            "usage_cache_read_tokens": 0, "usage_cache_write_tokens": 0,
        })
    else:
        for i, date in enumerate(priced_as_of):
            tokens = ({"usage_input_tokens": 1000, "usage_output_tokens": 200} if i == 0
                      else {"usage_input_tokens": 500, "usage_output_tokens": 100})
            store.save_revision(slug, engine_output, provenance={
                **tokens, "usage_rate_per_mtok": [2.0, 10.0], "usage_priced_as_of": date,
            })
    return slug


@pytest.mark.parametrize("scenario,priced_as_of,glyph", [
    ("unpriced", None, "—"),
    ("multi_dated", ["2026-01-01", "2026-06-01"], "·"),
])
def test_requivo_status_survives_a_console_that_cannot_encode_the_session_cost_line(scenario, priced_as_of, glyph, tmp_path, monkeypatch):
    """#292: the SESSION COST line's em dash and middle dot ride the same chokepoint, and the escaped glyph must appear."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=priced_as_of)
    r = _cli(["status", slug], _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, f"`requivo status` died rendering the SESSION COST line ({scenario}): {detail}"
    assert r.returncode == 0, detail
    text = r.stdout.decode("ascii", "strict")
    assert "SESSION COST" in text, f"render_session_cost never ran for the {scenario} scenario. stdout: {text}"
    escaped = glyph.encode("ascii", "backslashreplace").decode("ascii")
    assert escaped in text, f"the {scenario} glyph was dropped rather than escaped. Expected {escaped!r} in: {text}"


def test_render_session_cost_alone_raises_on_a_stream_nothing_can_fix(tmp_path, monkeypatch):
    """MUST-FIRE for the test above: unguarded, the em dash really does raise."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=None)
    from requivo.render.terminal import render_session_cost
    from requivo.services.sessions import SessionService

    revisions = SessionService().repo.read_meta(slug).revisions
    monkeypatch.setattr(sys, "stdout", _unconfigurable_stdout())
    with pytest.raises(UnicodeEncodeError):
        render_session_cost(revisions)

# ---- the chokepoint's own three states ----


def _wrapper(encoding: str, errors: str):
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def test_describe_stream_separates_safe_from_will_crash_from_unknown():
    """Three answers, not two: `will_crash` and `unknown` are different findings with different remedies."""
    safe = streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")
    assert safe["state"] == "safe" and safe["detail"] is None
    crashy = streams.describe_stream(_wrapper("ascii", "strict"), "stdout")
    assert crashy["state"] == "will_crash"
    assert "UnicodeEncodeError" in crashy["detail"], crashy
    blind = streams.describe_stream(io.BytesIO(), "stdout")
    assert blind["state"] == "unknown"
    assert "cannot look" in blind["detail"], blind
    assert streams.describe_stream(None, "stdout")["state"] == "unknown"


def test_the_published_stream_states_are_all_underscore_spelled():
    """#88: `output.streams[].state` is a published --json enum; `will-crash` was the one hyphen."""
    observed = {
        streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "replace"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "ignore"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "strict"), "stdout")["state"],
        streams.describe_stream(io.BytesIO(), "stdout")["state"],
    }
    assert observed == {"safe", "lossy", "will_crash", "unknown"}, observed
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", s) for s in observed), observed


def test_configure_stream_reports_a_stream_it_could_not_reach():
    """An unconfigurable stream is the one that can still kill the process, so it must be nameable."""
    reached = streams.configure_stream(_wrapper("ascii", "strict"), "stdout")
    assert reached["state"] in ("configured", "unchanged"), reached
    assert reached["errors"] == streams.ERRORS
    unreachable = streams.configure_stream(io.BytesIO(), "stdout")
    assert unreachable["state"] == "could-not"
    assert "reconfigure" in unreachable["reason"], unreachable
    closed = _wrapper("utf-8", "strict")
    closed.close()
    assert streams.configure_stream(closed, "stdout")["state"] == "could-not"


def test_configure_stream_does_not_overrule_an_operator_who_named_a_codec(monkeypatch):
    """`PYTHONIOENCODING` is the operator's decision; only the no-crash guarantee is applied over it."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    stream = _wrapper("ascii", "strict")
    report = streams.configure_stream(stream, "stdout")
    assert stream.encoding == "ascii", "the operator's codec was overruled"
    assert stream.errors == streams.ERRORS, "the no-crash guarantee was not applied"
    assert report["state"] == "unchanged" and "PYTHONIOENCODING" in report["reason"]


def test_safe_write_never_raises_on_a_character_it_cannot_encode():
    """An error report that dies on its own em dash is the whole failure this file exists to fix."""
    stream = _wrapper("ascii", "strict")
    streams.safe_write(stream, "verdict " + _CHECK_MARK + " done")
    stream.seek(0)
    written = stream.buffer.getvalue().decode("ascii")
    assert "verdict" in written and "done" in written, written
    assert _CHECK_MARK.encode("ascii", "backslashreplace").decode("ascii") in written, written


def test_safe_write_gives_up_quietly_on_a_stream_that_is_gone():
    closed = _wrapper("utf-8", "strict")
    closed.close()
    streams.safe_write(closed, "anything")  # must not raise


class _Unconfigurable(io.TextIOWrapper):
    """A real `TextIOWrapper` whose `reconfigure` refuses, so it stays strict/ascii."""

    def reconfigure(self, **kwargs):
        raise ValueError("underlying buffer has been detached")


def _unconfigurable_stdout():
    return _Unconfigurable(io.BytesIO(), encoding="ascii", errors="strict", write_through=True)


def test_configure_streams_reports_a_stream_it_could_not_fix(monkeypatch):
    """The precondition for everything below: this stream really is one Requivo cannot save."""
    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    report = {r["stream"]: r for r in streams.configure_streams()}["stdout"]
    assert report["state"] == "could-not", report
    assert streams.describe_stream(stream, "stdout")["state"] == "will_crash"
    with pytest.raises(UnicodeEncodeError):
        stream.write(_CHECK_MARK)


def _run_app_on_an_unconfigurable_stdout(monkeypatch, argv, ledger_calls=()):
    """Drive `cli.app()` with a stdout that cannot be made safe; return (exit code, stderr)."""
    out, err = _unconfigurable_stdout(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    class _Ledger:
        calls = list(ledger_calls)

    monkeypatch.setattr(cli, "track_usage", lambda: contextlib.nullcontext(_Ledger()))
    monkeypatch.setattr(cli_support, "render_usage", lambda ledger: None)
    with pytest.raises(SystemExit) as ei:
        cli.app(argv)
    return ei.value.code, err.getvalue()


def test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback(monkeypatch, tmp_path):
    """#29: the work is done by the time anything prints, so a traceback here reports a failure that did not happen."""
    monkeypatch.chdir(tmp_path)
    code, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"])
    assert code == cli.EXIT_RENDER_FAILED == 3, (code, err)
    assert "could not encode its output" in err, err
    assert "Traceback" not in err, err
    assert "requivo doctor" in err, "the message does not say how to find out which stream: " + err


def test_the_render_failure_message_does_not_claim_a_call_was_billed_when_none_was(monkeypatch, tmp_path):
    """The arm reads the usage ledger rather than assuming a change was applied."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=())
    assert "No provider call was made" in err, err
    assert "HAS completed and been billed" not in err, "`doctor` makes no provider call: " + err


def test_the_render_failure_message_does_say_so_when_a_call_was_billed(monkeypatch, tmp_path):
    """The must-fire half: the warning that matters is on the verbs that cost money."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=("one call",))
    assert "HAS completed and been billed" in err, err
    assert "Do not re-run" in err, err


def test_the_usage_line_cannot_kill_a_run_that_already_paid_for_its_call(monkeypatch, tmp_path):
    """`render_usage` runs after a wholly successful command, so it must degrade rather than raise (#167)."""
    from requivo.providers.anthropic.pricing import price_call
    from requivo.usage import CallRecord, UsageLedger

    ledger = UsageLedger()
    ledger.record(price_call(CallRecord(model="claude-sonnet-5", input_tokens=10, output_tokens=20,
                                        cache_read_tokens=0, cache_write_tokens=0, latency_ms=5)))
    monkeypatch.setattr(sys, "stdout", _unconfigurable_stdout())
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(UnicodeEncodeError):
        cli_support.render_usage(ledger)  # the control: unwrapped, this raises
    cli._render_usage_safely(ledger)
    assert "could not be encoded" in err.getvalue(), "the usage line vanished without a word: " + repr(err.getvalue())

# ---- a file the *user* named: the one read whose bytes this project did not write ----


def test_a_user_file_that_is_not_utf8_is_refused_by_name_not_by_traceback(tmp_path):
    """Mojibake validates, so the refusal is right; it has to name the byte rather than trace back."""
    brief = tmp_path / "brief.md"
    brief.write_bytes("Système de validation des congés.".encode("cp1252"))
    with pytest.raises(InvalidModelError) as ei:
        read_user_text(brief)
    message = str(ei.value)
    assert "not valid UTF-8" in message, message
    assert "0xe8" in message, f"the offending byte is not named: {message}"
    assert ei.value.details["path"] == str(brief)
    assert ei.value.details["expected_encoding"] == "utf-8"
    assert isinstance(ei.value.details["position"], int)


def test_a_user_file_that_is_utf8_is_read_unchanged(tmp_path):
    brief = tmp_path / "brief.md"
    original = "Système de validation des congés — 5 000 salariés."
    brief.write_bytes(original.encode("utf-8"))
    assert read_user_text(brief) == original


def test_the_refusal_does_not_let_a_path_forge_a_line_of_output(tmp_path):
    """A user-supplied path carrying a newline must not forge a line of Requivo's own output (#40)."""
    sneaky = tmp_path / "brief\nERROR: session verified OK.md"
    try:
        sneaky.write_bytes(b"\xe8")
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a newline in a filename; the forging path is untested here")
    with pytest.raises(InvalidModelError) as ei:
        read_user_text(sneaky)
    body = str(ei.value)
    assert not any(line.startswith("ERROR:") for line in body.splitlines()), "a path forged a line at column 0: " + repr(body)


_WARN_DEFAULT_ENCODING = {"PYTHONWARNDEFAULTENCODING": "1"}
_ERROR_ON_DEFAULT_ENCODING = ["-W", "error::EncodingWarning"]
_NO_LEVER_ON_39 = ("EncodingWarning and PYTHONWARNDEFAULTENCODING are 3.10+, so this lever cannot fire on 3.9. UNTESTED ON THIS "
                   "INTERPRETER: that the CLI reads its bundled assets with an explicit codec. The static guard covers the same claim.")


def test_the_default_encoding_lever_actually_bites(tmp_path):
    """The lever control for the read-side test below."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent("""
        import sys
        from pathlib import Path

        Path(sys.argv[1]).read_text()
    """), encoding="utf-8")
    target = tmp_path / "data.txt"
    target.write_text("plain", encoding="utf-8")
    r = subprocess.run([sys.executable, *_ERROR_ON_DEFAULT_ENCODING, str(probe), str(target)],
                       env=_clean_env(_WARN_DEFAULT_ENCODING), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "EncodingWarning" in r.stderr, "the default-encoding lever did not take: " + r.stderr


@pytest.mark.parametrize("verb", [["schema"], ["schema", "--framework"], ["context"], ["demo"], ["doctor"]])
def test_the_cli_reads_its_assets_with_an_explicit_encoding(verb, tmp_path):
    """#11 at runtime: `-W error::EncodingWarning` fires on a locale-default read that happens to succeed today."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    r = _cli(verb, {**_WARN_DEFAULT_ENCODING, **_ASCII_CONSOLE}, tmp_path, python_args=_ERROR_ON_DEFAULT_ENCODING)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"EncodingWarning" not in r.stderr, f"`requivo {' '.join(verb)}` read text with the locale's codec: {detail}"
    assert r.returncode == 0, detail

# ---- SECTION 3: references. A cited test name or decision slug has to resolve (#75, #384; ----
# ---- `decision: the-tree-records-the-rule` lets a citation name a test file as well as a function). ----

TESTS = REPO_ROOT / "tests"
REFERENCE_ROOTS = (SRC, REPO_ROOT / "scripts", REPO_ROOT / "docs", TESTS)
REFERENCE_EXTRA = (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "CONTRIBUTING.md")
REFERENCE_SUFFIXES = (".py", ".md", ".html", ".js")
DECISIONS = REPO_ROOT / "docs" / "decisions"
_REFERENCE = re.compile(r"\btest_[a-z0-9_]{10,}\b")  # the floor keeps `test_x` in a snippet from reading as a claim
_DECISION_REF = re.compile(r"`decision:\s*([a-z0-9-]+)`")


def reference_subjects() -> list[Path]:
    """Every file whose citations must resolve; a vendored bundle's identifiers are somebody else's (#504)."""
    found = list_files(REFERENCE_ROOTS, suffixes=REFERENCE_SUFFIXES, label="the reference guard", extra=REFERENCE_EXTRA)
    return [p for p in found if "vendor" not in p.parts]


def declared_test_names(roots: tuple[Path, ...] = (TESTS, SRC / "testing")) -> set[str]:
    """Every test function and every test module's stem: both forms of citation resolve."""
    names: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else ():
            names.add(path.stem)
            names.update(re.findall(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)", path.read_text(encoding="utf-8"), re.MULTILINE))
    if not names:
        raise AssertionError(f"the reference guard found no tests under {roots} to resolve against")
    return names


def declared_slugs(root: Path = DECISIONS) -> set[str]:
    """Every slug a decision record declares on its `**Slug:**` line; a record with none is a finding."""
    slugs: set[str] = set()
    for path in sorted(root.glob("*.md")):
        if path.name == "README.md":
            continue
        m = re.search(r"^\*\*Slug:\*\*\s*`([a-z0-9-]+)`", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not m:
            raise AssertionError(f"{path.name} declares no `**Slug:**` line, so nothing can reference it by name")
        slugs.add(m.group(1))
    return slugs


def dangling_references(subjects: list[Path], names: set[str], slugs: set[str]) -> list[str]:
    out = []
    for path in subjects:
        text = path.read_text(encoding="utf-8")
        out.extend(f"{path.name} -> {n}" for n in sorted(set(_REFERENCE.findall(text)) - names))
        out.extend(f"{path.name} -> decision: {s}" for s in sorted(set(_DECISION_REF.findall(text)) - slugs))
    return out


def test_every_named_test_reference_resolves():
    """A citation names a test function or a `tests/test_<name>.py` file; a `decision:` names a record's slug."""
    subjects = reference_subjects()
    assert len(subjects) > 20 and any(p.name == "CLAUDE.md" for p in subjects), "the scan is not seeing the tree"
    dangling = dangling_references(subjects, declared_test_names(), declared_slugs())
    assert not dangling, "these references name nothing:\n  " + "\n  ".join(dangling) + "\nRename the citation or delete it."


def test_the_reference_guard_sees_a_dangling_name_and_a_dangling_slug(tmp_path):
    """MUST-FIRE: a name and a slug that resolve nowhere are both reported; a file citation resolves by stem."""
    # Every name is assembled at runtime so this file's own literals cite nothing.
    file_stem, function, slug = "test_" + "declared_subject", "test_" + "the_declared_function", "declared" + "-slug"
    dangling_name, dangling_slug = "test_" + "not_declared_anywhere", "no-such-" + "record"
    tests = tmp_path / "tests"
    _write_tree(tests, {f"{file_stem}.py": f"def {function}():\n    pass\n"})
    records = tmp_path / "decisions"
    _write_tree(records, {"0001-a.md": f"**Slug:** `{slug}`\n"})
    doc = tmp_path / "note.md"
    doc.write_text(f"`{file_stem}.py`, `{function}`, `decision: {slug}`, `{dangling_name}` and "
                   f"`decision: {dangling_slug}`.\n", encoding="utf-8")
    found = dangling_references([doc], declared_test_names((tests,)), declared_slugs(records))
    assert found == [f"note.md -> {dangling_name}", f"note.md -> decision: {dangling_slug}"], found

# ---- SECTION 4: the lean ratchet (#553). `tests/lean_budget.toml` holds ceilings that only go down; ----
# ---- every breach is reported at once. ----

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import prose_measure  # noqa: E402

LEAN_BUDGET_TOML = REPO_ROOT / "tests" / "lean_budget.toml"

# The meta-guard estate: the tests that guard the repo's form rather than its runtime. Membership is a test diff.
ESTATE_FILES = (
    "tests/test_source_form.py",
    "tests/test_version_sites.py",
    "tests/test_cli_flag_names.py",
    "tests/test_dco_check.py",
    "tests/test_prompt_contracts.py",
    "tests/test_workflow_untrusted_output.py",
    "tests/_scan.py",
)


def _load_budget(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - taken on 3.9/3.10, not on the version CI lints
        import tomli as tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _largest_non_estate_module(tests):
    """The fattest ordinary test module; the estate has its own `[estate]` ceiling."""
    estate_paths = {REPO_ROOT / rel for rel in ESTATE_FILES}
    return max((f for f in tests.files if f.path not in estate_paths), key=lambda f: f.total, default=None)


def _lean_budget_breaches(budget: dict) -> list[str]:
    """Every ceiling `budget` names that the real tree currently exceeds, one string per breach."""
    breaches = []
    src = prose_measure.measure_group(prose_measure.GROUPS["src"])
    tests = prose_measure.measure_group(prose_measure.GROUPS["tests"])

    if src.prose_share > budget["src"]["prose_share_max"]:
        breaches.append(f"src/ prose share is {src.prose_share:.1%}, over the {budget['src']['prose_share_max']:.1%} ceiling")
    largest_src = src.largest
    if largest_src is not None and largest_src.total > budget["src"]["largest_module_max_lines"]:
        breaches.append(f"{largest_src.path.relative_to(REPO_ROOT)} is {largest_src.total} lines, over the "
                        f"{budget['src']['largest_module_max_lines']} ceiling")
    ratio = prose_measure.code_ratio(tests, src)
    if ratio > budget["tests"]["ratio_max"]:
        breaches.append(f"tests:src code ratio is {ratio:.2f}x, over the {budget['tests']['ratio_max']:.2f}x ceiling")
    largest_tests = _largest_non_estate_module(tests)
    if largest_tests is not None and largest_tests.total > budget["tests"]["largest_module_max_lines"]:
        breaches.append(f"{largest_tests.path.relative_to(REPO_ROOT)} is {largest_tests.total} lines (excluding the meta-guard "
                        f"estate, which has its own [estate] ceiling), over the {budget['tests']['largest_module_max_lines']} ceiling")
    mod_len, mod_where = tests.docstring_max("module")
    if mod_len > budget["tests"]["module_docstring_max_lines"]:
        breaches.append(f"{mod_where}'s module docstring is {mod_len} lines, over the {budget['tests']['module_docstring_max_lines']} ceiling")
    fn_len, fn_where = tests.docstring_max("function")
    if fn_len > budget["tests"]["function_docstring_max_lines"]:
        breaches.append(f"{fn_where}'s docstring is {fn_len} lines, over the {budget['tests']['function_docstring_max_lines']} ceiling")
    estate_total = sum(prose_measure.line_count(REPO_ROOT / rel) for rel in ESTATE_FILES)
    if estate_total > budget["estate"]["total_max_lines"]:
        breaches.append(f"the meta-guard estate is {estate_total} lines, over the {budget['estate']['total_max_lines']} ceiling")
    compat_lines = prose_measure.line_count(prose_measure.COMPATIBILITY_DOC)
    if compat_lines > budget["docs"]["compatibility_max_lines"]:
        breaches.append(f"docs/compatibility.md is {compat_lines} lines, over the {budget['docs']['compatibility_max_lines']} ceiling")
    src_mod_len, src_mod_where = src.docstring_max("module")
    if src_mod_len > budget["src"]["module_docstring_max_lines"]:
        breaches.append(f"{src_mod_where}'s module docstring is {src_mod_len} lines, over the {budget['src']['module_docstring_max_lines']} ceiling")
    src_fn_len, src_fn_where = src.docstring_max("function")
    if src_fn_len > budget["src"]["function_docstring_max_lines"]:
        breaches.append(f"{src_fn_where}'s docstring is {src_fn_len} lines, over the {budget['src']['function_docstring_max_lines']} ceiling")
    records = sorted(REPO_ROOT.glob("docs/decisions/0*.md"))
    if records:
        longest = max(records, key=prose_measure.line_count)
        longest_lines = prose_measure.line_count(longest)
        if longest_lines > budget["docs"]["decision_record_max_lines"]:
            breaches.append(f"{longest.relative_to(REPO_ROOT).as_posix()} is {longest_lines} lines, over the "
                            f"{budget['docs']['decision_record_max_lines']} ceiling")
    claude_lines = prose_measure.line_count(REPO_ROOT / "CLAUDE.md")
    if claude_lines > budget["claude_md"]["max_lines"]:
        breaches.append(f"CLAUDE.md is {claude_lines} lines, over the {budget['claude_md']['max_lines']} ceiling")
    return breaches


def test_the_tree_stays_within_its_lean_budget():
    """#553: every ceiling in `tests/lean_budget.toml`, re-measured against the real tree, every breach at once."""
    breaches = _lean_budget_breaches(_load_budget(LEAN_BUDGET_TOML))
    assert not breaches, "the lean budget was exceeded:\n" + "\n".join(f"  - {b}" for b in breaches)


def test_the_lean_budget_guard_fires_on_a_scratch_copy_and_names_every_breach(tmp_path):
    """MUST-FIRE: every numeric ceiling zeroed in a scratch copy is caught and named, not only the first."""
    text = LEAN_BUDGET_TOML.read_text(encoding="utf-8")
    zeroed, count = re.subn(r"(?m)^(\w[\w.]*\s*=\s*)[0-9][0-9.]*\s*$", r"\g<1>0", text)
    assert count == 12, f"expected 12 numeric ceilings in the real TOML, the scratch edit zeroed {count}"
    scratch = tmp_path / "lean_budget.toml"
    scratch.write_text(zeroed, encoding="utf-8")
    breaches = _lean_budget_breaches(_load_budget(scratch))
    assert len(breaches) == 12, breaches
    joined = "\n".join(breaches)
    # The largest module names are measured, not hardcoded: a split must not turn a passing guard red.
    src = prose_measure.measure_group(prose_measure.GROUPS["src"])
    tests = prose_measure.measure_group(prose_measure.GROUPS["tests"])
    largest_src_file, largest_tests_file = src.largest, _largest_non_estate_module(tests)
    assert largest_src_file is not None and largest_tests_file is not None
    for expected in ("src/", str(largest_src_file.path.relative_to(REPO_ROOT)), "code ratio",
                     str(largest_tests_file.path.relative_to(REPO_ROOT)), "docstring", "meta-guard estate",
                     "compatibility.md", "docs/decisions/", "CLAUDE.md"):
        assert expected in joined, f"a zeroed ceiling should have named {expected!r}: {joined}"
