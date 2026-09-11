"""The DCO check catches an unsigned commit, and says so about the right one (#439).

`scripts/check_dco.py` is the whole of the gate `.github/workflows/dco.yml` runs, so a test that
only asserted "a signed branch passes" would be green against a script that always passes -- which
is the failure mode a provenance check least survives. Every row below builds a real repository in a
temp directory and runs the real function over it: no mocked git, because what is under test is what
git reports, and a stub would be asserting this file's own idea of `--format`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_dco  # noqa: E402
from check_dco import EXIT_COULD_NOT_LOOK, EXIT_UNSIGNED, main, unsigned_commits  # noqa: E402

AUTHOR = "A Contributor <contributor@example.com>"
SIGNOFF = "Signed-off-by: A Contributor <contributor@example.com>"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


def _commit(repo: Path, message: str, *, author: str = AUTHOR) -> str:
    """One commit with `message` as its whole body, so a trailer in the fixture is a trailer in the
    commit. A module-level function rather than a fixture attribute: `Path` defines `__slots__`, so
    the helper cannot ride on the path object the tests already pass to `_git`."""
    (repo / "f.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "--author", author, "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A repository with one signed commit on `main`.

    `user.name`/`user.email` are set on the repository rather than inherited, so this test asserts
    the same thing on a maintainer's machine and on a runner with no global git identity."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "A Contributor")
    _git(path, "config", "user.email", "contributor@example.com")
    _commit(path, "base\n\n" + SIGNOFF)
    return path


def test_a_signed_commit_passes(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "a change\n\n" + SIGNOFF)
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == []


def test_an_unsigned_commit_is_caught_and_named(repo, monkeypatch):
    """The must-fire row. Without it every other assertion here is satisfied by a function that
    returns `[]` unconditionally, which is exactly what a provenance gate must not be."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "a change with no sign-off")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == [head]


def test_only_the_unsigned_commit_is_named(repo, monkeypatch):
    """A branch is not all-or-nothing: the message has to point at the commits to fix, or the
    contributor rewrites history they did not need to."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "signed\n\n" + SIGNOFF)
    bad = _commit(repo, "unsigned")
    head = _commit(repo, "signed again\n\n" + SIGNOFF)
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == [bad]
    assert head not in unsigned_commits(base, head)[0]


def test_a_sign_off_by_somebody_else_does_not_sign_this_commit(repo, monkeypatch):
    """The DCO is a statement by the person who wrote the commit. A trailer naming anyone else is a
    sign-off on someone else's behalf, which is the one thing the certificate cannot be."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: Someone Else <other@example.com>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == [head]


def test_the_email_comparison_ignores_case_but_not_identity(repo, monkeypatch):
    """Case-insensitive, because an email is case-insensitive in the part that matters and a
    contributor whose client upcased it has certified exactly the same thing. Not name-insensitive
    in the other direction: the row above is what stops this widening into "any trailer will do"."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: A CONTRIBUTOR <Contributor@Example.COM>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == []


def test_a_name_spelled_differently_still_signs(repo, monkeypatch):
    """Deliberate: the check keys on the email git itself keys on. Refusing a correct sign-off over
    a dropped accent or a middle initial would fail a contributor for a difference that certifies
    nothing."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: A. Contributor <contributor@example.com>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == []


def test_an_empty_range_is_refused_rather_than_read_as_a_pass(repo, monkeypatch):
    """The third state. A range with no commits is not "every commit is signed" -- it is a check
    that could not look, and folding it into a pass is the all-clear nobody earned that
    `tests/test_boundaries.py` refuses for its own scan set."""
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    with pytest.raises(LookupError):
        unsigned_commits(head, head)


