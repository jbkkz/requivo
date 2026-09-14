"""Source-form guards, merged (#551): the tiers that already shared `_scan.py` (#288) still carried three copies of the scaffolding around it. One file, three sections, one shared #10 control.
1. **Boundaries** (#10, #77, #76, #167, #183, #355, #422, #425) -- core stays provider- and process-free, and a surface reaches the provider or the store only through the seam named for it.
2. **Encoding** (#11, #29, #464, #469, #298) -- every text read/write names its codec, and a console that cannot encode a glyph degrades rather than crashing after the work it reports is done.
3. **Narrative references** (#75, #137, #156, #190, #286, #384, #504) -- a named test or decision record has to resolve and be findable by grep, not merely look like one.
`tests/test_boundaries.py`, `tests/test_encoding.py` and `tests/test_narrative_references.py` all stay on disk as near-empty stubs (see each) so the places outside this file -- `src/`, `CLAUDE.md`, `docs/` -- that cite any of the three by bare module name keep resolving without a src/ diff. `test_encoding`'s own name sits one character under this guard's ten-character reference floor, which let thirteen such citations (five under `src/`) go unnoticed until caught by inspection rather than by the guard itself -- a real gap in the floor, not fixed here; see the PR body.
"""
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
from collections.abc import Iterable
from pathlib import Path

import pytest
from _scan import list_files, list_python_files, parse_utf8, write_tree

from requivo import cli, cli_support, streams
from requivo.core.errors import InvalidModelError
from requivo.deterministic import read_user_text

REPO_ROOT = Path(__file__).resolve().parents[1]

# ════════════════════════════════════════════════════════════════════════════════════════════════
# Shared scaffold. #10: an empty or missing scan root is "could not look", never "looked and found
# nothing" -- _scan.py's own refusal, exercised once here rather than once per section as it was
# through each section's own scan()/list_files() wrapper before this merge.
# ════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_scan_helpers_refuse_a_root_they_could_not_look_at(tmp_path):
    """The positive control for #10, for both helpers every section below is built on. Each section keeps its own real-tree control (naming what it actually scanned); this is the mechanism."""
    missing, empty = tmp_path / "missing", tmp_path / "empty"
    empty.mkdir()
    for label in ("boundary guard", "the encoding guard", "the narrative-reference guard"):
        with pytest.raises(AssertionError, match="no such directory"):
            list_python_files(missing, label=label)
        with pytest.raises(AssertionError, match="no Python files"):
            list_python_files(empty, label=label)
    with pytest.raises(AssertionError, match="could not look"):
        list_files((missing,), suffixes=(".py",), label="the narrative-reference guard")
    with pytest.raises(AssertionError, match="could not look"):
        list_files((empty,), suffixes=(".py",), label="the narrative-reference guard")

_parse = parse_utf8
_write_tree = write_tree

# ════════════════════════════════════════════════════════════════════════════════════════════════
# SECTION 1 -- Boundaries: core stays provider- and process-free (invariant 7); a surface reaches
# the provider or the store only through the named service seam, never past it (#77, #76).
# ════════════════════════════════════════════════════════════════════════════════════════════════

CORE = REPO_ROOT / "src" / "requivo" / "core"
CORE_PACKAGE = "requivo.core"
# Anchor, not a full listing: adding a module to core must not fail this file; a renamed package must.
CORE_ANCHORS = ("__init__.py", "contracts.py")

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
    """The dotted package a file lives in, so its relative imports can be resolved."""
    parts = path.relative_to(root).parts[:-1]
    return ".".join((root_package, *parts))


def _resolve_relative(package: str, level: int) -> str:
    """The dotted base a relative import of depth `level` is measured from. Level 0 is absolute."""
    if level == 0:
        return ""
    parts = package.split(".")
    return ".".join(parts[: max(0, len(parts) - (level - 1))])


def scan(root: Path, package: str) -> list[tuple[Path, str]]:
    """Every Python file under `root`, paired with the dotted package its relative imports resolve against. The walk and the empty/missing-root refusal are `_scan.py`'s now (#288)."""
    found = list_python_files(root, label="boundary guard")
    return [(p, _package_for(p, root, package)) for p in found]


def imported_modules(path: Path, package: str) -> set[str]:
    """Every module `path` imports, as a dotted absolute name -- relative imports resolved against `package`, since `from .anthropic import Client` is the shortest way to write the violation."""
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            if node.module:
                names.add(f"{base}.{node.module}" if base else node.module)
            else:
                # `from . import x` / `from .. import x`: each alias is itself a module.
                names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def import_hits(modules: set[str], table: dict) -> dict:
    """The subset of `modules` whose dotted path crosses a forbidden name, mapped to the reason."""
    hits = {}
    for module in sorted(modules):
        for part in module.split("."):
            if part in table:
                hits[module] = table[part]
                break
    return hits


def process_violations(path: Path, package: str) -> list[str]:
    """Every place `path` talks to the process rather than to its caller, one string per violation."""
    out: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            # Keyed on the *call*, not the bare name: `def render(input: str)` must not read as a call.
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
                    out.append(
                        f"line {node.lineno}: from {node.module} import {alias.name} -- {_FORBIDDEN_ATTRIBUTES[key]}"
                    )
    for module, reason in sorted(import_hits(imported_modules(path, package), _TERMINAL_IMPORTS).items()):
        out.append(f"imports {module} -- {reason}")
    return sorted(out)


def _core_offenders(table: dict) -> dict:
    """Real-scan helper: forbidden imports across the actual core package, keyed by module."""
    offenders: dict = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        hits = import_hits(imported_modules(path, package), table)
        if hits:
            offenders[path.relative_to(CORE).as_posix()] = sorted(hits)
    return offenders

# ---- the scan set itself ----


def test_the_guard_scans_the_real_core_package():
    """Name what was scanned -- everything below is a negative assertion over this set."""
    scanned = scan(CORE, CORE_PACKAGE)
    names = sorted(p.relative_to(CORE).as_posix() for p, _ in scanned)
    missing = [anchor for anchor in CORE_ANCHORS if anchor not in names]
    assert not missing, (
        f"the boundary guard scanned {CORE} and did not find {missing}; it is not looking at " f"Requivo's core. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make():
    """#10's own regression: `src/product_copilot/core` is this package's *previous* name, and before #10 both boundary tests passed green against it -- `Path.glob` on a missing directory returns `[]`, and `assert not set()` holds."""
    renamed_away = REPO_ROOT / "src" / "product_copilot" / "core"
    assert not renamed_away.exists(), "this control assumes the pre-rename path is gone"
    assert list(renamed_away.glob("*.py")) == [], "the shape being guarded against: glob returns [], not an error"
    with pytest.raises(AssertionError, match="no such directory"):
        scan(renamed_away, CORE_PACKAGE)


def test_the_scan_is_recursive(tmp_path):
    """A future `core/<subpackage>/` must not be silently unscanned -- the old glob was `*.py`."""
    root = tmp_path / "core"
    _write_tree(root, {
        "__init__.py": "",
        "sub/__init__.py": "",
        "sub/buried.py": "from ...providers import anthropic\n",
    })
    found = {p.relative_to(root).as_posix(): pkg for p, pkg in scan(root, CORE_PACKAGE)}
    assert "sub/buried.py" in found, f"the scan is not recursive: {sorted(found)}"
    assert found["sub/buried.py"] == "requivo.core.sub", "a subpackage resolves its relative imports from itself"
    buried = root / "sub" / "buried.py"
    assert import_hits(imported_modules(buried, found["sub/buried.py"]), _PROVIDER_IMPORTS), (
        "a provider import buried one directory down went unflagged"
    )


def _force_default_encoding(monkeypatch, tmp_path: Path, encoding: str) -> bool:
    """Force what an encoding-less `open()` falls back to, and report whether the force took.

    There is only a Python-level hook to patch on some interpreters (`_bootlocale` was removed in 3.10; UTF-8 mode overrides it regardless), so every candidate is patched and then *measured* on a probe file. Returning False is the third state: the caller must skip, never assert, over a control that could not
    fire. Measured: takes on CPython 3.9, not on 3.10+."""
    for module_name, attr in (
        ("_bootlocale", "getpreferredencoding"),
        ("locale", "getencoding"),
        ("locale", "getpreferredencoding"),
    ):
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


def test_the_guard_reads_source_as_utf8(tmp_path, monkeypatch):
    """Every module in core carries an em dash, and `read_text()` with no encoding decodes with the *locale* codepage -- so under `LC_ALL=C` the pre-#10 guard died with `UnicodeDecodeError` before it could run. Forces the ambient default first (asserting only that `_parse` succeeds would prove nothing on a UTF-8
    locale) and skips loudly where the force cannot take."""
    if not _force_default_encoding(monkeypatch, tmp_path, "ascii"):
        pytest.skip(
            "the ambient default encoding could not be forced on this interpreter (CPython dropped " "_bootlocale in 3.10 and UTF-8 mode overrides it regardless). UNTESTED ON THIS " "INTERPRETER: that _parse passes an explicit encoding. The 3.9 CI leg does test it."
        )
    path = tmp_path / "dashes.py"
    # Raw bytes, so this source file stays pure ASCII while the fixture on disk does not.
    path.write_bytes(b'"""Core \xe2\x80\x94 the engine."""\nimport anthropic\n')  # an em dash, in utf-8
    with pytest.raises(UnicodeDecodeError):
        path.read_text()  # what the pre-#10 guard did, meeting the locale it would meet
    assert "anthropic" in imported_modules(path, CORE_PACKAGE)  # what this guard does instead

# ---- no provider, in either direction ----


def test_core_never_imports_anthropic():
    """requivo.core is provider-free by construction: no module may import the SDK, so the deterministic engine works with no API key and no `anthropic` installed."""
    offenders = _core_offenders({"anthropic": _PROVIDER_IMPORTS["anthropic"]})
    assert not offenders, f"requivo.core must not import anthropic; offenders: {offenders}"


def test_core_never_imports_a_provider():
    """Core must not import the provider package either -- the dependency arrow points provider -> core, never the reverse."""
    offenders = _core_offenders({"providers": _PROVIDER_IMPORTS["providers"]})
    assert not offenders, f"requivo.core must not import requivo.providers; offenders: {offenders}"

_IMPORT_VIOLATIONS = {
    "absolute_sdk.py": "import anthropic\n",
    "absolute_provider.py": "from requivo.providers.anthropic import AnthropicProvider\n",
    "dotted_import.py": "import requivo.providers.anthropic\n",
    "relative_sibling.py": "from .anthropic import Client\n",
    "relative_parent.py": "from ..providers import anthropic\n",
    "relative_bare.py": "from .. import providers\n",
    "relative_aliased.py": "from ..providers.anthropic import AnthropicProvider as P\n",
}


def test_the_import_guard_sees_every_way_of_writing_the_violation(tmp_path):
    """Positive control: "no offenders" also passes when the scan found nothing, so each forbidden shape gets a fixture the guard must flag -- including the three relative forms a previous version skipped outright on `node.level != 0`."""
    root = tmp_path / "core"
    _write_tree(root, _IMPORT_VIOLATIONS)
    missed = [
        path.name
        for path, package in scan(root, CORE_PACKAGE)
        if not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)
    ]
    assert not missed, f"the import guard is blind to these: {missed}"

_LEGITIMATE_IMPORTS = """
    from __future__ import annotations

    import json
    import sys
    from pathlib import Path

    from pydantic import BaseModel

    from ..paths import ASSETS
    from .contracts import SessionModel
    from . import errors
"""


