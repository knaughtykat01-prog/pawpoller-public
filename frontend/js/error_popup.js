/* ── Error popup — achievement-style error cards (2.159.0) ──────────
 *
 * When an API action fails, show a card in the same visual language as the
 * Laurels "Achievement unlocked" celebration (laurels.js) — but red, and with
 * two actions: copy the technical report, or send it straight to the dev via
 * the instance's Telegram (POST /api/report-error → polling/telegram.py).
 *
 * Wired from api.js: the mutating transports (POST / PATCH / DELETE) call
 * ErrorPopup.onApiError() on any failure, so every broken action gets one
 * consistent, pretty surface without each screen opting in. Screens that
 * already show their own inline error still do — this card adds the
 * copy/report affordance on top, it doesn't replace contextual handling.
 *
 * Guards (each one prevents a real annoyance):
 *   - /api/report-error itself is excluded — a failing report must never
 *     recurse into another popup.
 *   - /api/auth/* and connect/validate paths are excluded — login forms and
 *     credential-connect flows have their own inline messaging, and popping
 *     "Not authorised" over a login form would be absurd.
 *   - Duplicate (method+path+status) within 8s is dropped, and only one card
 *     shows at a time — a burst of failures (e.g. server down) yields one
 *     popup, not a stack.
 */
(function () {
    'use strict';

    const REPORT_PATH = '/api/report-error';
    const DEDUP_MS = 8000;

    let _open = null;                 // the overlay element while showing
    let _last = { key: '', t: 0 };    // dedup memory

    /* App version, parsed from api.js's cache-busting query (?v=X.Y.Z) —
     * the server stamps __APP_VERSION__ into every script src at serve time,
     * so this is free and always matches the running backend. */
    function _version() {
        const s = document.querySelector('script[src*="/js/api.js"]');
        if (!s) return '';
        try { return new URL(s.src, location.origin).searchParams.get('v') || ''; }
        catch (e) { return ''; }
    }

    /* FastAPI error bodies are {"detail": "..."} — surface just the detail. */
    function _clean(text) {
        try {
            const j = JSON.parse(text);
            if (j && typeof j.detail === 'string') return j.detail;
        } catch (e) { /* not JSON — use as-is */ }
        return String(text || '').slice(0, 500);
    }

    function _title(status) {
        if (!status) return 'Server unreachable';
        if (status === 401 || status === 403) return 'Not authorised';
        if (status === 404) return 'Not found';
        if (status >= 500) return 'Server error';
        return 'That didn’t work';
    }

    function _excluded(path) {
        const p = String(path || '');
        return p.includes(REPORT_PATH)
            || p.startsWith('/api/auth/')
            || /\/(connect|validate)\b/i.test(p);
    }

    function _reportText(r) {
        return [
            `PawPoller error report`,
            `Where:   ${r.context}`,
            `Screen:  ${r.url}`,
            `Version: ${r.version}`,
            `Time:    ${new Date().toISOString()}`,
            `Message: ${r.message}`,
            ``,
            r.detail,
        ].join('\n');
    }

    function _dismiss() {
        if (!_open) return;
        const el = _open;
        _open = null;
        el.classList.remove('show');
        setTimeout(() => el.remove(), 320);
        document.removeEventListener('keydown', _onKey);
    }

    function _onKey(e) {
        if (e.key === 'Escape') _dismiss();
    }

    /* Build + show the card. `r` = {context, message, detail, url, version}. */
    function show(r) {
        if (_open) return;            // one at a time; first error wins

        const el = document.createElement('div');
        el.className = 'ep-overlay';
        el.innerHTML = `
            <div class="ep-card" role="alertdialog" aria-live="assertive" aria-label="Error">
                <div class="ep-rays"></div>
                <div class="ep-ico">💥</div>
                <div class="ep-label">Something went wrong</div>
                <div class="ep-name"></div>
                <div class="ep-desc"></div>
                <details class="ep-tech">
                    <summary>Technical details</summary>
                    <pre class="ep-pre"></pre>
                </details>
                <div class="ep-actions">
                    <button type="button" class="ep-btn" data-ep="copy">📋 Copy report</button>
                    <button type="button" class="ep-btn ep-btn-send" data-ep="send">📨 Send to dev</button>
                </div>
                <div class="ep-hint">esc or tap outside to dismiss</div>
            </div>`;
        el.querySelector('.ep-name').textContent = r.title || 'Unexpected error';
        el.querySelector('.ep-desc').textContent =
            r.message || 'The app hit an error it didn’t expect.';
        el.querySelector('.ep-pre').textContent = _reportText(r);

        el.addEventListener('click', (e) => {
            if (!e.target.closest('.ep-card')) _dismiss();
        });
        el.querySelector('[data-ep="copy"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            try {
                await navigator.clipboard.writeText(_reportText(r));
                btn.textContent = '✅ Copied';
            } catch (err) {
                btn.textContent = '❌ Copy failed';
            }
        });
        el.querySelector('[data-ep="send"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.textContent = '📨 Sending…';
            try {
                // Raw fetch, NOT API.post — a report about a failing API must
                // not route back through the thing that's failing.
                const resp = await fetch(REPORT_PATH, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        context: r.context, message: r.message, detail: r.detail,
                        url: r.url, version: r.version, ua: navigator.userAgent,
                    }),
                });
                const data = resp.ok ? await resp.json() : { sent: false };
                btn.textContent = data.sent ? '✅ Sent to dev'
                    : '⚠ Not sent — Telegram not set up';
            } catch (err) {
                btn.textContent = '❌ Couldn’t send';
            }
        });

        document.body.appendChild(el);
        document.addEventListener('keydown', _onKey);
        _open = el;
        requestAnimationFrame(() => el.classList.add('show'));
    }

    /* The api.js hook. status 0 = network failure (fetch threw). */
    function onApiError(method, path, status, body) {
        if (_excluded(path)) return;
        const key = `${method} ${path} ${status}`;
        const now = Date.now();
        if (key === _last.key && now - _last.t < DEDUP_MS) return;
        _last = { key, t: now };

        show({
            title: _title(status),
            context: `${method} ${path} → ${status || 'network error'}`,
            message: _clean(body),
            detail: String(body || '').slice(0, 1200),
            url: location.hash || '#/',
            version: _version(),
        });
    }

    window.ErrorPopup = { show, onApiError };
})();
