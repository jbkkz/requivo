"""The two guards over the vendored third-party bundles: nothing normalizes their line endings (#504), and
their bytes are what was vendored (#510)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two vendor trees, scanned rather than enumerated.
_VENDOR_DIRS = (
    "src/requivo/api/static/vendor",
    "src/requivo/web/static/vendor",
)

# `shasum -a 256` output, verbatim and with no comment lines, so `shasum -a 256 -c THIRD-PARTY-DIGESTS.txt` (and GNU `sha256sum -c`) verifies the tree from the repository root with no argument and no parsing of ours.
_DIGESTS = REPO_ROOT / "THIRD-PARTY-DIGESTS.txt"


def _vendored_files() -> list[str]:
    """Every file under the vendor trees, repo-relative and POSIX-spelled."""
    found: list[str] = []
    for rel in _VENDOR_DIRS:
        root = REPO_ROOT / rel
        assert root.is_dir(), f"{rel} is not a directory -- this whole module asserts nothing"
        found += [p.relative_to(REPO_ROOT).as_posix() for p in root.rglob("*") if p.is_file()]
    assert found, f"no vendored files found under {_VENDOR_DIRS} -- the scan set is empty"
    return sorted(found)


def _recorded_digests() -> dict[str, str]:
    """`THIRD-PARTY-DIGESTS.txt`, parsed. Explicitly UTF-8 (invariant 16)."""
    digests: dict[str, str] = {}
    for line in _DIGESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, path = line.partition("  ")
        assert len(digest) == 64 and path, f"not a shasum -a 256 line: {line!r}"
        digests[path] = digest
    return digests


def _sha256(rel: str) -> str:
    """The file's digest over its **bytes** -- never a text read."""
    return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()


_VENDORED_BUNDLES = tuple(_vendored_files())

# The must-fire half (#503's own review found this exact gap in the two docstrings this file corrects, one file along): a file this policy does not name, so a check that always answers "unset" would pass whether or not `.gitattributes` said anything at all.
_NOT_VENDORED = (
    "src/requivo/web/templates/base.html",
    "src/requivo/web/static/css/app.css",
    "src/requivo/api/static/swagger-initializer.js",
    "src/requivo/api/static/favicon.svg",
)


def _text_attr(path: str) -> str:
    """The `text` attribute git would apply to `path` on checkout, per `.gitattributes` (#456)."""
    result = subprocess.run(
        ["git", "check-attr", "text", "--", path], cwd=REPO_ROOT, capture_output=True, check=True)
    stdout = result.stdout.decode("utf-8")
    # `git check-attr`'s own output shape: "<path>: text: <value>".
    return stdout.strip().rsplit(": ", 1)[-1]


# -- #510: the bytes are what was vendored ------------------------------------------------------


def _membership_gap(present, recorded) -> tuple[list[str], list[str]]:
    """`(vendored with no recorded digest, recorded with no file)`."""
    return sorted(set(present) - set(recorded)), sorted(set(recorded) - set(present))


def test_every_vendored_file_has_a_recorded_digest_and_every_digest_a_file():
    """Both directions, because each catches a different mistake."""
    unrecorded, orphaned = _membership_gap(_vendored_files(), _recorded_digests())
    assert not unrecorded, (
        f"vendored with no recorded digest: {unrecorded} -- add it to {_DIGESTS.name} "
        f"(`shasum -a 256 <path>`)")
    assert not orphaned, (
        f"recorded in {_DIGESTS.name} but not in the tree: {orphaned}")


def test_the_membership_check_notices_each_direction_it_claims_to():
    """Must-fire control for the row above (`test_a_single_changed_byte_fails_the_digest` is its byte-content
    sibling)."""
    assert _membership_gap(["a"], {"a": "x"}) == ([], [])
    assert _membership_gap(["a", "b"], {"a": "x"}) == (["b"], []), "a file with no digest"
    assert _membership_gap(["a"], {"a": "x", "b": "y"}) == ([], ["b"]), "a digest with no file"
    assert _membership_gap(["a"], {"b": "y"}) == (["a"], ["b"]), "both at once, stated separately"


@pytest.mark.parametrize("rel", _VENDORED_BUNDLES)
def test_every_vendored_file_matches_its_recorded_digest(rel):
    """The claim `THIRD-PARTY-NOTICES.md` and `.gitattributes` both make, now falsifiable."""
    recorded = _recorded_digests()
    assert rel in recorded, f"{rel} has no recorded digest"
    assert _sha256(rel) == recorded[rel], (
        f"{rel} is not the bytes recorded in {_DIGESTS.name}. Either the file was edited -- which "
        f"THIRD-PARTY-NOTICES.md forbids outright -- or it was refreshed without updating the "
        f"digest and the version line beside it")


def test_a_single_changed_byte_fails_the_digest(tmp_path):
    """Must-fire control. Without it, a digest function that returned a constant, or a comparison that always
    held, would pass every row above."""
    rel = _VENDORED_BUNDLES[0]
    original = (REPO_ROOT / rel).read_bytes()
    altered = tmp_path / "altered"
    altered.write_bytes(original + b" ")
    assert hashlib.sha256(altered.read_bytes()).hexdigest() != _recorded_digests()[rel]
    # And the other half: the unmodified bytes do match, so the inequality above is about the change rather than about the comparison being broken in both directions.
    assert hashlib.sha256(original).hexdigest() == _recorded_digests()[rel]


# -- #504: nothing normalizes their line endings -------------------------------------------------


# Two classes merged into one table (#555): every vendored bundle must be exempt (-text) from line-ending normalization, and -- the must-fire control -- an ordinary first-party file must NOT be swept into that exemption.
@pytest.mark.parametrize("rel, expected", (
    [(rel, "unset") for rel in _VENDORED_BUNDLES]
    + [(rel, "unspecified") for rel in _NOT_VENDORED]
), ids=[f"vendored:{rel}" for rel in _VENDORED_BUNDLES] + [f"not-vendored:{rel}" for rel in _NOT_VENDORED])
def test_a_vendored_bundle_is_exempt_and_an_ordinary_file_is_not(rel, expected):
    assert (REPO_ROOT / rel).is_file(), f"{rel} does not exist -- this row asserts nothing"
    assert _text_attr(rel) == expected, (
        f"{rel} should have text={expected!r} per .gitattributes -- a vendored, byte-exact "
        f"third-party bundle must be exempt from line-ending normalization, and an ordinary "
        f"first-party file must be left to git's own default, not swept into the exemption")
