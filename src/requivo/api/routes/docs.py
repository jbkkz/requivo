"""Self-hosted `/docs` and `/redoc` (#504) -- the same OpenAPI docs `create_api()` has always
turned on (`docs/decisions/0004-the-http-api-facade.md` §4), served from this project's own
static mount instead of a third-party CDN.

`create_api()` sets `docs_url=None, redoc_url=None` on the `FastAPI(...)` constructor -- the same
switch `web/app.py` already uses to turn its own docs off entirely -- so neither of FastAPI's
built-in routes, both of which hardcode a `cdn.jsdelivr.net` URL, is ever wired. These two routes
replace them, referencing only `/api-static/...` (the mount `create_api()` installs for the
directory this package ships under `src/requivo/api/static/`) and `/openapi.json` (this app's own
spec endpoint). `tests/api/test_api_docs_assets.py`'s
`test_no_external_origin_appears_in_the_served_docs_html` asserts the property directly on the
returned HTML, so nothing here needs re-auditing by eye.

`fastapi.openapi.docs.get_swagger_ui_html` is deliberately not used for `/docs`. It writes the
Swagger UI initialization call as an *inline* `<script>` block, which this app's CSP
(`script-src 'self'`, no `'unsafe-inline'` -- see `api/app.py`) refuses to execute, and its one
`swagger_js_url` parameter cannot express "load the standalone preset as a second external script
too" -- `swagger-ui-bundle.js` alone has no `SwaggerUIStandalonePreset` (verified directly against
the vendored file: zero occurrences of that string). `static/swagger-initializer.js` carries the
same initialization as an ordinary external file instead, the way `swagger-ui-dist`'s own
`index.html` structures it. `get_redoc_html` has no equivalent obstacle -- ReDoc needs one external
script and no inline one -- and is skipped anyway so both pages are defined the same way, in one
file, rather than one hand-written and one library-generated.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_FAVICON = "/api-static/favicon.svg"

_SWAGGER_UI_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Requivo API - Swagger UI</title>
<link rel="stylesheet" type="text/css" href="/api-static/vendor/swagger-ui/swagger-ui.css">
<link rel="icon" type="image/svg+xml" href="{_FAVICON}">
</head>
<body>
<div id="swagger-ui"></div>
<script src="/api-static/vendor/swagger-ui/swagger-ui-bundle.js" charset="UTF-8"></script>
<script src="/api-static/vendor/swagger-ui/swagger-ui-standalone-preset.js" charset="UTF-8"></script>
<script src="/api-static/swagger-initializer.js" charset="UTF-8"></script>
</body>
</html>
"""

# The one inline `<style>` on this page -- allowed under this app's own widened `style-src` (see
# `api/app.py` for why: swagger-ui's vendored bundle sets computed layout via inline `style`
# attributes throughout its DOM, which this same directive already has to permit, so a two-line
# reset here costs nothing further).
_REDOC_HTML = f"""<!DOCTYPE html>
<html>
<head>
<title>Requivo API - ReDoc</title>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="shortcut icon" href="{_FAVICON}">
<style>
  body {{ margin: 0; padding: 0; }}
</style>
</head>
<body>
<noscript>ReDoc requires Javascript to function. Please enable it to browse the documentation.</noscript>
<redoc spec-url="/openapi.json"></redoc>
<script src="/api-static/vendor/redoc/redoc.standalone.js"></script>
</body>
</html>
"""


@router.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    return HTMLResponse(_SWAGGER_UI_HTML)


@router.get("/redoc", include_in_schema=False)
def redoc() -> HTMLResponse:
    return HTMLResponse(_REDOC_HTML)
