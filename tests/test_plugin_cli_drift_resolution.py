"""The plugin's CLI invocations, resolved against a *released* Requivo rather than this checkout (#96)."""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from plugin_cli_drift import (  # noqa: E402
    COULD_NOT_LOOK,
    DRIFT,
    INVOCATION_RE,
    PLUGIN_ROOT,
    RESOLVED,
    Surface,
    cli_surface,
    compare,
    invocation_sources,
    main,
    referenced_invocations,
    tree_typos,
)

README = PLUGIN_ROOT / "README.md"


def plugin_invocations():
    """Everything the real plugin executes: every skill plus the shared preflight (#542)."""
    return referenced_invocations(invocation_sources(PLUGIN_ROOT).paths)

# A stand-in for a released CLI.
RELEASED = Surface(version="1.0.1", verbs={
    "status": None,
    "doctor": None,
    "model": {"apply", "show", "validate", "diff"},
    "session": {"init", "show", "list", "export", "import", "verify", "migrate"},
    "artifact": {"save", "show", "list"},
})

SKILL_FIXTURE = """
Run `requivo doctor --json` first.
Then `requivo status <slug> --json` to read it back.
Apply with `requivo model apply <slug> - --expected-revision N`.
"""


def _tree(**overrides):
    verbs = dict(RELEASED.verbs)
    verbs.update(overrides)
    return Surface(version="1.1.0.dev0", verbs=verbs)


# -- extraction -------------------------------------------------------------------


def test_referenced_invocations_reads_the_real_skills_and_finds_the_two_level_calls():
    """A positive control on the extractor. The plugin's whole contract is that Claude reasons and the
    deterministic CLI applies."""
    found = plugin_invocations()
    assert found, "no invocations extracted -- this test would otherwise pass by having nothing to check"
    for expected in [("model", "apply"), ("model", "validate"), ("session", "init"),
                     ("artifact", "save"), ("status", None), ("doctor", None)]:
        assert expected in found, f"extractor missed {expected!r}; it found {sorted(found)}"
    # Every invocation names at least one file it came from, or a finding cannot be acted on.
    for inv, sources in found.items():
        assert sources, f"{inv!r} names no source file"


def test_the_shared_preflight_is_walked_and_not_only_the_skills():
    """`REASONING.md` holds the preflight every skill runs before its first `requivo` call."""
    sources = invocation_sources(PLUGIN_ROOT)
    names = [p.name for p in sources.paths]
    assert "REASONING.md" in names, f"the preflight is not walked; walked {names}"
    assert names.count("SKILL.md") == 10, f"expected ten skills, walked {names}"  # #542, #539, #545, #543
    # And the real plugin is fully readable, so this walk is a whole answer rather than a subset.
    assert sources.unreadable == [], sources.unreadable
    # And it must actually contribute -- a file that is walked but unreadable would look identical.
    from_preflight = referenced_invocations([PLUGIN_ROOT / "REASONING.md"])
    assert ("doctor", None) in from_preflight, (
        f"the preflight must name the probe it runs; extracted {sorted(from_preflight)}")


def test_a_flag_or_a_placeholder_is_not_read_as_a_subcommand(tmp_path):
    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
    found = referenced_invocations(invocation_sources(tmp_path).paths)
    assert ("doctor", None) in found
    assert ("status", None) in found
    assert ("model", "apply") in found
    assert not any(sub in {"--json", "<slug>"} for _, sub in found)
    assert found[("model", "apply")] == ["demo"]


# -- comparison: the three states -------------------------------------------------