def test_the_import_guard_does_not_fire_on_what_core_legitimately_imports(tmp_path):
    """The must-fire cases above only mean something next to a must-not-fire case: a detector that flags everything is as useless as one that flags nothing."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IMPORTS})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)

# ---- no argv, no standard streams, no environment, no exit ----


def test_core_never_touches_the_process():
    """The half of invariant 7 nothing enforced before #10 -- core was clean by luck, not by guard."""
    offenders = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        found = process_violations(path, package)
        if found:
            offenders[path.relative_to(CORE).as_posix()] = found
    assert not offenders, (
        "requivo.core must not talk to the process -- its arguments, its standard streams, its " f"environment or its exit; offenders: {offenders}"
    )

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
    """Positive control, one fixture per construct, so a construct that stops being detected shows up as itself rather than as a quiet narrowing of the guard."""
    root = tmp_path / "core"
    _write_tree(root, _PROCESS_VIOLATIONS)
    missed = [
        path.relative_to(root).as_posix()
        for path, package in scan(root, CORE_PACKAGE)
        if not process_violations(path, package)
    ]
    assert not missed, f"the process guard is blind to these: {missed}"

_LEGITIMATE_CORE = """
    from __future__ import annotations

    import json
    import logging
    import sys
    from pathlib import Path

    log = logging.getLogger(__name__)

    NEEDS_BACKPORT = sys.version_info < (3, 10)

    def load(path: Path) -> dict:
        # core reads and writes files by design: persistence, context, contracts and analysis all do
        return json.loads(path.read_text(encoding="utf-8"))

    def save(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")
        log.debug("wrote %s", path)

    def render(input: str) -> str:
        # a parameter that merely reads like a forbidden builtin must not trip the guard
        printed = input.strip()
        return printed
"""


def test_the_process_guard_allows_what_core_legitimately_does(tmp_path):
    """Where the line sits, pinned. A guard that fails on correct code is deleted by the next person, so file IO, `logging`, `sys.version_info` and a local named like a builtin must all pass."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_CORE})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert process_violations(path, package) == []

# ---- the other end of the same arrow: a surface reaches the provider through the services ----

CLI = REPO_ROOT / "src" / "requivo" / "cli.py"
CLI_PACKAGE = "requivo"
HTTP = REPO_ROOT / "src" / "requivo" / "http.py"
RENDER = REPO_ROOT / "src" / "requivo" / "render"
RENDER_PACKAGE = "requivo.render"

# What a surface may still pull out of `requivo.providers`, keyed by (file, name) so a global name
# list can't let a second file inherit a first file's argument. Everything else is a second
# orchestration of discovery -- #77: `cli.py` imported `run`/`advise`/`estimate` and reasoned two
# provider calls of its own. Add an entry only with a reason a reader can argue with.
_SURFACE_PROVIDER_ALLOWLIST = {
    ("cli.py", "new_client"): "builds the SDK client from the environment; DiscoveryService decides when to reason with it.",
    ("cli.py", "EngineError"): "an exception type, isinstance-caught by app()/_cmd_web; its provider_unavailable code is public --json (#135). From providers.errors since #167.",
    ("api/app.py", "EngineError"): "the identical shape one surface over (#425): create_api() raises it when the [api] extra's fastapi import fails.",
    ("web/config.py", "Anthropic"): "the SDK handle, probed in a try/except to answer one boolean -- is it installed? -- never built into a client.",
    ("web/config.py", "credential_present"): "answers is a credential visible?, via the SDK's own resolution chain since #334 (a transient client, built and discarded, #374) rather than re-deriving env-var names, which drifted at #332.",
    ("http.py", "EngineError"): "the same exception-type shape as cli.py's entry, moved out of web/app.py by #422; touches none of argv/stdout/HTTP, so it is scanned by name.",
    ("web/routes/sessions.py", "EngineError"): "caught for a routing decision the HTTP boundary can't make: session page rather than a bare 500 (#207); paired with ProviderOutputError since #253.",
    ("deterministic/doctor.py", "current_model_name"): "reads the provider's own model id for the row `requivo doctor` prints (#247); no client is built, no call is made. Drifted once already (#364, #268).",
    ("deterministic/doctor.py", "credential_diagnosis"): "same reasoning as current_model_name, for api_key_present/credential_problem (#365, the drift #332 found one function along); also builds a transient client since #334 (#374).",
}
# `track_usage` was a third entry until #167 removed it: the ledger it scopes counts calls, tokens,
# cache tiers and latency, never Anthropic's, so it moved to requivo.usage and stopped being a
# provider name. Its removal was the guard doing its job, not a relaxation.

PROVIDER_TREES = (
    (RENDER, RENDER_PACKAGE),
    (REPO_ROOT / "src" / "requivo" / "web", "requivo.web"),
    (REPO_ROOT / "src" / "requivo" / "deterministic", "requivo.deterministic"),
    (REPO_ROOT / "src" / "requivo" / "api", "requivo.api"),  # #425, the second HTTP surface
)


def provider_subjects() -> list[tuple[Path, str, str]]:
    """Every surface the provider guard watches, as (path, package, label) -- the allowlist key. `cli.py` and `http.py` are named individually (the latter is not itself a surface by this guard's own "touches argv/stdout/HTTP" test, but the one module outside the trees below that legitimately reaches a provider
    name); the rest are walked, so a module added later arrives inside the scan set rather than beside it."""
    src = REPO_ROOT / "src" / "requivo"
    subjects = [
        (subject_module(CLI), CLI_PACKAGE, "cli.py"),
        (subject_module(HTTP), CLI_PACKAGE, "http.py"),
    ]
    for root, package in PROVIDER_TREES:
        subjects.extend((p, pkg, p.relative_to(src).as_posix()) for p, pkg in scan(root, package))
    return subjects

# The marker a whole-module import contributes instead of a name -- unspellable as an allowlist key
# (it carries dots and spaces), so `import requivo.providers.anthropic` cannot launder unrestricted
# access to the module behind a three-entry table.
_WHOLE_MODULE = "(the whole module)"


def subject_module(path: Path) -> Path:
    """`scan`, for a guard whose subject is one named module. Same #10 rule: `cli.py` has been renamed along with its package once already, and a guard reading a missing file as an empty import set would have gone green straight through it."""
    if not path.is_file():
        raise AssertionError(
            f"boundary guard could not read {path}: no such file. This is 'could not look', not " f"'looked and found nothing' -- fix the path, never the assertion."
        )
    return path


def _crosses_providers(module: str) -> bool:
    """True if a dotted module name passes through `providers`, at any depth and under any prefix."""
    return "providers" in module.split(".")


def provider_names(path: Path, package: str) -> set[str]:
    """Every name `path` can reach inside `requivo.providers`. A *module* import contributes the `_WHOLE_MODULE` marker instead of a bare name, since `import requivo.providers.anthropic` leaves every function in it one attribute away. Relative imports resolved against `package`."""
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(f"{a.name} {_WHOLE_MODULE}" for a in node.names if _crosses_providers(a.name))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            module = f"{base}.{node.module}" if base and node.module else (node.module or base)
            if node.module is None:
                # `from . import providers`: each alias is itself a module, not a symbol.
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    if _crosses_providers(full):
                        names.add(f"{full} {_WHOLE_MODULE}")
            elif _crosses_providers(module):
                # `from requivo.providers import anthropic` names a submodule; anything deeper
                # (`from requivo.providers.anthropic import advise`) names a symbol.
                submodules = module.split(".")[-1] == "providers"
                for alias in node.names:
                    names.add(f"{module}.{alias.name} {_WHOLE_MODULE}" if submodules else alias.name)
    return names


def test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns():
    """#77, #167: every interface is a thin layer over the services, with no second orchestration. False on `cli.py` (imported `run`/`advise`/`estimate`) and on `render/terminal.py` (imported `PRICING_AS_OF`/`UsageLedger` to print a cost line) -- the purest view layer named a vendor. Both directions asserted: an
    allowlist entry naming an import no longer made fails too, so a surface emptied or renamed away goes red rather than reading as one that reaches nothing."""
    reached = {(label, name)
               for path, package, label in provider_subjects()
               for name in provider_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_PROVIDER_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past the service seam to {unexpected}. Route it through " f"DiscoveryService, or -- if what it needs is provider-neutral -- move that name out of " f"`providers/` (`requivo.usage`, `providers.errors`), or add it to "
        f"_SURFACE_PROVIDER_ALLOWLIST with the reason it is a surface concern. Currently allowed: " f"{sorted(_SURFACE_PROVIDER_ALLOWLIST)}"
    )
    stale = sorted(set(_SURFACE_PROVIDER_ALLOWLIST) - reached)
    assert not stale, (
        f"_SURFACE_PROVIDER_ALLOWLIST still names {stale}, which no surface imports any more. " f"Either the guard is reading the wrong files, or the entry is stale prose -- delete it."
    )


def test_the_provider_guard_names_what_it_scanned():
    """#10, for this scan set: `render/`, `web/`, `deterministic/` and `api/` are packages, and a walk that silently found nothing under one would be an all-clear over exactly the layer #167 (render/), #183 (web/, deterministic/) and #425 (api/) each found unguarded."""
    labels = sorted(label for _, _, label in provider_subjects())
    assert "cli.py" in labels
    assert "http.py" in labels, "the provider guard did not scan http.py; it scanned " + str(labels)
    for expected in ("render/terminal.py", "web/config.py", "deterministic/sessions/lifecycle.py", "api/app.py"):
        assert expected in labels, f"the provider guard did not scan {expected}; it scanned {labels}"

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
    """Positive control: each shape of the violation gets a fixture the guard must see, module forms included, since those are the ones that would otherwise look like one tidy name."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_PROVIDER_IMPORTS)
    missed = [path.name for path, package in scan(root, CLI_PACKAGE) if not provider_names(path, package)]
    assert not missed, f"the surface guard is blind to these: {missed}"


def test_a_whole_module_import_cannot_pass_as_a_named_surface_concern(tmp_path):
    """`import requivo.providers.anthropic` leaves `advise` one attribute away, so it must not reduce to a name a bare allowlist could hold."""
    root = tmp_path / "requivo"
    _write_tree(root, {"dotted.py": "import requivo.providers.anthropic\n"})
    path, package = scan(root, CLI_PACKAGE)[0]
    reached = provider_names(path, package)
    assert reached == {f"requivo.providers.anthropic {_WHOLE_MODULE}"}
    allowed_names = {name for _, name in _SURFACE_PROVIDER_ALLOWLIST}
    assert reached - allowed_names == reached, (
        "a whole-module import matched an allowlist key -- the marker no longer separates them"
    )

_LEGITIMATE_SURFACE = """
    from __future__ import annotations

    import json
    import sys
    from pathlib import Path

    from requivo.core.errors import RequivoError
    from requivo.render.terminal import render_turn
    from requivo.services.discovery import DiscoveryService
    from requivo.services.sessions import SessionService

    def show(providers: list) -> list:
        # a parameter named like the package must not read as an import of it
        return sorted(providers)
"""


def test_the_surface_guard_does_not_fire_on_what_an_interface_legitimately_imports(tmp_path):
    """The must-not-fire half: an interface is *supposed* to import the services, the renderers and the core error types."""
    root = tmp_path / "requivo"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_SURFACE})
    path, package = scan(root, CLI_PACKAGE)[0]
    assert provider_names(path, package) == set()


def test_the_surface_guard_refuses_a_subject_it_could_not_read():
    """The #10 control, one file down: `src/product_copilot/cli.py` is the pre-rename path, and reading a missing file as "imports nothing" is the same all-clear nobody earned."""
    renamed_away = REPO_ROOT / "src" / "product_copilot" / "cli.py"
    assert not renamed_away.exists(), "this control assumes the pre-rename path is gone"
    with pytest.raises(AssertionError, match="no such file"):
        subject_module(renamed_away)

