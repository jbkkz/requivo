// Requivo's own copy of swagger-ui-dist's `swagger-initializer.js` (#504) -- the one file in the
// vendored set this project edits rather than ships verbatim, because upstream's version hardcodes
// a demo spec (`https://petstore.swagger.io/v2/swagger.json`) and this app has its own.
//
// Two deliberate departures from upstream, beyond the URL:
//   - `validatorUrl: null`. Unset, Swagger UI's default config POSTs the loaded spec to
//     `https://validator.swagger.io/validator` to paint a green/red badge -- a live phone-home this
//     surface's own CSP already blocks (no `connect-src` override, so it inherits `default-src
//     'self'`), but the point of self-hosting is that the browser never *attempts* the call, not
//     that it silently fails one.
//   - No inline `<script>` at all. `fastapi.openapi.docs.get_swagger_ui_html()` writes the
//     initialization call as an inline block in the HTML it returns, which this project's CSP
//     (`script-src 'self'`, no `'unsafe-inline'`) refuses to execute -- so `docs.py` does not call
//     it, and builds the page by hand instead, exactly the way `swagger-ui-dist`'s own `index.html`
//     does: three external `<script src=...>` tags, this file being the third.
window.onload = function () {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    validatorUrl: null,
    presets: [
      SwaggerUIBundle.presets.apis,
      SwaggerUIStandalonePreset
    ],
    plugins: [
      SwaggerUIBundle.plugins.DownloadUrl
    ],
    layout: "StandaloneLayout"
  });
};
