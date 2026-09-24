/* PWA glue — registers the service worker and keeps the mobile status-bar tint
 * (<meta name="theme-color">) in sync with whichever theme is active, since the
 * manifest's theme_color is static. External file so it's covered by the strict
 * CSP's `script-src 'self'` (no inline hash to maintain). */
(function () {
    'use strict';

    // This script's own ?v= is the app version (index.html substitutes it). Read at
    // module scope, where `currentScript` is still valid — inside the load handler
    // below it is null.
    var _appVer = '';
    try {
        _appVer = new URL(document.currentScript.src).searchParams.get('v') || '';
    } catch (e) { /* older browser or no query — fall through to the plain URL */ }

    /* Register the service worker (installability + offline shell). Guarded so a
     * failure is silent — the app works fine without it.
     *
     * Two anti-staleness measures, because a cached service worker is uniquely bad
     * (4.34.1, SWCACHE). `dashboard.py` already serves /sw.js with `Cache-Control:
     * no-cache` and **Cloudflare overrides it** — the public response carries
     * `max-age=14400`. A worker cached for four hours means a phone can keep serving
     * the OLD app after a deploy, which is what happened after 3.26–3.32.
     *
     *  - `updateViaCache: 'none'` tells the browser to bypass the HTTP cache when it
     *    checks the worker script for an update. That is the spec's answer to exactly
     *    this, and it does not care what an edge cache says.
     *  - The `?v=` makes the URL itself change per release, so even a browser that
     *    ignores the hint sees a script it has never fetched. Belt and braces, because
     *    the failure mode is a user stuck on an old app with no way to know.
     *
     * A Cloudflare cache rule for /sw.js is still worth adding, but this no longer
     * depends on one — the fix should live where the app is, not in a dashboard nobody
     * remembers to check.
     */
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            var url = '/sw.js' + (_appVer ? '?v=' + encodeURIComponent(_appVer) : '');
            navigator.serviceWorker.register(url, { updateViaCache: 'none' })
                .catch(function (e) {
                    console.debug('[pwa] service worker registration failed', e);
                });
        });
    }

    // Keep the status-bar / task-switcher tint matching the resolved theme's
    // paper colour (so a dark theme gets a dark bar, quill gets warm paper).
    try {
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) {
            var apply = function () {
                var bg = getComputedStyle(document.documentElement)
                    .getPropertyValue('--bg-primary').trim();
                if (bg) meta.setAttribute('content', bg);
            };
            apply();
            // data-theme flips when the user switches themes — re-tint on change.
            if ('MutationObserver' in window) {
                new MutationObserver(apply).observe(document.documentElement, {
                    attributes: true, attributeFilter: ['data-theme'],
                });
            }
        }
    } catch (e) { /* non-critical */ }
})();
