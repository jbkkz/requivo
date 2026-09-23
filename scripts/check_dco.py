"""Every commit in a pull request carries a `Signed-off-by:` matching its author (#439).

The DCO is a per-commit assertion, so commits are checked, never a PR description or squash message.
A script rather than workflow shell so `tests/test_dco_check.py` can call it and prove the must-fire half.

It prints SHAs and never a name, email or subject: those are contributor text, and a CI log parses
`::error::`/`##[error]` anywhere. There is no delimiter to embed (SHAs come from their own
`--format=%H`, then author and message are read per commit), and `_SHA_RE` is asserted on every line
before it is used, so a parse gone wrong raises instead of printing contributor text.

Exit codes: 0 every commit signed, 1 at least one is not, 2 the check could not look (no range, git
refused or could not run, a line that is not a commit id). Merge commits are skipped (their author is
whoever pressed the button) and counted on the success line rather than passed over in silence."""

from __future__ import annotations

import os
import re
import subprocess
import sys

# `Signed-off-by: Real Name <email@example.com>`, matched per line: trailer config varies by runner.
SIGNOFF = re.compile(r"^\s*Signed-off-by:\s*.+?\s*<(?P<email>[^<>]+)>\s*$", re.MULTILINE)

EXIT_OK = 0
EXIT_UNSIGNED = 1
EXIT_COULD_NOT_LOOK = 2

# A full commit id: the only thing this script prints, asserted on every line git hands back.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(args: list[str]) -> str:
    """One git command, decoded as UTF-8; never `text=True`, whose newline translation blurs lines (#456)."""
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def _commit_shas(base: str, head: str) -> tuple[list[str], int]:
    """`(the non-merge commit ids in base..head, how many merge commits were skipped)`; a non-id line raises."""
    shas = [line.strip() for line in _git(
        ["log", "--no-merges", "--format=%H", f"{base}..{head}"]).split("\n") if line.strip()]
    for sha in shas:
        if not _SHA_RE.match(sha):
            raise ValueError("git log returned a line that is not a commit id")
    merges = len([line for line in _git(
        ["log", "--merges", "--format=%H", f"{base}..{head}"]).split("\n") if line.strip()])
    return shas, merges


def _author_and_message(sha: str) -> tuple[str, str]:
    """One commit's author email (`%ae`, first line: git refuses a newline in an ident) and full message."""
    raw = _git(["show", "-s", "--format=%ae%n%B", sha])
    email, _, message = raw.partition("\n")
    return email, message


def unsigned_commits(base: str, head: str) -> tuple[list[str], int]:
    """`(the SHAs in base..head with no sign-off matching their own author, merge commits skipped)`.

        Compared on email, case-insensitively: a name has several legitimate spellings, an email is the
        identity git keys on.
    """
    shas, merges = _commit_shas(base, head)
    if not shas and not merges:
        raise LookupError("no commits in range")
    unsigned: list[str] = []
    for sha in shas:
        author_email, message = _author_and_message(sha)
        emails = {m.group("email").strip().lower() for m in SIGNOFF.finditer(message)}
        if author_email.strip().lower() not in emails:
            unsigned.append(sha)
    return unsigned, merges


def main(argv: list[str]) -> int:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if not base or not head:
        print("DCO: BASE_SHA and HEAD_SHA must both be set -- this check could not look",
              file=sys.stderr)
        return EXIT_COULD_NOT_LOOK
    try:
        unsigned, merges = unsigned_commits(base, head)
    except (subprocess.CalledProcessError, LookupError, ValueError, OSError) as exc:
        # A git that cannot run exits 2, not 1: "could not answer" is not "a commit is unsigned".
        print(f"DCO: could not read the commit range ({type(exc).__name__})", file=sys.stderr)
        return EXIT_COULD_NOT_LOOK
    skipped = f" {merges} merge commit(s) were not checked." if merges else ""
    if not unsigned:
        print("DCO: every non-merge commit carries a Signed-off-by matching its author." + skipped)
        return EXIT_OK
    print(f"DCO: {len(unsigned)} commit(s) carry no Signed-off-by matching their author:",
          file=sys.stderr)
    for sha in unsigned:
        print(f"  {sha}", file=sys.stderr)
    if skipped:
        print(skipped.strip(), file=sys.stderr)
    print(
        "\nSign off by certifying the Developer Certificate of Origin (https://developercertificate.org):"
        "\n  git commit -s            # on the next commit"
        "\n  git rebase --signoff " + base + "   # to add it to the commits above, then force-push"
        "\nCONTRIBUTING.md's 'Licensing of contributions' section states what you are certifying.",
        file=sys.stderr)
    return EXIT_UNSIGNED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
