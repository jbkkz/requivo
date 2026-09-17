"""Untrusted text reaching the drift report: a skill/directory name, or a probe's own output, must never forge
a line of the printed finding -- the same defect class `render_untrusted_output` guards elsewhere in the
suite, here at the one place this checker prints parsed, attacker-reachable strings (#555)."""
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


# -- untrusted text in a parsed CI log ---------------------------------------------
#
# A directory entry name is untrusted text, and so is a verb name read back out of the probe.
#
# It parses it TWICE, with two different anchors, and modelling only the first is what #176 was:
#
# TryParseV2 `::name::data` tested after `message.TrimStart()`.
#
# So a skills entry named `plain<LF>::error::…` forges by breaking a line.
#
# This repository has had precisely this defect before (#40).


def _forging_dir(parent, label):
    """Stage a directory whose name breaks a line, or skip saying what went untested (#176)."""
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
    """The rule itself, on every leg. The end-to-end newline cases below need a filesystem that will hold a
    newline in a name, which Windows will not, so this is what keeps the claim asserted there."""
    assert _one_line("plain\n::error::forged") == "plain ::error::forged"
    assert _one_line("a\r\nb\tc  d") == "a b c d"

    # The newline-borne form: gone at the whitespace step, and the `::` broken besides, so the value is safe wherever it lands on a line rather than only where something else precedes it.
    assert _log_safe("plain\n::error::forged") == "plain : :error: :forged"
    # The form with no newline in it at all, which is #176.
    assert _log_safe("brief##[error]title=x") == "brief## [error]title=x"
    assert _log_safe("a\r\nb\tc  d") == "a b c d"

    # The two properties the exact strings above are only examples of.
    for hostile in ("::error::x", ":::error::x", "::::", "###[error]x", "##[a]##[b]", "#", "::",
                    "  ::error::x  ", "plain\n::error::x", "brief##[error]title=x"):
        assert not _log_safe(hostile).lstrip().startswith("::"), hostile
        assert "##[" not in _log_safe(hostile), hostile
        # Idempotent, because `_annotate` applies this as a backstop to a message whose parts were already sanitised where they entered.
        assert _log_safe(_log_safe(hostile)) == _log_safe(hostile), hostile

    # Must-not-fire: an ordinary path, and the lone colon every one of them carries, comes back byte-identical.
    assert _log_safe("ordinary/path/SKILL.md: Permission denied") == \
        "ordinary/path/SKILL.md: Permission denied"
    assert _log_safe("C:/Users/runner/work/requivo") == "C:/Users/runner/work/requivo"


def test_a_skill_directory_name_is_squashed_before_it_reaches_a_finding():
    """The `_label` half of the class, asserted on every platform including the one that cannot stage the
    fixture."""
    forged = Path("skills") / "plain\n::error::forged" / "SKILL.md"
    assert _label(forged) == "plain : :error: :forged"
    # The legacy form, which needs no newline and is therefore the half NTFS does not refuse (#176).
    legacy = Path("skills") / "brief##[error]title=forged" / "SKILL.md"
    assert _label(legacy) == "brief## [error]title=forged"
    # The must-not-fire half: an ordinary skill name comes back unchanged.
    assert _label(Path("skills") / "brief" / "SKILL.md") == "brief"