# ---- the storage half of the same arrow: a surface reaches the store through SessionRepository ----
# #76, #77's twin: services/ takes storage as an injected seam, but 27 call sites across cli.py and
# deterministic/ reached core.persistence directly, so a second backing would hold there and break
# on the surfaces. The target is zero *unjustified* direct calls, not zero -- `session migrate`,
# `export`, `init` are legitimately about files -- and this table is where each is argued for.

SURFACE_MODULE = REPO_ROOT / "src" / "requivo" / "cli.py"
SURFACE_SUPPORT_MODULE = REPO_ROOT / "src" / "requivo" / "cli_support.py"  # split out by #550
SURFACE_TREES = (
    (REPO_ROOT / "src" / "requivo" / "deterministic", "requivo.deterministic"),
    (REPO_ROOT / "src" / "requivo" / "web", "requivo.web"),
    (REPO_ROOT / "src" / "requivo" / "providers", "requivo.providers"),  # #355
    (REPO_ROOT / "src" / "requivo" / "api", "requivo.api"),  # #425
)
PERSISTENCE_MODULE = "requivo.core.persistence"

# Keyed by (file, name), not by name alone -- a global list would let `deterministic/model.py`
# newly call `canonical_dir` and stay green because `sessions.py` was allowed to, which is the
# unjustified call this guard exists to catch arriving under a name already argued for elsewhere.
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
    """Every surface file, as (path, package, label) -- the allowlist key. `cli.py`/`cli_support.py` are named individually and the rest walked, so a module added later (`deterministic/` became a package at #73) arrives inside the scan set rather than beside it."""
    src = REPO_ROOT / "src" / "requivo"
    subjects = [
        (subject_module(SURFACE_MODULE), "requivo", "cli.py"),
        (subject_module(SURFACE_SUPPORT_MODULE), "requivo", "cli_support.py"),
    ]
    for root, package in SURFACE_TREES:
        subjects.extend((p, pkg, p.relative_to(src).as_posix()) for p, pkg in scan(root, package))
    return subjects


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` as a string, for an Attribute chain rooted in a plain Name. None if it is not one."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def persistence_names(path: Path, package: str) -> set[str]:
    """Every name `path` can reach inside `core.persistence`. Every surface writes `from requivo.core import persistence as store`, so the import set alone is one entry for a file making eighteen calls -- module aliases are resolved first, then every attribute taken off one is collected, which is what makes the
    allowlist per-function rather than per-import."""
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
                aliases.update(alias.asname or alias.name
                               for alias in node.names if alias.name == "persistence")
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
    """#76. Both directions asserted: an allowlist entry naming a call no longer made fails too, so a surface emptied, renamed or split into a package goes red rather than reading as clean."""
    reached = {(label, name)
               for path, package, label in surface_subjects()
               for name in persistence_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_STORAGE_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past SessionRepository to core.persistence: {unexpected}. Route it " f"through the repository if an equivalent exists (`exists`, `read_meta`, `lock`, " f"`list_slugs`, `load_model`, `save_artifact`, …), or add it to "
        f"_SURFACE_STORAGE_ALLOWLIST with the reason no backing-neutral form is possible."
    )
    stale = sorted(set(_SURFACE_STORAGE_ALLOWLIST) - reached)
    assert not stale, (
        f"_SURFACE_STORAGE_ALLOWLIST still names {stale}, which the surfaces no longer reach. " f"Either the guard is reading the wrong files, or the entry is prose about a call site " f"that is gone -- delete it."
    )

_SURFACE_STORAGE_IMPORTS = {
    "aliased.py": "from requivo.core import persistence as store\nstore.canonical_dir('s')\n",
    "bare.py": "from requivo.core import persistence\npersistence.canonical_dir('s')\n",
    "symbol.py": "from requivo.core.persistence import canonical_dir\n",
    "dotted.py": "import requivo.core.persistence as p\np.canonical_dir('s')\n",
    "relative.py": "from .core import persistence as store\nstore.canonical_dir('s')\n",
    "relative_symbol.py": "from .core.persistence import canonical_dir\n",
}


def test_the_storage_guard_sees_every_way_of_reaching_the_store(tmp_path):
    """Positive control: the extractor has to resolve an alias before it can see anything, so a blind version returns empty for every file here and the real test above passes over nothing."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_STORAGE_IMPORTS)
    missed = [path.name for path, package in scan(root, "requivo")
              if "canonical_dir" not in persistence_names(path, package)]
    assert not missed, f"the storage guard is blind to these: {missed}"


def test_the_storage_guard_separates_two_calls_behind_one_import(tmp_path):
    """One import, two functions, two allowlist keys -- otherwise one reviewed entry stands in for every call the module makes."""
    root = tmp_path / "requivo"
    _write_tree(root, {"two.py": (
        "from requivo.core import persistence as store\n" "store.canonical_dir('s')\n" "store.session_lock('s')\n"
    )})
    path, package = scan(root, "requivo")[0]
    assert persistence_names(path, package) == {"canonical_dir", "session_lock"}

_LEGITIMATE_STORAGE_SURFACE = """
    from __future__ import annotations

    from pathlib import Path

    from requivo.core.errors import RequivoError
    from requivo.services.sessions import SessionService

    def show(slug: str) -> str:
        svc = SessionService()
        # a repository call is the point of the seam, and must not read as a store call
        meta = svc.repo.read_meta(slug)
        store = Path(slug)          # a local named like the alias
        return f"{meta.slug} {store.name} {store.parent}"
"""


def test_the_storage_guard_does_not_fire_on_what_a_surface_legitimately_does(tmp_path):
    """The must-not-fire half: a local variable called `store` is not an import of the module."""
    root = tmp_path / "requivo"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_STORAGE_SURFACE})
    path, package = scan(root, "requivo")[0]
    assert persistence_names(path, package) == set()


def test_the_storage_guard_names_what_it_scanned():
    """#10, for this scan set. `providers/anthropic/completion.py` joined at #355 and `api/dependencies.py` at #425 -- each is what would have gone unscanned the moment it dropped out of `SURFACE_TREES`."""
    labels = sorted(label for _, _, label in surface_subjects())
    assert "cli.py" in labels
    for expected in (
        "deterministic/sessions/lifecycle.py", "deterministic/doctor.py", "web/dependencies.py",
        "providers/anthropic/completion.py", "api/dependencies.py",
    ):
        assert expected in labels, f"the storage guard did not scan {expected}; it scanned {labels}"

# ════════════════════════════════════════════════════════════════════════════════════════════════
# SECTION 2 -- Encoding: every text read/write names its codec (#11), and a console that cannot
# encode a glyph degrades rather than crashing after the work it reports has already landed (#29).
# 29 sites fell back to the locale's codec -- a UTF-8 session round-trips into mojibake on Windows
# that still validates. A static walk removes the class; a subprocess under a forced narrow encoder
# reaches the console encoder this suite's io.StringIO capture cannot; every runtime control has a
# control on its own lever, since "did not crash" also passes when the env var did nothing.
# ════════════════════════════════════════════════════════════════════════════════════════════════

SCAN_ROOTS = {
    "src/requivo": "the package",
    "scripts": "the golden harness and the install-free launcher",
}
SCAN_ANCHORS = {
    "src/requivo": ("core/persistence/store.py", "deterministic/__init__.py"),
    "scripts": ("golden_lib.py",),
}
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
    """`_scan.py`'s `list_python_files` now, shared with the boundary guard above (#288)."""
    return list_python_files(root, label="the encoding guard")


def _has_keyword(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


def _mode_of(node: ast.Call, positional_index: int) -> tuple[str | None, bool]:
    """The literal `mode` of an open() call, and whether it could be determined at all. A mode assembled at runtime is `(None, False)`, reported as a finding rather than waved through."""
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
    return "r", True  # the default, and it is text

_UNKNOWN_MODE = (
    "whose mode is not a literal -- this guard cannot tell text from binary here, so pass " "encoding= explicitly or read bytes"
)


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
    """Every string constant reachable from `node` that is not pure ASCII."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and any(ord(c) > 127 for c in n.value)]


def fixture_violations(path: Path) -> list:
    """Encoding-less text IO in a *test* whose content is not ASCII -- narrower than `encoding_violations` on purpose: ~90 of this suite's bare reads/writes move pure ASCII between a fixture and an assertion in one process, where the locale's codec agrees with itself. Only a fixture carrying a non-ASCII
    character crosses a real boundary (test writes locale, product reads UTF-8) and reddens a Windows leg about a product that is correct (#3)."""
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
            out.append(
                f"line {node.lineno}: .{node.func.attr}() with non-ASCII content and no encoding -- " f"the fixture is written with the locale's codec and read back by a product that " f"decodes UTF-8; they disagree on Windows. Content: {found[0][:60]!r}"
            )
    return sorted(out)

# Two sites read with the locale's default *on purpose*, to measure what the default does; fixing
# them would destroy the control. Keyed by path relative to tests/ (not basename, since a future
# tests/web/test_source_form.py must not silently inherit this file's exemptions) and by enclosing
# function, which drifts less than a line number.
_LOCALE_DEFAULT_BY_DESIGN = {
    "test_source_form.py": {
        "_force_default_encoding":
            "probes whether the ambient default could be forced at all; encoding= here would make "
            "the probe measure nothing",
        "test_the_guard_reads_source_as_utf8":
            "demonstrates the pre-#10 failure by performing it -- the bare read IS the thing under "
            "test, asserted to raise",
    },
    "test_persistence.py": {
        "test_an_artifact_round_trips_non_ascii_content":
            "same shape, one commit later: the bare read performs the defect under a forced ASCII "
            "default so `raises` can catch it -- invisible on 3.10+, where the force does not take",
    },
}


def _enclosing_function_names(tree: ast.Module) -> dict:
    """Map each AST node's id() to the name of the function it sits in. Outermost wins."""
    out: dict = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                out.setdefault(id(node), fn.name)
    return out


def read_violations_in_test(path: Path) -> list:
    """Encoding-less *reads* in a test, minus the two deliberately measuring the default. Reads and writes are asymmetric here on purpose: a write's hazard is a literal `fixture_violations` can see; a read's hazard is in the file being read, which the source never mentions -- the first Windows leg this branch
    added went red on a bare `read_text()` over a bundled asset full of em dashes, with no literal for the narrower write-side rule to have caught."""
    tree = _parse(path)
    try:
        key = path.relative_to(REPO_ROOT / "tests").as_posix()
    except ValueError:
        key = ""  # a fixture tree under tmp_path: no exemption can apply to it, which is correct
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
        out.append(
            f"line {node.lineno}: .read_text() with no encoding -- the file being read may hold " f"characters the locale's codec cannot decode, and unlike a write there is no literal " f"here for a narrower rule to inspect"
        )
    return sorted(out)


def test_every_read_in_the_suite_declares_its_encoding():
    """The gap the first Windows leg found: the old guard walked src/ and scripts/, and the 30th bare-read site was in the one directory it did not."""
    offenders: dict = {}
    for path in encoding_scan(REPO_ROOT / "tests"):
        found = read_violations_in_test(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "a test that reads text without naming its codec decodes with whatever the platform offers, " "so it passes on Linux and fails on Windows about a file the product reads correctly: "
        + repr(offenders)
    )


def test_the_by_design_exemptions_still_exist():
    """An exemption for a renamed/deleted function covers nothing, or worse, covers the wrong one reusing that name. Pin it, and prove the table is not dead weight suppressing nothing."""
    for filename, functions in _LOCALE_DEFAULT_BY_DESIGN.items():
        path = REPO_ROOT / "tests" / filename
        assert path.is_file(), f"{filename} is exempted from the read rule and does not exist"
        defined = {n.name for n in ast.walk(_parse(path))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = sorted(set(functions) - defined)
        assert not missing, (
            f"{filename} exempts {missing}, which it no longer defines. An exemption naming " f"nothing is either dead or about to cover the wrong function."
        )
        assert read_violations_in_test(path) == [], "unexpected: exempted file has other bare reads"
        saved = globals()["_LOCALE_DEFAULT_BY_DESIGN"]
        globals()["_LOCALE_DEFAULT_BY_DESIGN"] = {}
        try:
            without = read_violations_in_test(path)
        finally:
            globals()["_LOCALE_DEFAULT_BY_DESIGN"] = saved
        assert len(without) == len(functions), (
            f"{filename} exempts {len(functions)} function(s) but only {len(without)} bare "
            f"read(s) exist without the table: {without}")


def test_the_read_rule_fires_and_spares_correctly(tmp_path):
    """Both edges. A rule with an exemption table is the one that quietly stops firing."""
    root = tmp_path / "tests"
    _write_tree(root, {"test_x.py": """
        from pathlib import Path

        def test_bare(p: Path) -> str:
            return p.read_text()

        def test_explicit(p: Path) -> str:
            return p.read_text(encoding="utf-8")
    """})
    path = encoding_scan(root)[0]
    found = read_violations_in_test(path)
    assert len(found) == 1 and "line 5" in found[0], found


def test_no_test_fixture_writes_non_ascii_with_the_locale_codec():
    """The harness half of #3, kept honest by the same walk that keeps the product honest."""
    offenders: dict = {}
    for path in encoding_scan(REPO_ROOT / "tests"):
        found = fixture_violations(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "a test fixture carrying non-ASCII text must name its codec, or the Windows leg reddens "
        "about a product that is correct: " + repr(offenders)
    )

_FIXTURE_CASES = {
    "must_fire.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context")
    """,
    "must_not_fire_ascii.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME - the context")
    """,
    "must_not_fire_explicit.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context", encoding="utf-8")
    """,
}


def test_the_fixture_rule_fires_exactly_where_it_should(tmp_path):
    """Both edges of the narrow rule, because a rule this narrow is the easy one to get backwards."""
    root = tmp_path / "tests"
    _write_tree(root, _FIXTURE_CASES)
    verdicts = {p.name: bool(fixture_violations(p)) for p in encoding_scan(root)}
    assert verdicts == {
        "must_fire.py": True,
        "must_not_fire_ascii.py": False,
        "must_not_fire_explicit.py": False,
    }, verdicts


@pytest.mark.parametrize("relative", sorted(SCAN_ROOTS))
def test_the_guard_scans_the_real_trees(relative):
    """Name what was scanned -- everything below is a negative assertion over this set."""
    root = REPO_ROOT / relative
    names = sorted(p.relative_to(root).as_posix() for p in encoding_scan(root))
    missing = [a for a in SCAN_ANCHORS[relative] if a not in names]
    assert not missing, (
        f"the encoding guard scanned {root} and did not find {missing}; it is not looking at " f"{SCAN_ROOTS[relative]}. Scanned: {names}"
    )


def test_every_text_read_declares_its_encoding():
    """#11 itself: every text read/write in the shipped trees names its codec, so a session written as UTF-8 reads back as what was written, on every platform Requivo installs on. A class, not 29 instances: a bare `read_text()` has already reappeared once after a fix (three lines from #33's own) and once in the
    #10 guard itself -- an instance list does not prevent a 30th."""
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in encoding_scan(REPO_ROOT / relative):
            found = encoding_violations(path)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "every text read and write must pass encoding= explicitly: the default is the *locale's* " "codec, so a file this project wrote as UTF-8 is decoded as cp1252 on Windows and the "
        "round-trip corrupts silently while still validating (#11). Offenders: " + repr(offenders)
    )

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


def test_the_guard_sees_each_way_of_writing_the_violation(tmp_path):
    """Positive control, one fixture per shape -- the buried one proves the walk is recursive."""
    root = tmp_path / "src"
    _write_tree(root, _ENCODING_VIOLATIONS)
    missed = [p.relative_to(root).as_posix() for p in encoding_scan(root) if not encoding_violations(p)]
    assert not missed, f"the encoding guard is blind to these: {missed}"

_LEGITIMATE_IO = """
    from __future__ import annotations

    import os
    import webbrowser
    from pathlib import Path

    def load(p: Path) -> str:
        return p.read_text(encoding="utf-8")

    def save(p: Path, text: str) -> None:
        p.write_text(text, encoding="utf-8")

    def load_bytes(p: Path) -> bytes:
        return p.read_bytes()

    def save_bytes(p: Path, blob: bytes) -> None:
        p.write_bytes(blob)

    def binary_stream(p: Path):
        return p.open("rb")

    def builtin_binary(name):
        return open(name, "rb")

    def builtin_text(name):
        return open(name, encoding="utf-8")

    def lock(d: Path) -> int:
        # a raw file descriptor: no text layer, so no codec to declare
        return os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)

    def show(url: str) -> None:
        webbrowser.open(url)
"""


def test_the_guard_does_not_fire_on_correct_io(tmp_path):
    """The must-not-fire half: a detector that flags everything is deleted the first time it reddens correct code."""
    root = tmp_path / "src"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IO})
    assert encoding_violations(encoding_scan(root)[0]) == []

# ---- a keyword the floor does not have (#464, #469) ----
# `_atomic_write`'s fix for #464 reached for `write_text(..., newline="")` -- correct on 3.13, a
# TypeError under the declared 3.9 floor (518 failures per leg, one keyword, the one function every
# persistence path goes through). Same walk, same two trees (#288, #355) rather than a fourth tier.
# pyright's `pythonVersion = "3.9"` does not catch this -- typeshed doesn't version-gate `newline`.
_KEYWORDS_YOUNGER_THAN_THE_FLOOR = {
    ("write_text", "newline"): (3, 10),
    ("read_text", "newline"): (3, 13),
}


def _declared_floor() -> tuple:
    """The `requires-python` floor, read out of pyproject rather than restated here -- a version written into this file would be a number in prose that nothing can falsify."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*"[><=~^ ]*(\d+)\.(\d+)', text, re.MULTILINE)
    assert m, (
        "could not read requires-python out of pyproject.toml -- this guard derives the floor from "
        "it, and a floor it cannot read is not a floor it may guess at")
    return (int(m.group(1)), int(m.group(2)))


def floor_violations(path: Path, floor: tuple) -> list:
    """Every text call in `path` passing a keyword the declared floor's interpreter does not accept. Attribute calls only, by method name -- the shape the defect actually took."""
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
                out.append(
                    f"line {node.lineno}: .{node.func.attr}({kw.arg}=...) -- that keyword reached " f"{node.func.attr} in Python {added[0]}.{added[1]}, and this project's floor is " f"{floor[0]}.{floor[1]}, where it is a TypeError on every call. Write it through "
                    f".open(..., {kw.arg}=...) instead, which has always taken it."
                )
    return sorted(out)


def test_no_text_call_passes_a_keyword_the_declared_floor_rejects():
    """#469: `_atomic_write` wrote `tmp.write_text(content, encoding="utf-8", newline="")`, a TypeError on the three 3.9 legs -- 518 failures each, from one keyword on the one function every persistence path goes through."""
    floor = _declared_floor()
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in encoding_scan(REPO_ROOT / relative):
            found = floor_violations(path, floor)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        f"a text call passes a keyword the declared {floor[0]}.{floor[1]} floor does not accept, " f"which is a TypeError there and invisible on a newer interpreter -- and invisible to the "
        f"Types leg too, since typeshed does not version-gate these. Offenders: " + repr(offenders)
    )


def test_the_floor_guard_fires_on_the_exact_call_that_shipped_red(tmp_path):
    """The must-fire control, written as the line #469 actually shipped."""
    root = tmp_path / "src"
    _write_tree(root, {"as_shipped.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            tmp.write_text(content, encoding="utf-8", newline="")
    """})
    found = floor_violations(encoding_scan(root)[0], (3, 9))
    assert len(found) == 1 and "write_text(newline=...)" in found[0], found


def test_the_floor_guard_spares_the_fix_and_the_read_side_twin(tmp_path):
    """Two must-not-fire cases: `.open(..., newline="")` is the fix, and a floor that has caught up (3.10) must relax the rule rather than keep reporting the identical call."""
    root = tmp_path / "src"
    _write_tree(root, {"fixed.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                fh.write(content)
    """})
    assert floor_violations(encoding_scan(root)[0], (3, 9)) == []

    raised = tmp_path / "raised"
    _write_tree(raised, {"as_shipped.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            tmp.write_text(content, encoding="utf-8", newline="")
    """})
    assert floor_violations(encoding_scan(raised)[0], (3, 10)) == []

# RETIRED (#551): test_the_new_3_14_leg_is_declared_consistently (#298) -- pinned that the 3.14 CI
# leg and pyproject's classifiers were added together. One incident (the 3.14 leg landing without
# its classifier), never repeated since (no further Python-version leg has been added), and cited
# from nowhere else in the tree. `test_the_floor_is_read_from_pyproject_and_matches_what_ci_runs`
# below still guards the load-bearing half: a floor CI does not actually run.


def test_the_floor_is_read_from_pyproject_and_matches_what_ci_runs():
    """The lever control: a floor this guard could not read makes every case above vacuous, and a floor disagreeing with the CI matrix would guard a version nothing runs."""
    floor = _declared_floor()
    matrix = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'"{floor[0]}.{floor[1]}"' in matrix or f"'{floor[0]}.{floor[1]}'" in matrix, (
        f"pyproject declares a {floor[0]}.{floor[1]} floor and no CI leg runs it, so nothing "
        f"observes what that interpreter actually accepts")

# ---- the runtime half (#29): nothing in-process can reach the console encoder, so a subprocess ----


def _clean_env(extra: dict) -> dict:
    env = dict(os.environ)
    for name in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONWARNDEFAULTENCODING"):
        env.pop(name, None)  # any inherited would override the levers below silently
    env.update(extra)
    return env


def _cli(args: list, extra_env: dict, cwd: Path, python_args: list = None):
    env = _clean_env(extra_env)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, *(python_args or []), "-m", "requivo", *args],
        cwd=str(cwd), env=env, capture_output=True, timeout=300,
    )

_ASCII_CONSOLE = {"PYTHONIOENCODING": "ascii"}
# Named by codepoint so this source file stays pure ASCII.
_CHECK_MARK = chr(0x2705)
_WARNING_SIGN = chr(0x26A0)


def test_the_ascii_console_lever_actually_bites():
    """Positive control for the console tests below: `PYTHONIOENCODING=ascii` is narrower than Windows cp1252, so a process that survives it survives cp1252 too -- but only if the lever actually took, which is measured rather than assumed."""
    probe = subprocess.run(
        [sys.executable, "-c", "print(chr(0x2705))"],
        env=_clean_env(_ASCII_CONSOLE), capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode != 0, (
        "PYTHONIOENCODING=ascii did not take on this interpreter; the console controls below cannot " "fire and must not be read as evidence."
    )
    assert "UnicodeEncodeError" in probe.stderr, probe.stderr


@pytest.mark.parametrize("verb", [["doctor"], ["schema"], ["demo"]])
def test_the_cli_survives_a_console_that_cannot_encode_its_glyphs(verb, tmp_path):
    """#29: `doctor` prints a check mark on its first line and used to die there on a console that cannot encode it -- after the diagnosis it exists to report was already computed. Worst on the write verbs: `requivo brief` would die after a paid call had already landed a revision."""
    r = _cli(verb, _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, (
        f"`requivo {' '.join(verb)}` died in its own renderer on a console that cannot encode its " f"glyphs: {detail}"
    )
    assert r.returncode == 0, detail
    assert r.stdout.strip(), "the command exited 0 and printed nothing at all"


def test_the_console_chokepoint_degrades_rather_than_dropping_the_glyph(tmp_path):
    """Survival must not mean silently printing nothing where a glyph was -- an escape is ugly and honest, a hole in the line is neither."""
    r = _cli(["doctor"], _ASCII_CONSOLE, tmp_path)
    assert r.returncode == 0, r.stderr.decode("ascii", "backslashreplace")
    text = r.stdout.decode("ascii", "strict")  # it must be pure ascii on an ascii console
    escaped = [g.encode("ascii", "backslashreplace").decode("ascii") for g in (_CHECK_MARK, _WARNING_SIGN)]
    assert any(e in text for e in escaped), (
        "the glyphs were dropped rather than escaped; a reader cannot tell a missing character from "
        "a character that was never there. Expected one of " + repr(escaped) + " in: " + text
    )


def _seed_a_usage_bearing_session(tmp_path, monkeypatch, *, priced_as_of):
    """A session with one revision carrying token/rate provenance (#292), written directly through `core.persistence`. `priced_as_of=None` leaves the revision unpriced (the em-dash branch); two dates make the "rates as of" stamp middle-dot-joined."""
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
    ("unpriced", None, "—"),                          # em dash: "n/a — no price on file"
    ("multi_dated", ["2026-01-01", "2026-06-01"], "·"),  # middle dot: "... as of X · Y"
])
def test_requivo_status_survives_a_console_that_cannot_encode_the_session_cost_line(
    scenario, priced_as_of, glyph, tmp_path, monkeypatch
):
    """#292: `render_session_cost` prints an em dash or a middle dot, dispatched through `_cmd_status` behind the same `configure_streams()` guard as `doctor`/`schema`/`demo` above. Discriminating, not merely surviving: the escaped glyph must appear in stdout, or a session that never became usage-bearing would
    pass the same way (see the positive control immediately below)."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=priced_as_of)
    r = _cli(["status", slug], _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, (
        f"`requivo status` died rendering the SESSION COST line ({scenario}) on a console that " f"cannot encode its glyphs: {detail}"
    )
    assert r.returncode == 0, detail
    text = r.stdout.decode("ascii", "strict")  # it must be pure ascii on an ascii console
    assert "SESSION COST" in text, (
        f"render_session_cost never ran for the {scenario} scenario -- the sweep proved nothing " f"about it. stdout: {text}"
    )
    escaped = glyph.encode("ascii", "backslashreplace").decode("ascii")
    assert escaped in text, (
        f"the {scenario} glyph was dropped rather than escaped, or the guarded path never reached " f"it. Expected {escaped!r} in: {text}"
    )


def test_render_session_cost_alone_raises_on_a_stream_nothing_can_fix(tmp_path, monkeypatch):
    """The positive control the test above depends on: called directly, against a stream nothing can save, `render_session_cost`'s em dash really does raise -- without this, "no crash" above would be unfalsifiable."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=None)
    from requivo.render.terminal import render_session_cost
    from requivo.services.sessions import SessionService

    revisions = SessionService().repo.read_meta(slug).revisions
    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(UnicodeEncodeError):
        render_session_cost(revisions)

# ---- the chokepoint's own three states ----


def _wrapper(encoding: str, errors: str):
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def test_describe_stream_separates_safe_from_will_crash_from_unknown():
    """Three answers, not two: `will_crash` and `unknown` are different findings with different remedies, and both differ from `safe` -- conflating "I looked and it is fine" with "I could not look" would put an absence into doctor's own report."""
    safe = streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")
    assert safe["state"] == "safe" and safe["detail"] is None

    crashy = streams.describe_stream(_wrapper("ascii", "strict"), "stdout")
    assert crashy["state"] == "will_crash"
    assert "UnicodeEncodeError" in crashy["detail"], crashy

    blind = streams.describe_stream(io.BytesIO(), "stdout")  # no .encoding at all
    assert blind["state"] == "unknown"
    assert "cannot look" in blind["detail"], blind

    assert streams.describe_stream(None, "stdout")["state"] == "unknown"


def test_the_published_stream_states_are_all_underscore_spelled():
    """`output.streams[].state` is a published --json enum, and every other one in this project is one word or underscore-joined. `will-crash` was the one hyphen, costing a consumer a special case (#88). Drives all four arms, the only coverage `lossy` has ever had."""
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
    """The third state on the *configuring* side: an unconfigurable stream is exactly the one that can still kill the process later, so it has to be nameable rather than silent."""
    reached = streams.configure_stream(_wrapper("ascii", "strict"), "stdout")
    assert reached["state"] in ("configured", "unchanged"), reached
    assert reached["errors"] == streams.ERRORS

    unreachable = streams.configure_stream(io.BytesIO(), "stdout")  # no .reconfigure
    assert unreachable["state"] == "could-not"
    assert "reconfigure" in unreachable["reason"], unreachable

    closed = _wrapper("utf-8", "strict")
    closed.close()
    assert streams.configure_stream(closed, "stdout")["state"] == "could-not"


def test_configure_stream_does_not_overrule_an_operator_who_named_a_codec(monkeypatch):
    """`PYTHONIOENCODING` is somebody's pipeline decision; this module guarantees their stream cannot crash, it does not get to decide what their stream is for."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    stream = _wrapper("ascii", "strict")
    report = streams.configure_stream(stream, "stdout")
    assert stream.encoding == "ascii", "the operator's codec was overruled"
    assert stream.errors == streams.ERRORS, "the no-crash guarantee was not applied"
    assert report["state"] == "unchanged" and "PYTHONIOENCODING" in report["reason"]


def test_safe_write_never_raises_on_a_character_it_cannot_encode():
    """The message this writes is usually an error report, and one that dies on its own em dash is the whole failure this file exists to fix."""
    stream = _wrapper("ascii", "strict")
    streams.safe_write(stream, "verdict " + _CHECK_MARK + " done")
    stream.seek(0)
    written = stream.buffer.getvalue().decode("ascii")
    assert "verdict" in written and "done" in written, written
    assert _CHECK_MARK.encode("ascii", "backslashreplace").decode("ascii") in written, written


def test_safe_write_gives_up_quietly_on_a_stream_that_is_gone():
    """A closed stream is not a reason to raise from inside an error handler -- there is nowhere left to report to."""
    closed = _wrapper("utf-8", "strict")
    closed.close()
    streams.safe_write(closed, "anything")  # must not raise


class _Unconfigurable(io.TextIOWrapper):
    """A stream `configure_streams` cannot fix: `reconfigure` refuses, so it stays strict/ascii -- a real `TextIOWrapper`, not a mock, so the `UnicodeEncodeError` below is the real encoder's."""

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
        stream.write(_CHECK_MARK)          # the failure is the encoder's, not the test's


def _run_app_on_an_unconfigurable_stdout(monkeypatch, argv, ledger_calls=()):
    """Drive `cli.app()` with a stdout that cannot be made safe, return (exit code, stderr)."""
    out, err = _unconfigurable_stdout(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    class _Ledger:
        calls = list(ledger_calls)

    monkeypatch.setattr(cli, "track_usage", lambda: contextlib.nullcontext(_Ledger()))
    # render_usage moved to cli_support.py by #550; cli.py no longer imports the name at all.
    monkeypatch.setattr(cli_support, "render_usage", lambda ledger: None)
    with pytest.raises(SystemExit) as ei:
        cli.app(argv)
    return ei.value.code, err.getvalue()


def test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback(monkeypatch, tmp_path):
    """#29's ordering rule at the last line of defence: the command has already done its work by the time anything prints, so a traceback here reports a failure that did not happen."""
    monkeypatch.chdir(tmp_path)
    code, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"])
    assert code == cli.EXIT_RENDER_FAILED == 3, (code, err)
    assert "could not encode its output" in err, err
    assert "Traceback" not in err, err
    assert "requivo doctor" in err, "the message does not say how to find out which stream: " + err


def test_the_render_failure_message_does_not_claim_a_call_was_billed_when_none_was(monkeypatch, tmp_path):
    """Several verbs print before they mutate, or never mutate at all -- a single message asserting a change HAS been applied would be false for those, so the arm reads the usage ledger instead of assuming."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=())
    assert "No provider call was made" in err, err
    assert "HAS completed and been billed" not in err, (
        "`doctor` makes no provider call, and telling the user one was billed is a false "
        "statement in the message that exists to stop a false statement: " + err)


def test_the_render_failure_message_does_say_so_when_a_call_was_billed(monkeypatch, tmp_path):
    """The must-fire half: the warning that matters is the one on the verbs that cost money."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=("one call",))
    assert "HAS completed and been billed" in err, err
    assert "Do not re-run" in err, err


def test_the_usage_line_cannot_kill_a_run_that_already_paid_for_its_call(monkeypatch, tmp_path):
    """Found by audit: `render_usage` has two call sites outside the `UnicodeEncodeError` arm, including the one after a wholly successful command -- so a successful `requivo brief` on an unreachable stream still died at the usage line, after the provider call was billed."""
    from requivo.providers.anthropic.pricing import price_call
    from requivo.usage import CallRecord, UsageLedger

    # Priced through price_call, the way the provider files a real call (#167): a bare CallRecord
    # carries no rate, and an unpriced ledger renders the other branch.
    ledger = UsageLedger()
    ledger.record(price_call(CallRecord(model="claude-sonnet-5", input_tokens=10, output_tokens=20,
                                        cache_read_tokens=0, cache_write_tokens=0, latency_ms=5)))

    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)

    # The control: unwrapped, this really does raise on this ledger and this stream.
    with pytest.raises(UnicodeEncodeError):
        cli_support.render_usage(ledger)

    cli._render_usage_safely(ledger)      # the wrapper must not
    assert "could not be encoded" in err.getvalue(), (
        "the usage line vanished without a word; a line nobody can read is not the same as a run "
        "that made no calls, and they must not print the same way: " + repr(err.getvalue()))

# ---- a file the *user* named -- the one read whose bytes this project did not write ----


def test_a_user_file_that_is_not_utf8_is_refused_by_name_not_by_traceback(tmp_path):
    """Refusing is right -- mojibake validates -- but a bare `UnicodeDecodeError` trades a silently wrong answer for an unexplained crash, the same trade one step along. The refusal must answer."""
    brief = tmp_path / "brief.md"
    # A French sentence saved as cp1252 by a Windows editor: the realistic input, not an exotic one.
    brief.write_bytes("Système de validation des congés.".encode("cp1252"))

    with pytest.raises(InvalidModelError) as ei:
        read_user_text(brief)

    message = str(ei.value)
    assert "not valid UTF-8" in message, message
    assert "0xe8" in message, ("the offending byte is not named, so the user cannot tell which "
                              f"character to look for: {message}")
    assert ei.value.details["path"] == str(brief)
    assert ei.value.details["expected_encoding"] == "utf-8"
    assert isinstance(ei.value.details["position"], int)


def test_a_user_file_that_is_utf8_is_read_unchanged(tmp_path):
    """The must-not-fire half, and the case that matters most here: correctly encoded French prose."""
    brief = tmp_path / "brief.md"
    original = "Système de validation des congés — 5 000 salariés."
    brief.write_bytes(original.encode("utf-8"))
    assert read_user_text(brief) == original


def test_the_refusal_does_not_let_a_path_forge_a_line_of_output(tmp_path):
    """The message interpolates a user-supplied path -- one carrying a newline must not forge a second, authoritative-looking line of Requivo's own output, the shape #40 found in `doctor`."""
    sneaky = tmp_path / "brief\nERROR: session verified OK.md"
    try:
        sneaky.write_bytes(b"\xe8")
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a newline in a filename; the forging path is untested here")
    with pytest.raises(InvalidModelError) as ei:
        read_user_text(sneaky)
    body = str(ei.value)
    assert not any(line.startswith("ERROR:") for line in body.splitlines()), (
        "a user-supplied path forged a line at column 0 of Requivo's own message: " + repr(body))

_WARN_DEFAULT_ENCODING = {"PYTHONWARNDEFAULTENCODING": "1"}
_ERROR_ON_DEFAULT_ENCODING = ["-W", "error::EncodingWarning"]
_NO_LEVER_ON_39 = (
    "EncodingWarning and PYTHONWARNDEFAULTENCODING are 3.10+, so this lever cannot fire on 3.9. " "UNTESTED ON THIS INTERPRETER: that the CLI reads its bundled assets with an explicit codec " "rather than the locale's. The static guard above covers the same claim on every interpreter, "
    "and the 3.10-3.13 legs of the CI matrix do run this one."
)


def test_the_default_encoding_lever_actually_bites(tmp_path):
    """Positive control for the read-side test below, on the same reasoning as the console one."""
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
    r = subprocess.run(
        [sys.executable, *_ERROR_ON_DEFAULT_ENCODING, str(probe), str(target)],
        env=_clean_env(_WARN_DEFAULT_ENCODING), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0 and "EncodingWarning" in r.stderr, (
        "the default-encoding lever did not take; the read-side control below cannot fire and must "
        "not be read as evidence: " + r.stderr
    )


@pytest.mark.parametrize("verb", [["schema"], ["schema", "--framework"], ["context"], ["demo"], ["doctor"]])
def test_the_cli_reads_its_assets_with_an_explicit_encoding(verb, tmp_path):
    """#11's read half, at runtime: `-W error::EncodingWarning` turns any locale-default text read into an exception on every platform -- sharper than forcing a locale, since it fires on a read that happens to succeed today. All five bundled context cards are cp1252-decodable, which is why that path corrupts
    silently on Windows rather than crashing, and why a crash-based control would have missed it."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    r = _cli(verb, {**_WARN_DEFAULT_ENCODING, **_ASCII_CONSOLE}, tmp_path,
             python_args=_ERROR_ON_DEFAULT_ENCODING)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"EncodingWarning" not in r.stderr, (
        f"`requivo {' '.join(verb)}` read text with the locale's codec rather than an explicit " f"one: {detail}"
    )
    assert r.returncode == 0, detail

# ════════════════════════════════════════════════════════════════════════════════════════════════
# SECTION 3 -- Narrative references: a named test or decision record is a reference, so it has to
# resolve and be findable by grep (#75) -- renamed/deleted, or split by a line wrap (`contracts.py`
# once carried a test name broken across two comment lines: the test existed, nobody could grep it).
# Checks only that references that exist are true and findable; whether one *should* exist is a
# judgement call CLAUDE.md states for a person.
# ════════════════════════════════════════════════════════════════════════════════════════════════

SRC = REPO_ROOT / "src" / "requivo"
TESTS = REPO_ROOT / "tests"
SCRIPTS = REPO_ROOT / "scripts"  # not shipped in the wheel, but read by a maintainer's grep (#137)
DOCS = REPO_ROOT / "docs"        # CLAUDE.md's own "narrative's right home" (#156)

# `.js` joined at #156 (`tests/web/busy_harness.js`, the motivating instance, had zero references
# in vendored/first-party JS elsewhere). `.md` covers CLAUDE.md, named below as an extra subject.
SUBJECT_SUFFIXES = (".py", ".html", ".md", ".js")
EXTRA_SUBJECTS = (REPO_ROOT / "CLAUDE.md",)

# The *resolution* roots. `tests/` joined at #190 (closing a gap #156 opened and #188 declined),
# after widening the glob produced eleven false positives of two kinds: "Split out of `X.py` by
# #141" recounting a deletion (matched narrowly by `_HISTORICAL_MENTION`, see
# `test_a_similarly_shaped_mention_that_is_not_the_idiom_still_dangles`), and this guard's own
# fixtures (excluded by identity, `RESOLUTION_EXEMPT_FILES`). `CHANGELOG.md` is excluded everywhere:
# released history's dead pointers are correct, not stale.
RESOLUTION_ROOTS = (SRC, SCRIPTS, DOCS, TESTS)
_HISTORICAL_MENTION = re.compile(r"[Ss]plit out of `(test_[a-z0-9_]+)\.py`")
RESOLUTION_EXEMPT_FILES = (Path(__file__).resolve(),)
# A second, distinct reason a file is resolution-exempt: vendored code where a `test_`-shaped
# identifier is somebody else's function, not a claim about this suite (#504's swagger-ui-bundle.js,
# which coincidentally defines test_cookie_name/test_cookie_value). Kept as its own tuple, verified
# by `test_a_vendored_bundles_coincidental_identifier_is_not_a_dangling_reference` on every run so a
# re-vendor that drops the collision is caught rather than leaving a stale exemption.
VENDORED_RESOLUTION_EXEMPT_FILES = (
    (SRC / "api" / "static" / "vendor" / "swagger-ui" / "swagger-ui-bundle.js").resolve(),
)
# The *wrap* roots: purely mechanical, no pointer-vs-mention problem, so tests/ was already here
# before #190 widened RESOLUTION_ROOTS to match -- it is where busy_harness.js actually sat.
WRAP_ROOTS = (SRC, SCRIPTS, DOCS, TESTS)

# A reference is `test_` plus 10+ chars; the floor keeps `test_x` in an example snippet from
# reading as a claim about the suite.
_REFERENCE = re.compile(r"\btest_[a-z0-9_]{10,}\b")
# A decision record is referenced by its slug, the same convention and for the same reason.
_DECISION_REF = re.compile(r"`decision:\s*([a-z0-9-]+)`")
DECISIONS = REPO_ROOT / "docs" / "decisions"
# docs/decisions/ is a *subject* (its own references resolve like any other file's) but deliberately
# **not a referrer** (#384): a record explaining where its own pointer belongs has to quote its own
# slug to say so, and if records inside the directory could vouch for each other, a whole cluster
# citing only itself would read as reachable. Measured: at #384's base commit one record was
# referenced from nowhere but itself and this guard passed.
REFERRER_EXEMPT_ROOTS = (DECISIONS,)

# The wrap: an identifier ending a line on a trailing underscore -- no Python identifier here ends
# in `_` legitimately, so this is the split, not a style.
_WRAPPED = re.compile(r"\btest_[a-z0-9_]*_$", re.MULTILINE)
# The slug version, detected by the *missing closing backtick* rather than a trailing hyphen: an
# inline code span that opens on a line and does not close on it is broken wherever it broke. A
# break *before* the slug (spanned by the whitespace class) leaves the slug whole and is left alone
# -- see `test_the_decision_wrap_detector_leaves_the_shape_that_still_resolves_alone`.
_WRAPPED_DECISION = re.compile(r"`decision:[ \t]*[a-z0-9-]*[a-z0-9-](?=[^`\n]*$)", re.MULTILINE)


def _scan_subjects(roots: tuple[Path, ...], extra: tuple[Path, ...] = ()) -> list[Path]:
    """`_scan.py`'s `list_files` now (#288), shared with the two guards above."""
    return list_files(roots, suffixes=SUBJECT_SUFFIXES, label="the narrative-reference guard",
                       extra=extra)


def subjects() -> list[Path]:
    """Every file the *resolution* check reads -- `RESOLUTION_ROOTS` minus the two exemptions."""
    exempt = set(RESOLUTION_EXEMPT_FILES) | set(VENDORED_RESOLUTION_EXEMPT_FILES)
    return [p for p in _scan_subjects(RESOLUTION_ROOTS, EXTRA_SUBJECTS) if p.resolve() not in exempt]


def wrap_subjects() -> list[Path]:
    """Every file the *wrap* check reads -- wider than `subjects()` on purpose."""
    return _scan_subjects(WRAP_ROOTS, EXTRA_SUBJECTS)


def _referrers_among(paths: Iterable[Path], exempt_roots: tuple[Path, ...]) -> list[Path]:
    """The subset of `paths` that may vouch for a decision record's reachability."""
    return [p for p in paths if not any(root in p.parents for root in exempt_roots)]


def referrer_subjects() -> list[Path]:
    """`subjects()` minus the decision records themselves. See `REFERRER_EXEMPT_ROOTS`."""
    return _referrers_among(subjects(), REFERRER_EXEMPT_ROOTS)


def _orphan_slugs(declared: set[str], referrers: Iterable[Path]) -> list[str]:
    """The declared slugs nothing in `referrers` points at."""
    referenced = {slug for path in referrers
                  for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))}
    return sorted(declared - referenced)

# `requivo.testing` (#424) is the one place under src/ that legitimately *defines* test methods --
# `SessionRepositoryConformance`, a pytest mixin collected only through a tests/-side subclass.
TESTING_PACKAGE = SRC / "testing"


def declared_test_names() -> set[str]:
    """Every test callable the suite defines, plus every test module's stem -- both are used as references (`services/artifacts.py` names a whole file, since the claim is the file's subject)."""
    names: set[str] = set()
    roots = (TESTS, TESTING_PACKAGE) if TESTING_PACKAGE.is_dir() else (TESTS,)
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            names.add(path.stem)
            for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)",
                                     path.read_text(encoding="utf-8"), re.MULTILINE):
                names.add(match.group(1))
    if not names:
        raise AssertionError(
            f"the narrative-reference guard found no tests under {TESTS} to resolve against — " f"every reference would 'resolve' against an empty set."
        )
    return names


def references(path: Path) -> set[str]:
    """Every test reference in one file, as a plain reader would see it -- historical mentions included. Resolution has a narrower view; see `resolvable_references()`."""
    return set(_REFERENCE.findall(path.read_text(encoding="utf-8")))


def resolvable_references(path: Path) -> set[str]:
    """`references()` minus the name inside a recognized "Split out of `X.py`" idiom. Blanks the matched span rather than the name everywhere, so a file that also points at the same dead name the ordinary way still answers for that second occurrence."""
    text = path.read_text(encoding="utf-8")
    stripped = _HISTORICAL_MENTION.sub(
        lambda m: m.group(0).replace(m.group(1), "x" * len(m.group(1))), text
    )
    return set(_REFERENCE.findall(stripped))


def test_the_guard_reads_the_real_tree():
    """Name what was scanned -- everything below is a negative assertion over this set."""
    files = subjects()
    assert len(files) > 20, f"only {len(files)} subject files — the scan is not seeing the package"
    assert any(p.name == "contracts.py" for p in files)
    assert any(p.parent.name == "scripts" for p in files), (
        "scripts/ is not in the scan, and it carries references — this guard would report them clean"
    )
    assert any(p.name == "CLAUDE.md" for p in files)
    assert any(p.suffix == ".md" and "docs" in p.parts for p in files), (
        "docs/ is not in the resolution scan, and CLAUDE.md names it as narrative's right home"
    )
    assert not any(p.name == "CHANGELOG.md" for p in files), (
        "CHANGELOG.md is released history — its dead pointers are correct, not stale, and it must " "never be swept"
    )
    assert len(declared_test_names()) > 100


def test_the_wrap_scan_still_reaches_one_file_further_than_the_resolution_scan():
    """#190's decision, pinned: `tests/` is now in *both* roots, and the only gap between them is this guard's own module plus, since #504, the one vendored bundle."""
    resolution_files = set(subjects())
    wrap_files = set(wrap_subjects())
    assert resolution_files < wrap_files, "the wrap scan must be a strict superset of the resolution scan"
    expected_gap = set(RESOLUTION_EXEMPT_FILES) | set(VENDORED_RESOLUTION_EXEMPT_FILES)
    assert wrap_files - resolution_files == expected_gap, (
        "the only files the wrap scan reaches and the resolution scan does not should be this " "guard's own module and the vendored bundle -- anything else means a root or an exemption " "drifted from what the comments claim"
    )
    assert any(p.name == "busy_harness.js" for p in wrap_files), (
        "tests/web/busy_harness.js is not in the wrap scan — the motivating instance for #156 would " "still be invisible"
    )
    assert any(p.name == "busy_harness.js" for p in resolution_files), (
        "tests/web/busy_harness.js dropped out of the resolution scan — #190 widened RESOLUTION_ROOTS " "to cover tests/*.js like every other suffix, and this file resolves cleanly today"
    )
    assert not any(p.name == "CHANGELOG.md" for p in wrap_files), (
        "CHANGELOG.md must never be swept, wrap check included"
    )


def test_a_vendored_bundles_coincidental_identifier_is_not_a_dangling_reference():
    """Two must-fire halves: the raw file really does contain the `test_cookie_name`/ `test_cookie_value` collision, and the exemption keeps it out of *resolution* only, not out of scanning altogether."""
    vendored = VENDORED_RESOLUTION_EXEMPT_FILES[0]
    raw = vendored.read_text(encoding="utf-8")
    assert "test_cookie_name" in raw and "test_cookie_value" in raw, (
        "the vendored file no longer contains the collision this exemption exists for -- if " "re-vendored at a version that dropped these helpers, shrink the tuple back to empty"
    )
    resolution_files = {p.resolve() for p in subjects()}
    assert vendored not in resolution_files, "the vendored bundle must not be in the resolution scan"
    wrap_files = {p.resolve() for p in wrap_subjects()}
    assert vendored in wrap_files, (
        "the vendored bundle dropped out of the wrap scan too -- the exemption is about resolution " "only, never about no longer looking at this file at all"
    )


def test_every_named_test_reference_resolves():
    """A reference that names nothing spends a reader's trust and returns nothing."""
    known = declared_test_names()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}"
        for path in subjects()
        for name in resolvable_references(path)
        if name not in known
    )
    assert not dangling, (
        "these references name a test that does not exist:\n  " + "\n  ".join(dangling) +
        "\nEither the test was renamed (update the reference) or it was deleted (delete the " "reference, and ask what is guarding the line it was attached to)."
    )


def test_no_reference_is_split_across_a_line():
    """The only way one of these is used is: select it, grep it. Two of sixteen were split when this was written (`contracts.py`, `deterministic/doctor.py`) -- both real tests, both invisible to a search. Reflow the comment; never hyphenate or wrap an identifier."""
    split = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}:{path.read_text(encoding='utf-8')[:m.start()].count(chr(10)) + 1} " f"-> {m.group(0)}…"
        for path in wrap_subjects()
        for m in _WRAPPED.finditer(path.read_text(encoding="utf-8"))
    )
    assert not split, (
        "these references are split by a line wrap and cannot be found by grep:\n  " +
        "\n  ".join(split) + "\nReflow the surrounding text so the identifier is on one line."
    )


def declared_slugs() -> set[str]:
    """Every slug a decision record declares, from its `**Slug:**` line -- never from its filename, which carries an ordering number that is deliberately not the reference."""
    slugs: set[str] = set()
    for path in sorted(DECISIONS.glob("*.md")):
        if path.name == "README.md":
            continue
        m = re.search(r"^\*\*Slug:\*\*\s*`([a-z0-9-]+)`", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not m:
            raise AssertionError(
                f"{path.relative_to(REPO_ROOT).as_posix()} declares no `**Slug:**` line. A record " f"nothing can reference by name is a record only its path can reach, which is the " f"one thing docs/decisions/README.md says not to build."
            )
        slugs.add(m.group(1))
    return slugs


def test_every_decision_reference_resolves_to_a_record():
    """The slug half of the same rule: a `decision:` reference naming no record is a dangling pointer wearing a different prefix."""
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet — nothing declares a slug, so nothing can dangle")
    known = declared_slugs()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {slug}"
        for path in subjects()
        for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))
        if slug not in known
    )
    assert not dangling, (
        "these `decision:` references name no record:\n  " + "\n  ".join(dangling) +
        f"\nDeclared slugs: {sorted(known)}"
    )


def test_the_decision_records_are_reachable_from_somewhere():
    """A record nothing points at is a document nobody opens -- the signal that a narrative moved out of the code and the pointer was never left behind. The referrer set is `referrer_subjects()`, never `subjects()`: a record is not a referrer for itself or a sibling (#384). `.github/` is included since it is
    where the first record's referrer actually lives."""
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet")
    referrers = list(referrer_subjects())
    referrers.extend(sorted((REPO_ROOT / ".github").rglob("*.yml")))
    orphans = _orphan_slugs(declared_slugs(), referrers)
    assert not orphans, (
        f"these records are referenced from nowhere a reader enters through: {orphans}. Leave a " f"`decision: <slug>` line at whatever the record explains -- in CLAUDE.md, in docs/, or at " f"the call site -- or the move traded a paragraph for a file nobody opens. A pointer from "
        f"another decision record does not count: see REFERRER_EXEMPT_ROOTS."
    )


def test_no_decision_reference_is_split_across_a_line():
    """The slug half of the wrap rule (#384). A wrap *inside* the slug is worse than the test-name case: `_DECISION_REF` needs the closing backtick after it and `[a-z0-9-]+` cannot cross a newline, so the reference matches nothing at all and `test_every_decision_reference_resolves_to_a_record` never sees it --
    the record silently loses a pointer. A wrap *after* the prefix is left alone: the whitespace class spans the newline, the slug stays on one line, and resolution still works."""
    split = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}:" f"{path.read_text(encoding='utf-8')[:m.start()].count(chr(10)) + 1} -> {m.group(0)}…"
        for path in wrap_subjects()
        for m in _WRAPPED_DECISION.finditer(path.read_text(encoding="utf-8"))
    )
    assert not split, (
        "these `decision:` references are split by a line wrap, so the slug cannot be found by "
        "grep and the reference resolves to nothing at all:\n  " + "\n  ".join(split) +
        "\nReflow the surrounding text so the slug is on one line."
    )

# ---- controls: a guard that cannot fail is not a guard ----


@pytest.mark.parametrize("source, expected", [
    ("# see `test_the_persisted_contract_is_permissive_all_the_way_down` for why",
     {"test_the_persisted_contract_is_permissive_all_the_way_down"}),
    ("`tests/test_artifact_provenance.py` asserts it", {"test_artifact_provenance"}),
    ("def test_x(): pass", set()),                       # too short to be a reference
    ("no reference here at all", set()),
])
def test_the_extractor_sees_a_reference_and_only_a_reference(tmp_path, source, expected):
    p = tmp_path / "sample.py"
    p.write_text(source, encoding="utf-8")
    assert references(p) == expected


@pytest.mark.parametrize("source, blanked_name", [
    ("Split out of `test_cli_deterministic.py` by #141; the shared harness is elsewhere.",
     "test_cli_deterministic"),
    ("Split out of `test_engine_wide_reasoning_paths.py` (#72). One file rather than two.",
     "test_engine_wide_reasoning_paths"),
])
def test_the_historical_mention_idiom_is_excluded_from_resolvable_references(tmp_path, source, blanked_name):
    """The must-not-fire half: a name appearing only inside the recognized idiom, in either citation style this repository actually uses, is history, not a pointer -- `references()` still sees it."""
    p = tmp_path / "sample.py"
    p.write_text(source, encoding="utf-8")
    assert blanked_name in references(p)
    assert blanked_name not in resolvable_references(p)


def test_a_similarly_shaped_mention_that_is_not_the_idiom_still_dangles(tmp_path):
    """The must-fire complement: the exemption is the exact phrase, not any `.py`-suffixed name in a past-tense sentence -- a rename with no recognized idiom is as loud as any other broken link."""
    p = tmp_path / "sample.py"
    p.write_text(
        "Renamed from `test_cli_deterministic_and_then_some.py`, which used to hold this.\n",
        encoding="utf-8",
    )
    assert "test_cli_deterministic_and_then_some" in resolvable_references(p)


def test_the_same_dangling_name_outside_the_idiom_still_dangles(tmp_path):
    """The exemption blanks the matched span, not every occurrence of the name in the file."""
    p = tmp_path / "sample.py"
    p.write_text(
        "Split out of `test_cli_deterministic_once_more.py` by #141.\n"
        "See `test_cli_deterministic_once_more` for the original discussion.\n",
        encoding="utf-8",
    )
    assert "test_cli_deterministic_once_more" in resolvable_references(p)


def test_this_guards_own_file_is_wrap_checked_but_not_resolution_checked():
    """This module is excluded from `subjects()` by identity, not by pattern -- it necessarily contains broken-looking references by design and cannot trustworthily resolve its own examples. It stays in `wrap_subjects()`, which has no such hazard."""
    here = Path(__file__).resolve()
    assert here in RESOLUTION_EXEMPT_FILES
    assert here not in {p.resolve() for p in subjects()}
    assert here in {p.resolve() for p in wrap_subjects()}


def test_the_wrap_detector_sees_the_shape_it_was_written_for(tmp_path):
    """The positive control, and the one that matters: two real references were in exactly this shape and every other check in the repository was blind to them."""
    p = tmp_path / "wrapped.py"
    p.write_text("# … the defect class #14 exists to remove. `test_the_persisted_mirror_copies_every_\n"
                 "# constraint_it_restates` pins the general property.\n", encoding="utf-8")
    assert _WRAPPED.search(p.read_text(encoding="utf-8"))


def test_the_wrap_detector_does_not_fire_on_an_intact_reference(tmp_path):
    """The must-not-fire half: a reference ending a line *complete* is fine."""
    p = tmp_path / "intact.py"
    p.write_text("# pinned by `test_the_persisted_mirror_copies_every_constraint_it_restates`\n"
                 "# which is why it cannot drift.\n", encoding="utf-8")
    assert not _WRAPPED.search(p.read_text(encoding="utf-8"))


def _record(path: Path, slug: str, body: str) -> None:
    """One decision record, in the only two respects this guard reads it."""
    path.write_text(f"**Slug:** `{slug}`\n\n{body}\n", encoding="utf-8")


def test_a_record_that_only_quotes_its_own_slug_is_an_orphan(tmp_path):
    """#384's motivating instance, as a fixture: a record explaining where its own pointer belongs has to quote its own slug to say so, which makes this the *normal* shape of a record."""
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    record = decisions / "0002-only-itself.md"
    _record(record, "only-itself",
            "A `decision: only-itself` pointer belongs in CLAUDE.md's Extending section.")
    entry = tmp_path / "CLAUDE.md"
    entry.write_text("The Extending section, with no pointer in it.\n", encoding="utf-8")

    referrers = _referrers_among([record, entry], (decisions,))
    assert _orphan_slugs({"only-itself"}, referrers) == ["only-itself"]

    # Must-not-fire, same fixture: one pointer from a file a reader enters through is all it wants.
    entry.write_text("Kept by hand: `decision: only-itself` says why.\n", encoding="utf-8")
    assert _orphan_slugs({"only-itself"}, referrers) == []


def test_a_record_reachable_only_from_a_sibling_record_is_an_orphan(tmp_path):
    """Why the whole directory comes out of the referrer set, not only self-reference (#384): excluding self alone still passes a cluster of records citing only each other, and that unreachable island is exactly the failure the guard is named for."""
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    first, second = decisions / "0001-a.md", decisions / "0002-b.md"
    _record(first, "slug-a", "Superseded by `decision: slug-b`.")
    _record(second, "slug-b", "Supersedes `decision: slug-a`.")
    entry = tmp_path / "CLAUDE.md"
    entry.write_text("No pointer at either of them.\n", encoding="utf-8")

    referrers = _referrers_among([first, second, entry], (decisions,))
    assert _orphan_slugs({"slug-a", "slug-b"}, referrers) == ["slug-a", "slug-b"]


def test_the_records_are_resolution_checked_but_are_not_their_own_referrers():
    """A record's own references still have to resolve (it stays a *subject*), and it still cannot vouch for anybody's reachability, its own included."""
    records = sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")
    assert records, "no decision records — the exclusion asserted below would be vacuous"
    subject_paths = {p.resolve() for p in subjects()}
    referrer_paths = {p.resolve() for p in referrer_subjects()}
    for record in records:
        assert record.resolve() in subject_paths, (
            f"{record.name} dropped out of the resolution scan — its own references would stop " f"being checked, which is not what #384 asked for"
        )
        assert record.resolve() not in referrer_paths, (
            f"{record.name} is still counted as a referrer — see REFERRER_EXEMPT_ROOTS"
        )
    assert referrer_paths < subject_paths


def test_the_decision_wrap_detector_sees_a_slug_split_across_the_break(tmp_path):
    """The positive control for `_WRAPPED_DECISION`: `_DECISION_REF` cannot see this reference at all (the closing backtick is past the newline), so nothing else in this module would notice."""
    p = tmp_path / "wrapped.md"
    p.write_text("kept by hand: see `decision: elicitation-schema-\nhand-kept` for the measurement\n",
                 encoding="utf-8")
    text = p.read_text(encoding="utf-8")
    assert _WRAPPED_DECISION.search(text)
    assert _DECISION_REF.findall(text) == [], (
        "if this ever finds the slug, the resolution check covers the case and the wrap detector " "should be weighed again rather than kept out of habit"
    )


def test_the_decision_wrap_detector_leaves_the_shape_that_still_resolves_alone(tmp_path):
    """The must-not-fire half: a break before the slug keeps it whole and still greppable."""
    wrapped_prefix = tmp_path / "prefix.md"
    wrapped_prefix.write_text("see `decision:\nelicitation-schema-hand-kept` for why\n", encoding="utf-8")
    text = wrapped_prefix.read_text(encoding="utf-8")
    assert not _WRAPPED_DECISION.search(text)
    assert _DECISION_REF.findall(text) == ["elicitation-schema-hand-kept"]

    intact = tmp_path / "intact.md"
    intact.write_text("see `decision: elicitation-schema-hand-kept` for why\n", encoding="utf-8")
    assert not _WRAPPED_DECISION.search(intact.read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# SECTION 4 -- The lean ratchet (#553): `tests/lean_budget.toml` holds ceilings that only go down.
# `scripts/prose_measure.py` does the counting; this section reads the TOML, re-measures the real
# tree, and reports every breach -- never only the first, so a PR that moved three numbers at once
# sees all three rather than fixing one and being told about the next only on the following run.
# Every ceiling was set from what `main` measured on the day it landed, rounded up by at most 5% --
# see the TOML's own header for the rule and `docs/compatibility.md`, #551, #555 and #556 for what
# happened before a test could check it.
# ════════════════════════════════════════════════════════════════════════════════════════════════

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import prose_measure  # noqa: E402

LEAN_BUDGET_TOML = REPO_ROOT / "tests" / "lean_budget.toml"

# The meta-guard estate (#551): the tests that guard the repo's own self-description rather than
# its runtime. Named here, not in the TOML, per #553's own direction -- the ceiling is a number the
# TOML owns, the membership is a test diff. `test_boundaries.py`/`test_encoding.py`/
# `test_narrative_references.py` are the near-empty stubs this file's own module docstring
# explains; they still count, at the handful of lines each now carries.
ESTATE_FILES = (
    "tests/test_source_form.py",
    "tests/test_boundaries.py",
    "tests/test_encoding.py",
    "tests/test_narrative_references.py",
    "tests/test_version_sites.py",
    "tests/test_cli_flag_names.py",
    "tests/test_doc_images.py",
    "tests/test_cost_claims.py",
    "tests/test_dco_check.py",
    "tests/test_prompt_contracts.py",
    "tests/test_workflow_untrusted_output.py",
    "tests/_scan.py",
)


def _load_budget(path: Path) -> dict:
    """Parse TOML with the standard library, or with `tomli` below 3.11 -- same code reached by two
    names (`scripts/dependency_floor.py`'s `_load_toml` carries the same note at more length)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - taken on 3.9/3.10, not on the version CI lints
        import tomli as tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _largest_non_estate_module(tests):
    """The fattest ordinary test module, excluding the meta-guard estate (#551's merged
    source-scanning tier, which already has its own `[estate]` ceiling). Without this exclusion
    `tests.largest_module_max_lines` is pinned by `test_source_form.py` -- the file this exclusion
    is named for -- which would license a module nearly 3x #555's own 800-line rule for an
    ordinary test module."""
    estate_paths = {REPO_ROOT / rel for rel in ESTATE_FILES}
    candidates = [f for f in tests.files if f.path not in estate_paths]
    return max(candidates, key=lambda f: f.total, default=None)


def _lean_budget_breaches(budget: dict) -> list[str]:
    """Every ceiling `budget` names that the real tree currently exceeds, one string per breach
    naming the file (or group), the measured number and the ceiling -- see this section's own
    banner for why every breach is collected rather than returning on the first."""
    breaches = []
    src = prose_measure.measure_group(prose_measure.GROUPS["src"])
    tests = prose_measure.measure_group(prose_measure.GROUPS["tests"])

    if src.prose_share > budget["src"]["prose_share_max"]:
        breaches.append(
            f"src/ prose share is {src.prose_share:.1%}, over the "
            f"{budget['src']['prose_share_max']:.1%} ceiling"
        )
    largest_src = src.largest
    if largest_src is not None and largest_src.total > budget["src"]["largest_module_max_lines"]:
        breaches.append(
            f"{largest_src.path.relative_to(REPO_ROOT)} is {largest_src.total} lines, over the "
            f"{budget['src']['largest_module_max_lines']} ceiling"
        )

    ratio = prose_measure.code_ratio(tests, src)
    if ratio > budget["tests"]["ratio_max"]:
        breaches.append(
            f"tests:src code ratio is {ratio:.2f}x, over the {budget['tests']['ratio_max']:.2f}x ceiling"
        )
    largest_tests = _largest_non_estate_module(tests)
    if largest_tests is not None and largest_tests.total > budget["tests"]["largest_module_max_lines"]:
        breaches.append(
            f"{largest_tests.path.relative_to(REPO_ROOT)} is {largest_tests.total} lines (excluding "
            f"the meta-guard estate, which has its own [estate] ceiling), over the "
            f"{budget['tests']['largest_module_max_lines']} ceiling"
        )
    mod_len, mod_where = tests.docstring_max("module")
    if mod_len > budget["tests"]["module_docstring_max_lines"]:
        breaches.append(
            f"{mod_where}'s module docstring is {mod_len} lines, over the "
            f"{budget['tests']['module_docstring_max_lines']} ceiling"
        )
    fn_len, fn_where = tests.docstring_max("function")
    if fn_len > budget["tests"]["function_docstring_max_lines"]:
        breaches.append(
            f"{fn_where}'s docstring is {fn_len} lines, over the "
            f"{budget['tests']['function_docstring_max_lines']} ceiling"
        )

    estate_total = sum(prose_measure.line_count(REPO_ROOT / rel) for rel in ESTATE_FILES)
    if estate_total > budget["estate"]["total_max_lines"]:
        breaches.append(
            f"the meta-guard estate is {estate_total} lines, over the "
            f"{budget['estate']['total_max_lines']} ceiling"
        )

    compat_lines = prose_measure.line_count(prose_measure.COMPATIBILITY_DOC)
    if compat_lines > budget["docs"]["compatibility_max_lines"]:
        breaches.append(
            f"docs/compatibility.md is {compat_lines} lines, over the "
            f"{budget['docs']['compatibility_max_lines']} ceiling"
        )
    return breaches


def test_the_tree_stays_within_its_lean_budget():
    """#553: every ceiling in `tests/lean_budget.toml`, re-measured against the real tree. Reports
    every breach at once; see `_lean_budget_breaches`'s docstring for why."""
    breaches = _lean_budget_breaches(_load_budget(LEAN_BUDGET_TOML))
    assert not breaches, "the lean budget was exceeded:\n" + "\n".join(f"  - {b}" for b in breaches)


def test_the_lean_budget_guard_fires_on_a_scratch_copy_and_names_every_breach(tmp_path):
    """MUST-FIRE, #553's own acceptance criterion: every numeric ceiling zeroed in a scratch copy of
    the real TOML is caught, not silently passed, and each is named -- not only the first."""
    text = LEAN_BUDGET_TOML.read_text(encoding="utf-8")
    zeroed, count = re.subn(r"(?m)^(\w[\w.]*\s*=\s*)[0-9][0-9.]*\s*$", r"\g<1>0", text)
    assert count == 8, f"expected 8 numeric ceilings in the real TOML, the scratch edit zeroed {count}"
    scratch = tmp_path / "lean_budget.toml"
    scratch.write_text(zeroed, encoding="utf-8")

    breaches = _lean_budget_breaches(_load_budget(scratch))

    assert len(breaches) == 8, breaches
    joined = "\n".join(breaches)

    # The largest src/ and non-estate tests/ module names are measured here, not hardcoded: a
    # split (the ratchet's own goal, and exactly what #550 already did once to src/requivo/cli.py)
    # must not turn a passing guard red merely by moving which file holds the title.
    src = prose_measure.measure_group(prose_measure.GROUPS["src"])
    tests = prose_measure.measure_group(prose_measure.GROUPS["tests"])
    largest_src_file = src.largest
    largest_tests_file = _largest_non_estate_module(tests)
    assert largest_src_file is not None and largest_tests_file is not None
    largest_src = str(largest_src_file.path.relative_to(REPO_ROOT))
    largest_tests = str(largest_tests_file.path.relative_to(REPO_ROOT))

    for expected in (
        "src/", largest_src, "code ratio", largest_tests, "docstring", "meta-guard estate", "compatibility.md",
    ):
        assert expected in joined, f"a zeroed ceiling should have named {expected!r}: {joined}"