def test_a_range_git_cannot_read_exits_could_not_look_rather_than_unsigned(repo, monkeypatch):
    """`git refused` and `a commit is unsigned` are different facts, and a contributor told the
    second about the first has nothing to fix."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", "0" * 40)
    monkeypatch.setenv("HEAD_SHA", "HEAD")
    assert main([]) == EXIT_COULD_NOT_LOOK


def test_no_range_at_all_is_could_not_look(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    assert main([]) == EXIT_COULD_NOT_LOOK


def test_the_exit_code_separates_unsigned_from_clean(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "unsigned")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", _git(repo, "rev-parse", "HEAD"))
    assert main([]) == EXIT_UNSIGNED


def test_the_failure_message_names_no_contributor_written_text(repo, monkeypatch, capsys):
    """A CI log is parsed -- `##[error]` needs no line start and `::` survives an indent -- so this
    script prints SHAs and nothing a contributor chose. The forged subject below would be a workflow
    command if it reached the log; the assertion is that it does not."""
    forged = "::error::forged\n##[error]forged"
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, forged)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", _git(repo, "rev-parse", "HEAD"))
    main([])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "forged" not in combined, "a contributor-written subject reached the log"
    assert "contributor@example.com" not in combined, "an author email reached the log"


# -- the parse itself, which the review of #518 reproduced a forgery through ---------------------

FORGED_BODY = "a change\x1e::error::forged-by-a-contributor\n##[error]forged-workflow-command"


def test_a_commit_message_cannot_split_itself_into_a_second_record(repo, monkeypatch, capsys):
    """The defect the review of this change reproduced against the shipped function.

    The first cut read one `git log` whose records were separated by `\\x1e` and whose fields were
    separated by `\\x1f`. Neither byte is absent from a commit message -- it is contributor-written
    text -- so a message carrying `\\x1e` split into a second forged record whose "sha" was the
    attacker's own text, which `main` then printed. Its second line landed at **column 0** of the CI
    log, which is exactly what this script's own docstring says cannot happen: `##[error]` needs no
    line start and `::` survives an indent.

    No sign-off was bypassed then and none is now -- the real commit was flagged either way. What
    this pins is the other half: a contributor cannot write a line of this workflow's log.
    """
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, FORGED_BODY)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", sha)
    assert main([]) == EXIT_UNSIGNED

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # The real commit is still caught -- without this the assertions below pass on a run that
    # reported nothing at all.
    assert sha in combined
    assert "forged" not in combined, (
        "contributor-written message text reached the log: a commit message can embed any byte, so "
        "the parse must not be splitting fields out of one")
    for line in combined.splitlines():
        assert not line.lstrip().startswith("::"), f"a workflow command was forged: {line!r}"
        assert "##[" not in line, f"a legacy workflow command was forged: {line!r}"


def test_only_commit_ids_are_ever_printed(repo, monkeypatch, capsys):
    """The guard that does not depend on getting the parse right. Every line `git log` hands back is
    matched against `_SHA_RE` before it is used, so a parse that went wrong raises rather than
    rendering whatever it found -- which would have caught the defect above even with the old
    delimiters in place."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "unsigned")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", _git(repo, "rev-parse", "HEAD"))
    main([])
    # Only the listing block: the remedy below it is this file's own prose and is indented too.
    printed = capsys.readouterr().err.split("\nSign off by")[0]
    listed = [ln.strip() for ln in printed.splitlines() if ln.startswith("  ") and ln.strip()]
    for item in listed:
        assert check_dco._SHA_RE.match(item), f"something that is not a commit id was printed: {item!r}"
    assert listed, "nothing was listed, so this row asserts nothing"


def test_a_line_that_is_not_a_commit_id_is_refused_rather_than_reported(repo, monkeypatch):
    """`git log` handing back something that is not a commit id means the parse is wrong, and a
    wrong parse must raise rather than render. Simulated at `_git`, the one seam a forged line could
    arrive through, because git itself will not produce one."""
    monkeypatch.setattr(check_dco, "_git", lambda _args: "not-a-sha\n")
    with pytest.raises(ValueError):
        unsigned_commits("a", "b")


def test_a_merge_commit_is_reported_as_not_checked_rather_than_passed_over(repo, monkeypatch,
                                                                          capsys):
    """`--no-merges` is right about the metadata and wrong to leave implicit: a merge that resolved
    a conflict carries a tree diff of its own that no parent certifies. This repository requires a
    linear history so such a commit cannot reach `main` -- what the receipt must not do is claim
    *every commit carries a sign-off* after declining to look at one."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-q", "-m", "side\n\n" + SIGNOFF)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "mainline\n\n" + SIGNOFF)
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge\n\n" + SIGNOFF)
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)

    unsigned, merges = unsigned_commits(base, head)
    assert unsigned == [], "the non-merge commits are all signed"
    assert merges == 1, "the merge commit was not counted"

    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "merge commit(s) were not checked" in out, (
        "the success line claims more than the check looked at")


def test_a_git_that_cannot_be_run_at_all_is_could_not_look(repo, monkeypatch):
    """An unspawnable binary raises `FileNotFoundError`, an `OSError` -- the shape this project
    names -- and a bare `CalledProcessError` arm would have let it out as a traceback instead of the
    exit code this script's own contract promises."""
    def _absent(_args):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(check_dco, "_git", _absent)
    monkeypatch.setenv("BASE_SHA", "a")
    monkeypatch.setenv("HEAD_SHA", "b")
    assert main([]) == EXIT_COULD_NOT_LOOK
