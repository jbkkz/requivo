"""The DCO check catches an unsigned commit and names the right one (#439). Every row builds a real repository
and runs the real `scripts/check_dco.py` over it: what is under test is what git reports."""

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
    """One commit with `message` as its whole body, so a trailer in the fixture is a trailer in the commit."""
    (repo / "f.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "--author", author, "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A repository with one signed commit on `main`; the identity is set locally so a runner needs none."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "A Contributor")
    _git(path, "config", "user.email", "contributor@example.com")
    _commit(path, "base\n\n" + SIGNOFF)
    return path


def _range_env(repo, monkeypatch, base, head):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)


@pytest.mark.parametrize("trailer, unsigned", [
    (SIGNOFF, False),
    ("Signed-off-by: Someone Else <other@example.com>", True),   # a sign-off on someone else's behalf is none
    ("Signed-off-by: A CONTRIBUTOR <Contributor@Example.COM>", False),  # email case-insensitive
    ("Signed-off-by: A. Contributor <contributor@example.com>", False),  # keyed on the email, not the name
], ids=["signed", "somebody-else", "upcased-email", "respelled-name"])
def test_a_sign_off_counts_only_when_it_is_the_authors(repo, monkeypatch, trailer, unsigned):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "a change\n\n" + trailer)
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == ([head] if unsigned else [])


def test_only_the_unsigned_commit_is_named(repo, monkeypatch):
    """MUST-FIRE: the message points at the commits to fix, not the whole branch."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "signed\n\n" + SIGNOFF)
    bad = _commit(repo, "unsigned")
    head = _commit(repo, "signed again\n\n" + SIGNOFF)
    monkeypatch.chdir(repo)
    assert unsigned_commits(base, head)[0] == [bad]


def test_an_empty_range_is_refused_rather_than_read_as_a_pass(repo, monkeypatch):
    """The third state: a range with no commits is a check that could not look."""
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    with pytest.raises(LookupError):
        unsigned_commits(head, head)


def test_a_range_git_cannot_read_exits_could_not_look_rather_than_unsigned(repo, monkeypatch):
    _range_env(repo, monkeypatch, "0" * 40, "HEAD")
    assert main([]) == EXIT_COULD_NOT_LOOK


def test_no_range_at_all_is_could_not_look(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    assert main([]) == EXIT_COULD_NOT_LOOK


def test_the_exit_code_separates_unsigned_from_clean(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "unsigned")
    _range_env(repo, monkeypatch, base, _git(repo, "rev-parse", "HEAD"))
    assert main([]) == EXIT_UNSIGNED


FORGED_BODY = "a change\x1e::error::forged-by-a-contributor\n##[error]forged-workflow-command"


@pytest.mark.parametrize("subject", ["::error::forged\n##[error]forged", FORGED_BODY], ids=["subject", "record-separator"])
def test_the_failure_message_names_no_contributor_written_text(repo, monkeypatch, capsys, subject):
    """A CI log is parsed, so only SHAs are printed; `\\x1e` must not split a message into a forged second record (#518)."""
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, subject)
    _range_env(repo, monkeypatch, base, sha)
    assert main([]) == EXIT_UNSIGNED
    combined = "".join(capsys.readouterr())
    assert sha in combined, "nothing was reported, so the assertions below prove nothing"
    assert "forged" not in combined and "contributor@example.com" not in combined, "contributor-written text reached the log"
    for line in combined.splitlines():
        assert not line.lstrip().startswith("::") and "##[" not in line, f"a workflow command was forged: {line!r}"


def test_only_commit_ids_are_ever_printed(repo, monkeypatch, capsys):
    """Every line `git log` hands back is matched against `_SHA_RE` before it is used."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "unsigned")
    _range_env(repo, monkeypatch, base, _git(repo, "rev-parse", "HEAD"))
    main([])
    printed = capsys.readouterr().err.split("\nSign off by")[0]  # the listing block only; the remedy is our own prose
    listed = [ln.strip() for ln in printed.splitlines() if ln.startswith("  ") and ln.strip()]
    assert listed, "nothing was listed, so this row asserts nothing"
    for item in listed:
        assert check_dco._SHA_RE.match(item), f"something that is not a commit id was printed: {item!r}"


def test_a_line_that_is_not_a_commit_id_is_refused_rather_than_reported(repo, monkeypatch):
    monkeypatch.setattr(check_dco, "_git", lambda _args: "not-a-sha\n")
    with pytest.raises(ValueError):
        unsigned_commits("a", "b")


def test_a_merge_commit_is_reported_as_not_checked_rather_than_passed_over(repo, monkeypatch, capsys):
    """`--no-merges` is right and must not stay implicit: the receipt says what it declined to look at."""
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
    assert unsigned == [] and merges == 1
    _range_env(repo, monkeypatch, base, head)
    assert main([]) == 0
    assert "merge commit(s) were not checked" in capsys.readouterr().out, "the success line claims more than the check looked at"


def test_a_git_that_cannot_be_run_at_all_is_could_not_look(repo, monkeypatch):
    """An unspawnable binary is an `OSError`, the shape this project names, not a traceback."""
    def _absent(_args):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(check_dco, "_git", _absent)
    _range_env(repo, monkeypatch, "a", "b")
    assert main([]) == EXIT_COULD_NOT_LOOK
