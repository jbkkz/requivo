"""The plugin's CLI invocations, resolved against a *released* Requivo rather than this checkout.

`tests/test_plugin.py` compares the plugin to `src/requivo/` in the same working tree. That cannot
see the gap a user actually meets: a marketplace pins the plugin to a commit SHA while
`uv tool install requivo` gets the last PyPI release, and the two drift by construction (#96).
`scripts/plugin_cli_drift.py` is the measurement; the three states below are the point, and the
third -- a released CLI that could not be introspected -- must render as neither `resolved` nor
`drift`, since an absence this leg produced is not an absence in the world (#93).

Split out of `test_plugin_cli_drift.py` (#555) once that file grew past the module ceiling.
"""
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

# A stand-in for a released CLI. `status` deliberately takes no subcommands and `model` does, because
# the difference between those two is what decides whether a bare second word is a subcommand claim
# or an ordinary positional argument.
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
    deterministic CLI applies, so `requivo model apply` and `requivo artifact save` are the two calls
    that mutate anything -- an extractor that only saw top-level verbs would report full coverage
    while never looking at either of them."""
    found = plugin_invocations()
    assert found, "no invocations extracted -- this test would otherwise pass by having nothing to check"
    for expected in [("model", "apply"), ("model", "validate"), ("session", "init"),
                     ("artifact", "save"), ("status", None), ("doctor", None)]:
        assert expected in found, f"extractor missed {expected!r}; it found {sorted(found)}"
    # Every invocation names at least one file it came from, or a finding cannot be acted on.
    for inv, sources in found.items():
        assert sources, f"{inv!r} names no source file"


def test_the_shared_preflight_is_walked_and_not_only_the_skills():
    """`REASONING.md` holds the preflight every skill runs before its first `requivo` call, so a
    command named there executes on every skill path. It introduces no verb the skills do not already
    name, which is exactly the shape that rots quietly: leave it out and the day it stops being
    redundant is the day nothing notices."""
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
    """The case a released-side classifier cannot see. If the release kept `model` but removed its
    subcommand group, the top-level verb still resolves -- so `requivo model apply` would grade clean
    while being broken for every user. The tree is what says `apply` is a subcommand rather than a
    positional argument, and the tree is the same commit as the plugin, so it is the authority on what
    the plugin *meant*."""
    referenced = {("model", "apply"): ["answer"]}
    released = Surface(version="1.0.1", verbs=dict(RELEASED.verbs, model=None))
    report = compare(referenced, tree=_tree(), released=released)
    assert report.state == DRIFT, report
    assert "takes no subcommands" in report.findings[0].reason


def test_a_bare_word_the_tree_does_not_call_a_subcommand_is_an_argument_not_drift():
    """The false-positive guard. `status` takes no subcommands in the tree, so the word after it in
    `requivo status ready` is prose or a positional -- not a claim about the CLI surface. Flagging it
    would make this leg red for a sentence somebody wrote, which is the failure mode
    `plugin-validate.yml`'s header spends four paragraphs arguing against."""
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
    """A CLI with zero verbs is not a measurement. Read as a surface it would report every single
    invocation as drift, which is a confident answer to a question nobody answered."""
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
    """An empty extraction is the shape a broken skills path produces, and `all()` over an empty set is
    True -- so the honest answer is that nothing was checked, not that everything passed."""
    report = compare({}, tree=_tree(), released=RELEASED)
    assert report.state == COULD_NOT_LOOK, report


# -- the in-tree half: a misspelled subcommand -------------------------------------
#
# Two cases in one fixture on purpose. The clean one asserts that nothing fires on the real skills,
# and on its own it would pass just as happily against a harness that cannot see anything at all --
# so the case below it must fire, loudly, on the same code path.


def test_the_real_skills_name_no_subcommand_this_checkout_does_not_have():
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- the silence below would be the harness, not the skills"
    assert tree_typos(referenced, tree) == []


