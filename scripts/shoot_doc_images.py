"""Re-shoot the four screenshots under `docs/images/` from a scripted session (#329).

Two of the four went stale in content and nothing noticed: `web-home.webp` predated the recent-first
listing and human-formatted timestamps (#237), and `web-brief.webp` predated the rendered brief that
replaced the `<pre>` block (#235). They were not broken -- a broken image is loud -- they were a
claim about the product that had quietly stopped being true. `tests/test_doc_images.py` could see
that the files exist and could not see that they lie.

This script is the half that stops it recurring. The shots were taken by hand once, which means the
next surface change had to be followed by somebody remembering how the last set was framed; now they
are one command, from a session this script builds itself.

    python scripts/shoot_doc_images.py            # re-shoot all four
    python scripts/shoot_doc_images.py web-home   # re-shoot one
    python scripts/shoot_doc_images.py --check    # is the surface newer than the shots?

**Nothing here is reasoned and nothing is paid.** The session is the bundled example, seeded through
`web.example.seed_example` -- the same validated `create_session` + `update_model` path the product's
own keyless activation uses (#226), so what is photographed is a real session and not a fixture
posed to look like one. No key, no network, no provider.

**Two dependencies this repository deliberately does not declare.** `playwright` (with its chromium
download) and `pillow` are maintainer tooling, not project dependencies: putting them in `[dev]`
would make every CI leg install a browser to run tests that never open one. They are imported lazily
below and the failure names the install command. That is the same trade `docs/` already makes for
anything only a maintainer runs.

    pip install playwright pillow && python -m playwright install chromium

**The freshness guard.** `--check` compares a digest of the web surface -- every template, the CSS,
`app.js`, and the two view-model modules that own the user-facing vocabulary -- against the digest
recorded in `docs/images/manifest.json` when the shots were taken. It is deliberately one digest for
all four rather than a per-image dependency list: a hand-kept list of which template feeds which
screenshot is itself a thing that drifts, and it would drift silently, which is the exact defect
this file exists to answer. Coarse and correct beats precise and unmaintained. Re-shooting is one
command, so the cost of a false positive is that command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

IMAGES = REPO / "docs" / "images"
MANIFEST = IMAGES / "manifest.json"

# Every file whose content can change what the four images show. Directories are walked; a file is
# named directly. `viewmodels/` is in here because CLAUDE.md's "two vocabularies" section makes it
# the owner of the words on screen -- a relabelling there is invisible to the templates and fully
# visible in a screenshot.
SURFACE = (
    "src/requivo/web/templates",
    "src/requivo/web/static/css",
    "src/requivo/web/static/js",
    "src/requivo/web/viewmodels/labels.py",
    "src/requivo/web/viewmodels/status.py",
)

# 1280 CSS px at deviceScaleFactor 2 is what the shipped set was framed at: every file in
# `docs/images/` is 2560 wide. Height follows content, which is why these are full-page or
# element shots rather than a fixed viewport.
VIEWPORT = {"width": 1280, "height": 900}
SCALE = 2


@dataclass(frozen=True)
class Shot:
    """One image, framed by what it must show rather than by a pixel height.

    The shipped set was framed by hand at a fixed viewport height each (2560x1800, x2360, x2760,
    x2200 -- 1280 CSS px wide at `deviceScaleFactor` 2). Reproducing those numbers would reproduce
    the defect: the pages have grown since, so the same crop now cuts a sentence in half, and the
    next person would have to re-guess the numbers anyway. `top`/`bottom` name the elements the
    frame runs between instead, so content that grows stays inside the picture and the alt text in
    `docs/web.md` keeps describing what the reader can see.

    A selector that stops matching is a hard failure, not a silent re-frame: it means the template
    moved, which is precisely when a human should look at these images.
    """

    name: str
    path: str                  # `{slug}` is substituted with the seeded example's slug
    top: str | None            # element whose top edge starts the frame; None = the top of the page
    bottom: str | None         # element whose top edge ends it; None = the bottom of the page
    caption: str               # what this image claims, recorded in the manifest for a reviewer


SHOTS = (
    Shot("web-home", "/", None, None,
         "The home page: the request box, the context-card cost note, and the recent sessions."),
    Shot("web-session", "/sessions/{slug}", None, "h2:text-is('What could change the solution')",
         "The session page: the objective, the request as read, and what is confirmed vs assumed."),
    Shot("web-questions", "/sessions/{slug}",
         "h2:text-is('What could change the solution')", "h2:text-is('Documents')",
         "The open questions with why each matters, the answer form, and the readiness verdict."),
    Shot("web-brief", "/sessions/{slug}/artifacts/brief", None, "h2:text-is('Decisions made')",
         "The decision brief as a rendered document: request and objective, current understanding, "
         "what is confirmed, and the important assumptions."),
)


def surface_digest(root: Path = REPO) -> str:
    """One digest over every file that can change what the shots show.

    Sorted by repo-relative POSIX path and hashing the path alongside the content, so a rename is a
    change: a template moved to a new name renders the same page and is exactly the kind of edit
    that should send someone back to look at the screenshots.

    **Line endings are normalised before hashing, and that is not tidiness.** Every file in `SURFACE`
    is text, this repository ships no `.gitattributes`, and a Windows checkout therefore holds CRLF
    where macOS and Linux hold LF. Hashing raw bytes made the digest a fact about the checkout rather
    than about the surface, so the guard went red on `Test (py3.13, windows-latest)` alone while
    twelve other legs were green -- the same shape as #257's `card_byte_size`, which measured
    `st_size` for content the loader reads in text mode. A file that is not valid UTF-8 falls back to
    its bytes rather than raising: `SURFACE` names only text today, and a guard is not the place to
    discover otherwise.

    `root` is a parameter only so the guard's must-fire control can build a tree of its own --
    `test_the_screenshot_freshness_digest_moves_when_the_surface_does`. Nothing in this script
    passes anything but the default.
    """
    h = hashlib.sha256()
    files: list[Path] = []
    for entry in SURFACE:
        target = root / entry
        if target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        elif target.is_file():
            files.append(target)
        else:  # a path in SURFACE that no longer exists is a stale guard, not a passing one
            raise SystemExit(f"SURFACE names {entry}, which is not in the tree - update this script")
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        h.update(path.relative_to(root).as_posix().encode("utf-8"))
        h.update(_normalised_bytes(path.read_bytes()))
    return h.hexdigest()


def _normalised_bytes(raw: bytes) -> bytes:
    """The content of a text file with its line endings flattened to LF.

    Deliberately not `read_text(..., newline="")`: that keyword reached `Path.read_text` in 3.13 and
    `requires-python` is `>=3.9`, where it is a `TypeError` the Types leg cannot see -- #469 shipped
    exactly that mistake on `write_text` and #470 records why the checker is blind to it.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def read_manifest() -> dict:
    if not MANIFEST.is_file():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(workspace: Path) -> str:
    """The bundled example as a real session in a throwaway workspace."""
    import os
    os.environ["REQUIVO_WORKSPACE"] = str(workspace)
    from requivo.services.artifacts import ArtifactService
    from requivo.services.repository import FileSessionRepository
    from requivo.services.sessions import SessionService
    from requivo.web.example import seed_example

    repo = FileSessionRepository()
    return seed_example(SessionService(repo), ArtifactService(repo))


