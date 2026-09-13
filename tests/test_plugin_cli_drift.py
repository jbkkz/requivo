"""The plugin's CLI invocations, resolved against a *released* Requivo rather than this checkout.

`tests/test_plugin.py` compares the plugin to `src/requivo/` in the same working tree. That is a
checkout-against-checkout comparison and it cannot see the gap a user actually meets: a community
marketplace pins the plugin to a commit SHA that Anthropic's CI advances as commits land on `main`,
while `uv tool install requivo` gets the last **PyPI release**. The two artifacts drift by
construction between releases and nothing measured the gap (#96).

`scripts/plugin_cli_drift.py` is the measurement. Its logic is tested here rather than left in the
workflow shell, because a check that only exists in YAML is a check the next skill added drops
silently -- which is exactly what #93 was, for six skills.

The three states are the point, and the third one most of all. A released CLI that could not be
introspected -- PyPI unreachable, no release published, the install failed -- must not render as
`resolved` and must not render as `drift` either. The two have identical user-facing consequence and
opposite maintainer meanings, and an absence this leg produced is not an absence in the world.
"""
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


# -- untrusted text in a parsed CI log ---------------------------------------------
#
# A directory entry name is untrusted text, and so is a verb name read back out of the probe: a fork
# pull request controls both, and `.github/workflows/plugin-validate.yml` runs this script with
# `--github` on `pull_request` with no `paths` filter. These strings are printed into a CI step's
# stdout, which the runner parses.
#
# It parses it TWICE, with two different anchors, and modelling only the first is what #176 was:
#
#   TryParseV2  `::name::data`   tested after `message.TrimStart()`. A line start is required;
#                                indenting is not containment. Killing newlines defeats this one.
#   TryParse    `##[name]data`   `message.IndexOf("##[")`. No anchor at all — no line start, no
#                                newline. Killing newlines defeats nothing here.
#
# So a skills entry named `plain<LF>::error::…` forges by breaking a line, and an entry named
# `brief##[error]title=…` forges with no newline in it at all. `_annotate` was never the hole (it
# builds its own `::` prefix, and the runner takes the whole line as that command); the bare `print`
# beside it was, and `_label` — which returns a skill's *directory name* into every finding this
# script prints — was the same hole one function over.
#
# This repository has had precisely this defect before: invariant 14's #40, where a stored context
# card name spent a release able to forge a line at column 0 of `doctor`'s own output. Every half is
# sanitised at the point the untrusted value enters, so a future consumer inherits the guarantee
# instead of having to remember it.


def _forging_dir(parent, label):
    """Stage a directory whose name breaks a line, or skip saying what went untested.

    Probed rather than decided from `sys.platform`, because the hazard is exactly *this filesystem
    cannot hold this name*, and the probe is the staging step itself — it cannot pass for a reason
    unrelated to what it checks. `tests/test_unexaminable_entries.py` stages the same shape the same
    way, and Windows is the case it catches: NTFS refuses every character from 1 to 31 in a name, and
    the colon besides, so `mkdir` fails with `WinError 123` before any assertion has run.

    **What that refusal covers, stated correctly this time.** It used to say the forging vector *is*
    the newline, so a platform refusing the character refused the vector with it. That was false and
    it made this skip unsound (#176): the runner's legacy parser is `message.IndexOf("##[")`, with no
    anchor at all, so a name with no newline and no colon in it forges — and NTFS holds such a name
    perfectly well. What the newline actually gates is one of the two forms, not the class.

    So the skip is narrower now, and the case NTFS cannot refuse has a test that runs everywhere:
    `test_a_skill_directory_name_cannot_forge_the_legacy_command_form` stages `brief##[error]…`,
    which contains no character any of the three platforms refuses.
    """
    directory = parent / f"plain\n::error::forged-by-{label}"
    try:
        directory.mkdir(parents=True)
    except (OSError, ValueError) as exc:
        pytest.skip(
            f"this filesystem refuses a directory name containing a newline ({type(exc).__name__}: "
            f"{exc}). UNTESTED HERE: the newline-borne half of the class — that a plugin path name "
            f"cannot start a line of its own in a CI log, end to end through `main`. The half that "
            f"needs no newline is NOT skipped here and runs on every platform, in "
            f"test_a_skill_directory_name_cannot_forge_the_legacy_command_form. Every other "
            f"platform runs this half too, and the value-level rule is asserted everywhere by "
            f"test_the_sanitiser_collapses_whitespace_and_breaks_both_command_forms and by "
            f"test_a_skill_directory_name_is_squashed_before_it_reaches_a_finding, neither of which "
            f"needs the name to exist on disk.")
    return directory


