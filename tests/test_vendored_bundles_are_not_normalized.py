"""`.gitattributes` (#504) keeps git's line-ending normalization off every vendored, byte-exact
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

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_VENDORED_BUNDLES = (
    "src/requivo/web/static/vendor/htmx.min.js",
    "src/requivo/api/static/vendor/swagger-ui/swagger-ui-bundle.js",
    "src/requivo/api/static/vendor/swagger-ui/swagger-ui-standalone-preset.js",
    "src/requivo/api/static/vendor/swagger-ui/swagger-ui.css",
    "src/requivo/api/static/vendor/redoc/redoc.standalone.js",
)

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
    normalization forced on)."""
    result = subprocess.run(
        ["git", "check-attr", "text", "--", path], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True)
    # `git check-attr`'s own output shape: "<path>: text: <value>".
    return result.stdout.strip().rsplit(": ", 1)[-1]


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
