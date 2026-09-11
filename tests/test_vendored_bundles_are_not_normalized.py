"""The two guards over the vendored third-party bundles: nothing normalizes their line endings
(#504), and their bytes are what was vendored (#510).

They are one file because they are one claim from two sides. `.gitattributes` asserts that a
checkout does not rewrite these files; `THIRD-PARTY-DIGESTS.txt` records what they are, so the
assertion can go red instead of being believed. Before #510 no digest was recorded anywhere, and a
hand-refresh that grabbed the wrong artifact -- or a local edit -- was undetectable from inside the
repository: `THIRD-PARTY-NOTICES.md` says of htmx that "the version above is therefore the only
record that this file has a version at all," which is exactly `CLAUDE.md`'s own named failure mode
(*a claim in prose that no test can falsify buys one release and then lies*) applied to a
byte-identity claim rather than to a number.

**Deliberately not a network call.** The point is to pin what was vendored, not to re-download it:
fetching from npm in CI would make the check flaky and would verify the registry rather than the
repository. The recorded digests were verified against `registry.npmjs.org` once, out of band, when
they were written -- 8 of 8 matching at swagger-ui-dist 5.32.15, redoc 2.5.3 and htmx.org 1.9.12,
with `git rev-parse HEAD:<path>` against `git hash-object` on each to confirm the *committed
objects* and not merely a working tree.

**The digest guard is what makes the `-text` rule enforceable rather than aspirational**, and that
is why both live here. A Windows checkout that rewrote a byte inside a minified string literal
changes the file's bytes, so it fails the digest -- which is the half `.gitattributes`'s own comment
concedes is missing when it says "nothing in this repository's CI verifies these bundles' served
bytes against what was vendored, on any platform."

The `-text` half, in its own words:

`.gitattributes` (#504) keeps git's line-ending normalization off every vendored, byte-exact
third-party bundle -- `THIRD-PARTY-NOTICES.md`'s own rule for each one is "no local edits, ever, or
the version stops describing what is shipped," and a `core.autocrlf` checkout is exactly that kind
of edit: it rewrites every LF byte with no awareness of whether it sits inside a JS/CSS string
literal or is merely source formatting. `redoc.standalone.js` is the reproduced instance -- 1837
lines, several of its own minified string/template literals spanning more than one line -- found by
the v3.2.0 release audit reviewing #504, because nothing before this file checked it.

`git check-attr` is the mechanism, not a proxy for it: it reads the same `.gitattributes` a real
`git checkout` consults, so this is the actual attribute a Windows clone would apply, asked directly
rather than inferred from file contents.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two vendor trees, scanned rather than enumerated. A hardcoded list is how a *new* vendored file
# inherits neither guard: the previous version of this file named five bundles and the trees held
# eight, so the three `.LICENSE.txt` files -- redistributed third-party notices, which
# `THIRD-PARTY-NOTICES.md` argues at length are part of what is shipped -- were covered by nothing.
_VENDOR_DIRS = (
    "src/requivo/api/static/vendor",
    "src/requivo/web/static/vendor",
)

# `shasum -a 256` output, verbatim and with no comment lines, so `shasum -a 256 -c
# THIRD-PARTY-DIGESTS.txt` (and GNU `sha256sum -c`) verifies the tree from the repository root with
# no argument and no parsing of ours. A `#` header would make macOS `shasum` warn on every run, and
# a file a human cannot run the standard tool against is a file they will not check.
_DIGESTS = REPO_ROOT / "THIRD-PARTY-DIGESTS.txt"


def _vendored_files() -> list[str]:
    """Every file under the vendor trees, repo-relative and POSIX-spelled.

    Fails when a tree is missing or a scan comes back empty, for the reason `tests/_scan.py` and
    `test_boundaries.py` both state: a glob over a directory that no longer exists returns `[]`, and
    `assert not []` is an all-clear nobody earned."""
    found: list[str] = []
    for rel in _VENDOR_DIRS:
        root = REPO_ROOT / rel
        assert root.is_dir(), f"{rel} is not a directory -- this whole module asserts nothing"
        found += [p.relative_to(REPO_ROOT).as_posix() for p in root.rglob("*") if p.is_file()]
    assert found, f"no vendored files found under {_VENDOR_DIRS} -- the scan set is empty"
    return sorted(found)


def _recorded_digests() -> dict[str, str]:
    """`THIRD-PARTY-DIGESTS.txt`, parsed. Explicitly UTF-8 (invariant 16); every line must parse,
    because a line this reader silently skipped is a file nothing checks."""
    digests: dict[str, str] = {}
    for line in _DIGESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, path = line.partition("  ")
        assert len(digest) == 64 and path, f"not a shasum -a 256 line: {line!r}"
        digests[path] = digest
    return digests


def _sha256(rel: str) -> str:
    """The file's digest over its **bytes** -- never a text read. That is the point rather than a
    detail: the hazard this pins is a line-ending rewrite, and a text read with universal newlines
    would normalize exactly the difference the digest exists to catch."""
    return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()


_VENDORED_BUNDLES = tuple(_vendored_files())

# The must-fire half (#503's own review found this exact gap in the two docstrings this file
# corrects, one file along): a file this policy does not name, so a check that always answers
# "unset" would pass whether or not `.gitattributes` said anything at all.
_NOT_VENDORED = (
    "src/requivo/web/templates/base.html",
    "src/requivo/web/static/css/app.css",
    "src/requivo/api/static/swagger-initializer.js",
    "src/requivo/api/static/favicon.svg",
)


def _text_attr(path: str) -> str:
    """The `text` attribute git would apply to `path` on checkout, per `.gitattributes` -- `"unset"`
    (`-text`, normalization off), `"unspecified"` (git's own default applies), or `"set"` (`text`,
    normalization forced on).

    Captured as **bytes** and decoded explicitly with `.decode("utf-8")` (invariant 16), not
    `subprocess.run(text=True)` -- mirroring `scripts/golden_lib.py`'s `_git()`, whose own docstring
    explains why: `text=True` turns on Python's universal-newlines translation, silently rewriting a
    lone `\r`/`\r\n` in the child's stdout into `\n` before any caller-level parsing runs (#456).
    Not live against the five hardcoded ASCII paths this file calls it with today -- `git
    check-attr` emits one line per call and `.strip()` already removes a trailing `\r` regardless --
    found by the v3.2.0 release audit reviewing #504 as the same pattern this repository already has
    a named rule against, not as a reproduced defect."""
    result = subprocess.run(
        ["git", "check-attr", "text", "--", path], cwd=REPO_ROOT, capture_output=True, check=True)
    stdout = result.stdout.decode("utf-8")
    # `git check-attr`'s own output shape: "<path>: text: <value>".
    return stdout.strip().rsplit(": ", 1)[-1]


# -- #510: the bytes are what was vendored ------------------------------------------------------


def test_every_vendored_file_has_a_recorded_digest_and_every_digest_a_file():
    """Both directions, because each catches a different mistake: a newly vendored file nobody
    recorded is covered by nothing, and a recorded line whose file is gone is a check that passes by
    having nothing to check -- the same shape `test_boundaries.py`'s allowlist asserts in both
    directions for its own entries."""
    recorded = set(_recorded_digests())
    present = set(_vendored_files())
    assert present - recorded == set(), (
        f"vendored with no recorded digest: {sorted(present - recorded)} -- add it to "
        f"{_DIGESTS.name} (`shasum -a 256 <path>`)")
    assert recorded - present == set(), (
        f"recorded in {_DIGESTS.name} but not in the tree: {sorted(recorded - present)}")


@pytest.mark.parametrize("rel", _VENDORED_BUNDLES)
def test_every_vendored_file_matches_its_recorded_digest(rel):
    """The claim `THIRD-PARTY-NOTICES.md` and `.gitattributes` both make, now falsifiable. Runs on
    every CI leg including Windows, which is where the line-ending hazard `.gitattributes` guards
    against would actually appear."""
    recorded = _recorded_digests()
    assert rel in recorded, f"{rel} has no recorded digest"
    assert _sha256(rel) == recorded[rel], (
        f"{rel} is not the bytes recorded in {_DIGESTS.name}. Either the file was edited -- which "
        f"THIRD-PARTY-NOTICES.md forbids outright -- or it was refreshed without updating the "
        f"digest and the version line beside it")


def test_a_single_changed_byte_fails_the_digest(tmp_path):
    """Must-fire control. Without it, a digest function that returned a constant, or a comparison
    that always held, would pass every row above -- and this guard exists precisely because nothing
    in the tree could previously tell a correct vendored file from a wrong one."""
    rel = _VENDORED_BUNDLES[0]
    original = (REPO_ROOT / rel).read_bytes()
    altered = tmp_path / "altered"
    altered.write_bytes(original + b" ")
    assert hashlib.sha256(altered.read_bytes()).hexdigest() != _recorded_digests()[rel]
    # And the other half: the unmodified bytes do match, so the inequality above is about the change
    # rather than about the comparison being broken in both directions.
    assert hashlib.sha256(original).hexdigest() == _recorded_digests()[rel]


# -- #504: nothing normalizes their line endings -------------------------------------------------


def test_every_vendored_bundle_is_exempt_from_line_ending_normalization():
    for rel in _VENDORED_BUNDLES:
        assert (REPO_ROOT / rel).is_file(), f"{rel} does not exist -- this row asserts nothing"
        assert _text_attr(rel) == "unset", (
            f"{rel} is a vendored, byte-exact third-party bundle and must be exempt (-text) from "
            f"git's line-ending normalization, or a Windows checkout can silently rewrite what it "
            f"actually contains")


def test_an_ordinary_first_party_file_is_not_swept_into_the_exemption():
    """Must-fire control: `.gitattributes` scopes `-text` to `*/static/vendor/**` specifically, and
    this is the row that would catch a future edit widening that pattern by accident -- a first-party
    file this project's own encoding/line-ending guards (`test_encoding.py`,
    `test_the_digest_is_the_same_whatever_the_line_endings`) still need to reason about as ordinary
    text must not read as "unset" here."""
    for rel in _NOT_VENDORED:
        assert (REPO_ROOT / rel).is_file(), f"{rel} does not exist -- this row asserts nothing"
        assert _text_attr(rel) == "unspecified", (
            f"{rel} is not a vendored bundle and must be left to git's own default, not swept into "
            f"the vendor exemption")
