"""Every image a document references resolves to a file in this repository.

A broken image is the one docs defect that is invisible to the person who introduced it: the README
renders on GitHub *and* on PyPI, both from URLs, and a missing file shows as an empty frame on a page
the author is not looking at. There is no import to fail and no link checker in the required CI legs.

Two forms, because the README and `docs/` cannot use the same one. `pyproject` sets
`readme = README.md`, so PyPI renders that file and does not rewrite relative hrefs -- every image
there has to be an absolute `raw.githubusercontent.com` URL, which is unverifiable as a URL and
entirely verifiable as the repository path inside it. Pages under `docs/` are only ever read on
GitHub, so they use relative paths and are checked as paths (#224).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com/jbkkz/requivo/main/"
# `![alt](path)` and the `[ref]: url` definition an `![alt][ref]` resolves through.
INLINE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
REFDEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$", re.MULTILINE)
IMAGE_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def _doc_pages():
    return [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]


def test_every_readme_image_hosted_from_this_repo_names_a_file_that_exists():
    """Only the repository's own images. A shields.io badge and the Actions status SVG are images
    too, and neither is a file anyone here can check."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    urls = [u for _, u in REFDEF.findall(text) if Path(u).suffix.lower() in IMAGE_SUFFIXES]
    urls += INLINE.findall(text)
    ours = [u for u in urls if u.startswith(RAW)]
    assert ours, "the README references none of this repository's own images"
    for u in ours:
        assert (REPO / u[len(RAW):]).is_file(), f"{u} names no file in this repository"


def test_no_readme_image_is_relative():
    """`pyproject` sets `readme = README.md`, so PyPI renders this file verbatim and does not rewrite
    relative hrefs. A relative image renders on GitHub and 404s on the project page -- half the
    audience, and the half deciding whether to install."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    candidates = [u for _, u in REFDEF.findall(text) if Path(u).suffix.lower() in IMAGE_SUFFIXES]
    candidates += INLINE.findall(text)
    for u in candidates:
        assert u.startswith("http"), f"README image {u!r} is relative and will 404 on PyPI"


@pytest.mark.parametrize("page", _doc_pages(), ids=lambda p: p.name)
def test_every_relative_image_path_in_a_doc_page_resolves(page):
    text = page.read_text(encoding="utf-8")
    for rel in INLINE.findall(text):
        if rel.startswith("http") or rel.startswith("#"):
            continue
        assert (page.parent / rel).is_file(), f"{page.name} references {rel}, which does not exist"


# -- #329: an image is a claim about the product, and a claim needs a guard ----------------------
#
# The tests above ask whether a file exists. Two of the four shipped images stopped being true while
# every one of them still existed: `web-home.webp` predated the recent-first listing and the
# human-formatted timestamps (#237), and `web-brief.webp` predated the rendered brief that replaced
# the `<pre>` block (#235). Stale in *content* is the harder kind, because nothing is broken and the
# person who falsified the claim is not looking at the picture.
#
# Two named instances is the bar CLAUDE.md sets for funding coverage, and #237/#235 are them. It
# folds into this file rather than opening a new one, per the same rule.
#
# The digest is deliberately coarse -- one hash over every template, the CSS, `app.js` and the
# view-model package, rather than a per-image dependency list. A hand-kept map of which template
# feeds which screenshot is a second thing to keep in sync, and it would drift silently, which is
# the defect this guard exists to answer. Re-shooting is one command, so a false positive costs
# that command.
#
# **Coarse in what it watches, per-image in what it records, and those are different questions.**
# The digest was written once at the top of the manifest, so a partial re-run -- which the script
# takes shot names for -- stamped the current tree onto all four entries while re-taking one. Each
# entry carries the digest of the tree *it* was shot against now. That is not the per-image
# dependency map rejected above: it is a fact the script observed at capture time, not a mapping
# anyone has to keep true.

MANIFEST = REPO / "docs" / "images" / "manifest.json"


def _shoot_module():
    """`scripts/` is not a package; load the shooter by path so this test and the script cannot
    disagree about what the surface is. Importing it must not need playwright or pillow -- both are
    lazy inside `shoot()` for exactly this reason."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "shoot_doc_images", REPO / "scripts" / "shoot_doc_images.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves its own module out of `sys.modules`, and a
    # module that is not there yet makes the decorator fail on `NoneType.__dict__` -- an error about
    # dataclasses that says nothing about the real cause.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_screenshots_were_taken_from_the_web_surface_as_it_stands_now():
    """Red when a template, a stylesheet or a user-facing label moved after the shots were taken.
    The remedy is in the message because the whole point is that the next person should not have to
    reconstruct how the last set was framed."""
    import json

    assert MANIFEST.is_file(), (
        "docs/images/manifest.json is missing — run `python scripts/shoot_doc_images.py`"
    )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    shoot = _shoot_module()
    current = shoot.surface_digest()
    stale = shoot.stale_shots(manifest, current)
    assert not stale, (
        f"{', '.join(stale)} — the web surface has changed since {'these were' if len(stale) > 1 else 'this was'} "
        f"shot (manifest dated {manifest.get('shot_at')}), so they may now show a product that no "
        f"longer exists — the exact defect #329 was filed for. Re-shoot and commit both the images "
        f"and the manifest:\n    python scripts/shoot_doc_images.py {' '.join(stale)}\n"
        f"  current: {current}"
    )


