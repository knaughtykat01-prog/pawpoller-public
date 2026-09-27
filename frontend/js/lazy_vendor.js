/* ── Lazy vendor groups — load a page's heavy libraries when it opens (4.40.0) ──
 *
 * Spec 007 (specs/007-public-hygiene, contracts/lazy-vendor.md). The story
 * editor's five libraries — CodeMirror, the three beautifiers, Turndown — are
 * ~765 KB and nothing else uses them, but index.html loaded them on EVERY page.
 * They now load the first time the editor opens.
 *
 *   LazyVendor.load('editor') → Promise<void>
 *
 * - Appends same-origin <script> tags IN ORDER (each waits for the previous;
 *   order matters for UMD globals) and resolves when the last one has loaded.
 * - One Promise per group per session: repeat and concurrent calls share it.
 * - Rejects on the first failed script and forgets the Promise, so the
 *   caller's Retry fetches again rather than replaying the failure.
 * - URLs carry this file's own `?v=` — the server stamps the app version into
 *   every script src at serve time — so a deferred file always matches the
 *   page that asked for it, and a new version is a new cache key.
 *
 * CSP: `script-src 'self'` already permits a same-origin script added at
 * runtime; nothing here evaluates strings or loads cross-origin.
 */
(function () {
    const GROUPS = {
        editor: [
            'codemirror-bundle.min.js',
            'turndown.min.js',
            'beautify.min.js',
            'beautify-html.min.js',
            'beautify-css.min.js',
        ],
    };

    // The `v` of this script's own src ("" when unversioned, e.g. a test page).
    const self = document.currentScript;
    let version = '';
    try { version = new URL(self && self.src, location.href).searchParams.get('v') || ''; }
    catch (e) { /* no version — the files still load, just unversioned */ }

    const pending = {};

    function addScript(file) {
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = `/js/vendor/${file}${version ? `?v=${encodeURIComponent(version)}` : ''}`;
            s.async = false;
            s.onload = () => resolve();
            s.onerror = () => { s.remove(); reject(new Error(`Could not load ${file}`)); };
            document.head.appendChild(s);
        });
    }

    window.LazyVendor = {
        GROUPS,
        load(group) {
            const files = GROUPS[group];
            if (!files) return Promise.reject(new Error(`Unknown vendor group: ${group}`));
            if (!pending[group]) {
                pending[group] = files.reduce((p, f) => p.then(() => addScript(f)), Promise.resolve())
                    .catch((err) => { delete pending[group]; throw err; });
            }
            return pending[group];
        },
    };
})();
