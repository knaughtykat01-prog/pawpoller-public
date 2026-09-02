/* PWA glue — registers the service worker and keeps the mobile status-bar tint
 * (<meta name="theme-color">) in sync with whichever theme is active, since the
 * manifest's theme_color is static. External file so it's covered by the strict
 * CSP's `script-src 'self'` (no inline hash to maintain). */
(function () {
    'use strict';

    // Register the service worker (installability + offline shell). Guarded so a
    // failure is silent — the app works fine without it.
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js').catch(function (e) {
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
