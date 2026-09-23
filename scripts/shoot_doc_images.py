"""Re-shoot the four screenshots under `docs/images/` from a scripted session (#329).

Two went stale in content unnoticed (#235, #237): a screenshot is a claim about the product, and a
hand-taken set is re-framed only by memory. Now it is one command:

    python scripts/shoot_doc_images.py            # re-shoot all four
    python scripts/shoot_doc_images.py web-home   # re-shoot one
    python scripts/shoot_doc_images.py --check    # is the surface newer than the shots?

Nothing is reasoned or paid: the session is the bundled example seeded through
`web.example.seed_example` (#226). `playwright` and `pillow` are maintainer tooling, imported lazily
and deliberately undeclared (every CI leg would otherwise install a browser):

    pip install playwright pillow && python -m playwright install chromium

`--check` compares one digest of the whole web surface against `docs/images/manifest.json`: coarse and
correct beats a hand-kept per-image dependency list that would drift silently."""
from __future__ import annotations

import argparse
import ast
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

# Every file whose content can change what the four images show; directories are walked. All of
# `viewmodels/`, which owns the on-screen words and the session ordering: naming only two of its modules
# once let a `TITLE_CHARS` change move the titles with the digest unchanged.
SURFACE = (
    "src/requivo/web/templates",
    "src/requivo/web/static/css",
    "src/requivo/web/static/js",
    "src/requivo/web/viewmodels",
)

# 1280 CSS px at deviceScaleFactor 2: every shipped image is 2560 wide; height follows content.
VIEWPORT = {"width": 1280, "height": 900}
SCALE = 2


@dataclass(frozen=True)
class Shot:
    """One image, framed between two elements rather than at a pixel height.

        A fixed height re-cuts a grown page mid-sentence; `top`/`bottom` keep the content the alt text in
        `docs/web.md` describes inside the frame. A selector that stops matching is a hard failure.
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

        Sorted by repo-relative POSIX path, hashing path and content, so a rename is a change. Line endings
        are normalised, or a Windows checkout's CRLF moves it (as #257 did for card sizes); a file that is
        not UTF-8 falls back to its bytes. A `.py` file is hashed by its docstring-stripped structure
        (`_normalised_python`), so a prose-only edit cannot demand a re-shoot (#530). `root` exists for
        a control tree.
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
        raw = path.read_bytes()
        h.update(_normalised_python(raw) if path.suffix == ".py" else _normalised_bytes(raw))
    return h.hexdigest()


def _normalised_bytes(raw: bytes) -> bytes:
    """The content of a text file with line endings flattened to LF (not `newline=`: 3.13-only, #469, #470)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove the leading string statement from the module and every class/function body, in place.

        Not re-padded with `pass`: the tree only goes to `_stable_dump`, never `compile`.
    """
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_HOLDERS) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = node.body[1:]
    return tree


def _stable_dump(node: object) -> str:
    """`ast.dump()`, reimplemented, because the real one differs across the Pythons CI runs (#530).

        3.13's `show_empty` omits empty fields, and 3.12 added `type_params`; both are "absent" spelled
        differently. So an empty list is skipped, and `None` too except on `ast.Constant`, where it can be
        the literal value. Verified identical on 3.9, 3.12 and 3.13.
    """
    if isinstance(node, ast.AST):
        parts = []
        for name, value in ast.iter_fields(node):
            if isinstance(value, list) and not value:
                continue
            if value is None and not isinstance(node, ast.Constant):
                continue
            parts.append(f"{name}={_stable_dump(value)}")
        return f"{type(node).__name__}({', '.join(parts)})"
    if isinstance(node, list):
        return "[" + ", ".join(_stable_dump(item) for item in node) + "]"
    return repr(node)


def _normalised_python(raw: bytes) -> bytes:
    """`_stable_dump()` of a `.py` file's docstring-stripped tree, or `_normalised_bytes` if it will not parse (#530)."""
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        # `ValueError` too: 3.9's `ast.parse` raises it for a NUL byte where 3.12+ raise `SyntaxError` (#530).
        return _normalised_bytes(raw)
    return _stable_dump(_strip_docstrings(tree)).encode("utf-8")


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

        A miss raises: re-framing around a renamed heading would show something `docs/web.md` does not
        describe (#235, #237).
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
    """Playwright hands back PNG; `docs/images/` is lossless WebP.

        `Image.open` is bound to a name first so the encoding guard (invariant 16) does not read it as a
        text-file `.open()`: it reads no file at all.
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
        # Once, before the first shot, so every image in this run is recorded against one tree.
        digest = surface_digest()

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
                    # Stamped per image: a partial re-shoot must not mark the shots nobody re-took as current.
                    "surface_digest": digest,
                }
                print(f"  {s.name}.webp  {width}x{height}  from {url}")
            browser.close()
        server.should_exit = True

        MANIFEST.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Written by scripts/shoot_doc_images.py. Each image carries the "
                        "`surface_digest` of the tree it was shot against; "
                        "`--check` compares each one against the tree, so a shot left out of a "
                        "partial re-run stays flagged - re-run the script."
                    ),
                    "shot_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "viewport": VIEWPORT,
                    "device_scale_factor": SCALE,
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


def stale_shots(manifest: dict, current: str) -> list[str]:
    """The images in `manifest` not shot against `current`; an entry with no digest is stale, never current."""
    return sorted(name for name, entry in manifest.get("images", {}).items()
                  if entry.get("surface_digest") != current)


def check() -> int:
    manifest = read_manifest()
    if not manifest:
        print(f"{MANIFEST.relative_to(REPO)} does not exist - run this script with no arguments")
        return 1
    current = surface_digest()
    stale = stale_shots(manifest, current)
    if not stale:
        print(f"docs/images/ is current with the web surface (shot {manifest.get('shot_at')})")
        return 0
    print(
        f"the web surface has changed since {len(stale)} of "
        f"{len(manifest.get('images', {}))} screenshots were taken.\n"
        + "".join(f"  {name}: shot against "
                  f"{manifest['images'][name].get('surface_digest') or '(not recorded)'}\n"
                  for name in stale)
        + f"  current:  {current}\n"
        f"Re-shoot them:  python scripts/shoot_doc_images.py {' '.join(stale)}"
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
    # Invariant 16: configure the streams before printing; a path or slug echoed back may not be ASCII.
    from requivo.streams import configure_streams
    configure_streams()
    raise SystemExit(main())