def _webp_size(path: Path) -> tuple[int, int]:
    """WebP dimensions, read from the header rather than through Pillow.

    Pillow is maintainer tooling for the shooter, not a project dependency, and this file runs on
    every CI leg -- so the guard reads the three container forms itself. `VP8X` carries a 24-bit
    canvas size minus one, `VP8L` packs 14 bits each into the bitstream, `VP8 ` (lossy) puts them
    after the start code. A file that is none of these is a hard failure, not a skip: an unreadable
    image in `docs/images/` is a finding.
    """
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise AssertionError(f"{path.name} is not a WebP file")
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF)
    raise AssertionError(f"{path.name} has an unrecognised WebP chunk {chunk!r}")


def test_every_manifest_entry_names_an_image_that_exists_and_matches_its_recorded_size():
    """The manifest is only worth trusting if it describes the files actually in the tree. A shot
    renamed in `SHOTS` but left on disk under the old name would otherwise keep passing the digest
    check above while `docs/web.md` pointed at the stale file."""
    import json

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["images"], "the manifest records no images"
    for name, entry in manifest["images"].items():
        path = REPO / "docs" / "images" / f"{name}.webp"
        assert path.is_file(), f"the manifest records {name}, which is not in docs/images/"
        assert _webp_size(path) == (entry["width"], entry["height"]), (
            f"{name}.webp is {_webp_size(path)} and the manifest records "
            f"({entry['width']}, {entry['height']}) — one of the two was edited by hand"
        )


def _surface_tree(root: Path) -> None:
    """The smallest tree `surface_digest` accepts: one file at every path `SURFACE` names.

    `write_bytes`, never `write_text`, and that is the whole fixture. Text mode translates `\n` to
    the platform's line ending on write, so a baseline written with `write_text` is already CRLF on
    Windows -- and the line-ending test below would then be converting that to `\r\r\n` and
    comparing two things that are both wrong. A test that builds its own fixture in text mode has
    already lost the platform difference it exists to reproduce: it went red on the two Windows legs
    and nowhere else, which is the same sentence as the defect it guards.
    """
    for entry in _shoot_module().SURFACE:
        target = root / entry
        if target.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"original\n")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "a.html").write_bytes(b"original\n")