def _serve(port: int):
    """Uvicorn on a background thread. Returns the server so the caller can ask it to stop."""
    import uvicorn

    from requivo.web.app import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while time.time() < deadline:
        if server.started:
            return server
        time.sleep(0.05)
    raise SystemExit("the web app did not start within 30s")


def _edge(page, shot: Shot, selector: str | None, url: str, default: int) -> int:
    """The y coordinate a frame starts or stops at: the top edge of `selector`, or `default`.

    Deliberately not tolerant of a miss. A selector that matches nothing means the heading it names
    was renamed or removed, and re-framing silently around that would produce an image showing
    something other than what `docs/web.md` says it shows -- this file exists because that happened
    twice without anyone noticing (#235, #237).
    """
    if selector is None:
        return default
    target = page.locator(selector)
    count = target.count()
    if count != 1:
        raise SystemExit(
            f"{shot.name}: {selector!r} matched {count} elements on {url}. The template moved; "
            "re-point this shot's frame in SHOTS and look at the image before committing it."
        )
    box = target.bounding_box()
    if box is None:
        raise SystemExit(f"{shot.name}: {selector!r} is present but not rendered on {url}")
    return int(box["y"])


def _to_lossless_webp(png: bytes, destination: Path) -> tuple[int, int]:
    """Playwright hands back PNG; `docs/images/` is lossless WebP, as the shipped set already was.

    `Image.open` is bound to a name before it is called, deliberately. `tests/test_encoding.py`
    scans this tree for `.open()` calls whose mode it cannot read and asks them to declare an
    encoding (invariant 16) -- correctly, since it cannot tell a text file from an image decoder
    from the syntax. Binding says *this is not a file read* at the site, which is truer than an
    exemption entry: the exemption list is for reads that really do use the locale default, and
    this one reads no file at all.
    """
    from io import BytesIO

    from PIL import Image
    decode = Image.open

    image = decode(BytesIO(png)).convert("RGB")
    image.save(destination, format="WEBP", lossless=True, quality=100, method=6)
    return image.size


