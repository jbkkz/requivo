# Third-party notices

Requivo itself is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`). Its Python
dependencies are declared in `pyproject.toml` and installed from PyPI, so they carry their own
licenses and are not redistributed here. This file covers the one thing that *is* copied into this
repository and shipped inside the wheel.

## The digests, and why they exist

Every file under `src/requivo/api/static/vendor/` and `src/requivo/web/static/vendor/` has its
SHA-256 recorded in **`THIRD-PARTY-DIGESTS.txt`**, and
`tests/test_vendored_bundles_are_not_normalized.py` recomputes them on every CI leg, Windows
included. Until #510 there was no digest anywhere, so this file's own version lines were the only
record of what had been vendored — and a hand-refresh that grabbed the wrong artifact, or a local
edit, was undetectable from inside the repository. That is the failure `CLAUDE.md` names about
itself (*a claim in prose that no test can falsify buys one release and then lies*), applied to a
byte-identity claim rather than to a number.

The file is plain `shasum -a 256` output with no comment lines, so it verifies with the standard
tool and no arguments:

```bash
shasum -a 256 -c THIRD-PARTY-DIGESTS.txt     # or: sha256sum -c THIRD-PARTY-DIGESTS.txt
```

It also makes `.gitattributes`'s `-text` rule enforceable rather than aspirational: a checkout that
rewrote a byte inside a minified string literal fails the digest instead of silently serving
different code. Nothing fetches from npm to check this, deliberately — the point is to pin what was
vendored, not to re-download it; a network call would make the check flaky and would verify the
registry rather than the repository. The digests were verified against `registry.npmjs.org` once,
out of band, when they were recorded: 8 of 8 matching, with `git rev-parse HEAD:<path>` against
`git hash-object` on each to confirm the committed objects rather than a working tree.

**Refreshing any vendored bundle is therefore four steps, not three:** replace the file verbatim,
update its digest in `THIRD-PARTY-DIGESTS.txt`, bump the **Version** line in this file, and re-run
the tests. Each procedure below states it in place.

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
3. Update its line in `THIRD-PARTY-DIGESTS.txt` (`shasum -a 256 src/requivo/web/static/vendor/htmx.min.js`).
4. Bump the **Version** line above.
5. Re-run the web tests: `pytest tests/web -q`, and
   `pytest tests/test_vendored_bundles_are_not_normalized.py -q` for the digest.

No Node toolchain is added for one file, and none should be. 1.9.12 is on the 1.x maintenance line,
superseded by 2.x; moving is a deliberate decision rather than a routine bump, since 2.x changes
default behaviours the templates rely on.

## swagger-ui-dist

- **Version:** 5.32.15
- **Files:** `src/requivo/api/static/vendor/swagger-ui/swagger-ui-bundle.js`,
  `swagger-ui-standalone-preset.js`, `swagger-ui.css`, and their two companion `.LICENSE.txt` files
  (`swagger-ui-bundle.js.LICENSE.txt`, `swagger-ui-standalone-preset.js.LICENSE.txt`)
- **Upstream:** https://www.npmjs.com/package/swagger-ui-dist — https://github.com/swagger-api/swagger-ui
- **License:** Apache License 2.0 — the same license as this project (see `LICENSE`); no separate
  text reproduced here for that reason.

`swagger-ui-bundle.js.LICENSE.txt` and `swagger-ui-standalone-preset.js.LICENSE.txt` ride along
next to their respective bundles, unedited from the npm package — each is upstream's own webpack
build attributing the third-party (mostly MIT) sub-dependencies compiled into that one file
(`classnames`, `buffer`, `ieee754`, and others). Found missing by the v3.2.0 release audit
reviewing #504: vendoring a bundle that itself bundles other people's code redistributes their
license notices too, not only swagger-ui-dist's own Apache-2.0 one — the same "a redistributed file
should be traceable to its source" argument this file opens with for htmx, one layer further in.

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
   `swagger-ui-standalone-preset.js`, `swagger-ui.css`, and their two companion `.LICENSE.txt` files.
2. Replace the five files under `src/requivo/api/static/vendor/swagger-ui/` verbatim — no local
   edits, ever.
3. Leave `swagger-initializer.js` alone unless the new version's own `index.html` changed which
   scripts it loads or in what order (it has not, across 5.x).
4. Update all five lines in `THIRD-PARTY-DIGESTS.txt`
   (`shasum -a 256 src/requivo/api/static/vendor/swagger-ui/*`).
5. Bump the **Version** line above.
6. Re-run `pytest tests/api -q`, which includes `test_api_docs_assets.py`'s no-external-origin and
   `validatorUrl: null` assertions, and
   `pytest tests/test_vendored_bundles_are_not_normalized.py -q` for the digests.

## redoc

- **Version:** 2.5.3
- **Files:** `src/requivo/api/static/vendor/redoc/redoc.standalone.js` and its companion
  `redoc.standalone.js.LICENSE.txt`
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
concatenated into this same file at build time upstream). `redoc.standalone.js.LICENSE.txt` rides
along next to it for the same reason as swagger-ui-dist's own two `.LICENSE.txt` files above —
upstream's webpack build attributing the third-party sub-dependencies compiled into the bundle
(`classnames`, Stickyfill, and others), unedited from the npm package.

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

1. Download `redoc` at the target version from npm and extract `bundles/redoc.standalone.js` and
   its companion `bundles/redoc.standalone.js.LICENSE.txt`.
2. Replace both files under `src/requivo/api/static/vendor/redoc/` verbatim — no local edits, ever.
3. Update both lines in `THIRD-PARTY-DIGESTS.txt`
   (`shasum -a 256 src/requivo/api/static/vendor/redoc/*`).
4. Bump the **Version** line above.
5. Re-run `pytest tests/api -q`, and
   `pytest tests/test_vendored_bundles_are_not_normalized.py -q` for the digests.