def test_a_plugin_that_resolves_is_resolved():
    referenced = {("status", None): ["status"], ("model", "apply"): ["answer"]}
    report = compare(referenced, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report
    assert report.findings == []
    assert report.checked == 2


def test_a_verb_the_release_does_not_have_is_drift():
    referenced = {("epic", None): ["brief"]}
    report = compare(referenced, tree=_tree(epic=None), released=RELEASED)
    assert report.state == DRIFT, report
    assert [f.invocation for f in report.findings] == ["requivo epic"]
    assert report.findings[0].sources == ["brief"]


def test_a_subcommand_the_release_does_not_have_is_drift():
    referenced = {("model", "rebase"): ["answer"]}
    report = compare(referenced, tree=_tree(model={"apply", "show", "validate", "diff", "rebase"}),
                     released=RELEASED)
    assert report.state == DRIFT, report
    assert report.findings[0].invocation == "requivo model rebase"


def test_a_release_that_dropped_a_verbs_subcommands_entirely_is_drift():
    """The case a released-side classifier cannot see."""
    referenced = {("model", "apply"): ["answer"]}
    released = Surface(version="1.0.1", verbs=dict(RELEASED.verbs, model=None))
    report = compare(referenced, tree=_tree(), released=released)
    assert report.state == DRIFT, report
    assert "takes no subcommands" in report.findings[0].reason


def test_a_bare_word_the_tree_does_not_call_a_subcommand_is_an_argument_not_drift():
    """The false-positive guard. `status` takes no subcommands in the tree."""
    referenced = {("status", "ready"): ["status"]}
    report = compare(referenced, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report


def test_an_unreachable_release_is_could_not_look_and_is_neither_of_the_other_two():
    referenced = {("status", None): ["status"]}
    report = compare(referenced, tree=_tree(), released=None)
    assert report.state == COULD_NOT_LOOK, report
    assert report.state != RESOLVED and report.state != DRIFT
    assert report.detail, "could-not-look must say what it could not do"
    assert report.checked == 0


def test_an_empty_released_surface_is_could_not_look_not_wholesale_drift():
    """A CLI with zero verbs is not a measurement."""
    referenced = {("status", None): ["status"], ("model", "apply"): ["answer"]}
    report = compare(referenced, tree=_tree(), released=Surface(version="?", verbs={}))
    assert report.state == COULD_NOT_LOOK, report
    assert report.findings == []


def test_an_unreadable_tree_is_could_not_look_rather_than_a_verdict_about_the_release():
    referenced = {("status", None): ["status"]}
    report = compare(referenced, tree=None, released=RELEASED)
    assert report.state == COULD_NOT_LOOK, report
    assert "tree" in report.detail.lower()


def test_no_invocations_at_all_is_could_not_look_rather_than_a_clean_bill():
    """An empty extraction is the shape a broken skills path produces."""
    report = compare({}, tree=_tree(), released=RELEASED)
    assert report.state == COULD_NOT_LOOK, report


# -- the in-tree half: a misspelled subcommand -------------------------------------
#
# Two cases in one fixture on purpose.


def test_the_real_skills_name_no_subcommand_this_checkout_does_not_have():
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- the silence below would be the harness, not the skills"
    assert tree_typos(referenced, tree) == []


def test_a_misspelled_subcommand_is_caught_rather_than_dropped(tmp_path):
    """The gap `compare()` deliberately leaves."""
    skills = tmp_path / "skills"
    (skills / "typo").mkdir(parents=True)
    (skills / "typo" / "SKILL.md").write_text(
        "Fix it up with `requivo model rebase <slug>` afterwards.", encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    findings = tree_typos(referenced_invocations(invocation_sources(tmp_path).paths), tree)
    assert [f.invocation for f in findings] == ["requivo model rebase"], findings
    assert findings[0].sources == ["typo"]
    assert "apply" in findings[0].reason, "the finding must name what the verb does offer"


def test_a_word_after_a_verb_with_no_subcommand_group_is_left_alone(tmp_path):
    """The other side of the same rule. `status` has no subcommand group."""
    skills = tmp_path / "skills"
    (skills / "prose").mkdir(parents=True)
    (skills / "prose" / "SKILL.md").write_text(
        "Then requivo status reports whether it is ready.", encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    assert tree_typos(referenced_invocations(invocation_sources(tmp_path).paths), tree) == []


# -- the entry point ---------------------------------------------------------------
#
# The units above are all called directly.


def test_main_resolves_the_real_plugin_against_this_checkout():
    assert main(["--released-python", sys.executable]) == 0


def test_main_flags_a_subcommand_that_exists_nowhere_rather_than_reporting_resolved(tmp_path, capsys):
    """The regression the reviewer caught. `compare()` deliberately drops a bare word the tree does not call a
    subcommand."""
    skill = tmp_path / "skills" / "typo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "requivo model rebase" in out, out
    # And the headline state must not contradict the list underneath it.
    assert "state         : drift" in out, out
    assert "resolves against the released CLI" not in out, out


def test_main_reports_could_not_look_rather_than_a_verdict_when_the_release_is_unreachable(tmp_path, capsys):
    code = main(["--released-python", str(ROOT / "no" / "such" / "python")])
    out = capsys.readouterr().out
    assert code == 3, out
    assert "could-not-look" in out
    assert "not a clean result" in out


def test_an_unreadable_skill_file_is_could_not_look_not_drift(tmp_path, capsys):
    """An unhandled exception exits 1, and 1 is the drift code."""
    skill = tmp_path / "skills" / "broken"
    skill.mkdir(parents=True)
    target = skill / "SKILL.md"
    target.write_text("requivo status", encoding="utf-8")
    target.chmod(0o000)
    if os.access(target, os.R_OK):        # root, or a filesystem that ignores the mode
        target.chmod(0o644)
        pytest.skip("this user can read a 0o000 file, so the unreadable case cannot be staged here. "
                    "UNTESTED ON THIS RUN: that an unreadable skill maps to could-not-look.")
    try:
        code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
        captured = capsys.readouterr()
    finally:
        target.chmod(0o644)
    assert code == 3, captured
    assert "not evidence of drift" in captured.err, captured.err


def test_a_non_ascii_word_after_requivo_is_not_captured_at_all(tmp_path):
    """Python's word-character class is Unicode-aware by default, so without `re.ASCII` this captures a token
    that is then printed."""
    skill = tmp_path / "skills" / "prose"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Le CLI requivo ecrit la session, et requivo ecrase le modele.",
                                    encoding="utf-8")
    ascii_only = referenced_invocations(invocation_sources(tmp_path).paths)
    # The control, and note what it shows: ASCII prose after `requivo` IS captured, second token and all.
    assert {verb for verb, _ in ascii_only} == {"ecrit", "ecrase"}, ascii_only

    (skill / "SKILL.md").write_text("Le CLI requivo écrit la session.", encoding="utf-8")
    found = referenced_invocations(invocation_sources(tmp_path).paths)
    assert found == {}, f"a non-ASCII token was captured and would be printed: {found}"


def test_every_verb_the_plugin_names_exists_in_this_checkout():
    """The first-token counterpart of `tree_typos`, and it covers a file the existing gate does not."""
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- a silent pass would be the harness, not the plugin"
    unknown = sorted({verb for verb, _ in referenced if verb not in tree.verbs})
    assert not unknown, (
        f"the plugin names {unknown}, which this checkout's CLI does not have. If that is prose "
        f"rather than a command, rewrite it: the extractor cannot tell them apart.")


def test_the_phantom_verb_guard_fires_on_prose(tmp_path):
    """The must-fire half of the case above. Without it, a guard that can no longer see anything reports the
    same clean result as a plugin with no phantom verbs."""
    (tmp_path / "REASONING.md").write_text(
        "Note that requivo requires an API key for provider verbs.", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = referenced_invocations(invocation_sources(tmp_path).paths)
    unknown = sorted({verb for verb, _ in referenced if verb not in tree.verbs})
    assert unknown == ["requires"], f"expected the phantom verb to be caught; got {referenced}"


# -- the plugin README: the one page a stranger types by hand ----------------------
#
# `plugins/claude-code/README.md` is the landing page a marketplace listing sends an uncloned reader to (#95).
#
# It is deliberately NOT added to `invocation_sources()`'s walked set (#96).

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
    tree = cli_surface(sys.executable)
    assert tree is not None
    named = readme_invocations(README)
    assert named, (
        f"no `requivo ...` invocation was read out of {README.name}. The page names several, so a "
        f"silent pass here would be the reader, not the README.")

    unknown = sorted({verb for verb, _ in named if verb not in tree.verbs})
    assert not unknown, (
        f"{README.name} names {unknown}, which this CLI does not have. This is the landing page a "
        f"marketplace sends an uncloned reader to, so a stale verb here is typed by a stranger.")
    typos = tree_typos(named, tree)
    assert not typos, [f"{f.invocation}: {f.reason}" for f in typos]


def test_the_readme_verb_guard_fires_on_a_verb_and_on_a_subcommand(tmp_path):
    """The must-fire half. The case above is a negative assertion over a page that is currently correct."""
    page = tmp_path / "README.md"
    page.write_text("Run `requivo estimates <slug>`, then `requivo model rebase <slug>`.",
                    encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    named = readme_invocations(page)
    assert sorted(v for v, _ in named if v not in tree.verbs) == ["estimates"], named
    assert [f.invocation for f in tree_typos(named, tree)] == ["requivo model rebase"], named


def test_the_readme_reader_sees_code_and_never_prose(tmp_path):
    """The answer to #138's open question, asserted rather than argued."""
    page = tmp_path / "README.md"
    page.write_text(
        "Note that requivo requires an API key for the optional provider mode.\n"
        "A session created with requivo discover lands under your workspace.\n"
        "\n"
        "Run `requivo doctor` first.\n"
        "\n"
        "```\n"
        "requivo session list\n"
        "```\n",
        encoding="utf-8")
    assert set(readme_invocations(page)) == {("doctor", None), ("session", "list")}, \
        readme_invocations(page)


# -- the walk: a plugin tree this process can only partly read ---------------------


def _make_unreadable(directory):
    """`chmod 000` the directory, or skip saying exactly what went untested on this run."""
    if os.name == "nt":
        pytest.skip("POSIX directory modes do not bite on Windows. UNTESTED HERE: that a directory "
                    "the walk cannot descend into maps to could-not-look. Every other platform runs "
                    "it.")
    directory.chmod(0o000)
    try:
        (directory / "SKILL.md").stat()
    except PermissionError:
        return
    except OSError:
        pass
    directory.chmod(0o755)
    pytest.skip("this user can descend into a 0o000 directory, so the case cannot be staged here. "
                "UNTESTED ON THIS RUN: that a directory the walk cannot descend into maps to "
                "could-not-look.")


def _stage_partly_readable(tmp_path):
    """The v1.1.0 audit's fixture: three files, one of them behind a directory mode."""
    skills = tmp_path / "skills"
    (skills / "visible").mkdir(parents=True)
    (skills / "visible" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    hidden = skills / "hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")
    return hidden


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


def test_a_stray_file_in_the_skills_directory_is_absent_and_not_could_not_look(tmp_path):
    """The other side of the same three-way split, and the one that decides whether this leg cries wolf."""
    skills = tmp_path / "skills"
    (skills / "real").mkdir(parents=True)
    (skills / "real" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (skills / "README.md").write_text("not a skill", encoding="utf-8")
    (skills / "empty").mkdir()

    sources = invocation_sources(tmp_path)
    assert [p.parent.name for p in sources.paths] == ["real"], sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_the_skills_directory_itself_being_unreadable_is_could_not_look(tmp_path):
    """The `iterdir()` arm, which the three cases above never reach."""
    skills = tmp_path / "skills"
    (skills / "hidden").mkdir(parents=True)
    (skills / "hidden" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    _make_unreadable(skills)
    try:
        sources = invocation_sources(tmp_path)
    finally:
        skills.chmod(0o755)

    # The preflight is outside `skills/`, so it is still walked.
    assert [p.name for p in sources.paths] == ["REASONING.md"], sources.paths
    assert len(sources.unreadable) == 1, sources.unreadable
    assert str(skills) in sources.unreadable[0], sources.unreadable
