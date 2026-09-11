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

**That claim was false in the first cut of this file, and the review reproduced it.** The parser
read one `git log` whose records were separated by `\x1e` and whose fields were separated by
`\x1f`. Neither byte is absent from a commit *message*, which is fully contributor-controlled: a
`git commit -m $'...\x1e::error::forged\n##[error]forged'` split into a second forged "record"
whose "sha" was the attacker's own text, printed by the loop at the foot of `main` -- and its second
line landed at column 0 of the CI log, which is exactly what the paragraph above says cannot happen.
No sign-off was bypassed (the real commit was still flagged), but a contributor could inject
`::error::` / `##[error]` / `::add-mask::` into this workflow's log by opening a pull request.

Two things hold it now, and the second is the one that does not depend on getting a parser right:

- **There is no delimiter to embed.** The SHAs come from their own `git log --format=%H`, one per
  line, and a SHA cannot contain a newline; the author and message are then read per commit, where
  the whole output *is* the field and nothing has to be split out of it. Choosing a rarer separator
  byte (NUL was the obvious candidate) would have been the narrower fix; removing the split is the
  one that cannot be wrong about which bytes a message may hold.
- **Nothing that is not a SHA is ever printed.** `_SHA_RE` is asserted on every line before it is
  used or reported, so a parse that went wrong raises rather than rendering contributor text. That
  is the guard that would have caught the defect above even with the old delimiters, which is why
  it is here as well as rather than instead of.

Exit codes: 0 every commit signed, 1 at least one is not, 2 this check could not look (no range, git
refused, git could not be run at all, or a line came back that is not a commit id). The third is not
folded into either: an empty commit list would otherwise be an all-clear nobody earned, which is the
rule `tests/test_boundaries.py` applies to its own scan set.

**A merge commit is not checked, and is reported rather than passed over in silence.** `--no-merges`
is right about the metadata -- a merge commit's author is whoever pressed the button, not the person
certifying provenance -- and wrong to leave implicit, because a merge that resolved a conflict
carries a tree diff of its own that no parent certifies. This repository requires a linear history,
so such a commit cannot reach `main` anyway; what the receipt must not do is say *every commit
carries a sign-off* when it declined to look at one. The count is stated on the success line.
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

# A full commit id, and the only thing this script ever prints. Asserted on every line `git log`
# hands back, so a parse that went wrong raises instead of rendering whatever it found -- see the
# module docstring for the defect that makes this a guard rather than a formality.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(args: list[str]) -> str:
    """One git command, decoded explicitly as UTF-8.

    Never `subprocess.run(text=True)`: that turns on Python's universal-newline translation, which
    silently rewrites a lone `\\r` or a `\\r\\n` in the child's stdout into `\\n` before any parsing
    here runs. `scripts/golden_lib.py`'s `_git` carries the same note for the same reason (#456), and
    the line boundary this script splits on is exactly what that translation would blur.
    """
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def _commit_shas(base: str, head: str) -> tuple[list[str], int]:
    """`(the non-merge commit ids in base..head, how many merge commits were skipped)`.

    One field, one commit per line, and a line that is not a commit id is a parse this function
    refuses rather than reports -- there is nothing for a message to embed here, because `%H` is the
    whole of the output."""
    shas = [line.strip() for line in _git(
        ["log", "--no-merges", "--format=%H", f"{base}..{head}"]).split("\n") if line.strip()]
    for sha in shas:
        if not _SHA_RE.match(sha):
            raise ValueError("git log returned a line that is not a commit id")
    merges = len([line for line in _git(
        ["log", "--merges", "--format=%H", f"{base}..{head}"]).split("\n") if line.strip()])
    return shas, merges


def _author_and_message(sha: str) -> tuple[str, str]:
    """One commit's author email and full message.

    `%ae` on the first line and `%B` after it: git refuses a newline inside an ident field, so the
    first line is the whole email and the rest is the whole message. Nothing is split out of a
    field a contributor controls."""
    raw = _git(["show", "-s", "--format=%ae%n%B", sha])
    email, _, message = raw.partition("\n")
    return email, message


def unsigned_commits(base: str, head: str) -> tuple[list[str], int]:
    """`(the SHAs in base..head with no sign-off matching their own author, merge commits skipped)`.

    `--no-merges`: a merge commit's author is whoever pressed the button, not the person certifying
    provenance, so checking one would ask the wrong person to sign. The count comes back rather than
    being dropped, because a receipt that says *every commit carries a sign-off* after declining to
    look at one is overstating what it verified -- which is the whole of what this script is for.

    The comparison is on **email**, case-insensitively, and not on the display name. A name is
    spelled several legitimate ways by one person (an accent dropped, a middle initial), an email is
    the identity git itself keys on, and requiring both would refuse correct sign-offs for a
    difference that certifies nothing.
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
        # `OSError` covers a git that cannot be spawned at all -- the shape this project already
        # names for an unspawnable binary, which a bare `CalledProcessError` arm would have let
        # through as a traceback instead of the exit code this script's own contract promises.
        #
        # Deliberately not folded into a refusal: "git could not answer" and "a commit is unsigned"
        # are different facts, and a contributor told the second about the first has nothing to fix.
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