def test_a_misspelled_subcommand_is_caught_rather_than_dropped(tmp_path):
    """The gap `compare()` deliberately leaves. A skill writing `requivo model rebase` names a
    subcommand that does not exist anywhere -- neither the release nor this checkout -- and the drift
    comparison drops it, because from the released side it is indistinguishable from a positional
    argument. Here the tree can answer, so here it is asserted."""
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
    """The other side of the same rule. `status` has no subcommand group, so the word after it is a
    positional or prose, and flagging it would redden the leg for a sentence somebody wrote."""
    skills = tmp_path / "skills"
    (skills / "prose").mkdir(parents=True)
    (skills / "prose" / "SKILL.md").write_text(
        "Then requivo status reports whether it is ready.", encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    assert tree_typos(referenced_invocations(invocation_sources(tmp_path).paths), tree) == []


# -- the entry point ---------------------------------------------------------------
#
# The units above are all called directly. That is not the same as the entry point calling them: a
# reviewer found `main()` reporting `resolved` and exiting 0 for a plugin naming a subcommand that
# exists nowhere, because `main()` ran `compare()` and never `tree_typos()`. Full coverage of a
# function and an entry point that never calls it look identical from outside, so `main()` gets its
# own cases -- a must-fire and a must-not-fire, as always.


def test_main_resolves_the_real_plugin_against_this_checkout():
    assert main(["--released-python", sys.executable]) == 0


def test_main_flags_a_subcommand_that_exists_nowhere_rather_than_reporting_resolved(tmp_path, capsys):
    """The regression the reviewer caught. `compare()` deliberately drops a bare word the tree does
    not call a subcommand, so this plugin's only defect is invisible to it; `main()` has to ask
    `tree_typos()` as well or it prints a clean bill over a broken invocation."""
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
    """An unhandled exception exits 1, and 1 is the drift code -- so a `SKILL.md` this process cannot
    read would have been reported by the CI leg as "drift, annotated above" for a run that annotated
    nothing. A crash is could-not-look: the question was not answered and we know it was not."""
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
    """Python's word-character class is Unicode-aware by default, so without `re.ASCII` this captures
    a token that is then printed -- and on a Windows console at cp1252 that `print` raises
    `UnicodeEncodeError` and kills the process after the comparison was already done (invariant 16).
    Every real verb is ASCII, because they are argparse choices this project declares, so nothing is
    lost by refusing here."""
    skill = tmp_path / "skills" / "prose"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Le CLI requivo ecrit la session, et requivo ecrase le modele.",
                                    encoding="utf-8")
    ascii_only = referenced_invocations(invocation_sources(tmp_path).paths)
    # The control, and note what it shows: ASCII prose after `requivo` IS captured, second token and
    # all. That fragility is inherited from the regex `tests/test_plugin.py` has always used, and it
    # is what `test_every_verb_the_plugin_names_exists_in_this_checkout` below exists to catch.
    assert {verb for verb, _ in ascii_only} == {"ecrit", "ecrase"}, ascii_only

    (skill / "SKILL.md").write_text("Le CLI requivo écrit la session.", encoding="utf-8")
    found = referenced_invocations(invocation_sources(tmp_path).paths)
    assert found == {}, f"a non-ASCII token was captured and would be printed: {found}"


def test_every_verb_the_plugin_names_exists_in_this_checkout():
    """The first-token counterpart of `tree_typos`, and it covers a file the existing gate does not.

    `tests/test_plugin.py::test_skills_reference_only_real_cli_commands` makes this assertion, but
    only over `skills/*/SKILL.md`. This module also walks `REASONING.md`, so prose there such as
    "requivo requires an API key" would be captured as a verb named `requires`, sail past that gate,
    and be reported by the advisory leg as drift against the release -- a false positive in the one
    file the older test never opens. Asserted here, where the walked set is defined."""
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- a silent pass would be the harness, not the plugin"
    unknown = sorted({verb for verb, _ in referenced if verb not in tree.verbs})
    assert not unknown, (
        f"the plugin names {unknown}, which this checkout's CLI does not have. If that is prose "
        f"rather than a command, rewrite it: the extractor cannot tell them apart.")


def test_the_phantom_verb_guard_fires_on_prose(tmp_path):
    """The must-fire half of the case above. Without it, a guard that can no longer see anything
    reports the same clean result as a plugin with no phantom verbs."""
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
# `plugins/claude-code/README.md` is the landing page a marketplace listing sends an uncloned reader
# to (#95), and it was rewritten to be exactly that. So it is the one page in the plugin whose verbs
# somebody types by hand, and until #138 it was the only page whose verbs nothing verified: it names
# `requivo estimate`, `requivo stories` and `requivo session list`, which no skill does. A verb
# renamed or dropped from the CLI left the README naming it, every test green, and the person who
# found out was a new user following the page.
#
# It is deliberately NOT added to `invocation_sources()`'s walked set, and the reason #96 gave for
# leaving it out is a real one rather than an excuse. That walk feeds `INVOCATION_RE` whole files,
# and the README is a page of English: `requivo requires an API key` captures a verb called
# `requires`, and an advisory leg that cries wolf on a sentence somebody wrote is an advisory leg
# that gets ignored. So the README gets something narrower instead, which is what the two cases
# below are -- read the code spans and the fenced blocks, never the prose. That cannot false-positive
# on a sentence, because it never looks at one.

_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def readme_invocations(path):
    """Every `requivo <verb> [<word>]` a markdown page names *in code*.

    Shaped like `referenced_invocations`' return value so `tree_typos` can be reused on it verbatim:
    the rule for when a bare second word is a subcommand claim is subtle, it is already written down
    once, and a second copy of it here would be the thing that drifts.
    """
    found = {}
    for span in _CODE.finditer(path.read_text(encoding="utf-8")):
        for verb, token in INVOCATION_RE.findall(span.group(0)):
            found.setdefault((verb, token or None), [path.name])
    return found


