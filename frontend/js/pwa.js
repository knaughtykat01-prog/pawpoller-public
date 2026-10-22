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

    /* Fresh copy (4.81.1, SWSTUCK). A phone kept a tester on an old app: an old worker plus its saved
     * copy, with the page's requests dying on the phone while the server answered fine (Incognito
     * worked). Two triggers lead to one cure (drop every worker + saved copy, reload once):
     *
     *  - Version watch: on load, and when the tab comes back after 5 minutes or more, ask
     *    /api/health which version the server runs. A different one means this page is old → refresh.
     *    That's the cache busting. It runs once per version (sessionStorage), so a stale edge copy
     *    can't loop it, and it waits while someone is typing.
     *  - Stuck requests: 3 network failures inside 30 s while /api/health answers means the server
     *    is fine and this page isn't → refresh, at most once per 10 minutes. A real outage fails the
     *    health check too, so nothing reloads into an offline page. */
    var _ss = {
        get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
        set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    };

    function _health() {
        var ctl = 'AbortController' in window ? new AbortController() : null;
        var t = ctl && setTimeout(function () { ctl.abort(); }, 8000);
        return fetch('/api/health?t=' + Date.now(), { cache: 'no-store', signal: ctl && ctl.signal })
            .then(function (r) { return r.ok ? r.json() : null; })
            .catch(function () { return null; })
            .finally(function () { if (t) clearTimeout(t); });
    }

    function _editing() {
        var a = document.activeElement;
        return !!a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
    }

    function freshCopy() {
        var jobs = [];
        if ('serviceWorker' in navigator) {
            jobs.push(navigator.serviceWorker.getRegistrations().then(function (regs) {
                return Promise.all(regs.map(function (r) { return r.unregister(); }));
            }));
        }
        if ('caches' in window) {
            jobs.push(caches.keys().then(function (keys) {
                return Promise.all(keys.map(function (k) { return caches.delete(k); }));
            }));
        }
        return Promise.all(jobs).catch(function () {}).then(function () { location.reload(); });
    }

    var _lastCheck = 0;
    function checkVersion() {
        if (!_appVer || _editing()) return;
        _lastCheck = Date.now();
        _health().then(function (h) {
            var v = h && (h.asset || h.version);                      // asset = version + frontend fingerprint
            if (!v || v === _appVer) return;
            if (_ss.get('pp-fresh-for') === v) return;                // already tried for this version
            _ss.set('pp-fresh-for', v);
            freshCopy();
        });
    }

    var _fails = [];
    var _healing = false;
    function networkError() {
        var now = Date.now();
        _fails = _fails.filter(function (t) { return now - t < 30000; });
        _fails.push(now);
        if (_fails.length < 3 || _healing) return;
        if (now - Number(_ss.get('pp-healed-at') || 0) < 600000) return;
        _healing = true;
        _health().then(function (h) {
            if (!h) { _healing = false; return; }                     // the server is down, not us
            _ss.set('pp-healed-at', String(Date.now()));
            freshCopy();
        });
    }

    window.PawFresh = { networkError: networkError, freshCopy: freshCopy };
    window.addEventListener('load', checkVersion);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && Date.now() - _lastCheck > 300000) checkVersion();
    });

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
