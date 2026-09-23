"""The plugin's CLI invocations resolved against a released Requivo (#96), the walk's third state (#139),
untrusted text in the printed report (#176, #555) and the console it survives (#174)."""
import ast
import io
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plugin_cli_drift as drift  # noqa: E402  - for monkeypatching a module global
from plugin_cli_drift import (  # noqa: E402
    COULD_NOT_LOOK,
    DRIFT,
    INVOCATION_RE,
    PLUGIN_ROOT,
    RESOLVED,
    Surface,
    _label,
    _log_safe,
    _one_line,
    cli_surface,
    compare,
    invocation_sources,
    main,
    parse_surface,
    referenced_invocations,
    tree_typos,
)

README = PLUGIN_ROOT / "README.md"
NO_SUCH_PYTHON = str(ROOT / "no" / "such" / "python")

# A stand-in for a released CLI.
RELEASED = Surface(version="1.0.1", verbs={
    "status": None,
    "doctor": None,
    "model": {"apply", "show", "validate", "diff"},
    "session": {"init", "show", "list", "export", "import", "verify", "migrate"},
    "artifact": {"save", "show", "list"},
})


def plugin_invocations():
    """Everything the real plugin executes: every skill plus the shared preflight (#542)."""
    return referenced_invocations(invocation_sources(PLUGIN_ROOT).paths)


def _tree(**overrides):
    verbs = dict(RELEASED.verbs)
    verbs.update(overrides)
    return Surface(version="1.1.0.dev0", verbs=verbs)


def _this_tree():
    tree = cli_surface(sys.executable)
    assert tree is not None, "could not introspect this checkout's own CLI"
    return tree


def _skill(root, name, text):
    """A plugin skill under `root/skills/<name>`; returns the directory."""
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def _extracted(root):
    return referenced_invocations(invocation_sources(root).paths)


def _run_main(plugin, *extra):
    return main(["--released-python", sys.executable, "--plugin", str(plugin), *extra])


def _make_unreadable(directory):
    """`chmod 000` the directory, or skip saying exactly what went untested on this run."""
    if os.name == "nt":
        pytest.skip("POSIX directory modes do not bite on Windows. UNTESTED HERE: that a directory the walk "
                    "cannot descend into maps to could-not-look. Every other platform runs it.")
    directory.chmod(0o000)
    try:
        (directory / "SKILL.md").stat()
    except PermissionError:
        return
    except OSError:
        pass
    directory.chmod(0o755)
    pytest.skip("this user can descend into a 0o000 directory, so the case cannot be staged here. UNTESTED ON "
                "THIS RUN: that a directory the walk cannot descend into maps to could-not-look.")


def _stage_partly_readable(tmp_path):
    """The v1.1.0 audit's fixture: three files, one of them behind a directory mode."""
    _skill(tmp_path, "visible", "Run `requivo status <slug>`.")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    return _skill(tmp_path, "hidden", "Fix it with `requivo model rebase <slug>`.")


# -- extraction -------------------------------------------------------------------


def test_referenced_invocations_reads_the_real_skills_and_finds_the_two_level_calls():
    """A positive control on the extractor: Claude reasons and the deterministic CLI applies."""
    found = plugin_invocations()
    assert found, "no invocations extracted -- this test would otherwise pass by having nothing to check"
    for expected in [("model", "apply"), ("model", "validate"), ("session", "init"),
                     ("artifact", "save"), ("status", None), ("doctor", None)]:
        assert expected in found, f"extractor missed {expected!r}; it found {sorted(found)}"
    for inv, sources in found.items():
        assert sources, f"{inv!r} names no source file"


def test_the_shared_preflight_is_walked_and_not_only_the_skills():
    """`REASONING.md` holds the preflight every skill runs before its first `requivo` call."""
    sources = invocation_sources(PLUGIN_ROOT)
    names = [p.name for p in sources.paths]
    assert "REASONING.md" in names, f"the preflight is not walked; walked {names}"
    assert names.count("SKILL.md") == 10, f"expected ten skills, walked {names}"  # #542, #539, #545, #543
    assert sources.unreadable == [], sources.unreadable
    from_preflight = referenced_invocations([PLUGIN_ROOT / "REASONING.md"])
    assert ("doctor", None) in from_preflight, f"the preflight must name the probe it runs; extracted {sorted(from_preflight)}"


