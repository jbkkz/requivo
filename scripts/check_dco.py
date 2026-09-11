"""Every commit in a pull request carries a `Signed-off-by:` matching its author (#439).

The Developer Certificate of Origin is a per-commit assertion by the person who wrote the commit, so
this checks commits and not the pull request: a sign-off in a description, a comment or a squash
message certifies nothing about the commits under it.

**Written as a script rather than as shell in the workflow, deliberately.** The one other place this
repository put logic in a workflow's `run:` block had to be extracted back out of the YAML and run
under `bash` to be testable at all (`tests/test_workflow_untrusted_output.py` says so at length). A
script is imported and called directly by `tests/test_dco_check.py`, which is what lets the
must-fire half -- *a missing sign-off is actually caught* -- be a test rather than a hope.

**It prints SHAs and never a name, an email or a subject.** Those are contributor-written text, and
a CI log is parsed: `##[error]` needs no line start and `::` survives an indent, which is the class
`tests/test_workflow_untrusted_output.py` documents against the runner's own parser. A SHA is fixed
hex, so the whole class is sidestepped rather than contained -- and the contributor does not need to
be told their own name back to act on the message.

Exit codes: 0 every commit signed, 1 at least one is not, 2 this check could not look (no range, or
git refused). The third is not folded into either: an empty commit list would otherwise be an
all-clear nobody earned, which is the rule `tests/test_boundaries.py` applies to its own scan set.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# `Signed-off-by: Real Name <email@example.com>` -- the trailer `git commit -s` writes. Matched
# per line rather than as a trailer block because `git interpret-trailers` is not guaranteed to be
# configured the same way on every runner, and the shape is fixed by convention.
SIGNOFF = re.compile(r"^\s*Signed-off-by:\s*.+?\s*<(?P<email>[^<>]+)>\s*$", re.MULTILINE)

EXIT_OK = 0
EXIT_UNSIGNED = 1
EXIT_COULD_NOT_LOOK = 2

# Separates the fields of one `git log --format` record. `%x1f` is the ASCII unit separator: it
# cannot occur in an email or a message, unlike every printable character a subject may carry.
_SEP = "\x1f"
_RECORD = "\x1e"


def _git(args: list[str]) -> str:
    """One git command, decoded explicitly as UTF-8.

    Never `subprocess.run(text=True)`: that turns on Python's universal-newline translation, which
    silently rewrites a lone `\\r` or a `\\r\\n` in the child's stdout into `\\n` before any parsing
    here runs. `scripts/golden_lib.py`'s `_git` carries the same note for the same reason (#456), and
    the record separator below is exactly the boundary that translation would blur.
    """
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def unsigned_commits(base: str, head: str) -> list[str]:
    """The SHAs in `base..head` whose message carries no sign-off matching the commit's own author.

    `--no-merges`: this repository requires a linear history, so a merge commit in a pull request is
    already refused by branch protection -- and a merge commit's author is whoever pressed the
    button, not the person certifying provenance. Checking one would ask the wrong person to sign.

    The comparison is on **email**, case-insensitively, and not on the display name. A name is
    spelled several legitimate ways by one person (an accent dropped, a middle initial), an email is
    the identity git itself keys on, and requiring both would refuse correct sign-offs for a
    difference that certifies nothing.
    """
    fmt = f"%H{_SEP}%ae{_SEP}%B{_RECORD}"
    raw = _git(["log", "--no-merges", f"--format={fmt}", f"{base}..{head}"])
    unsigned: list[str] = []
    found_any = False
    for record in raw.split(_RECORD):
        if not record.strip():
            continue
        found_any = True
        sha, _, rest = record.lstrip("\n").partition(_SEP)
        author_email, _, message = rest.partition(_SEP)
        emails = {m.group("email").strip().lower() for m in SIGNOFF.finditer(message)}
        if author_email.strip().lower() not in emails:
            unsigned.append(sha)
    if not found_any:
        raise LookupError("no commits in range")
    return unsigned


def main(argv: list[str]) -> int:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if not base or not head:
        print("DCO: BASE_SHA and HEAD_SHA must both be set -- this check could not look",
              file=sys.stderr)
        return EXIT_COULD_NOT_LOOK
    try:
        unsigned = unsigned_commits(base, head)
    except (subprocess.CalledProcessError, LookupError) as exc:
        # Deliberately not folded into a refusal: "git could not answer" and "a commit is unsigned"
        # are different facts, and a contributor told the second about the first has nothing to fix.
        print(f"DCO: could not read the commit range ({type(exc).__name__})", file=sys.stderr)
        return EXIT_COULD_NOT_LOOK
    if not unsigned:
        print("DCO: every commit carries a Signed-off-by matching its author.")
        return EXIT_OK
    print(f"DCO: {len(unsigned)} commit(s) carry no Signed-off-by matching their author:",
          file=sys.stderr)
    for sha in unsigned:
        print(f"  {sha}", file=sys.stderr)
    print(
        "\nSign off by certifying the Developer Certificate of Origin (https://developercertificate.org):"
        "\n  git commit -s            # on the next commit"
        "\n  git rebase --signoff " + base + "   # to add it to the commits above, then force-push"
        "\nCONTRIBUTING.md's 'Licensing of contributions' section states what you are certifying.",
        file=sys.stderr)
    return EXIT_UNSIGNED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
