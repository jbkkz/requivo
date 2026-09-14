"""Untrusted text reaching the drift report: a skill/directory name, or a probe's own output,
must never forge a line of the printed finding -- the same defect class `render_untrusted_output`
guards elsewhere in the suite, here at the one place this checker prints parsed, attacker-reachable
strings.

Split out of `test_plugin_cli_drift.py` (#555) once that file grew past the module ceiling.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plugin_cli_drift as drift  # noqa: E402  - for monkeypatching a module global
from plugin_cli_drift import (  # noqa: E402
    Surface,
    _label,
    _log_safe,
    _one_line,
    invocation_sources,
    main,
    parse_surface,
)


def _make_unreadable(directory):
    """`chmod 000` the directory, or skip saying exactly what went untested on this run. See
    `test_plugin_cli_drift_resolution.py`'s copy of this helper for the platform reasoning."""
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
    """The v1.1.0 audit's fixture: three files, one of them behind a directory mode. See
    `test_plugin_cli_drift_resolution.py`'s copy of this helper for what it pins."""
    skills = tmp_path / "skills"
    (skills / "visible").mkdir(parents=True)
    (skills / "visible" / "SKILL.md").write_text("Run `requivo status <slug>`.", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")
    hidden = skills / "hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")
    return hidden


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
    unrelated to what it checks. `tests/test_persistence_scan.py` stages the same shape the same
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