def test_the_sanitiser_collapses_whitespace_and_breaks_both_command_forms():
    """The rule itself, on every leg. The end-to-end newline cases below need a filesystem that will
    hold a newline in a name and Windows will not, so this is what keeps the claim asserted there.

    Two functions rather than one, because they answer two questions and only one of them is about
    the CI log. `_one_line` is the whitespace rule and three other call sites in `scripts/` compare
    themselves against it by name; `_log_safe` is the rule this script's stdout needs, and it is what
    every untrusted value now passes through.
    """
    assert _one_line("plain\n::error::forged") == "plain ::error::forged"
    assert _one_line("a\r\nb\tc  d") == "a b c d"

    # The newline-borne form: gone at the whitespace step, and the `::` broken besides, so the value
    # is safe wherever it lands on a line rather than only where something else precedes it.
    assert _log_safe("plain\n::error::forged") == "plain : :error: :forged"
    # The form with no newline in it at all, which is #176. Spaced apart rather than deleted, so the
    # value still reads as what was on disk.
    assert _log_safe("brief##[error]title=x") == "brief## [error]title=x"
    assert _log_safe("a\r\nb\tc  d") == "a b c d"

    # The two properties the exact strings above are only examples of, asserted over the shapes a
    # single left-to-right `str.replace` could plausibly leave a key in: an odd run of colons, and a
    # `#` run long enough that the match does not start at index 0. A residual `::` mid-string is
    # harmless (`TryParseV2` needs the line start, and the sanitised value can never supply it) and a
    # residual `##[` would not be, so the two are asserted differently on purpose.
    for hostile in ("::error::x", ":::error::x", "::::", "###[error]x", "##[a]##[b]", "#", "::",
                    "  ::error::x  ", "plain\n::error::x", "brief##[error]title=x"):
        assert not _log_safe(hostile).lstrip().startswith("::"), hostile
        assert "##[" not in _log_safe(hostile), hostile
        # Idempotent, because `_annotate` applies this as a backstop to a message whose parts were
        # already sanitised where they entered. A rule that changed a value it had already seen would
        # make the plain `print` of one string and the annotation of it disagree.
        assert _log_safe(_log_safe(hostile)) == _log_safe(hostile), hostile

    # Must-not-fire: an ordinary path, and the lone colon every one of them carries, comes back
    # byte-identical. Without this the assertions above would pass for a sanitiser that mangled
    # everything, and every finding this script prints would be quietly rewritten.
    assert _log_safe("ordinary/path/SKILL.md: Permission denied") == \
        "ordinary/path/SKILL.md: Permission denied"
    assert _log_safe("C:/Users/runner/work/requivo") == "C:/Users/runner/work/requivo"


def test_a_skill_directory_name_is_squashed_before_it_reaches_a_finding():
    """The `_label` half of the class, asserted on every platform including the one that cannot
    stage the fixture.

    `_label` reads `.parent.name` off a path and never touches the filesystem, so the hostile name
    only has to be *representable*, not creatable — and a `PurePath` represents it identically on
    both flavours. That is the difference between Windows skipping this class whole and Windows
    checking the half of it a pure path can reach.
    """
    forged = Path("skills") / "plain\n::error::forged" / "SKILL.md"
    assert _label(forged) == "plain : :error: :forged"
    # The legacy form, which needs no newline and is therefore the half NTFS does not refuse (#176).
    legacy = Path("skills") / "brief##[error]title=forged" / "SKILL.md"
    assert _label(legacy) == "brief## [error]title=forged"
    # The must-not-fire half: an ordinary skill name comes back unchanged, or the sanitiser would be
    # rewriting every finding's source and the assertions above would pass for the wrong reason.
    assert _label(Path("skills") / "brief" / "SKILL.md") == "brief"


