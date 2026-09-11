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
    assert unsigned_commits(base, head) == []


def test_an_unsigned_commit_is_caught_and_named(repo, monkeypatch):
    """The must-fire row. Without it every other assertion here is satisfied by a function that
    returns `[]` unconditionally, which is exactly what a provenance gate must not be."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "a change with no sign-off")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head) == [head]


def test_only_the_unsigned_commit_is_named(repo, monkeypatch):
    """A branch is not all-or-nothing: the message has to point at the commits to fix, or the
    contributor rewrites history they did not need to."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "signed\n\n" + SIGNOFF)
    bad = _commit(repo, "unsigned")
    head = _commit(repo, "signed again\n\n" + SIGNOFF)
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head) == [bad]
    assert head not in unsigned_commits(base, head)


def test_a_sign_off_by_somebody_else_does_not_sign_this_commit(repo, monkeypatch):
    """The DCO is a statement by the person who wrote the commit. A trailer naming anyone else is a
    sign-off on someone else's behalf, which is the one thing the certificate cannot be."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: Someone Else <other@example.com>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head) == [head]


def test_the_email_comparison_ignores_case_but_not_identity(repo, monkeypatch):
    """Case-insensitive, because an email is case-insensitive in the part that matters and a
    contributor whose client upcased it has certified exactly the same thing. Not name-insensitive
    in the other direction: the row above is what stops this widening into "any trailer will do"."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: A CONTRIBUTOR <Contributor@Example.COM>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head) == []


def test_a_name_spelled_differently_still_signs(repo, monkeypatch):
    """Deliberate: the check keys on the email git itself keys on. Refusing a correct sign-off over
    a dropped accent or a middle initial would fail a contributor for a difference that certifies
    nothing."""
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, # type: ignore[attr-defined]
        "a change\n\nSigned-off-by: A. Contributor <contributor@example.com>")
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head) == []


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
