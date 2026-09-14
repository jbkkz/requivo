"""The live probe against this checkout's own CLI, and #174/invariant 16: the module that reads
the most foreign content stays stdlib-only and survives a console that cannot encode what it read.

Split out of `test_plugin_cli_drift.py` (#555) once that file grew past the module ceiling.
"""
import ast
import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plugin_cli_drift as drift  # noqa: E402  - for monkeypatching a module global
from plugin_cli_drift import (  # noqa: E402
    DRIFT,
    RESOLVED,
    Surface,
    cli_surface,
    compare,
    invocation_sources,
    referenced_invocations,
)

PLUGIN_ROOT = drift.PLUGIN_ROOT


def plugin_invocations():
    """Everything the real plugin executes: every skill plus the shared preflight (#542)."""
    return referenced_invocations(invocation_sources(PLUGIN_ROOT).paths)


RELEASED = Surface(version="1.0.1", verbs={
    "status": None,
    "doctor": None,
    "model": {"apply", "show", "validate", "diff"},
    "session": {"init", "show", "list", "export", "import", "verify", "migrate"},
    "artifact": {"save", "show", "list"},
})


# -- the probe --------------------------------------------------------------------


def test_the_probe_returns_none_rather_than_an_empty_surface_when_it_cannot_run():
    missing = str(ROOT / "no" / "such" / "python")
    assert cli_surface(missing) is None


def test_this_checkouts_own_cli_introspects():
    """The other half of the probe: it has to actually work somewhere, or the test above passes for a
    reason that has nothing to do with the code."""
    surface = cli_surface(sys.executable)
    assert surface is not None, "could not introspect this checkout's own CLI"
    assert "model" in surface.verbs and surface.verbs["model"], "expected `requivo model` to have subcommands"
    assert surface.verbs["status"] is None, "expected `requivo status` to take no subcommands"


def test_the_plugin_resolves_against_this_checkouts_cli():
    """Offline end-to-end, on the real files. This is the in-tree half of #96's question and it is what
    `tests/test_plugin.py` already asserts at the top level; asserting it here too keeps the drift
    script honest about a comparison whose answer we independently know."""
    surface = cli_surface(sys.executable)
    assert surface is not None
    report = compare(plugin_invocations(), tree=surface, released=surface)
    assert report.state == RESOLVED, [f"{f.invocation}: {f.reason}" for f in report.findings]


@pytest.mark.skipif(
    not os.environ.get("REQUIVO_RELEASED_PYTHON"),
    reason="needs a released Requivo already installed elsewhere: set REQUIVO_RELEASED_PYTHON to its "
           "interpreter. Not run by default because it needs network access to have happened. The CI "
           "leg in .github/workflows/plugin-validate.yml provisions one and runs the script directly.")
def test_the_plugin_resolves_against_a_released_cli_when_one_is_provisioned():
    released = cli_surface(os.environ["REQUIVO_RELEASED_PYTHON"])
    tree = cli_surface(sys.executable)
    assert tree is not None
    report = compare(plugin_invocations(), tree=tree, released=released)
    assert report.state != DRIFT, [f"{f.invocation}: {f.reason}" for f in report.findings]


# -- #174: invariant 16, for the file that reads the most foreign content -----------