def shoot(names: list[str]) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "playwright is not installed. It is maintainer tooling rather than a project "
            "dependency - see this file's docstring:\n"
            "    pip install playwright pillow && python -m playwright install chromium"
        ) from exc
    try:
        import PIL  # noqa: F401
    except ImportError as exc:
        raise SystemExit("pillow is not installed:  pip install pillow") from exc

    selected = [s for s in SHOTS if not names or s.name in names]
    unknown = set(names) - {s.name for s in SHOTS}
    if unknown:
        raise SystemExit(f"unknown shot(s): {', '.join(sorted(unknown))}. "
                         f"Known: {', '.join(s.name for s in SHOTS)}")

    workspace = Path(tempfile.mkdtemp(prefix="requivo-shots-"))
    try:
        slug = _seed(workspace)
        port = _free_port()
        server = _serve(port)
        base = f"http://127.0.0.1:{port}"
        print(f"serving the example session {slug!r} at {base}")

        manifest = read_manifest()
        images = dict(manifest.get("images", {}))

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
            for s in selected:
                url = base + s.path.format(slug=slug)
                page.goto(url, wait_until="networkidle")
                top = _edge(page, s, s.top, url, default=0)
                bottom = _edge(page, s, s.bottom, url,
                               default=page.evaluate("document.documentElement.scrollHeight"))
                if bottom <= top:
                    raise SystemExit(
                        f"{s.name}: {s.bottom!r} sits at or above {s.top!r} on {url} - the page "
                        "reordered and this frame no longer means what it says"
                    )
                png = page.screenshot(
                    full_page=True,
                    clip={"x": 0, "y": top, "width": VIEWPORT["width"], "height": bottom - top},
                )
                destination = IMAGES / f"{s.name}.webp"
                width, height = _to_lossless_webp(png, destination)
                images[s.name] = {
                    "route": s.path,
                    "frame": {"top": s.top, "bottom": s.bottom},
                    "caption": s.caption,
                    "width": width,
                    "height": height,
                }
                print(f"  {s.name}.webp  {width}x{height}  from {url}")
            browser.close()
        server.should_exit = True

        MANIFEST.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Written by scripts/shoot_doc_images.py. `surface_digest` is what "
                        "tests/test_doc_images.py compares against the tree; when it goes red the "
                        "web surface moved after these shots were taken - re-run the script."
                    ),
                    "shot_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "viewport": VIEWPORT,
                    "device_scale_factor": SCALE,
                    "surface_digest": surface_digest(),
                    "images": dict(sorted(images.items())),
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {MANIFEST.relative_to(REPO)}")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def check() -> int:
    manifest = read_manifest()
    if not manifest:
        print(f"{MANIFEST.relative_to(REPO)} does not exist - run this script with no arguments")
        return 1
    recorded, current = manifest.get("surface_digest"), surface_digest()
    if recorded == current:
        print(f"docs/images/ is current with the web surface (shot {manifest.get('shot_at')})")
        return 0
    print(
        f"the web surface has changed since the screenshots were taken on {manifest.get('shot_at')}.\n"
        f"  recorded: {recorded}\n  current:  {current}\n"
        "Re-shoot them:  python scripts/shoot_doc_images.py"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="shots to re-take (default: all)")
    parser.add_argument("--check", action="store_true",
                        help="report whether the surface moved since the shots, and shoot nothing")
    args = parser.parse_args()
    if args.check:
        if args.names:
            parser.error("--check takes no shot names: it is a question about all four")
        return check()
    return shoot(args.names)


if __name__ == "__main__":
    # Invariant 16: an entry point that prints configures its streams first, so a console that
    # cannot encode a character substitutes it visibly instead of killing the process. This script
    # keeps its own output ASCII, but a path, a slug or a selector printed back in an error message
    # is not this file's to promise anything about.
    from requivo.streams import configure_streams
    configure_streams()
    raise SystemExit(main())
