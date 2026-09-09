"""Architectural boundary guards.

These tests fail loudly if the core/provider separation regresses -- the single most important
invariant of the refactor. They are static (they read source), so they hold even in an environment
where the Anthropic SDK is not installed.

Two boundaries, one arrow seen from each end
--------------------------------------------
`core/` may not reach *down* to a provider (invariant 7). A surface may not reach *past* the services
to one either: every interface is a thin caller over `services/`, so an interface that calls a
provider function itself is a second orchestration of discovery. That is what #77 found -- `cli.py`
imported `run`, `advise` and `estimate`, and the interactive `discover` branch drove two provider
calls of its own before handing the result to `DiscoveryService` for the write, while CLAUDE.md, the
README and docs/architecture.md all stated the opposite rule.

The surface half of the *provider* rule covers `cli.py`, `render/`, `web/` and `deterministic/` --
every layer outside `providers/` that touches argv, stdout or HTTP, the same reading the storage
guard below already uses for `SURFACE_TREES`. `render/` joined with #167, and the reason it had to is
the reason this sentence is worth keeping accurate: for a release it read "scoped to `cli.py`" and
was correct, while `render/terminal.py` imported `PRICING_AS_OF` and `UsageLedger` from
`providers.anthropic` and no test in this file could see it. `web/` joined with #183, on the same
argument: `web/config.py` probes for the SDK by name and `web/app.py` imported `EngineError`
(moved to `http.py` by #422, below), both legitimate per #167, and both were unguarded until this
scan set caught up with the storage guard's.
`deterministic/` joined at the same time even though it reaches no provider today -- the narrower
reading ("only the layers a violation has already been found in") is exactly the reasoning that let
`render/` go unguarded through an entire hardening effort; a layer with zero current imports is the
cheapest one to scan and the one a narrower rule would leave out again next time.

`http.py` joined by name, not by tree, with #422. It is the RequivoError-code-to-HTTP-status
classification, moved out of `web/app.py` into a framework-free top-level module so a second HTTP
surface can import it with no `[web]` extra installed -- and by this guard's own "touches argv,
stdout or HTTP" test for what counts as a surface, it is not one: no argv, no stdout, and, despite
the name, no HTTP transport either. It still imports `EngineError` from `requivo.providers.errors`,
which is exactly the reach this guard exists to watch, so it is scanned as an individually named
subject (like `cli.py`) rather than left out on the theory that its directory puts it out of scope.
See `provider_subjects()` and its `_SURFACE_PROVIDER_ALLOWLIST` entry for the fuller argument.

The **storage** half of the same defect -- a surface reaching past `SessionRepository` to
`core.persistence` -- is #76, and it is guarded now, over cli.py, `deterministic/` and `web/`. It
needed a different extractor rather than a second table: every surface writes `from requivo.core
import persistence as store`, so the import set is one entry for a file making eighteen calls, and a
name-only guard would let one reviewed line stand in for all of them. `persistence_names` resolves
the alias first and collects the attributes taken off it, which is what makes
`_SURFACE_STORAGE_ALLOWLIST` per-function and keyed by file.

`providers/` joined the same scan set with #355, and it is not the same join as `render/` (#167) or
`web/`/`deterministic/` (#183): a provider is not a surface -- it never touches argv, stdout or HTTP
-- so its inclusion here is not automatic the way theirs was. What is automatic is the property this
particular guard protects: only `services/` holds storage as an injected seam, and anything outside
it that reaches `core.persistence` directly bypasses that seam whether or not it is a UI layer.
Reviewed and found already true: `providers/anthropic/completion.py` reaches `_atomic_write` and
`ensure_store_dir` to preserve a malformed reply for a bug report (#283) -- a private,
underscore-prefixed name, crossing a module boundary, with no allowlist entry and nothing watching
for a second one. The import itself is not the defect (the alternative is a second atomic-write
implementation, which invariant 16 exists to prevent); the allowlist entries below are what
"watching for a second one" now looks like.

Where the line sits
-------------------
Invariant 7 used to read "provider-free and IO-free". The second half was never true and was never
meant to be: `persistence.py` writes sessions, `context.py` reads context cards, `contracts.py` and
`analysis.py` read the framework schema. A guard written against that wording would have to fail on
correct code, so this file encodes what the invariant *means*:

    core may read and write files; core may not talk to a provider, and may not talk to the
    *process* -- its arguments, its standard streams, its environment, or its exit.

Every entry in the `_FORBIDDEN_*` tables below carries the reason it is there, so the next person can
argue with a named line instead of deleting the file. Two things are deliberately *not* banned, and
`test_the_process_guard_allows_what_core_legitimately_does` pins both so that a later tightening
which would fail correct code goes red here first:

  - file IO, per the paragraph above;
  - `logging`, which is the library-correct way of *not* printing. Its default handler writing to
    stderr is the application's configuration, not core's.

What this guard cannot see
--------------------------
Stated rather than left to read as clean:

  - an aliased module -- `import sys as s; s.argv` resolves nowhere a static walk can follow;
  - dynamic access -- `getattr(sys, "argv")`, `importlib.import_module("anthropic")`;
  - a violation reached through a re-export in some other package.

The failure this file exists in order not to repeat (#10)
---------------------------------------------------------
`Path.glob` on a directory that does not exist returns `[]` and raises nothing, so a guard that
asserts "no offenders" over an empty scan passes green while checking nothing. This package has
already been renamed once (`product_copilot` -> `requivo`) and the guard survived it by luck. `scan`
therefore treats an unscannable or empty root as an error rather than as an answer, and
`test_the_guard_refuses_a_scan_it_could_not_make` is the positive control for that.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from _scan import list_python_files, parse_utf8, write_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "src" / "requivo" / "core"
CORE_PACKAGE = "requivo.core"

# A module whose absence means the scan is not looking at Requivo's core at all -- a moved `src/`
# layout, a renamed package, a relocated test file. One stable anchor rather than the full listing,
# so adding a module to core does not fail this file while a rename still does.
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


_parse = parse_utf8  # was its own duplicate of test_encoding.py's copy; see _scan.py (#288)


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
    """Every Python file under `root`, recursively, paired with the dotted package it lives in.

    The recursive walk and the refusal on an unscannable or empty root are `_scan.py`'s
    `list_python_files` now (#288); pairing each file with the dotted package its relative imports
    resolve against is this function's own job, specific to the import-boundary guards below.
    """
    found = list_python_files(root, label="boundary guard")
    return [(p, _package_for(p, root, package)) for p in found]


def imported_modules(path: Path, package: str) -> set[str]:
    """Every module `path` imports, as a dotted absolute name.

    Relative imports are resolved against `package`, because `from .anthropic import Client` is the
    shortest way to write the violation and the previous version of this guard skipped every
    `node.level != 0` import outright.
    """
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
    """Every place `path` talks to the process rather than to its caller.

    One string per violation, carrying the line, the construct and the reason -- a guard that says
    only "False" costs the next reader the whole search again.
    """
    out: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            # Keyed on the *call*, not on the bare name: `def render(input: str)` is a fair signature
            # and using that parameter must not read as a call to the builtin.
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


_write_tree = write_tree  # was its own duplicate of test_encoding.py's copy; see _scan.py (#288)


# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

def test_the_guard_scans_the_real_core_package():
    """Name what was scanned. Everything below this line is a negative assertion, and a negative
    assertion over an empty set is an all-clear nobody earned."""
    scanned = scan(CORE, CORE_PACKAGE)
    names = sorted(p.relative_to(CORE).as_posix() for p, _ in scanned)
    missing = [anchor for anchor in CORE_ANCHORS if anchor not in names]
    assert not missing, (
        f"the boundary guard scanned {CORE} and did not find {missing}; it is not looking at "
        f"Requivo's core. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The positive control for #10. `src/product_copilot/core` is this package's *previous* name:
    before this change both boundary tests passed green against it, because `Path.glob` on a missing
    directory returns `[]` and `assert not set()` holds. That is the whole issue in one line."""
    renamed_away = REPO_ROOT / "src" / "product_copilot" / "core"
    assert not renamed_away.exists(), "this control assumes the pre-rename path is gone"
    assert list(renamed_away.glob("*.py")) == [], "the shape being guarded against: glob returns [], not an error"
    with pytest.raises(AssertionError, match="no such directory"):
        scan(renamed_away, CORE_PACKAGE)


def test_the_guard_refuses_an_empty_directory(tmp_path):
    """The other shape of the same hole: the directory resolves, and holds nothing."""
    empty = tmp_path / "core"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        scan(empty, "somewhere.core")


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
    """Force what an encoding-less `open()` falls back to, and report whether the force actually took.

    There is only a Python-level hook to patch on some interpreters: `_bootlocale` was removed in 3.10
    and `_io` has resolved the locale encoding in C ever since, and UTF-8 mode overrides it anyway. So
    every candidate is patched and the result is then *measured* on a probe file rather than assumed.
    Measured here: the force takes on CPython 3.9 and does not on 3.10 or later. Returning False is
    the third state -- the caller must skip rather than assert, because a control that cannot fail is
    worse than no control.
    """
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
    """Every module in core carries at least one em dash, and `read_text()` with no encoding decodes
    with the *locale* codepage -- so under `LC_ALL=C`, or a DBCS Windows shell, the pre-#10 guard dies
    with `UnicodeDecodeError` instead of running.

    Asserting only that `_parse` succeeds would prove nothing: on a UTF-8 locale it succeeds with or
    without the explicit `encoding=`, which is to say the control could not fire. So the fallback is
    forced first, and where it cannot be forced this test skips *loudly* rather than passing.
    """
    if not _force_default_encoding(monkeypatch, tmp_path, "ascii"):
        pytest.skip(
            "the ambient default encoding could not be forced on this interpreter (CPython dropped "
            "_bootlocale in 3.10 and resolves the locale encoding in C, and UTF-8 mode overrides it "
            "regardless), so this control cannot fire here. UNTESTED ON THIS INTERPRETER: that _parse "
            "passes an explicit encoding rather than taking the locale's. The 3.9 leg of the CI "
            "matrix does test it."
        )
    path = tmp_path / "dashes.py"
    # Written as raw bytes so this source file stays pure ASCII while the fixture on disk does not:
    # a console whose codepage cannot represent an em dash must not be able to kill a failure report.
    path.write_bytes(b'"""Core \xe2\x80\x94 the engine."""\nimport anthropic\n')  # an em dash, in utf-8
    with pytest.raises(UnicodeDecodeError):
        path.read_text()  # what the pre-#10 guard did, meeting the locale it would meet
    assert "anthropic" in imported_modules(path, CORE_PACKAGE)  # what this guard does instead


# --------------------------------------------------------------------------------------------------
# No provider, in either direction.
# --------------------------------------------------------------------------------------------------

def test_core_never_imports_anthropic():
    """requivo.core is provider-free by construction: not one module may import the SDK, so the
    deterministic engine works with no API key and no `anthropic` installed."""
    offenders = _core_offenders({"anthropic": _PROVIDER_IMPORTS["anthropic"]})
    assert not offenders, f"requivo.core must not import anthropic; offenders: {offenders}"


def test_core_never_imports_a_provider():
    """Core must not import the provider package either -- the dependency arrow points provider->core,
    never the reverse."""
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
    """Positive control. "No offenders" also passes when the scan found nothing, so each forbidden
    shape gets a fixture that the guard must flag -- including the three relative forms the previous
    `_imports` skipped outright on `node.level != 0`."""
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
    """The must-fire cases above are only meaningful next to a must-not-fire case: a detector that
    flags everything is as useless as one that flags nothing."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IMPORTS})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)


# --------------------------------------------------------------------------------------------------
# No argv, no standard streams, no environment, no exit. The half of invariant 7 that nothing
# enforced before #10 -- core was clean by luck rather than by guard.
# --------------------------------------------------------------------------------------------------

def test_core_never_touches_the_process():
    offenders = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        found = process_violations(path, package)
        if found:
            offenders[path.relative_to(CORE).as_posix()] = found
    assert not offenders, (
        "requivo.core must not talk to the process -- its arguments, its standard streams, its "
        f"environment or its exit; offenders: {offenders}"
    )


_PROCESS_VIOLATIONS = {
    "prints.py": """
        def show(model):
            print(model)
    """,
    "prompts.py": """
        def ask():
            return input("slug? ")
    """,
    "breakpoints.py": """
        def debug():
            breakpoint()
    """,
    "reads_argv.py": """
        import sys

        def slug():
            return sys.argv[1]
    """,
    "reads_argv_by_name.py": """
        from sys import argv

        def slug():
            return argv[1]
    """,
    "writes_stdout.py": """
        import sys

        def show(text):
            sys.stdout.write(text)
    """,
    "writes_stderr.py": """
        import sys

        def warn(text):
            sys.stderr.write(text)
    """,
    "reads_stdin.py": """
        import sys

        def read():
            return sys.stdin.read()
    """,
    "exits.py": """
        import sys

        def stop():
            sys.exit(1)
    """,
    "reads_env.py": """
        import os

        def root():
            return os.environ["REQUIVO_WORKSPACE"]
    """,
    "getenvs.py": """
        import os

        def root():
            return os.getenv("REQUIVO_WORKSPACE")
    """,
    "getenv_by_name.py": """
        from os import getenv

        def root():
            return getenv("REQUIVO_WORKSPACE")
    """,
    "parses_args.py": """
        import argparse

        def parser():
            return argparse.ArgumentParser()
    """,
    "buried/deeper.py": """
        import sys

        def slug():
            return sys.argv[1]
    """,
}


def test_the_process_guard_sees_each_forbidden_construct(tmp_path):
    """Positive control, one fixture per construct, so a construct that stops being detected shows up
    as itself rather than as a quiet narrowing of the guard."""
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
    """Where the line sits, pinned. A guard that fails on correct code is deleted by the next person,
    so file IO, `logging`, `sys.version_info` and locals that read like a builtin must all pass."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_CORE})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert process_violations(path, package) == []


# --------------------------------------------------------------------------------------------------
# The other end of the same arrow: a surface reaches the provider through the services, never itself.
# --------------------------------------------------------------------------------------------------

CLI = REPO_ROOT / "src" / "requivo" / "cli.py"
CLI_PACKAGE = "requivo"
HTTP = REPO_ROOT / "src" / "requivo" / "http.py"
RENDER = REPO_ROOT / "src" / "requivo" / "render"
RENDER_PACKAGE = "requivo.render"

# What a surface may still pull out of `requivo.providers`, and why each one is a *surface* concern
# rather than an orchestration one. Everything else is an interface running a pipeline of its own,
# which is what #77 was: `run`, `advise` and `estimate` all lived in `cli.py`, and the interactive
# `discover` branch reasoned two provider calls itself before letting the service do the write.
#
# Keyed by (file, name), not by name alone, for the reason `_SURFACE_STORAGE_ALLOWLIST` is: a global
# name list lets a *second* file import a name already argued for in a first, and arrive green under
# an argument nobody made about it.
#
# `render/` is in this scan set as of #167, and the asymmetry it closes is worth stating. The guard
# watched `core/` from one end and `cli.py` from the other, so the layer with the *weakest* claim to
# a provider import -- a renderer, which turns data into strings and reaches nothing -- was the one
# layer with no guard at all. `render/terminal.py` imported `PRICING_AS_OF` and `UsageLedger` from
# `providers.anthropic` through the entire hardening effort that produced this table, and no test in
# this file could see it.
#
# Add an entry only with a reason a reader can argue with. The failure this guards is not a rewrite,
# it is one more convenient import.
_SURFACE_PROVIDER_ALLOWLIST = {
    ("cli.py", "new_client"): (
        "constructs the SDK client from the environment. Building a client is the surface's job -- it "
        "is handed to DiscoveryService, which is the layer that decides when to reason with it."
    ),
    ("cli.py", "EngineError"): (
        "an exception type, not a call. `app()` catches it (it is a RequivoError, so it surfaces "
        "without a traceback) and `_cmd_web` raises it. Importing a class the CLI never calls "
        "orchestrates nothing. The `_cmd_web` half was the weakest part of this entry and was "
        "examined rather than tidied in #135: its code, `provider_unavailable`, is published in the "
        "--json envelope, so moving it to a core error is a breaking change and not a rename. It "
        "stays, and the call site now carries the argument. It comes from `providers.errors` since "
        "#167 -- provider-neutral, SDK-free, and no longer a name only the Anthropic module has."
    ),
    ("api/app.py", "EngineError"): (
        "the identical shape as the `cli.py` entry above, one surface over (#425): an exception "
        "type, not a call, raised by `create_api()` when the `[api]` extra's own fastapi import "
        "fails, so a missing optional install reports the same `provider_unavailable` code every "
        "other optional-install refusal in this project already uses (`new_client()` for "
        "`[anthropic]`, `_cmd_web` for `[web]`) rather than a bare `ImportError` traceback. Nothing "
        "on the provider is called or constructed."
    ),
    ("web/config.py", "Anthropic"): (
        "the SDK handle, probed inside a try/except to answer one boolean -- is the SDK installed? "
        "-- that crosses to the template as `ProviderStatus.sdk_installed`, never as a client. "
        "Building a client and reasoning with it both stay behind DiscoveryService; this call site "
        "never does either."
    ),
    ("web/config.py", "credential_present"): (
        "since #334 asks the SDK's own resolution chain rather than reading env-var names itself, to "
        "answer the same boolean the entry above answers for the SDK -- is a credential visible? -- "
        "that crosses to the template as `ProviderStatus.key_present`, never the value itself. "
        "Since #334 this DOES construct a client -- `_resolve_client()` builds a transient "
        "`Anthropic()` to read the SDK's own resolution and discards it (measured by spying on "
        "`Anthropic.__init__`, #374, which is what caught this reason's stale predecessor). It makes "
        "no call, though, which is the boundary #167's rule actually turns on. Weighed the same way the "
        "`deterministic/doctor.py` entry below is: the alternative is this file keeping its own "
        "`os.getenv('ANTHROPIC_API_KEY')`, which is exactly what drifted (#332) the day "
        "`new_client()`'s own set of names widened and this file's copy did not move with it. "
        "`test_an_allowlist_reason_claiming_no_client_is_built_is_true_of_the_function_it_names` in "
        "tests/test_provider.py checks that this entry no longer makes the claim it just made."
    ),
    ("http.py", "EngineError"): (
        "an exception type, not a call, same as the cli.py entry above -- read by an isinstance "
        "check that turns it into a status code, never invoked. #422 moved this import (and the "
        "classification table it feeds) out of `web/app.py`, where an equivalent entry with the "
        "same reasoning lived until this move; `http.py` is not one of `render/`, `web/` or "
        "`deterministic/` (it touches none of argv, stdout or HTTP transport -- it is a pure "
        "function over an exception, framework-free by design), so it is scanned as an individually "
        "named subject here, the same way `cli.py` is, rather than picked up by walking a tree. The "
        "alternative -- leaving it unscanned, on the theory that a neutral top-level module like "
        "`usage.py` or `paths.py` needs no entry here -- does not hold: those two import nothing "
        "from `providers/` at all, so there is nothing for this guard to watch; `http.py` does, "
        "which is exactly the shape #167's own lesson warns is easy to leave uncovered. Scanning it "
        "costs one subject and one entry against a real, not a hypothetical, provider import."
    ),
    ("web/routes/sessions.py", "EngineError"): (
        "an exception type, not a call, and caught for a *routing* decision the HTTP boundary above "
        "cannot make: the create route knows it has already claimed a session, so a transient "
        "provider failure sends the reader to that session's page -- which carries the request and "
        "the retry button -- instead of to the 500 page, which hides both (#207). Choosing which "
        "page a reader lands on is the surface's whole job; the reasoning still happens behind "
        "DiscoveryService, and this file calls nothing on the provider. Named alongside "
        "`ProviderOutputError` (a core, not a provider, name -- out of this guard's scope) in the "
        "module-level `_PROVIDER_FAILURE` tuple since #253, because the JSON retry loop's own "
        "give-up needs the identical routing decision `EngineError` gets and used not to receive "
        "it: `routes/discovery.py` imports that tuple rather than `EngineError` itself, which is "
        "why its own former entry below is gone rather than merely renamed."
    ),
    ("deterministic/doctor.py", "current_model_name"): (
        "a read of the provider's own model id, for the row `requivo doctor` prints so a bug report "
        "is one paste (#247). It orchestrates nothing: no client is built, no call is made, and the "
        "verb answers about the *install* rather than about a session -- which is why this is not "
        "reachable through DiscoveryService, whose every method takes a slug. Weighed against "
        "#167's rule (move the neutral concept out of `providers/` rather than allowlist it) and "
        "that rule does not apply here: since #268 the value is `REQUIVO_MODEL` if set, else "
        "`os.getenv('MODEL', 'claude-sonnet-5')` -- a two-name precedence, and both the fallback "
        "name and the default in it are Anthropic's own facts, not a neutral one waiting to be "
        "relocated. The alternative -- doctor re-deriving the same precedence with its own copy -- "
        "is worse in the specific way this file exists to prevent: it would be correct until the day "
        "the precedence changed (as this very entry was, and stayed stale for one, per #364), and "
        "then quietly wrong in the verb people paste into bug reports."
    ),
    ("deterministic/doctor.py", "credential_diagnosis"): (
        "the same reasoning as `current_model_name` immediately above, for the "
        "`provider_anthropic.api_key_present`/`credential_problem` rows: the set of env-var names a "
        "credential can come from, and the SDK's own reason when a configured profile could not be "
        "loaded, are both Anthropic's own facts (`new_client()`'s `_AUTH_ENV_VARS`, "
        "`_resolve_client()`'s `problem` arm), not a neutral one waiting to be relocated out of "
        "`providers/`. #365 is the same drift #332 found one function along: `credential_present()` "
        "alone is right for a caller that only wants a yes/no, and wrong for the verb whose whole job "
        "is naming the remedy -- it read the bool and told a reader with an unloadable profile to set "
        "a variable that was never the fault. Since #334 this DOES construct a client, the same way "
        "the `web/config.py` entry above does and for the same reason (`_resolve_client()`, measured "
        "by spying on `Anthropic.__init__`, #374 -- see "
        "`test_an_allowlist_reason_claiming_no_client_is_built_is_true_of_the_function_it_names` in "
        "tests/test_provider.py) -- it makes no call, which is the boundary that actually matters "
        "here."
    ),
}

# `track_usage` was a third entry until #167, and its removal is the guard doing its job rather than
# a relaxation. The ledger it scopes was never Anthropic's -- it counts calls, tokens, cache tiers
# and latency -- so it moved to `requivo.usage` and stopped being a provider name. The stale half of
# the assertion below is what made deleting this entry mandatory instead of optional.


PROVIDER_TREES = (
    (RENDER, RENDER_PACKAGE),
    (REPO_ROOT / "src" / "requivo" / "web", "requivo.web"),
    (REPO_ROOT / "src" / "requivo" / "deterministic", "requivo.deterministic"),
    # #425: the second HTTP surface. It touches HTTP the same way `web/` does, so it is a surface by
    # this guard's own "touches argv, stdout or HTTP" test, and it is walked as a tree for the same
    # reason `web/` is -- route modules will be added under it slice by slice, and a hand-listed
    # subject would go quietly narrower with each one, exactly the trap `render/`, `web/` and
    # `deterministic/` were each added here to close.
    (REPO_ROOT / "src" / "requivo" / "api", "requivo.api"),
)


def provider_subjects() -> list[tuple[Path, str, str]]:
    """Every surface the provider guard watches, as (path, package, label). The label is the
    allowlist key.

    `cli.py` is named individually and the rest are walked, for the reason `surface_subjects()`
    walks its trees: a module added next year must arrive inside the scan set rather than beside
    it. Both helpers refuse an absent or empty subject, so a renamed package is 'could not look'
    here too (#10).

    `http.py` joined the same way as `cli.py` -- named individually, not by tree -- since #422. It is
    not itself a surface (`render/`, `web/`, `deterministic/` are the trees below because each
    touches argv, stdout or HTTP; `http.py` touches none of those, framework-free by construction)
    but it is the one place outside those trees that legitimately reaches into `requivo.providers`
    for a name (`EngineError`, read by isinstance and never called), and the allowlist entry for it
    carries the reason. See that entry in `_SURFACE_PROVIDER_ALLOWLIST` for the fuller argument for
    scanning a module this guard's own definition of "surface" would otherwise exclude.
    """
    src = REPO_ROOT / "src" / "requivo"
    subjects = [
        (subject_module(CLI), CLI_PACKAGE, "cli.py"),
        (subject_module(HTTP), CLI_PACKAGE, "http.py"),
    ]
    for root, package in PROVIDER_TREES:
        subjects.extend((p, pkg, p.relative_to(src).as_posix()) for p, pkg in scan(root, package))
    return subjects

# The marker a module import contributes instead of a name. Deliberately unspellable as an allowlist
# key: it carries dots and spaces, and every key above is a bare identifier.
_WHOLE_MODULE = "(the whole module)"


def subject_module(path: Path) -> Path:
    """`scan`, for a guard whose subject is one named module rather than a tree.

    Same rule, and it is the rule this file exists for (#10): a subject that is not there is "could
    not look", never "looked and found nothing". `cli.py` has been renamed along with its package
    once already, and a guard that read a missing file as an empty import set would have gone green
    straight through it.
    """
    if not path.is_file():
        raise AssertionError(
            f"boundary guard could not read {path}: no such file. This is 'could not look', not "
            f"'looked and found nothing' -- fix the path, never the assertion."
        )
    return path


def _crosses_providers(module: str) -> bool:
    """True if a dotted module name passes through `providers`, at any depth and under any prefix."""
    return "providers" in module.split(".")


def provider_names(path: Path, package: str) -> set[str]:
    """Every name `path` can reach inside `requivo.providers`.

    `from requivo.providers.anthropic import advise` contributes `advise`. A *module* import
    contributes a `(the whole module)` marker instead, because `import requivo.providers.anthropic`
    puts every function in that file one attribute away -- reducing it to a bare module name would
    let a three-entry allowlist launder unrestricted access to the provider.

    Relative imports are resolved against `package`, for the same reason `imported_modules` does it:
    `from .providers.anthropic import run` is the shortest way to write the violation.
    """
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            # `import requivo.providers.anthropic`, with or without an `as` alias.
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
    """#77, and #167. The rule CLAUDE.md, the README and docs/architecture.md all state is that every
    interface is a thin layer over the services and there is never a second implementation of a
    generation. It was false on the primary surface: `cli.py` imported `run`, `advise` and
    `estimate`, so the interactive `discover` path orchestrated the provider itself and would not
    inherit whatever `DiscoveryService` gained next -- the revision-zero gate, the snapshot
    discipline -- without someone remembering to add it in two places.

    `render/` joined the scan set with #167. It is not a second rule, it is the same one applied to
    the layer that had the weakest claim and no guard: `render/terminal.py` imported `PRICING_AS_OF`
    and `UsageLedger` from `providers.anthropic`, so the purest view layer in the tree named a
    vendor to print a cost line. A guard scoped to `cli.py` reads as covering the surfaces and
    covered one of them.

    Both directions are asserted, and the second is what stops this from being a negative assertion
    over an empty set: an allowlist entry naming an import that is no longer there fails too. So a
    surface that was emptied, truncated or renamed away goes red here rather than reading as one
    that reaches nothing.
    """
    reached = {(label, name)
               for path, package, label in provider_subjects()
               for name in provider_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_PROVIDER_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past the service seam to {unexpected}. Route it through "
        f"DiscoveryService, or -- if what it needs is provider-neutral -- move that name out of "
        f"`providers/` (`requivo.usage`, `providers.errors`), or add it to "
        f"_SURFACE_PROVIDER_ALLOWLIST with the reason it is a surface concern. Currently allowed: "
        f"{sorted(_SURFACE_PROVIDER_ALLOWLIST)}"
    )
    stale = sorted(set(_SURFACE_PROVIDER_ALLOWLIST) - reached)
    assert not stale, (
        f"_SURFACE_PROVIDER_ALLOWLIST still names {stale}, which no surface imports any more. "
        f"Either the guard is reading the wrong files, or the entry is stale prose -- delete it."
    )


def test_the_provider_guard_names_what_it_scanned():
    """The #10 rule, for this scan set. Everything above is a negative assertion, and `render/`,
    `web/`, `deterministic/` and `api/` are packages rather than modules -- a walk that silently
    found nothing under one of them would be an all-clear over exactly the layer #167 (render/), #183
    (web/, deterministic/) and #425 (api/) found unguarded."""
    labels = sorted(label for _, _, label in provider_subjects())
    assert "cli.py" in labels
    assert "http.py" in labels, "the provider guard did not scan http.py; it scanned " + str(labels)
    for expected in ("render/terminal.py", "web/config.py", "deterministic/sessions.py", "api/app.py"):
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
    """Positive control. "Nothing unexpected" also passes when the extractor is blind, so each shape
    of the violation gets a fixture the guard must see -- the module forms included, since those are
    the ones that would otherwise look like a single tidy name."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_PROVIDER_IMPORTS)
    missed = [path.name for path, package in scan(root, CLI_PACKAGE) if not provider_names(path, package)]
    assert not missed, f"the surface guard is blind to these: {missed}"


def test_a_whole_module_import_cannot_pass_as_a_named_surface_concern(tmp_path):
    """`import requivo.providers.anthropic` leaves `advise` one attribute away, so it must not reduce
    to something an allowlist of bare names can hold. Checked rather than assumed: the marker is the
    only thing standing between "three reviewed imports" and unrestricted access."""
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
    """The must-not-fire half. A guard that flags every interface is deleted by the next person: an
    interface is *supposed* to import the services, the renderers and the core error types."""
    root = tmp_path / "requivo"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_SURFACE})
    path, package = scan(root, CLI_PACKAGE)[0]
    assert provider_names(path, package) == set()


def test_the_surface_guard_refuses_a_subject_it_could_not_read():
    """The #10 control, one file down. `src/product_copilot/cli.py` is this package's previous name;
    reading a missing file as "imports nothing" is the same all-clear nobody earned."""
    renamed_away = REPO_ROOT / "src" / "product_copilot" / "cli.py"
    assert not renamed_away.exists(), "this control assumes the pre-rename path is gone"
    with pytest.raises(AssertionError, match="no such file"):
        subject_module(renamed_away)


# --------------------------------------------------------------------------------------------------
# The storage half of the same arrow: a surface reaches the store through SessionRepository.
# --------------------------------------------------------------------------------------------------
#
# #76, and it is #77's twin rather than a separate rule. `services/` takes both its storage and its
# reasoning as injected seams, which is what makes "a Postgres repository reuses the orchestration
# verbatim" true of that layer. It was **not** true of the surfaces: 27 call sites across `cli.py`
# and `deterministic/` reached `core.persistence` directly, so on the day a second backing exists
# the services hold and the CLI breaks. The claim was read as a property of the system and was a
# property of one layer.
#
# The target is not zero direct calls and never was. `requivo session migrate` is *about* the retired
# filesystem layout; `session export` zips a directory; `session init` prints where the session
# landed because a path is the answer the caller asked for. A CLI that talks about files is entitled
# to know about files. The target is zero *unjustified* ones, and this is where the justification is
# recorded so that the next one has to be argued for rather than merely typed.

SURFACE_MODULE = REPO_ROOT / "src" / "requivo" / "cli.py"
SURFACE_TREES = (
    (REPO_ROOT / "src" / "requivo" / "deterministic", "requivo.deterministic"),
    (REPO_ROOT / "src" / "requivo" / "web", "requivo.web"),
    # #355. Not a surface by this guard's own name -- see the module docstring for the argument --
    # but the one other tree outside `services/` capable of reaching `core.persistence` directly.
    (REPO_ROOT / "src" / "requivo" / "providers", "requivo.providers"),
    # #425: the second HTTP surface, walked for the identical reason `web/` is -- route modules
    # arrive slice by slice, and a hand-listed subject would go quietly narrower with each one.
    (REPO_ROOT / "src" / "requivo" / "api", "requivo.api"),
)
PERSISTENCE_MODULE = "requivo.core.persistence"

# Keyed by (file, name), not by name alone. A global name list would let `deterministic/model.py`
# newly call `canonical_dir` and stay green because `sessions.py` is allowed to -- which is the
# unjustified call this guard exists to catch, arriving under a name already argued for elsewhere.
_SURFACE_STORAGE_ALLOWLIST = {
    ("cli.py", "canonical_dir"): (
        "prints where a session landed, after discover and after answer. `SessionRepository` exposes "
        "no path on purpose -- a Postgres backing has none -- so there is no seam to route this "
        "through, and 'where is it?' is the answer the caller asked for."
    ),
    ("cli.py", "artifact_path"): (
        "prints the path a generated artifact was written to. Through the chokepoint rather than "
        "joined at the call site (#36): it validates a filename that came off disk, and a printed "
        "path is a disclosure like any other."
    ),
    ("cli.py", "write_artifact_file"): (
        "writes the three neutral epic exports, which are extra *views* of one already-saved "
        "artifact and deliberately untracked -- no artifact-list type, no staleness row. "
        "`repo.save_artifact` would put three rows in `artifact list` that no generator can "
        "refresh. Since #274 the export envelope itself carries a `source_revision` stamp -- "
        "provenance, not a tracked staleness flag; that stamp is data inside the file this "
        "call writes, not a second call site."
    ),
    ("cli.py", "load_model"): (
        "reads a bare `model.json` the user named on the command line. That file is not a session "
        "and has no slug, so no repository method can reach it: the reference resolver falls back "
        "to it precisely when the store has nothing."
    ),
    ("deterministic/sessions.py", "canonical_dir"): (
        "four sites, all about a directory: `session init` and `session import` report where the "
        "session landed, and `session export` zips the tree. See the cli.py entry."
    ),
    ("deterministic/sessions.py", "ensure_store_dir"): (
        "creates `.requivo/sessions/` before the import moves a session into it. On a fresh "
        "workspace `session import` is one of the calls that can bring the store root into "
        "existence, and whichever one does writes the privacy `.gitignore` (#211) -- a statement "
        "about a directory appearing, which is the same kind of fact as the `canonical_dir` entry "
        "above and has no backing-neutral form: a repository with no filesystem has no root to "
        "create."
    ),
    ("deterministic/sessions.py", "migrate_legacy"): (
        "converts a session in the retired `out/` layout into one in `.requivo/sessions/`. A "
        "statement about two filesystem layouts, which is what the verb *is*; a backing with "
        "neither has nothing to migrate."
    ),
    ("deterministic/sessions.py", "validate_slug"): (
        "checks that a directory name inside an uploaded archive is slug-shaped, before anything is "
        "extracted. Asked about a name, before any session exists to ask a repository about."
    ),
    ("deterministic/doctor.py", "scan_session_root"): (
        "the one caller that needs all three parts of *one* partition. `list_slugs` and "
        "`list_unexaminable` are two scans by design (see `FileSessionRepository.list_unexaminable`) "
        "and two scans are two instants -- a `session.json` landing between them lands in no answer "
        "at all, which is the invisible state this key exists to end."
    ),
    ("deterministic/doctor.py", "scan_lock_root"): (
        "the lock-root residue check (#180). A lock-root scan is a fact about the file backing the "
        "same way `canonical_dir` is -- a Postgres backing has no lock files to enumerate -- so "
        "there is no backing-neutral form to route it through."
    ),
    ("deterministic/artifacts.py", "artifact_path"): (
        "prints where `artifact save` put the file. See the cli.py entry."
    ),
    ("web/dependencies.py", "_slug_shape"): (
        "refuses a slug-shaped path segment at the HTTP boundary, before it reaches any service. A "
        "name rule, applied to a request, with no session in hand yet. The shape half of what used "
        "to be one `validate_slug` call (#396) -- see the entry below it for why the split."
    ),
    ("web/dependencies.py", "_refuse_new_reserved_slug"): (
        "the other half, and the reason `safe_slug` no longer calls `validate_slug`: every `{slug}` "
        "route addresses a session that must already exist, so the reserved-device-name refusal is "
        "conditional here (#372's read-time form) rather than unconditional. A private name because "
        "core owns the creation/read split and a public wrapper would be a second statement of it; "
        "no repository method can answer it, since it is a question about a *name* before any "
        "session is in hand -- the same argument the shape half above makes (#396)."
    ),
    ("web/routes/sessions.py", "validate_slug"): (
        "the creation-time half, at the route that takes a slug from a form field. Deliberately "
        "*not* the conditional pair the dependency above uses: `POST /sessions` can bring a slug "
        "into existence, so it gets the unconditional refusal #221 specifies."
    ),
    ("api/dependencies.py", "_slug_shape"): (
        "the identical call as `web/dependencies.py`'s entry above, one surface over (#425): "
        "refuses a slug-shaped path segment at the HTTP boundary before it reaches any service. "
        "Slice 1 has no creation route, so only the read-time half exists here so far -- see the "
        "sibling entry immediately below."
    ),
    ("api/dependencies.py", "_refuse_new_reserved_slug"): (
        "the other half, mirroring `web/dependencies.py`'s pair: every `{slug}` route this slice "
        "ships addresses a session that must already exist, so the reserved-device-name refusal is "
        "conditional on a name already occupying the session root (#372's read-time form). A "
        "private name because core owns the creation/read split; no repository method can answer "
        "it, since it is a question about a *name* before any session is in hand."
    ),
    ("providers/anthropic/completion.py", "_atomic_write"): (
        "writes the final malformed reply the JSON retry loop gave up on into `.requivo/debug/`, so "
        "a bug report is one paste (#283). Not a session and not routable through the repository -- "
        "`.requivo/debug/` is a human-read debugging aid with no repository method and none should "
        "exist for it, the same argument the `deterministic/sessions.py` entries above make for "
        "`.requivo/sessions/`. Reached by its private, underscore-prefixed name rather than a "
        "promoted public one (#355): the alternative was a second atomic-write implementation, which "
        "invariant 16 exists to prevent, and this is the one caller outside `core/persistence.py` "
        "that needs it."
    ),
    ("providers/anthropic/completion.py", "ensure_store_dir"): (
        "creates `.requivo/debug/` before the first failed reply is written into it, the same "
        "reasoning as the `deterministic/sessions.py` entry above -- a repository with no filesystem "
        "has no debug root to create."
    ),
    ("deterministic/sessions.py", "UnexaminableEntry"): (
        "a plain dataclass (name, error), not a call -- the identical shape as the `EngineError` "
        "entry above: importing a type orchestrates nothing. It is the vocabulary this store already "
        "speaks for 'could not examine' (`SessionRepository.list_unexaminable` already returns it "
        "across the repository seam for the canonical root), reused rather than redeclared for the "
        "*legacy* `out/` root's own scan (#411) -- which has no repository method at all, the same "
        "argument the `migrate_legacy` entry above makes: a backing with no filesystem has no "
        "legacy layout to enumerate, unreadable entries included."
    ),
}


def surface_subjects() -> list[tuple[Path, str, str]]:
    """Every surface file, as (path, package, label). The label is the allowlist key.

    `cli.py` is named individually and the other three are walked, for the reason `scan` walks core
    recursively: `deterministic/` became a package five days ago (#73) and `web/` gains route
    modules, so a guard listing files by hand would go quietly narrower with each one. Both
    helpers refuse an absent or empty subject, so a renamed package is 'could not look' here too.
    """
    src = REPO_ROOT / "src" / "requivo"
    subjects = [(subject_module(SURFACE_MODULE), "requivo", "cli.py")]
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
    """Every name `path` can reach inside `core.persistence`.

    Import names alone are not enough here and that is the whole difficulty: every surface writes
    `from requivo.core import persistence as store`, so the import set is one entry -- the module --
    for a file making eighteen different calls. The provider guard can stop at imports because a
    whole-module import there is itself the violation; here it is the *idiom*, and reducing it to a
    single name would let one allowlist entry launder unrestricted access to the store.

    So the module aliases are resolved first and then every attribute taken off one is collected,
    which is what makes the allowlist per-function. A symbol imported by name
    (`from requivo.core.persistence import validate_slug`) contributes itself directly.

    Not seen, and stated rather than left to read as clean: an alias rebound at runtime, a name
    reached through `getattr`, and a re-export of a persistence function from some other module.
    Each is reachable and none is the shape this catches, which is one more convenient call.
    """
    tree = _parse(path)
    aliases: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PERSISTENCE_MODULE:
                    # `import requivo.core.persistence as store` binds the alias; without `as` it
                    # binds `requivo`, and the call is written as the full dotted chain instead.
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            module = f"{base}.{node.module}" if base and node.module else (node.module or base)
            if module == PERSISTENCE_MODULE:
                names.update(alias.name for alias in node.names)
            elif module == "requivo.core":
                # `from requivo.core import persistence as store` -- the idiom in every surface.
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
    """#76. Both directions asserted, for the reason the provider guard gives: "nothing unexpected"
    also passes over an empty set, so an allowlist entry naming a call that is no longer made fails
    too. A surface that was emptied, renamed away or split into a package goes red here rather than
    reading as one that reaches nothing.

    A stale entry is not bookkeeping. Every line above is a claim that a direct call is justified,
    and a claim about a call site that no longer exists is prose nobody can check.
    """
    reached = {(label, name)
               for path, package, label in surface_subjects()
               for name in persistence_names(path, package)}
    unexpected = sorted(reached - set(_SURFACE_STORAGE_ALLOWLIST))
    assert not unexpected, (
        f"a surface reaches past SessionRepository to core.persistence: {unexpected}. Route it "
        f"through the repository if an equivalent exists (`exists`, `read_meta`, `lock`, "
        f"`list_slugs`, `load_model`, `save_artifact`, …), or add it to "
        f"_SURFACE_STORAGE_ALLOWLIST with the reason no backing-neutral form is possible."
    )
    stale = sorted(set(_SURFACE_STORAGE_ALLOWLIST) - reached)
    assert not stale, (
        f"_SURFACE_STORAGE_ALLOWLIST still names {stale}, which the surfaces no longer reach. "
        f"Either the guard is reading the wrong files, or the entry is prose about a call site that "
        f"is gone -- delete it."
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
    """Positive control, and the one that matters most: the extractor has to resolve an alias before
    it can see anything at all, so a blind version of it returns the empty set for every file here
    and the real test above passes green over nothing."""
    root = tmp_path / "requivo"
    _write_tree(root, _SURFACE_STORAGE_IMPORTS)
    missed = [path.name for path, package in scan(root, "requivo")
              if "canonical_dir" not in persistence_names(path, package)]
    assert not missed, f"the storage guard is blind to these: {missed}"


def test_the_storage_guard_separates_two_calls_behind_one_import(tmp_path):
    """The property a name-only extractor does not have. One import, two functions, two allowlist
    keys -- otherwise a single reviewed entry stands in for every call the module makes."""
    root = tmp_path / "requivo"
    _write_tree(root, {"two.py": (
        "from requivo.core import persistence as store\n"
        "store.canonical_dir('s')\n"
        "store.session_lock('s')\n"
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
    """The must-not-fire half. A surface is *supposed* to call the services and the repository, and
    a local variable that happens to be called `store` is not an import of the module."""
    root = tmp_path / "requivo"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_STORAGE_SURFACE})
    path, package = scan(root, "requivo")[0]
    assert persistence_names(path, package) == set()


def test_the_storage_guard_names_what_it_scanned():
    """The #10 rule, for this scan set. Everything above is a negative assertion, and `deterministic/`
    is a package rather than a module as of #73 -- a walk that silently found nothing under it would
    be an all-clear over the surface with the most direct calls in the repository.

    `providers/anthropic/completion.py` is here since #355: before that fix `SURFACE_TREES` held only
    `deterministic/` and `web/`, so this assertion is what would have gone red the moment `providers/`
    dropped out of the tuple above -- the same "could not look" the module docstring points at,
    applied to one directory this scan set used to skip entirely. `api/dependencies.py` joined with
    #425, the identical reasoning one surface later."""
    labels = sorted(label for _, _, label in surface_subjects())
    assert "cli.py" in labels
    for expected in (
        "deterministic/sessions.py", "deterministic/doctor.py", "web/dependencies.py",
        "providers/anthropic/completion.py", "api/dependencies.py",
    ):
        assert expected in labels, f"the storage guard did not scan {expected}; it scanned {labels}"