def test_the_module_stays_stdlib_only():
    """The released CLI is probed in a SEPARATE interpreter, so this module never needs `requivo`
    importable to run the comparison, and must be runnable even when the tree-side install failed --
    exactly the case this script exists to report as could-not-look rather than crash on (#174).
    This is why `_harden_streams()` is not `golden_lib.configure_output()`: that would reach
    `requivo.streams` and turn a missing install into an unhandled `ModuleNotFoundError` at import."""
    source = Path(drift.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    banned = modules & {"requivo", "golden_lib"}
    assert not banned, f"plugin_cli_drift.py must stay stdlib-only; found imports of {banned}"


@pytest.fixture
def ascii_console(monkeypatch):
    """Substitute stdout and stderr with real ASCII-strict encoders, and hand back stdout's bytes.

    `PYTHONIOENCODING=ascii` plus real `io.TextIOWrapper` objects reach a genuine strict encoder on
    every platform in the matrix, not only a Windows leg -- the same mechanism
    `tests/test_golden_readout.py` and `tests/test_encoding.py` use for this exact invariant."""
    def install() -> io.BytesIO:
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
        monkeypatch.setattr(sys, "stderr",
                            io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
        return raw
    return install


def _plugin_with_a_non_ascii_skill_name(tmp_path):
    """A plugin whose skill directory name is non-ASCII -- a fork controls it, per the #176 note on
    `_label` -- and which names a verb no released CLI has, so `compare()` produces a real `Finding`
    whose `sources` carries that directory name straight into `_show`'s bare `print`. This is the
    manifest path the exposure is actually in, not a literal in this file: `INVOCATION_RE`'s
    `re.ASCII` already refuses a non-ASCII VERB (`test_a_non_ascii_word_after_requivo_is_not_captured_at_all`),
    so the directory name is the runtime value this issue is about."""
    skill = tmp_path / "skills" / "brïef"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Run `requivo zzzznotarealverb foo`.\n", encoding="utf-8")
    return tmp_path


def test_a_strict_console_reports_a_real_finding_as_could_not_look_when_streams_are_not_hardened(
        tmp_path, monkeypatch, ascii_console):
    """must fire. `main()`'s own blanket `except Exception` already turns an unhandled
    `UnicodeEncodeError` into could-not-look rather than letting it escape, so disabling
    `_harden_streams()` surfaces as exactly the ordering bug #174 is about: a real drift Finding is
    computed, the crash happens while PRINTING it, and the exit code describes the crash (3) instead
    of the walk that already found the drift (1) -- a silent substitution, not a raised exception."""
    plugin = _plugin_with_a_non_ascii_skill_name(tmp_path)
    ascii_console()
    monkeypatch.setattr(drift, "_harden_streams", lambda: None)
    code = drift.main(["--released-python", sys.executable, "--tree-python", sys.executable,
                        "--plugin", str(plugin)])
    assert code == 3, f"expected the crash to masquerade as could-not-look, got {code}"


def test_a_plugin_drift_run_survives_a_console_that_cannot_encode_a_skill_directory_name(
        tmp_path, ascii_console):
    """must not fire. With `_harden_streams()` doing its job, the same real finding is reported as
    drift -- not swallowed by the crash the hardening prevents -- and the non-ASCII directory name
    reaches the reader as a visible escape rather than a hole, the same reasoning
    `test_a_harness_script_survives_a_console_that_cannot_encode_its_output` uses for the golden
    harness."""
    plugin = _plugin_with_a_non_ascii_skill_name(tmp_path)
    raw = ascii_console()
    code = drift.main(["--released-python", sys.executable, "--tree-python", sys.executable,
                        "--plugin", str(plugin)])
    assert code == 1, "expected the real finding (drift) to survive"
    sys.stdout.flush()
    out = raw.getvalue()
    assert b"\\u" in out or b"\\x" in out, out
    assert sys.stdout.errors == "backslashreplace", sys.stdout.errors


def test_harden_streams_names_a_stream_it_could_not_reach(monkeypatch):
    """The third state. A stream substituted with something that has no `reconfigure()` -- a
    `StringIO`, or an SDK-wrapped object -- would otherwise crash later with nobody told it was never
    hardened. Named on stderr rather than swallowed, mirroring `requivo.streams.configure_stream`'s
    own third state for the product (`describe_stream`'s `will_crash` / `could-not` split)."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    drift._harden_streams()
    written = sys.stderr.getvalue()
    assert "stdout" in written, written
    assert "stderr" in written, written


def test_note_could_not_harden_survives_a_console_that_cannot_encode_its_own_reason(monkeypatch):
    """must not fire (silently). `reason` is composed from an exception's own `str()`, so it can
    itself carry a character stderr cannot encode -- the exact class this file is about, reproduced
    in the code that is supposed to REPORT it. Without the fallback this writes nothing: a reader
    cannot tell "both streams hardened fine" from "hardening and its own failure both failed
    silently" (#174, auditor-found)."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
    drift._note_could_not_harden("stdout", "café could not be represented")
    sys.stderr.flush()
    out = raw.getvalue()
    assert out, "the note went missing rather than surviving a reason it cannot encode"
    assert b"\\u" in out or b"\\x" in out, out