def test_the_plugin_readme_names_only_verbs_this_checkout_has():
    """The gap #138 filed, closed at the narrowest width that closes it.

    Checked against this checkout rather than against a release, deliberately: release skew is the
    advisory leg's question and the README is out of that leg by decision. What is checked here is
    the failure that actually happened -- a verb renamed or removed while the page kept naming it --
    and that is decidable offline, with no network and no false positives.
    """
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
    """The must-fire half. The case above is a negative assertion over a page that is currently
    correct, so on its own it would read identically if the extractor had stopped seeing anything."""
    page = tmp_path / "README.md"
    page.write_text("Run `requivo estimates <slug>`, then `requivo model rebase <slug>`.",
                    encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    named = readme_invocations(page)
    assert sorted(v for v, _ in named if v not in tree.verbs) == ["estimates"], named
    assert [f.invocation for f in tree_typos(named, tree)] == ["requivo model rebase"], named


def test_the_readme_reader_sees_code_and_never_prose(tmp_path):
    """The answer to #138's open question, asserted rather than argued.

    `requivo requires an API key` is a sentence, and feeding a page of English to `INVOCATION_RE`
    reads `requires` as a verb -- the exact false positive #96 already hit inside the skills. This
    reader never looks at prose, so the question of whether the classifier is strong enough for a
    page of English does not arise: it is not asked to be."""
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
    """`chmod 000` the directory, or skip saying exactly what went untested on this run.

    Two ways the case cannot be staged, and both must skip loudly rather than pass quietly: Windows
    does not honour POSIX modes at all, and root (or a filesystem mounted to ignore them) descends
    anyway. A silently-green leg here would be the defect under test wearing the harness as a
    costume."""
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
    """The v1.1.0 audit's fixture: three files, one of them behind a directory mode. The hidden one
    names `requivo model rebase`, a subcommand that exists in neither the release nor the checkout,
    so a walk that misses it reports a clean bill over a broken invocation."""
    skills = tmp_path / "skills"
    (skills / "visible").mkdir(parents=True)
    (skills / "visible" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    hidden = skills / "hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")
    return hidden


def test_the_walk_names_the_skill_directory_it_could_not_descend_into(tmp_path):
    """The unit half: the walk itself has to carry the third state, or nothing downstream can report
    it. `Path.glob` returns the same list for a directory that holds no `SKILL.md` and one it was
    refused entry to, which is the whole defect in one sentence."""
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
    """The must-not-fire half of the unit above, on the same fixture. A probe that called every path
    unreadable would satisfy that test and be useless."""
    _stage_partly_readable(tmp_path)
    sources = invocation_sources(tmp_path)
    assert len(sources.paths) == 3, sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_a_stray_file_in_the_skills_directory_is_absent_and_not_could_not_look(tmp_path):
    """The other side of the same three-way split, and the one that decides whether this leg cries
    wolf. `skills/` can hold something that is not a skill directory -- a `README.md`, a `.DS_Store`
    a contributor's machine dropped there -- and `skills/<that>/SKILL.md` cannot be stat'ed. That is
    an error which *decides* the question (there is no skill there) rather than one that refuses to
    answer it, so it must sort to absent. A walk that called it could-not-look would report the
    repository's own plugin as partly unreadable the first time somebody opened it in Finder.

    Both spellings are covered because the platforms differ: POSIX raises `NotADirectoryError`
    (ENOTDIR) for a path continuing through a regular file, and Windows more often reports the whole
    path as not found. `_collect_file` names both, so neither leg reaches the OSError arm.
    """
    skills = tmp_path / "skills"
    (skills / "real").mkdir(parents=True)
    (skills / "real" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (skills / "README.md").write_text("not a skill", encoding="utf-8")
    (skills / "empty").mkdir()

    sources = invocation_sources(tmp_path)
    assert [p.parent.name for p in sources.paths] == ["real"], sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_the_skills_directory_itself_being_unreadable_is_could_not_look(tmp_path):
    """The `iterdir()` arm, which the three cases above never reach: they chmod a *sub*directory, so
    the listing always succeeded and only the per-file stat failed. Found by audit as an untested
    branch rather than as a defect — the code was right and nothing pinned it, which is how a
    refactor removes a branch and no test goes red."""
    skills = tmp_path / "skills"
    (skills / "hidden").mkdir(parents=True)
    (skills / "hidden" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    _make_unreadable(skills)
    try:
        sources = invocation_sources(tmp_path)
    finally:
        skills.chmod(0o755)

    # The preflight is outside `skills/`, so it is still walked: an unreadable listing loses the
    # skills and nothing else, which is the difference between a partial walk and total blindness.
    assert [p.name for p in sources.paths] == ["REASONING.md"], sources.paths
    assert len(sources.unreadable) == 1, sources.unreadable
    assert str(skills) in sources.unreadable[0], sources.unreadable
