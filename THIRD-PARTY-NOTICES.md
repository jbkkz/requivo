# Third-party notices

Requivo itself is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`). Its Python
dependencies are declared in `pyproject.toml` and installed from PyPI, so they carry their own
licenses and are not redistributed here. This file covers the one thing that *is* copied into this
repository and shipped inside the wheel.

## htmx

- **Version:** 1.9.12
- **File:** `src/requivo/web/static/vendor/htmx.min.js`
- **Upstream:** https://htmx.org — https://github.com/bigskysoftware/htmx
- **License:** Zero-Clause BSD (0BSD)

```
Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee
is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE
INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE
LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING
FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

0BSD imposes no attribution requirement — this notice is here because a redistributed file should be
traceable to its source and version, not because the license demands it.

**Why it is vendored rather than fetched:** Requivo Web sets a strict Content-Security-Policy that
allows same-origin assets only, and it is meant to work offline. A CDN script tag would violate both.

**How it is updated, and who does it.** The maintainer, by hand, because nothing else can: a
minified `.js` file appears in no dependency manifest, so `.github/dependabot.yml` cannot watch it
and neither can any advisory scanner (#297). The version above is therefore the only record that
this file has a version at all — which is why it is stated here rather than left to the file's own
banner. The procedure:

1. Download the release from <https://github.com/bigskysoftware/htmx/releases> (`htmx.min.js`).
2. Replace `src/requivo/web/static/vendor/htmx.min.js` verbatim — no local edits, ever, or the
   version above stops describing what is shipped.
3. Bump the **Version** line above.
4. Re-run the web tests: `pytest tests/web -q`.

No Node toolchain is added for one file, and none should be. 1.9.12 is on the 1.x maintenance line,
superseded by 2.x; moving is a deliberate decision rather than a routine bump, since 2.x changes
default behaviours the templates rely on.

## swagger-ui-dist

- **Version:** 5.32.15
- **Files:** `src/requivo/api/static/vendor/swagger-ui/swagger-ui-bundle.js`,
  `swagger-ui-standalone-preset.js`, `swagger-ui.css`
- **Upstream:** https://www.npmjs.com/package/swagger-ui-dist — https://github.com/swagger-api/swagger-ui
- **License:** Apache License 2.0 — the same license as this project (see `LICENSE`); no separate
  text reproduced here for that reason.

**Why it is vendored rather than fetched from a CDN.** `/docs` used to load
`swagger-ui-dist@5` (a floating major, no lockfile) from `cdn.jsdelivr.net` into the same origin
`GET /api/v1/sessions` answers 200 with no credential (#504). This app's own CSP also sets
`script-src 'self'` (#503), which a CDN script tag cannot satisfy without an exception carved into
the one directive that governs code execution — the coupling both issues were filed and fixed
together over.

**`swagger-initializer.js` is this project's own file, not a verbatim copy** — see that file's own
header comment for the two departures from upstream's version (no hardcoded demo spec, and
`validatorUrl: null` so the page never phones home to `https://validator.swagger.io`).

**How it is updated, and who does it.** By hand, for the same reason as htmx above: a minified
bundle in three files appears in no dependency manifest, so nothing scans it for advisories.

1. Download `swagger-ui-dist` at the target version from npm (`npm view swagger-ui-dist@<version>
   dist.tarball`, or the registry tarball directly) and extract `swagger-ui-bundle.js`,
   `swagger-ui-standalone-preset.js` and `swagger-ui.css`.
2. Replace the three files under `src/requivo/api/static/vendor/swagger-ui/` verbatim — no local
   edits, ever.
3. Leave `swagger-initializer.js` alone unless the new version's own `index.html` changed which
   scripts it loads or in what order (it has not, across 5.x).
4. Bump the **Version** line above.
5. Re-run `pytest tests/api -q`, which includes `test_api_docs_assets.py`'s no-external-origin and
   `validatorUrl: null` assertions.

## redoc

- **Version:** 2.5.3
- **File:** `src/requivo/api/static/vendor/redoc/redoc.standalone.js`
- **Upstream:** https://www.npmjs.com/package/redoc — https://github.com/Redocly/redoc
- **License:** MIT

```
The MIT License (MIT)

Copyright (c) 2015-present, Rebilly, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

Vendored for the same reason as `swagger-ui-dist` above: `/redoc` loaded `redoc@2` from
`cdn.jsdelivr.net`, plus the Montserrat/Roboto Google Fonts (#504). The fonts are dropped rather
than vendored — `api/routes/docs.py`'s ReDoc page passes no font stylesheet at all, so ReDoc renders
in the browser's own default sans-serif rather than a self-hosted substitute; nothing in ReDoc's own
layout depends on Montserrat specifically. The worker `redoc.standalone.js` uses internally for
search indexing is created from an in-memory `Blob`, not fetched, so no second file is needed for it
(verified directly against the bundle: `new Blob([...])` wraps the worker source, which is
concatenated into this same file at build time upstream).

**Known limit, found by the v3.2.0 release audit reviewing this change and not yet closed.** The
sidebar `redoc.standalone.js` renders on every `/redoc` page carries an unconditional "API docs by
Redocly" attribution link with a small logo image, and mounting it always attempts
`https://cdn.redoc.ly/redoc/logo-mini.svg` regardless of any option this project passes — verified
directly against the bundle (the component's own `useEffect` fires on mount with no gate) and
against the rendered page. This app's CSP (`img-src 'self' data:`, no override — see `api/app.py`)
refuses the load before any byte leaves the machine, so there is no actual disclosure; what remains
true is narrower than "no request is attempted," which is the bar `swagger-initializer.js`'s
`validatorUrl: null` meets for the equivalent Swagger UI case. No supported ReDoc option suppresses
just this element — `disableSidebar` would remove the navigation sidebar entirely, which is a
disproportionate response, and patching the vendored bundle is exactly what this file's own "no
local edits, ever" rule forbids. Filed rather than fixed for that reason: the available remedies
(accept the CSP-mitigated residual, patch the vendor file as a named exception, or drop ReDoc) are a
maintainer decision, not one this change makes unilaterally.

**How it is updated, and who does it.** By hand, as above.

1. Download `redoc` at the target version from npm and extract `bundles/redoc.standalone.js`.
2. Replace `src/requivo/api/static/vendor/redoc/redoc.standalone.js` verbatim — no local edits, ever.
3. Bump the **Version** line above.
4. Re-run `pytest tests/api -q`.