def test_an_unreadable_path_cannot_forge_a_line_of_its_own(tmp_path, capsys):
    """The half this diff introduced, end to end: an unreadable entry's name reaching `print`
    un-squashed, ahead of anything `_annotate` would have squashed on its way to an annotation.

    Doubly unstageable on Windows — the name is refused by the filesystem, and the `chmod 000` would
    not bite either — so `_forging_dir` skips first and names the whole class, rather than leaving
    the second obstacle to be discovered by whoever removes the first."""
    skills = tmp_path / "skills"
    evil = _forging_dir(skills, "an-unreadable-name")
    (evil / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (skills / "ok").mkdir()
    (skills / "ok" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    _make_unreadable(evil)
    try:
        main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
        out = capsys.readouterr().out
    finally:
        evil.chmod(0o755)

    assert "forged-by-an-unreadable-name" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)


def test_a_skill_directory_name_cannot_forge_a_line_of_its_own(tmp_path, capsys):
    """The `_label` half end to end, on a real tree — the integration counterpart of
    `test_a_skill_directory_name_is_squashed_before_it_reaches_a_finding` above.

    Worth having in addition to that unit, because what it pins is not that `_label` squashes but
    that `_show` prints nothing else derived from the same name: full coverage of a function and an
    entry point that routes around it look identical from outside."""
    skills = tmp_path / "skills"
    evil = _forging_dir(skills, "a-skill-directory")
    (evil / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "forged-by-a-skill-directory" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)


LEGACY_NAME = "brief##[error]FORGED-BY-A-LEGACY-DIRECTORY-NAME"


def test_a_skill_directory_name_cannot_forge_the_legacy_command_form(tmp_path, capsys, monkeypatch):
    """#176 itself, end to end, on every platform including the one that skips the two cases above.

    The name holds no newline and no colon — nothing NTFS refuses — because the runner's legacy
    parser is `message.IndexOf("##[")` and needs neither. That is exactly why `_forging_dir`'s old
    claim was unsound: the platform that cannot stage a newline stages this one without complaint.

    Paired with a must-fire control in the same fixture, because the assertion below is a
    must-not-fire and a must-not-fire passes when the harness produced nothing at all. The control
    strips exactly one containment — `_label`'s sanitising — and asserts the forgery comes back.
    """
    skills = tmp_path / "skills"
    evil = skills / LEGACY_NAME
    # Deliberately not guarded the way `_forging_dir` is. If a platform DOES refuse this name, the
    # claim above is wrong and the right outcome is a loud error naming it, not a quiet skip that
    # would leave the only vector NTFS cannot refuse untested on the platform it matters for.
    evil.mkdir(parents=True)
    (evil / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "FORGED-BY-A-LEGACY-DIRECTORY-NAME" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)

    # The must-fire control. `_label` is where the value is sanitised; put the raw directory name
    # back and the same run has to forge, or nothing above was ever being contained.
    monkeypatch.setattr(drift, "_label",
                        lambda path: path.parent.name if path.name == "SKILL.md" else path.name)
    main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    unguarded = capsys.readouterr().out
    assert "FORGED-BY-A-LEGACY-DIRECTORY-NAME" in unguarded, "the control never reached the name"
    with pytest.raises(AssertionError):
        _assert_no_forged_workflow_command(unguarded)


FORGED_PROBE_PAYLOAD = (
    r'{"version": "1.0.1", "verbs": {"model": '
    r'["apply", "show\n##[error]title=FORGED-BY-A-VERB-NAME::pwned"]}}')


def test_a_verb_name_from_the_probe_cannot_forge_a_line_of_its_own(tmp_path, capsys, monkeypatch):
    """The second instance of the class, found by sweeping this file rather than by being filed.

    A skills directory name is not the only untrusted text here. `parse_surface` reads the probe's
    stdout, and the probe introspects `requivo.cli._build_parser()` **in the working tree** — which a
    fork pull request edits as freely as it names a directory. Those verb and subcommand names are
    interpolated into `tree_typos`' reason, which `_show` prints, and a newline in one puts the rest
    at column 0: strictly worse than #176's own case, since it reaches both parsers rather than one.

    The released half of the same read is not a vector (it comes from PyPI), but it enters through
    the same function and is sanitised by the same line.
    """
    skills = tmp_path / "skills" / "brief"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    monkeypatch.setattr(drift, "cli_surface", lambda python: parse_surface(FORGED_PROBE_PAYLOAD))
    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "FORGED-BY-A-VERB-NAME" in out, "the harness never reached the verb name at all"
    _assert_no_forged_workflow_command(out)

    # The must-fire control: the same surface built without going through `parse_surface`, which is
    # where the value is sanitised. One containment removed, and the forgery has to reappear.
    raw = Surface(version="1.0.1",
                  verbs={"model": {"apply", "show\n##[error]title=FORGED-BY-A-VERB-NAME::pwned"}})
    monkeypatch.setattr(drift, "cli_surface", lambda python: raw)
    main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    unguarded = capsys.readouterr().out
    assert "FORGED-BY-A-VERB-NAME" in unguarded, "the control never reached the verb name"
    with pytest.raises(AssertionError):
        _assert_no_forged_workflow_command(unguarded)


def _assert_no_forged_workflow_command(out):
    """No line may be readable as a workflow command by *either* of the runner's parsers, except the
    ones this script authors.

    Asserted as a whitelist rather than as "no `::error::`", because the injectable vocabulary is
    every workflow command there is — `set-output`, `add-mask`, `stop-commands` — and a denylist of
    one is a guard that ages badly.

    It used to test `line.startswith("::")` and nothing else, and that is #176: it modelled one of
    the two parsers in `actions/runner`'s `ActionCommand.cs`, so both end-to-end callers below ran,
    checked the wrong thing, and reported a pass over a live vector. Both are modelled now, each the
    way the runner spells it:

      TryParseV2   `::name::data`   tests the prefix AFTER `message.TrimStart()`, so an indented
                                    `::` is a command and only the line start matters.
      TryParse     `##[name]data`   is `message.IndexOf("##[")` — no anchor at all, so it needs
                                    neither a line start nor a newline anywhere in the value.

    `test_the_forgery_guard_sees_both_command_forms` is the must-fire control for this helper: a
    guard nobody can make fail is indistinguishable from one that always passes.
    """
    forged = []
    for line in out.splitlines():
        if line.startswith("::warning title="):
            continue                                  # one this script authored
        if line.lstrip().startswith("::") or "##[" in line:
            forged.append(line)
    assert not forged, f"a value forged a workflow command the runner would act on: {forged}"


def test_the_forgery_guard_sees_both_command_forms():
    """The must-fire half of `_assert_no_forged_workflow_command`, and the reason it exists.

    Every other use of that helper is a must-not-fire assertion, and a must-not-fire assertion
    passes when the harness produced nothing at all. So the guard is exercised here against a
    transcript that really does forge, in each of the two shapes the runner parses, plus one clean
    transcript so a helper that flagged everything would not read as coverage either."""
    for forged in ("::error::pwned",                         # TryParseV2, column 0
                   "  ::error::pwned",                       # TryParseV2 after TrimStart
                   "  requivo model x   (referenced by: brief##[error]pwned)",   # TryParse, mid-line
                   "##[set-output]name=x"):                  # TryParse, column 0
        with pytest.raises(AssertionError):
            _assert_no_forged_workflow_command("state : drift\n" + forged + "\nplain tail\n")

    # Must-not-fire: an ordinary transcript, including the annotations this script does author and a
    # lone colon of the kind every path and every reason line carries, has to come back clean.
    _assert_no_forged_workflow_command(
        "plugin root   : /repo/plugins/claude-code\n"
        "  requivo model rebase   (referenced by: brief)\n"
        "::warning title=Plugin/CLI drift::the released CLI (1.0.1) has no `requivo model`\n")


def test_a_skills_path_that_is_a_regular_file_is_absent_and_not_could_not_look(tmp_path):
    """The same three-way rule at the directory level, where it was stated and not applied.

    `_collect_file` sorts `NotADirectoryError` to absent, because a path continuing through a regular
    file *decides* the question. The `skills.iterdir()` arm above it named only `FileNotFoundError`,
    so the identical exception twelve lines apart meant two different things: a `skills` that is a
    file read as could-not-look. Found by review, and the edge case matters less than the split being
    uniform -- a rule that holds in one of the two places it is written is the thing that bites."""
    (tmp_path / "skills").write_text("not a directory", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")

    sources = invocation_sources(tmp_path)
    assert [p.name for p in sources.paths] == ["REASONING.md"], sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_the_reason_names_the_unreadable_path_when_nothing_could_be_extracted(tmp_path, capsys):
    """Could-not-look for the right reason, which is a separate question from could-not-look.

    Found by review. When the only invocation-bearing file is the one the walk could not open,
    `referenced` comes back empty and `compare()` -- which is never handed the unreadable set, and
    should not be -- returns its own could-not-look: *no `requivo` invocations were found in the
    plugin's files*. True as far as it goes and wrong about the cause, because the emptiness is not a
    fact about the plugin, it is a fact about what this process was allowed to read. Worse, `_run`
    returned on that arm before ever naming the path, so the exit code was right and the one line a
    reader could act on never printed.

    The remedy is ordering, not a new state: the unreadable set is named before any verdict detail,
    on every path through `_run`, so the emptiness below is always read next to its cause."""
    skills = tmp_path / "skills"
    only = skills / "only"
    only.mkdir(parents=True)
    (only / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    _make_unreadable(only)
    try:
        code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
        out = capsys.readouterr().out
    finally:
        only.chmod(0o755)

    assert code == 3, out
    assert "state         : could-not-look" in out, out
    assert str(only / "SKILL.md") in out, out
    # The cause has to come before the consequence, or the first sentence a reader meets is the one
    # that blames the plugin for an emptiness this process created.
    assert out.index("could not be read") < out.index("no `requivo` invocations were found"), out


def test_main_reports_could_not_look_when_it_could_only_walk_part_of_the_plugin(tmp_path, capsys):
    """The defect (#139), staged exactly as the v1.1.0 release audit found it.

    `invocation_sources` walked with `Path.glob` and `Path.is_file()`, and **both swallow
    `OSError`**: glob skips a subdirectory it cannot descend into and raises nothing, and `is_file()`
    returns False on EACCES. Both failures are silent, so a partially readable plugin was graded as a
    verdict over whatever subset the walk happened to manage -- three files staged, two walked,
    `state: resolved`, exit 0, about a plugin whose third file names a verb that exists nowhere.

    The script handled the two neighbouring cases and this was the gap between them: total blindness
    is `could-not-look` because `referenced` comes back empty, and a file-level EACCES is
    `could-not-look` through the total `except` in `main`. Only the partial walk graded.

    This is invariant 15's third paragraph one directory over -- a partition whose predicate can
    raise has three outcomes whether or not its return type says so, and an entry it could not decide
    about belongs in neither of the other two buckets. The vocabulary already existed: a directory
    the walk cannot descend into is `could-not-look` for the part it could not see.
    """
    hidden = _stage_partly_readable(tmp_path)
    _make_unreadable(hidden)
    try:
        code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
        out = capsys.readouterr().out
    finally:
        hidden.chmod(0o755)

    assert code == 3, out
    assert "state         : could-not-look" in out, out
    assert "resolves against the released CLI" not in out, out
    # The reader has to be able to act on it, so the verdict names the path it could not read.
    assert str(hidden / "SKILL.md") in out, out


def test_the_same_plugin_read_whole_is_a_verdict_and_reports_nothing_unreadable(tmp_path, capsys):
    """The must-not-fire half, on the identical fixture with the mode change as the only difference.

    Two things it pins that the case above cannot. That the walk really does reach the third file
    when it is allowed to -- otherwise the could-not-look above would be a walk that never sees three
    files at all -- and that `unreadable` is a counted 0 rather than a constant, which is what stops
    a broken probe from reporting every plugin as partly unreadable and calling that a guard."""
    _stage_partly_readable(tmp_path)
    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "files walked  : 3" in out, out
    assert "unreadable    : 0" in out, out
    assert "requivo model rebase" in out, out


def test_drift_in_the_part_it_could_read_outranks_the_part_it_could_not(tmp_path, capsys):
    """A complete answer outranks a partial one.

    Invariant 15 settles this for `session verify`: a session that is inconsistent **and** whose
    cards were unreadable exits on the firm negative, because could-not-look says the question is
    unanswered and here part of it is answered. So an invocation that resolves nowhere keeps the
    drift exit, and the partial walk is reported *alongside* it rather than instead of it."""
    skills = tmp_path / "skills"
    (skills / "visible").mkdir(parents=True)
    (skills / "visible" / "SKILL.md").write_text(
        "Fix it with `requivo model rebase <slug>`.", encoding="utf-8")
    hidden = skills / "hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    _make_unreadable(hidden)
    try:
        code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
        out = capsys.readouterr().out
    finally:
        hidden.chmod(0o755)

    assert code == 1, out
    assert "state         : drift" in out, out
    assert "requivo model rebase" in out, out
    # ...and the partial walk is still stated, or the reader takes the drift list for the whole list.
    assert str(hidden / "SKILL.md") in out, out


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
    """The released CLI is probed in a SEPARATE interpreter (the PROBE subprocess), so this module
    itself never needs `requivo` importable to run the comparison -- and per the module docstring it
    must be runnable even when the tree-side `pip install -e .` step failed
    (`continue-on-error` in `.github/workflows/plugin-validate.yml`), which is exactly the case this
    script exists to report as could-not-look rather than crash on before it ever reaches `main()`.

    This is why `_harden_streams()` is not `golden_lib.configure_output()`: that function reaches
    `requivo.streams`, and `golden_lib` itself imports `requivo.core.analysis` and friends, so either
    import would turn a missing tree install into an unhandled `ModuleNotFoundError` at import time --
    worse than the bug #174 is about, since it would happen before this module's own could-not-look
    handling ever runs."""
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
    `UnicodeEncodeError` into could-not-look rather than letting it escape -- so disabling
    `_harden_streams()` does not surface as a raised exception here, it surfaces as exactly the
    ordering bug #174 is about: a real drift Finding is computed, the crash happens while PRINTING
    it, and the exit code ends up describing the crash (3, could-not-look) instead of the walk that
    already found the drift (1). That silent substitution -- not a raised exception -- is the
    positive control for the test below, and it is why this asserts an exit code rather than
    `pytest.raises`."""
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
    reaches the reader as a visible escape rather than a hole. The escape is the evidence the print
    ran rather than fell silent, the same reasoning
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
    itself carry a character stderr cannot encode -- the exact class this whole file is about,
    reproduced one function inward, in the code that is supposed to REPORT that class. Without the
    fallback this writes nothing at all: a reader of stderr cannot tell "both streams hardened fine"
    from "hardening and the report of its own failure both failed silently". Auditor-found in the
    #174 review."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
    drift._note_could_not_harden("stdout", "café could not be represented")
    sys.stderr.flush()
    out = raw.getvalue()
    assert out, "the note went missing rather than surviving a reason it cannot encode"
    assert b"\\u" in out or b"\\x" in out, out