def test_a_flag_or_a_placeholder_is_not_read_as_a_subcommand(tmp_path):
    _skill(tmp_path, "demo", "Run `requivo doctor --json` first.\nThen `requivo status <slug> --json` to read it "
                             "back.\nApply with `requivo model apply <slug> - --expected-revision N`.\n")
    found = _extracted(tmp_path)
    assert {("doctor", None), ("status", None), ("model", "apply")} <= set(found)
    assert not any(sub in {"--json", "<slug>"} for _, sub in found)
    assert found[("model", "apply")] == ["demo"]


def test_a_non_ascii_word_after_requivo_is_not_captured_at_all(tmp_path):
    """Without `re.ASCII` a Unicode word after `requivo` is captured and then printed."""
    skill = _skill(tmp_path, "prose", "Le CLI requivo ecrit la session, et requivo ecrase le modele.")
    assert {verb for verb, _ in _extracted(tmp_path)} == {"ecrit", "ecrase"}  # the ASCII control is captured
    (skill / "SKILL.md").write_text("Le CLI requivo écrit la session.", encoding="utf-8")
    found = _extracted(tmp_path)
    assert found == {}, f"a non-ASCII token was captured and would be printed: {found}"


# -- comparison: the three states -------------------------------------------------


def test_a_plugin_that_resolves_is_resolved():
    referenced = {("status", None): ["status"], ("model", "apply"): ["answer"]}
    report = compare(referenced, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report
    assert report.findings == []
    assert report.checked == 2


@pytest.mark.parametrize(("referenced", "tree", "released", "invocation", "reason"), [
    ({("epic", None): ["brief"]}, _tree(epic=None), RELEASED, "requivo epic", ""),
    ({("model", "rebase"): ["answer"]}, _tree(model={"apply", "show", "validate", "diff", "rebase"}), RELEASED,
     "requivo model rebase", ""),
    ({("model", "apply"): ["answer"]}, _tree(), Surface(version="1.0.1", verbs=dict(RELEASED.verbs, model=None)),
     "requivo model apply", "takes no subcommands"),
], ids=["a-verb-the-release-lacks", "a-subcommand-the-release-lacks", "a-release-that-dropped-the-subcommands"])
def test_a_verb_the_release_does_not_have_is_drift(referenced, tree, released, invocation, reason):
    """The third row is the case a released-side classifier cannot see."""
    report = compare(referenced, tree=tree, released=released)
    assert report.state == DRIFT, report
    assert [f.invocation for f in report.findings] == [invocation]
    assert report.findings[0].sources == list(referenced.values())[0]
    assert reason in report.findings[0].reason


def test_a_bare_word_the_tree_does_not_call_a_subcommand_is_an_argument_not_drift():
    """The false-positive guard. `status` takes no subcommands in the tree."""
    report = compare({("status", "ready"): ["status"]}, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report


@pytest.mark.parametrize(("referenced", "tree", "released", "detail"), [
    ({("status", None): ["status"]}, _tree(), None, ""),
    ({("status", None): ["status"], ("model", "apply"): ["answer"]}, _tree(), Surface(version="?", verbs={}), ""),
    ({("status", None): ["status"]}, None, RELEASED, "tree"),
    ({}, _tree(), RELEASED, ""),
], ids=["unreachable-release", "empty-released-surface", "unreadable-tree", "no-invocations-at-all"])
def test_an_unreachable_release_is_could_not_look_and_is_neither_of_the_other_two(referenced, tree, released, detail):
    """A CLI with zero verbs is not a measurement, and an empty extraction is the shape a broken skills path produces."""
    report = compare(referenced, tree=tree, released=released)
    assert report.state == COULD_NOT_LOOK, report
    assert report.state != RESOLVED and report.state != DRIFT
    assert report.detail, "could-not-look must say what it could not do"
    assert detail in report.detail.lower()
    assert report.checked == 0 and report.findings == []


# -- the in-tree half: a misspelled subcommand, and a verb that exists nowhere ------


def test_the_real_skills_name_no_subcommand_this_checkout_does_not_have():
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- the silence below would be the harness, not the skills"
    assert tree_typos(referenced, _this_tree()) == []


@pytest.mark.parametrize(("text", "invocations"), [
    ("Fix it up with `requivo model rebase <slug>` afterwards.", ["requivo model rebase"]),
    ("Then requivo status reports whether it is ready.", []),
], ids=["a-misspelled-subcommand-is-caught", "a-word-after-a-verb-with-no-group-is-left-alone"])
def test_a_misspelled_subcommand_is_caught_rather_than_dropped(tmp_path, text, invocations):
    """The gap `compare()` deliberately leaves, and its other side: `status` has no subcommand group."""
    _skill(tmp_path, "typo", text)
    findings = tree_typos(_extracted(tmp_path), _this_tree())
    assert [f.invocation for f in findings] == invocations, findings
    if invocations:
        assert findings[0].sources == ["typo"]
        assert "apply" in findings[0].reason, "the finding must name what the verb does offer"


def test_every_verb_the_plugin_names_exists_in_this_checkout():
    """The first-token counterpart of `tree_typos`, and it covers a file the existing gate does not."""
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- a silent pass would be the harness, not the plugin"
    unknown = sorted({verb for verb, _ in referenced if verb not in _this_tree().verbs})
    assert not unknown, f"the plugin names {unknown}, which this checkout's CLI does not have; prose reads as a command"


def test_the_phantom_verb_guard_fires_on_prose(tmp_path):
    """The must-fire half of the case above."""
    (tmp_path / "REASONING.md").write_text("Note that requivo requires an API key for provider verbs.", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    referenced = _extracted(tmp_path)
    unknown = sorted({verb for verb, _ in referenced if verb not in _this_tree().verbs})
    assert unknown == ["requires"], f"expected the phantom verb to be caught; got {referenced}"


# -- the entry point ---------------------------------------------------------------


def test_main_resolves_the_real_plugin_against_this_checkout():
    assert main(["--released-python", sys.executable]) == 0


def test_the_plugin_resolves_against_this_checkouts_cli():
    """Offline end-to-end, on the real files: the in-tree half of #96's question."""
    surface = _this_tree()
    report = compare(plugin_invocations(), tree=surface, released=surface)
    assert report.state == RESOLVED, [f"{f.invocation}: {f.reason}" for f in report.findings]


def test_main_flags_a_subcommand_that_exists_nowhere_rather_than_reporting_resolved(tmp_path, capsys):
    """`compare()` deliberately drops a bare word the tree does not call a subcommand; `main` must not."""
    _skill(tmp_path, "typo", "Fix it with `requivo model rebase <slug>`.")
    code = _run_main(tmp_path)
    out = capsys.readouterr().out
    assert code == 1, out
    assert "requivo model rebase" in out, out
    assert "state         : drift" in out, out
    assert "resolves against the released CLI" not in out, out


def test_main_reports_could_not_look_rather_than_a_verdict_when_the_release_is_unreachable(capsys):
    code = main(["--released-python", NO_SUCH_PYTHON])
    out = capsys.readouterr().out
    assert code == 3, out
    assert "could-not-look" in out and "not a clean result" in out


def test_an_unreadable_skill_file_is_could_not_look_not_drift(tmp_path, capsys):
    """An unhandled exception exits 1, and 1 is the drift code."""
    target = _skill(tmp_path, "broken", "requivo status") / "SKILL.md"
    target.chmod(0o000)
    if os.access(target, os.R_OK):        # root, or a filesystem that ignores the mode
        target.chmod(0o644)
        pytest.skip("this user can read a 0o000 file. UNTESTED ON THIS RUN: that an unreadable skill maps to could-not-look.")
    try:
        code = _run_main(tmp_path)
        captured = capsys.readouterr()
    finally:
        target.chmod(0o644)
    assert code == 3, captured
    assert "not evidence of drift" in captured.err, captured.err


# -- the probe --------------------------------------------------------------------


def test_the_probe_returns_none_rather_than_an_empty_surface_when_it_cannot_run():
    assert cli_surface(NO_SUCH_PYTHON) is None


def test_this_checkouts_own_cli_introspects():
    """The other half of the probe: it has to actually work somewhere."""
    surface = _this_tree()
    assert "model" in surface.verbs and surface.verbs["model"], "expected `requivo model` to have subcommands"
    assert surface.verbs["status"] is None, "expected `requivo status` to take no subcommands"


@pytest.mark.skipif(not os.environ.get("REQUIVO_RELEASED_PYTHON"),
                    reason="needs a released Requivo installed elsewhere (REQUIVO_RELEASED_PYTHON); the CI leg in "
                           ".github/workflows/plugin-validate.yml provisions one and runs the script directly.")
def test_the_plugin_resolves_against_a_released_cli_when_one_is_provisioned():
    released = cli_surface(os.environ["REQUIVO_RELEASED_PYTHON"])
    report = compare(plugin_invocations(), tree=_this_tree(), released=released)
    assert report.state != DRIFT, [f"{f.invocation}: {f.reason}" for f in report.findings]


# -- the plugin README: the one page a stranger types by hand (#95, #138) --------------------
# Deliberately NOT added to `invocation_sources()`'s walked set (#96).

_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def readme_invocations(path):
    """Every `requivo <verb> [<word>]` a markdown page names *in code*."""
    found = {}
    for span in _CODE.finditer(path.read_text(encoding="utf-8")):
        for verb, token in INVOCATION_RE.findall(span.group(0)):
            found.setdefault((verb, token or None), [path.name])
    return found


def test_the_plugin_readme_names_only_verbs_this_checkout_has():
    """The gap #138 filed, closed at the narrowest width that closes it."""
    tree = _this_tree()
    named = readme_invocations(README)
    assert named, f"no `requivo ...` invocation was read out of {README.name}; a silent pass here would be the reader"
    unknown = sorted({verb for verb, _ in named if verb not in tree.verbs})
    assert not unknown, f"{README.name} names {unknown}, which this CLI does not have; a stranger types this page by hand"
    typos = tree_typos(named, tree)
    assert not typos, [f"{f.invocation}: {f.reason}" for f in typos]


def test_the_readme_verb_guard_fires_on_a_verb_and_on_a_subcommand(tmp_path):
    """The must-fire half: the case above is a negative assertion over a page that is currently correct."""
    page = tmp_path / "README.md"
    page.write_text("Run `requivo estimates <slug>`, then `requivo model rebase <slug>`.", encoding="utf-8")
    tree = _this_tree()
    named = readme_invocations(page)
    assert sorted(v for v, _ in named if v not in tree.verbs) == ["estimates"], named
    assert [f.invocation for f in tree_typos(named, tree)] == ["requivo model rebase"], named


def test_the_readme_reader_sees_code_and_never_prose(tmp_path):
    """The answer to #138's open question, asserted rather than argued."""
    page = tmp_path / "README.md"
    page.write_text("Note that requivo requires an API key for the optional provider mode.\n"
                    "A session created with requivo discover lands under your workspace.\n\n"
                    "Run `requivo doctor` first.\n\n```\nrequivo session list\n```\n", encoding="utf-8")
    assert set(readme_invocations(page)) == {("doctor", None), ("session", "list")}, readme_invocations(page)


# -- the walk: a plugin tree this process can only partly read (#139) ---------------


def test_the_walk_names_the_skill_directory_it_could_not_descend_into(tmp_path):
    """The unit half: the walk itself has to carry the third state, or nothing downstream can report it."""
    hidden = _stage_partly_readable(tmp_path)
    _make_unreadable(hidden)
    try:
        sources = invocation_sources(tmp_path)
    finally:
        hidden.chmod(0o755)
    assert sorted(p.name for p in sources.paths) == ["REASONING.md", "SKILL.md"]
    assert len(sources.unreadable) == 1, sources.unreadable
    assert str(hidden / "SKILL.md") in sources.unreadable[0], sources.unreadable


def test_the_walk_reports_nothing_unreadable_when_it_could_read_everything(tmp_path):
    """The must-not-fire half of the unit above, on the same fixture."""
    _stage_partly_readable(tmp_path)
    sources = invocation_sources(tmp_path)
    assert len(sources.paths) == 3, sources.paths
    assert sources.unreadable == [], sources.unreadable


@pytest.mark.parametrize("stage", ["a-stray-file-and-an-empty-directory", "skills-is-a-regular-file"])
def test_a_stray_file_in_the_skills_directory_is_absent_and_not_could_not_look(tmp_path, stage):
    """The other side of the same three-way split, at the entry and at the directory level."""
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    if stage == "skills-is-a-regular-file":
        (tmp_path / "skills").write_text("not a directory", encoding="utf-8")
        expected = ["REASONING.md"]
    else:
        _skill(tmp_path, "real", "Run `requivo status <slug>`.")
        (tmp_path / "skills" / "README.md").write_text("not a skill", encoding="utf-8")
        (tmp_path / "skills" / "empty").mkdir()
        expected = ["REASONING.md", "SKILL.md"]
    sources = invocation_sources(tmp_path)
    assert sorted(p.name for p in sources.paths) == expected, sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_the_skills_directory_itself_being_unreadable_is_could_not_look(tmp_path):
    """The `iterdir()` arm, which the cases above never reach; the preflight outside `skills/` is still walked."""
    skills = _skill(tmp_path, "hidden", "Run `requivo status <slug>`.").parent
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    _make_unreadable(skills)
    try:
        sources = invocation_sources(tmp_path)
    finally:
        skills.chmod(0o755)
    assert [p.name for p in sources.paths] == ["REASONING.md"], sources.paths
    assert len(sources.unreadable) == 1 and str(skills) in sources.unreadable[0], sources.unreadable


def test_the_reason_names_the_unreadable_path_when_nothing_could_be_extracted(tmp_path, capsys):
    """Could-not-look for the right reason, and the cause printed before the consequence."""
    only = _skill(tmp_path, "only", "Run `requivo status <slug>`.")
    _make_unreadable(only)
    try:
        code = _run_main(tmp_path)
        out = capsys.readouterr().out
    finally:
        only.chmod(0o755)
    assert code == 3, out
    assert "state         : could-not-look" in out, out
    assert str(only / "SKILL.md") in out, out
    assert out.index("could not be read") < out.index("no `requivo` invocations were found"), out


def test_main_reports_could_not_look_when_it_could_only_walk_part_of_the_plugin(tmp_path, capsys):
    """The defect (#139), staged as the v1.1.0 release audit found it; the verdict names the path it could not read."""
    hidden = _stage_partly_readable(tmp_path)
    _make_unreadable(hidden)
    try:
        code = _run_main(tmp_path)
        out = capsys.readouterr().out
    finally:
        hidden.chmod(0o755)
    assert code == 3, out
    assert "state         : could-not-look" in out, out
    assert "resolves against the released CLI" not in out, out
    assert str(hidden / "SKILL.md") in out, out


def test_the_same_plugin_read_whole_is_a_verdict_and_reports_nothing_unreadable(tmp_path, capsys):
    """The must-not-fire half, on the identical fixture with the mode change as the only difference."""
    _stage_partly_readable(tmp_path)
    code = _run_main(tmp_path)
    out = capsys.readouterr().out
    assert code == 1, out
    assert "files walked  : 3" in out and "unreadable    : 0" in out, out
    assert "requivo model rebase" in out, out


def test_drift_in_the_part_it_could_read_outranks_the_part_it_could_not(tmp_path, capsys):
    """A complete answer outranks a partial one, and the partial walk is still stated."""
    _skill(tmp_path, "visible", "Fix it with `requivo model rebase <slug>`.")
    hidden = _skill(tmp_path, "hidden", "Run `requivo status <slug>`.")
    _make_unreadable(hidden)
    try:
        code = _run_main(tmp_path)
        out = capsys.readouterr().out
    finally:
        hidden.chmod(0o755)
    assert code == 1, out
    assert "state         : drift" in out and "requivo model rebase" in out, out
    assert str(hidden / "SKILL.md") in out, out


# -- untrusted text in a parsed CI log (#176, #40): the runner parses a line twice, with two anchors ------


def _forging_dir(parent, label):
    """Stage a directory whose name breaks a line, or skip saying what went untested (#176)."""
    directory = parent / f"plain\n::error::forged-by-{label}"
    try:
        directory.mkdir(parents=True)
    except (OSError, ValueError) as exc:
        pytest.skip(f"this filesystem refuses a newline in a directory name ({type(exc).__name__}: {exc}). UNTESTED "
                    f"HERE: the newline-borne half of the class end to end; the value-level rule and the legacy form "
                    f"are asserted on every platform by the sibling tests.")
    return directory


def _assert_no_forged_workflow_command(out):
    """No line may be readable as a workflow command by *either* of the runner's parsers (#176)."""
    forged = []
    for line in out.splitlines():
        if line.startswith("::warning title="):
            continue                                  # one this script authored
        if line.lstrip().startswith("::") or "##[" in line:
            forged.append(line)
    assert not forged, f"a value forged a workflow command the runner would act on: {forged}"


def test_the_sanitiser_collapses_whitespace_and_breaks_both_command_forms():
    """The rule itself, on every leg, including the one that cannot hold a newline in a name."""
    assert _one_line("plain\n::error::forged") == "plain ::error::forged"
    assert _one_line("a\r\nb\tc  d") == "a b c d"
    assert _log_safe("plain\n::error::forged") == "plain : :error: :forged"
    assert _log_safe("brief##[error]title=x") == "brief## [error]title=x"      # the form with no newline, #176
    assert _log_safe("a\r\nb\tc  d") == "a b c d"
    for hostile in ("::error::x", ":::error::x", "::::", "###[error]x", "##[a]##[b]", "#", "::",
                    "  ::error::x  ", "plain\n::error::x", "brief##[error]title=x"):
        assert not _log_safe(hostile).lstrip().startswith("::"), hostile
        assert "##[" not in _log_safe(hostile), hostile
        assert _log_safe(_log_safe(hostile)) == _log_safe(hostile), hostile    # idempotent: `_annotate` re-applies it
    # Must-not-fire: an ordinary path, and the lone colon every one of them carries, comes back byte-identical.
    assert _log_safe("ordinary/path/SKILL.md: Permission denied") == "ordinary/path/SKILL.md: Permission denied"
    assert _log_safe("C:/Users/runner/work/requivo") == "C:/Users/runner/work/requivo"


def test_a_skill_directory_name_is_squashed_before_it_reaches_a_finding():
    """The `_label` half of the class, asserted on every platform."""
    assert _label(Path("skills") / "plain\n::error::forged" / "SKILL.md") == "plain : :error: :forged"
    assert _label(Path("skills") / "brief##[error]title=forged" / "SKILL.md") == "brief## [error]title=forged"
    assert _label(Path("skills") / "brief" / "SKILL.md") == "brief"


def test_an_unreadable_path_cannot_forge_a_line_of_its_own(tmp_path, capsys):
    """An unreadable entry's name reaching `print` un-squashed, ahead of anything `_annotate` would squash."""
    skills = tmp_path / "skills"
    evil = _forging_dir(skills, "an-unreadable-name")
    (evil / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    _skill(tmp_path, "ok", "Run `requivo status <slug>`.")
    _make_unreadable(evil)
    try:
        _run_main(tmp_path, "--github")
        out = capsys.readouterr().out
    finally:
        evil.chmod(0o755)
    assert "forged-by-an-unreadable-name" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)


def test_a_skill_directory_name_cannot_forge_a_line_of_its_own(tmp_path, capsys):
    """The `_label` half end to end, on a real tree."""
    evil = _forging_dir(tmp_path / "skills", "a-skill-directory")
    (evil / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")
    code = _run_main(tmp_path, "--github")
    out = capsys.readouterr().out
    assert code == 1, out
    assert "forged-by-a-skill-directory" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)


LEGACY_NAME = "brief##[error]FORGED-BY-A-LEGACY-DIRECTORY-NAME"


def test_a_skill_directory_name_cannot_forge_the_legacy_command_form(tmp_path, capsys, monkeypatch):
    """#176 itself, end to end, on every platform including the one that skips the two cases above."""
    _skill(tmp_path, LEGACY_NAME, "Fix it with `requivo model rebase <slug>`.")   # deliberately unguarded, unlike `_forging_dir`
    code = _run_main(tmp_path, "--github")
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FORGED-BY-A-LEGACY-DIRECTORY-NAME" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)
    # The must-fire control.
    monkeypatch.setattr(drift, "_label", lambda path: path.parent.name if path.name == "SKILL.md" else path.name)
    _run_main(tmp_path, "--github")
    unguarded = capsys.readouterr().out
    assert "FORGED-BY-A-LEGACY-DIRECTORY-NAME" in unguarded, "the control never reached the name"
    with pytest.raises(AssertionError):
        _assert_no_forged_workflow_command(unguarded)


FORGED_PROBE_PAYLOAD = (r'{"version": "1.0.1", "verbs": {"model": '
                        r'["apply", "show\n##[error]title=FORGED-BY-A-VERB-NAME::pwned"]}}')


def test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own(tmp_path, capsys, monkeypatch):
    """The second instance of the class, found by sweeping this file rather than by being filed."""
    _skill(tmp_path, "brief", "Fix it with `requivo model rebase <slug>`.")
    monkeypatch.setattr(drift, "cli_surface", lambda python: parse_surface(FORGED_PROBE_PAYLOAD))
    code = _run_main(tmp_path, "--github")
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FORGED-BY-A-VERB-NAME" in out, "the harness never reached the verb name at all"
    _assert_no_forged_workflow_command(out)
    # The must-fire control: the same surface built without going through `parse_surface`.
    raw = Surface(version="1.0.1", verbs={"model": {"apply", "show\n##[error]title=FORGED-BY-A-VERB-NAME::pwned"}})
    monkeypatch.setattr(drift, "cli_surface", lambda python: raw)
    _run_main(tmp_path, "--github")
    unguarded = capsys.readouterr().out
    assert "FORGED-BY-A-VERB-NAME" in unguarded, "the control never reached the verb name"
    with pytest.raises(AssertionError):
        _assert_no_forged_workflow_command(unguarded)


def test_the_forgery_guard_sees_both_command_forms():
    """The must-fire half of `_assert_no_forged_workflow_command`, and the reason it exists."""
    for forged in ("::error::pwned",                         # TryParseV2, column 0
                   "  ::error::pwned",                       # TryParseV2 after TrimStart
                   "  requivo model x   (referenced by: brief##[error]pwned)",   # TryParse, mid-line
                   "##[set-output]name=x"):                  # TryParse, column 0
        with pytest.raises(AssertionError):
            _assert_no_forged_workflow_command("state : drift\n" + forged + "\nplain tail\n")
    # Must-not-fire: an ordinary transcript, including the one annotation this script authors.
    _assert_no_forged_workflow_command(
        "plugin root   : /repo/plugins/claude-code\n"
        "  requivo model rebase   (referenced by: brief)\n"
        "::warning title=Plugin/CLI drift::the released CLI (1.0.1) has no `requivo model`\n")


# -- #174: invariant 16, for the file that reads the most foreign content -----------


def test_the_module_stays_stdlib_only():
    """The released CLI is probed in a SEPARATE interpreter (#174)."""
    tree = ast.parse(Path(drift.__file__).read_text(encoding="utf-8"))
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
    """Substitute stdout and stderr with real ASCII-strict encoders, and hand back stdout's bytes."""
    def install() -> io.BytesIO:
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
        return raw
    return install


def _plugin_with_a_non_ascii_skill_name(tmp_path):
    """A plugin whose skill directory name is non-ASCII (#176)."""
    _skill(tmp_path, "brïef", "Run `requivo zzzznotarealverb foo`.\n")
    return tmp_path


def test_a_strict_console_reports_a_real_finding_as_could_not_look_when_streams_are_not_hardened(
        tmp_path, monkeypatch, ascii_console):
    """must fire. `main()`'s blanket `except Exception` turns an unhandled `UnicodeEncodeError` into could-not-look (#174)."""
    plugin = _plugin_with_a_non_ascii_skill_name(tmp_path)
    ascii_console()
    monkeypatch.setattr(drift, "_harden_streams", lambda: None)
    code = main(["--released-python", sys.executable, "--tree-python", sys.executable, "--plugin", str(plugin)])
    assert code == 3, f"expected the crash to masquerade as could-not-look, got {code}"


def test_a_plugin_drift_run_survives_a_console_that_cannot_encode_a_skill_directory_name(tmp_path, ascii_console):
    """must not fire. With `_harden_streams()` doing its job, the same real finding is reported as drift."""
    plugin = _plugin_with_a_non_ascii_skill_name(tmp_path)
    raw = ascii_console()
    code = main(["--released-python", sys.executable, "--tree-python", sys.executable, "--plugin", str(plugin)])
    assert code == 1, "expected the real finding (drift) to survive"
    sys.stdout.flush()
    out = raw.getvalue()
    assert b"\\u" in out or b"\\x" in out, out
    assert sys.stdout.errors == "backslashreplace", sys.stdout.errors


def test_harden_streams_names_a_stream_it_could_not_reach(monkeypatch):
    """The third state. A stream substituted with something that has no `reconfigure()`."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    drift._harden_streams()
    written = sys.stderr.getvalue()
    assert "stdout" in written and "stderr" in written, written


def test_note_could_not_harden_survives_a_console_that_cannot_encode_its_own_reason(monkeypatch):
    """must not fire (silently). `reason` is composed from an exception's own `str()` (#174)."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
    drift._note_could_not_harden("stdout", "café could not be represented")
    sys.stderr.flush()
    out = raw.getvalue()
    assert out, "the note went missing rather than surviving a reason it cannot encode"
    assert b"\\u" in out or b"\\x" in out, out