def test_the_screenshot_freshness_digest_moves_when_the_surface_does(tmp_path):
    """The must-fire control for the guard above, which without it would pass forever on a digest
    that never changes. Both halves matter and the second is the one a checksum usually misses: a
    template *renamed* renders the same bytes, so a digest over content alone would call the surface
    unchanged — while `docs/web.md`'s captions and this repo's own template lookups have moved."""
    digest = _shoot_module().surface_digest
    _surface_tree(tmp_path)
    baseline = digest(tmp_path)

    edited = tmp_path / "src/requivo/web/templates/a.html"
    edited.write_bytes(b"edited\n")
    assert digest(tmp_path) != baseline, "an edited template left the digest unchanged"

    edited.write_bytes(b"original\n")
    assert digest(tmp_path) == baseline, "the digest is not stable for unchanged content"

    edited.rename(edited.with_name("b.html"))
    assert digest(tmp_path) != baseline, (
        "a renamed template left the digest unchanged — the path has to be hashed alongside the "
        "bytes or a move reads as no change at all"
    )


# #530: the digest hashed a `.py` viewmodel as text, so a comment or docstring edit -- which
# cannot change a pixel -- moved it exactly as a real code change would. The three source strings
# below share one function body and differ only in prose (comment/docstring reworded) or in the one
# line that is actual behaviour, so the two tests below pin both directions off the same fixture.
_PY_SOURCE_BASELINE = """\"\"\"Module docstring.\"\"\"


def f():
    \"\"\"Docstring for f.\"\"\"
    return 1
"""
_PY_SOURCE_PROSE_EDIT = """\"\"\"Module docstring, reworded for clarity.\"\"\"


def f():
    # a new comment that changes nothing
    \"\"\"Docstring for f, also reworded.\"\"\"
    return 1
"""
_PY_SOURCE_CODE_EDIT = """\"\"\"Module docstring.\"\"\"


def f():
    \"\"\"Docstring for f.\"\"\"
    return 2
"""


def test_a_comment_or_docstring_only_edit_to_a_viewmodel_does_not_move_the_digest(tmp_path):
    """The digest hashes a `.py` file's `ast.dump` with docstrings stripped, and comments are never
    part of the AST at all -- so English prose moving inside a viewmodel module cannot move the
    digest, because it cannot change what a screenshot shows (#530)."""
    digest = _shoot_module().surface_digest
    _surface_tree(tmp_path)
    target = tmp_path / "src/requivo/web/viewmodels/a.py"

    target.write_bytes(_PY_SOURCE_BASELINE.encode("utf-8"))
    baseline = digest(tmp_path)

    target.write_bytes(_PY_SOURCE_PROSE_EDIT.encode("utf-8"))
    assert digest(tmp_path) == baseline, (
        "a comment/docstring-only edit to a viewmodel moved the freshness digest"
    )


def test_a_code_edit_to_a_viewmodel_still_moves_the_digest(tmp_path):
    """The other direction of #530's fix: stripping docstrings must not swallow a real behaviour
    change along with the prose it was aimed at."""
    digest = _shoot_module().surface_digest
    _surface_tree(tmp_path)
    target = tmp_path / "src/requivo/web/viewmodels/a.py"

    target.write_bytes(_PY_SOURCE_BASELINE.encode("utf-8"))
    baseline = digest(tmp_path)

    target.write_bytes(_PY_SOURCE_CODE_EDIT.encode("utf-8"))
    assert digest(tmp_path) != baseline, "a code edit to a viewmodel left the freshness digest unchanged"


def test_a_python_file_that_fails_to_parse_falls_back_to_text_instead_of_crashing(tmp_path):
    """`_normalised_python` must degrade to `_normalised_bytes` on a `.py` file it cannot parse,
    never raise -- a guard is not the place to discover a syntax error (surface_digest's own
    docstring). Two distinct exceptions cover this on the versions this project supports: a NUL byte
    raises `SyntaxError` on 3.12/3.13 and `ValueError` on 3.9 for the identical input, found in
    self-review (#530) after the first cut of this function caught only `SyntaxError`."""
    shoot = _shoot_module()
    for broken in (b"def f(:\n", b"x = 1\x00\n"):
        assert shoot._normalised_python(broken) == shoot._normalised_bytes(broken), (
            f"{broken!r} should fall back to the text hash, not raise or diverge from it"
        )


