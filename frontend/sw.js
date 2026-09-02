/* PawPoller service worker — makes the dashboard an installable PWA.
 *
 * SAFETY RULES (this is a LIVE, auth'd, polling dashboard):
 *   - NEVER cache /api/* — polling data + auth must always hit the network,
 *     or the app would show stale numbers / broken sessions.
 *   - NEVER cache non-GET or cross-origin requests.
 *   - App navigations are network-first (the server decides login vs app), with
 *     the cached shell only as an OFFLINE fallback.
 *   - Static assets are cache-first ONLY because they carry ?v=APP_VERSION —
 *     a new release requests new URLs, so a stale asset is impossible.
 *
 * The cache name embeds APP_VERSION (spliced in by dashboard.py when it serves
 * /sw.js), so every deploy changes this file's bytes → the browser installs the
 * new worker → activate() purges all older caches. */
const VERSION = '__APP_VERSION__';
const CACHE = 'pawpoller-shell-' + VERSION;
const OFFLINE_URL = '/';

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        try {
            const cache = await caches.open(CACHE);
            await cache.add(new Request(OFFLINE_URL, { cache: 'reload' }));
        } catch (e) { /* offline at install — fine, populated on first online nav */ }
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => (k === CACHE ? null : caches.delete(k))));
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;                       // writes always hit the network
    let url;
    try { url = new URL(req.url); } catch (e) { return; }
    if (url.origin !== self.location.origin) return;        // ignore cross-origin
    if (url.pathname.startsWith('/api/')) return;           // live data + auth — never cache

    // App shell navigations: network-first, cached copy only when offline.
    if (req.mode === 'navigate') {
        event.respondWith((async () => {
            try {
                const net = await fetch(req);
                if (net && net.ok) {
                    const cache = await caches.open(CACHE);
                    cache.put(req, net.clone());
                }
                return net;
            } catch (e) {
                const cache = await caches.open(CACHE);
                return (await cache.match(req)) || (await cache.match(OFFLINE_URL)) || Response.error();
            }
        })());
        return;
    }

    // Versioned static assets: cache-first (URLs change per release → never stale).
    if (/\.(?:css|js|png|jpe?g|svg|gif|webp|ico|woff2?|ttf|webmanifest)$/i.test(url.pathname)) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE);
            const hit = await cache.match(req);
            if (hit) return hit;
            try {
                const net = await fetch(req);
                if (net && net.ok && net.type === 'basic') cache.put(req, net.clone());
                return net;
            } catch (e) {
                return hit || Response.error();
            }
        })());
    }
    // Anything else falls through to the default network handling.
});
