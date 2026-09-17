"""Self-hosted `/docs` and `/redoc` (#504), replacing FastAPI's built-ins, which hardcode a CDN;
`test_no_external_origin_appears_in_the_served_docs_html` pins it. `get_swagger_ui_html` is not
used: it writes an inline `<script>` this app's CSP refuses, and cannot load the standalone preset,
so `static/swagger-initializer.js` carries the initialization as an external file.
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

# The one inline `<style>`, allowed under this app's widened `style-src` (`api/app.py`).
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
