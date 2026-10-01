"""The OpenAPI skeleton (#425, slice 5): paths, methods and declared statuses pinned route by route, and
docs/api.md's route table held to the live app, so a route change goes red in both places, deliberately."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from requivo.api.routes import artifacts, discovery, health, sessions

API_DOC = Path(__file__).resolve().parents[2] / "docs" / "api.md"

# Every operation also declares the shared error envelope under these two ranges (`api/app.py`).
_FAILURES = ("4XX", "5XX")

# (method, path) -> the declared success statuses. Not a freeze (`decision: the-http-api-facade`): an
# edit here is a deliberate API change, made in the same diff as docs/api.md's table.
_SKELETON: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", "/api/v1/health"): ("200",),
    ("GET", "/api/v1/sessions"): ("200",),
    ("POST", "/api/v1/sessions"): ("200", "201"),
    ("GET", "/api/v1/sessions/{slug}"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/model"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/revisions"): ("200",),
    ("POST", "/api/v1/sessions/{slug}/revisions"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/revisions/{revision}"): ("200",),
    ("POST", "/api/v1/sessions/{slug}/revisions/preview"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/status"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/impact"): ("200",),
    ("PUT", "/api/v1/sessions/{slug}/context-cards"): ("200",),
    ("POST", "/api/v1/sessions/{slug}/discover"): ("200",),
    ("POST", "/api/v1/sessions/{slug}/answers"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/artifacts"): ("200",),
    ("GET", "/api/v1/sessions/{slug}/artifacts/{artifact_type}"): ("200",),
    ("POST", "/api/v1/sessions/{slug}/artifacts/{artifact_type}"): ("200",),
    ("PUT", "/api/v1/sessions/{slug}/artifacts/{artifact_type}"): ("200",),
}

# The first four cells of a route-table row: method, `path`, success statuses, backing service method.
_DOC_ROW = re.compile(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \| ([^|]+?) \| ([^|]*?) \|", re.MULTILINE)


def _declared(app) -> dict[tuple[str, str], tuple[str, ...]]:
    return {(method.upper(), path): tuple(sorted(op["responses"]))
            for path, ops in app.openapi()["paths"].items() for method, op in ops.items()}


def _diff(one: str, seen: dict, other: str, recorded: dict) -> list[str]:
    return ([f"{m} {p}: in {one}, not in {other}" for m, p in sorted(seen.keys() - recorded.keys())]
            + [f"{m} {p}: in {other}, not in {one}" for m, p in sorted(recorded.keys() - seen.keys())]
            + [f"{k[0]} {k[1]}: {one} says {seen[k]}, {other} says {recorded[k]}"
               for k in sorted(seen.keys() & recorded.keys()) if seen[k] != recorded[k]])


def test_the_openapi_skeleton_is_pinned_route_by_route(app):
    """The declared statuses are the true ones: 422 never appears, since `_validation_error` answers 400."""
    declared = _declared(app)
    assert len(declared) >= 18, declared   # must fire: the comparison iterates over something
    pinned = {key: tuple(sorted((*ok, *_FAILURES))) for key, ok in _SKELETON.items()}
    changed = _diff("openapi.json", declared, "_SKELETON", pinned)
    assert not changed, "the API skeleton moved; update `_SKELETON` and docs/api.md together:\n  " + "\n  ".join(changed)


def test_docs_api_md_route_table_is_the_live_app_and_names_each_backing_service_method(app):
    """Both directions against the live document, and each named method is called by its handler."""
    rows = _DOC_ROW.findall(API_DOC.read_text(encoding="utf-8"))
    documented = {(m, p): tuple(sorted(s.replace(",", " ").split())) for m, p, s, _ in rows}
    live = {key: tuple(s for s in statuses if s not in _FAILURES) for key, statuses in _declared(app).items()}
    changed = _diff("docs/api.md", documented, "the app", live)
    assert rows and not changed, "docs/api.md's route table disagrees with the app:\n  " + "\n  ".join(changed)
    handlers = {(m, f"/api/v1{r.path}"): r.endpoint for module in (health, sessions, discovery, artifacts)
                for r in module.router.routes for m in r.methods}
    unbacked = [f"{m} {p} -> {name}" for m, p, _, backing in rows for name in re.findall(r"`\w+\.(\w+)`", backing)
                if f".{name}(" not in inspect.getsource(handlers[(m, p)])]
    assert not unbacked, "docs/api.md names a backing method the handler never calls:\n  " + "\n  ".join(unbacked)