def test_an_unreadable_path_cannot_forge_a_line_of_its_own(tmp_path, capsys):
    """The half this diff introduced, end to end: an unreadable entry's name reaching `print` un-squashed,
    ahead of anything `_annotate` would have squashed on its way to an annotation."""
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
    """The `_label` half end to end, on a real tree."""
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
    """#176 itself, end to end, on every platform including the one that skips the two cases above."""
    skills = tmp_path / "skills"
    evil = skills / LEGACY_NAME
    # Deliberately not guarded the way `_forging_dir` is.
    evil.mkdir(parents=True)
    (evil / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "FORGED-BY-A-LEGACY-DIRECTORY-NAME" in out, "the harness never reached the name at all"
    _assert_no_forged_workflow_command(out)

    # The must-fire control.
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
    """The second instance of the class, found by sweeping this file rather than by being filed."""
    skills = tmp_path / "skills" / "brief"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    monkeypatch.setattr(drift, "cli_surface", lambda python: parse_surface(FORGED_PROBE_PAYLOAD))
    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "FORGED-BY-A-VERB-NAME" in out, "the harness never reached the verb name at all"
    _assert_no_forged_workflow_command(out)

    # The must-fire control: the same surface built without going through `parse_surface`.
    raw = Surface(version="1.0.1",
                  verbs={"model": {"apply", "show\n##[error]title=FORGED-BY-A-VERB-NAME::pwned"}})
    monkeypatch.setattr(drift, "cli_surface", lambda python: raw)
    main(["--released-python", sys.executable, "--plugin", str(tmp_path), "--github"])
    unguarded = capsys.readouterr().out
    assert "FORGED-BY-A-VERB-NAME" in unguarded, "the control never reached the verb name"
    with pytest.raises(AssertionError):
        _assert_no_forged_workflow_command(unguarded)


def _assert_no_forged_workflow_command(out):
    """No line may be readable as a workflow command by *either* of the runner's parsers (#176)."""
    forged = []
    for line in out.splitlines():
        if line.startswith("::warning title="):
            continue                                  # one this script authored
        if line.lstrip().startswith("::") or "##[" in line:
            forged.append(line)
    assert not forged, f"a value forged a workflow command the runner would act on: {forged}"


def test_the_forgery_guard_sees_both_command_forms():
    """The must-fire half of `_assert_no_forged_workflow_command`, and the reason it exists."""
    for forged in ("::error::pwned",                         # TryParseV2, column 0
                   "  ::error::pwned",                       # TryParseV2 after TrimStart
                   "  requivo model x   (referenced by: brief##[error]pwned)",   # TryParse, mid-line
                   "##[set-output]name=x"):                  # TryParse, column 0
        with pytest.raises(AssertionError):
            _assert_no_forged_workflow_command("state : drift\n" + forged + "\nplain tail\n")

    # Must-not-fire: an ordinary transcript, including the annotations this script does author and a lone colon of the kind every path and every reason line carries, has to come back clean.
    _assert_no_forged_workflow_command(
        "plugin root   : /repo/plugins/claude-code\n"
        "  requivo model rebase   (referenced by: brief)\n"
        "::warning title=Plugin/CLI drift::the released CLI (1.0.1) has no `requivo model`\n")


def test_a_skills_path_that_is_a_regular_file_is_absent_and_not_could_not_look(tmp_path):
    """The same three-way rule at the directory level, where it was stated and not applied."""
    (tmp_path / "skills").write_text("not a directory", encoding="utf-8")
    (tmp_path / "REASONING.md").write_text("Preflight: `requivo doctor`.", encoding="utf-8")

    sources = invocation_sources(tmp_path)
    assert [p.name for p in sources.paths] == ["REASONING.md"], sources.paths
    assert sources.unreadable == [], sources.unreadable


def test_the_reason_names_the_unreadable_path_when_nothing_could_be_extracted(tmp_path, capsys):
    """Could-not-look for the right reason, which is a separate question from could-not-look."""
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
    # The cause has to come before the consequence, or the first sentence a reader meets is the one that blames the plugin for an emptiness this process created.
    assert out.index("could not be read") < out.index("no `requivo` invocations were found"), out


def test_main_reports_could_not_look_when_it_could_only_walk_part_of_the_plugin(tmp_path, capsys):
    """The defect (#139), staged as the v1.1.0 release audit found it."""
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
    """The must-not-fire half, on the identical fixture with the mode change as the only difference."""
    _stage_partly_readable(tmp_path)
    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "files walked  : 3" in out, out
    assert "unreadable    : 0" in out, out
    assert "requivo model rebase" in out, out


def test_drift_in_the_part_it_could_read_outranks_the_part_it_could_not(tmp_path, capsys):
    """A complete answer outranks a partial one."""
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
