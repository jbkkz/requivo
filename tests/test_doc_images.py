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
# The digest is deliberately coarse -- one hash over every template, the CSS, `app.js` and the two
# view-model modules, rather than a per-image dependency list. A hand-kept map of which template
# feeds which screenshot is a second thing to keep in sync, and it would drift silently, which is
# the defect this guard exists to answer. Re-shooting is one command, so a false positive costs
# that command.

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
    recorded = manifest.get("surface_digest")
    current = _shoot_module().surface_digest()
    assert recorded == current, (
        f"the web surface has changed since the screenshots in docs/images/ were taken on "
        f"{manifest.get('shot_at')}, so they may now show a product that no longer exists — the "
        f"exact defect #329 was filed for. Re-shoot them and commit both the images and the "
        f"manifest:\n    python scripts/shoot_doc_images.py\n"
        f"  recorded: {recorded}\n  current:  {current}"
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
    in. This repository ships no `.gitattributes`, so a Windows clone holds CRLF where macOS and
    Linux hold LF -- and hashing raw bytes made the guard above fail on `Test (py3.13,
    windows-latest)` alone while twelve other legs were green, reporting a stale screenshot on a
    tree where nothing had moved. Same shape as #257's `card_byte_size`, one directory along."""
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