def test_the_freshness_guard_refuses_a_surface_path_that_no_longer_exists(tmp_path):
    """The third state. A `SURFACE` entry pointing at a deleted directory must be a hard failure and
    not a quietly smaller digest: a guard that silently stops watching half the surface is the
    all-clear nobody earned, which is the same reason `tests/test_boundaries.py` fails on an empty
    scan set."""
    _surface_tree(tmp_path)
    shutil.rmtree(tmp_path / "src/requivo/web/templates")
    with pytest.raises(SystemExit, match="not in the tree"):
        _shoot_module().surface_digest(tmp_path)


def test_the_digest_is_the_same_whatever_the_line_endings(tmp_path):
    """The freshness digest is a fact about the web surface, not about the checkout it was computed
    in. `.gitattributes` (added by #504) covers only `*/static/vendor/**` -- the byte-exact
    third-party bundles that must never be normalized -- and says nothing about `SURFACE`, none of
    which lives under `vendor/`; those files are still subject to git's ordinary default, so a
    Windows clone still holds CRLF where macOS and Linux hold LF for every one of them. Hashing raw
    bytes made the guard above fail on `Test (py3.13, windows-latest)` alone while twelve other legs
    were green, reporting a stale screenshot on a tree where nothing had moved. Same shape as #257's
    `card_byte_size`, one directory along."""
    digest = _shoot_module().surface_digest
    _surface_tree(tmp_path)
    lf = digest(tmp_path)

    for entry in _shoot_module().SURFACE:
        target = tmp_path / entry
        for path in ([target] if target.suffix else target.rglob("*")):
            if path.is_file():
                # Flatten first: converting blind would turn an existing CRLF into `\r\r\n`.
                flat = path.read_bytes().replace(b"\r\n", b"\n")
                path.write_bytes(flat.replace(b"\n", b"\r\n"))
    assert digest(tmp_path) == lf, (
        "the digest moved when the same content was checked out with CRLF endings — it is measuring "
        "the checkout rather than the surface, and will go red on Windows and nowhere else"
    )


def test_a_partial_reshoot_does_not_bless_the_shots_it_did_not_take():
    """The must-fire control for the per-image digest, and the defect that produced it.

    `shoot(names)` takes shot names, preserves the manifest entries and the files of everything it
    was not asked for, and used to overwrite one shared `surface_digest` with the current tree
    regardless. So the documented partial refresh --  `python scripts/shoot_doc_images.py web-home`
    -- silently certified three screenshots nobody had re-taken, in the guard whose entire subject
    is a screenshot that quietly stopped being true.

    Asserted against `stale_shots`, the predicate both `check()` and the guard above read, rather
    than by driving `shoot()`: taking a real screenshot needs playwright, a browser and a served
    session, none of which this suite has -- and the arithmetic that went wrong is here, not in the
    capture.
    """
    shoot = _shoot_module()
    fresh, old = "d" * 64, "0" * 64
    partially = {"images": {
        "web-home": {"surface_digest": fresh},        # re-taken
        "web-session": {"surface_digest": old},       # not, and still shows the old surface
        "web-questions": {"surface_digest": old},
        "web-brief": {"surface_digest": old},
    }}
    assert shoot.stale_shots(partially, fresh) == ["web-brief", "web-questions", "web-session"], (
        "a partial re-shoot marked the shots it never took as current"
    )
    assert shoot.stale_shots({"images": {n: {"surface_digest": fresh} for n in partially["images"]}},
                             fresh) == [], "a full re-shoot must leave nothing flagged"


def test_a_manifest_entry_with_no_recorded_digest_reads_as_stale():
    """The third state, and the direction it has to fail in. An entry written before the digest was
    recorded per image knows nothing about the tree it was shot against, and *unknown* is not
    *current*: a freshness guard that answers "fine" when it has nothing to compare is the all-clear
    nobody earned -- `test_the_freshness_guard_refuses_a_surface_path_that_no_longer_exists` above
    is the same rule one function along, and `golden_diff`'s `unknown` baseline state is it in
    another script entirely."""
    shoot = _shoot_module()
    assert shoot.stale_shots({"images": {"web-home": {"width": 2560}}}, "d" * 64) == ["web-home"]
