/* Sounds (spec 049, TEXTURE) — one gate for every sound the app plays.
 *
 *   Sound.play(name)   name = a file stem in /sounds/ (see sounds/SOURCES.md)
 *   Sound.get() / Sound.set({mode, volume}) / Sound.preview()
 *
 * Per device, in localStorage: `pp-sound` ('off' | 'key', Off by default) and `pp-sound-vol` (0–100).
 * The same sound twice inside a second plays once, so ten posts finishing together don't stack.
 * A play the browser refuses (no click yet, file missing) is dropped, never queued to play late.
 * No library: a plain Audio element per play; the service worker caches the files. */
(function () {
    'use strict';

    const NAMES = new Set(['post_up', 'post_failed', 'error', 'news', 'batch_done', 'medal', 'level_up',
        'connected', 'scheduled', 'connection_lost', 'connection_back', 'job_done', 'commission']);
    const GAP_MS = 1000;
    const last = {};

    let ver = '';
    try { ver = new URL(document.currentScript.src).searchParams.get('v') || ''; } catch (e) { /* no ?v= */ }

    function read(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } }
    function write(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { /* storage blocked */ } }

    function get() {
        const mode = read('pp-sound', 'off') === 'key' ? 'key' : 'off';
        const n = parseInt(read('pp-sound-vol', '60'), 10);
        return { mode, volume: Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 60 };
    }

    function set(o) {
        if (o.mode !== undefined) write('pp-sound', o.mode === 'key' ? 'key' : 'off');
        if (o.volume !== undefined) write('pp-sound-vol', Math.min(100, Math.max(0, Math.round(o.volume))));
    }

    function out(name, volume) {
        try {
            const a = new Audio('/sounds/' + name + '.mp3' + (ver ? '?v=' + encodeURIComponent(ver) : ''));
            a.volume = volume / 100;
            const p = a.play();
            if (p && p.catch) p.catch(() => {});
        } catch (e) { /* no audio here */ }
    }

    function play(name) {
        if (!NAMES.has(name)) return;
        const s = get();
        if (s.mode === 'off') return;
        const now = Date.now();
        if (now - (last[name] || 0) < GAP_MS) return;
        last[name] = now;
        out(name, s.volume);
    }

    window.Sound = { play, get, set, preview: () => out('news', get().volume) };
})();
