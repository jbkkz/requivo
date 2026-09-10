"""`/docs` and `/redoc` self-host every asset they need (#504) -- no third-party origin, floating
or otherwise, loads into the same origin `GET /api/v1/sessions` answers 200 with no credential.

Asserted on the served HTML directly, per the issue's own acceptance criterion, rather than by
eyeballing the rendered page: a scheme prefix (`http://`/`https://`) appearing anywhere in the
initial response is exactly what a CDN `<script src=...>`, a `<link href=...>` favicon, or a
Google Fonts `<link>` would look like, and their absence is the property this file exists to hold.
"""

from __future__ import annotations

import re

_EXTERNAL_ORIGIN = re.compile(r"https?://")


def test_no_external_origin_appears_in_the_served_docs_html(client):
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} did not render"
        assert not _EXTERNAL_ORIGIN.search(r.text), (
            f"{path} references an external origin:\n{r.text}")


def test_every_asset_the_docs_pages_reference_is_served_from_this_app(client):
    """The favicon and the fonts are the acceptance criterion's own named risk: a page can drop
    every external `<script>`/`<link>` and still leak one `<link rel=\"icon\">`, and that single
    miss reintroduces the whole property. So this walks every `src=`/`href=` this app's own two
    pages carry and asserts each one both starts with `/` (same-origin, never a bare hostname) and
    actually resolves -- a 404 on a same-origin reference is not the CDN, but it is exactly as
    undiscoverable air-gapped, which is the other half of what #504 asks for."""
    referenced = set()
    for path in ("/docs", "/redoc"):
        html = client.get(path).text
        referenced.update(re.findall(r'(?:src|href)="([^"]+)"', html))

    assert referenced, "no asset references found -- this test would pass on an empty page too"
    for ref in referenced:
        assert ref.startswith("/"), f"{ref} is not a same-origin reference"
        resp = client.get(ref)
        assert resp.status_code == 200, f"{ref} is referenced but does not resolve ({resp.status_code})"


def test_the_swagger_ui_page_does_not_phone_home_to_the_spec_validator(client):
    """Unset, Swagger UI's default config POSTs the loaded spec to
    `https://validator.swagger.io/validator` to paint a green/red badge -- a live call this
    project's own CSP already blocks (no `connect-src` override, so it inherits `default-src
    'self'`), but self-hosting is supposed to mean the browser never *attempts* the call, not that
    it silently fails one. `swagger-initializer.js` sets `validatorUrl: null` for exactly this."""
    r = client.get("/api-static/swagger-initializer.js")
    assert r.status_code == 200
    assert "validatorUrl: null" in r.text
    # The upstream URL is named in this file's own explanatory comment (why it was replaced), so the
    # assertion is on the executable config line rather than on the substring appearing anywhere at
    # all -- `url:` is the one line SwaggerUIBundle actually reads its target from.
    assert re.search(r'url:\s*"https?://', r.text) is None
    assert 'url: "/openapi.json"' in r.text


def test_works_under_the_strict_script_src_with_no_exception_carved_for_it(client):
    """#503's `script-src 'self'` and #504's self-hosted assets land together, and the coupling is
    the acceptance criterion: no exception may be carved into `script-src` for either page. The
    style directive is a separate, documented widening (`api/app.py`'s own comment) for a different
    reason -- inline `style` attributes the vendored Swagger UI bundle sets unconditionally -- and
    this test is deliberately about the directive that governs code execution, not presentation."""
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        csp = r.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]


def test_the_redoc_pages_img_src_has_no_cdn_exception_either(client):
    """`redoc.standalone.js`'s own sidebar unconditionally attempts
    `https://cdn.redoc.ly/redoc/logo-mini.svg` on every `/redoc` page mount -- a known limit, not
    fixable without either patching the vendored bundle (`THIRD-PARTY-NOTICES.md`'s "no local edits,
    ever" rule forbids it) or dropping the sidebar entirely, and recorded in
    `THIRD-PARTY-NOTICES.md`'s own "Known limit" paragraph rather than silently accepted. What keeps
    it from being an actual disclosure is `img-src 'self' data:` carrying no override: the browser
    refuses the load before any byte reaches the network, so this pins the one directive load-bearing
    for that claim -- a future widening of `img-src` on this app would reopen it with nothing here to
    notice."""
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        csp = r.headers["Content-Security-Policy"]
        img_src = csp.split("img-src", 1)[1].split(";", 1)[0]
        assert img_src.strip() == "'self' data:", f"{path}: img-src widened to {img_src!r}"
