/* PawPoller quick-reconnect modal.
 *
 * When a platform's session goes expired/error (a dead cookie, an invalidated
 * token — e.g. Meta code 190), the user can paste fresh credentials right here
 * instead of digging through Settings. It POSTs to the SAME per-platform
 * /api/{code}/auth/connect endpoint — which validates live and re-saves in one
 * call — then re-checks the session and kicks a fresh poll ("...and sync").
 *
 * Public API (window.Reconnect):
 *   open(code)          open the modal for a platform (no-op if no field spec)
 *   canReconnect(code)  is there a reconnect field spec for this platform
 *
 * CSP-safe (no inline handlers); mirrors the guide-modal shell for a consistent
 * look. The field spec per platform mirrors each connect endpoint's body. */
window.Reconnect = (function () {
    'use strict';

    function f(key, label, ph, req, type) {
        return { key: key, label: label, ph: ph || '', req: !!req, type: type || 'text' };
    }

    // Per-platform reconnect fields — the checkable (session-validated) set.
    // Token/key-based platforms are a single paste; the login-based ones carry
    // their full field set. Posts to POST /api/{code}/auth/connect.
    const SPEC = {
        thr:  { label: 'Threads',   emoji: '🧵', hint: 'Regenerate a long-lived token in your Meta app (scopes: threads_basic, threads_manage_insights).',
                fields: [ f('access_token', 'Access token', 'Long-lived access token', true), f('user_id', 'User ID', 'optional — auto-resolved', false) ] },
        ig:   { label: 'Instagram', emoji: '📷', hint: 'Regenerate a long-lived Instagram token (scopes: instagram_business_basic, instagram_business_manage_insights).',
                fields: [ f('access_token', 'Access token', 'Long-lived access token', true), f('user_id', 'User ID', 'optional — auto-resolved', false) ] },
        pix:  { label: 'Pixiv',     emoji: '🅿️', hint: '',
                fields: [ f('refresh_token', 'Refresh token', 'Refresh token', true), f('user_id', 'Target user ID', 'optional — defaults to you', false) ] },
        mast: { label: 'Mastodon',  emoji: '🐘', hint: '',
                fields: [ f('instance_url', 'Instance URL', 'https://mastodon.social', true), f('access_token', 'Access token', 'Access token', true) ] },
        bsky: { label: 'Bluesky',   emoji: '🦋', hint: 'Create a fresh app password in Bluesky → Settings → App Passwords.',
                fields: [ f('identifier', 'Handle', 'you.bsky.social', true), f('app_password', 'App password', 'xxxx-xxxx-xxxx-xxxx', true) ] },
        tum:  { label: 'Tumblr',    emoji: '📱', hint: '',
                fields: [ f('blog', 'Blog', 'staff or staff.tumblr.com', true), f('api_key', 'OAuth consumer key', 'API key', true) ] },
        sqw:  { label: 'SquidgeWorld', emoji: '🦑', hint: '',
                fields: [ f('username', 'Username', '', true), f('password', 'Password', '', true, 'password'), f('target_user', 'Username to track', '', true) ] },
        sf:   { label: 'SoFurry',   emoji: '🦊', hint: '',
                fields: [ f('username', 'Email', '', true), f('password', 'Password', '', true, 'password'), f('display_name', 'Display name', '', true), f('totp_code', '2FA code', 'only if 2FA is enabled', false) ] },
        ao3:  { label: 'AO3',       emoji: '📕', hint: 'Paste a fresh _otwarchive_session cookie, or your login username + password.',
                fields: [ f('target_user', 'Username to track', 'your AO3 username', true), f('session_cookie', 'Session cookie', '_otwarchive_session value', false), f('username', 'Login username', 'optional', false), f('password', 'Password', 'optional', false, 'password') ] },
        e621: { label: 'e621',      emoji: '🐾', hint: 'Regenerate an API key on e621 → Account → Manage API Access (this is the API key, not your password).',
                fields: [ f('username', 'Username', 'your e621 username', true), f('api_key', 'API key', 'API key', true, 'password') ] },
    };

    let _el = null;

    function esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function canReconnect(code) { return !!SPEC[code]; }

    function _onKey(e) { if (e.key === 'Escape') close(); }

    function close() {
        if (_el) { _el.remove(); _el = null; }
        document.removeEventListener('keydown', _onKey);
    }

    function open(code) {
        const spec = SPEC[code];
        if (!spec) return;
        close();   // never stack two

        const fieldsHtml = spec.fields.map(fl => `
            <label class="rc-field">
                <span class="rc-flabel">${esc(fl.label)}${fl.req ? '' : ' <em>optional</em>'}</span>
                <input class="rc-input" type="${esc(fl.type)}" data-key="${esc(fl.key)}"
                       placeholder="${esc(fl.ph)}" autocomplete="off" autocapitalize="off" spellcheck="false">
            </label>`).join('');

        _el = document.createElement('div');
        _el.className = 'guide-modal';
        _el.id = 'reconnect-modal';
        _el.innerHTML =
            `<div class="guide-modal-card rc-card" role="dialog" aria-modal="true" aria-label="Reconnect ${esc(spec.label)}">
                <div class="guide-modal-head">
                    <span class="guide-modal-emoji">${spec.emoji || '🔑'}</span>
                    <h3 class="guide-modal-title">Reconnect ${esc(spec.label)}</h3>
                    <button class="guide-modal-close" type="button" aria-label="Close">&times;</button>
                </div>
                <div class="guide-modal-body rc-body">
                    <p class="rc-lead">Paste fresh credentials to get ${esc(spec.label)} polling again — they're validated the moment you save.</p>
                    ${spec.hint ? `<p class="rc-hint">${esc(spec.hint)}</p>` : ''}
                    <form class="rc-form">
                        ${fieldsHtml}
                        <div class="rc-msg" hidden></div>
                        <div class="rc-actions">
                            <button type="button" class="rc-btn rc-cancel">Cancel</button>
                            <button type="submit" class="rc-btn rc-primary rc-save">Save &amp; sync</button>
                        </div>
                    </form>
                </div>
            </div>`;

        _el.addEventListener('click', e => { if (e.target === _el) close(); });
        _el.querySelector('.guide-modal-close').addEventListener('click', close);
        _el.querySelector('.rc-cancel').addEventListener('click', close);
        _el.querySelector('.rc-form').addEventListener('submit', (e) => { e.preventDefault(); submit(code, spec); });

        document.body.appendChild(_el);
        document.addEventListener('keydown', _onKey);
        const first = _el.querySelector('.rc-input');
        if (first) first.focus();
    }

    async function submit(code, spec) {
        if (!_el) return;
        const msg = _el.querySelector('.rc-msg');
        const body = {};
        _el.querySelectorAll('.rc-input').forEach(inp => {
            const v = (inp.value || '').trim();
            if (v) body[inp.dataset.key] = v;
        });
        const missing = spec.fields.filter(fl => fl.req && !body[fl.key]);
        if (missing.length) {
            _msg(msg, `Please fill in: ${missing.map(x => x.label).join(', ')}.`, 'err');
            return;
        }

        const saveBtn = _el.querySelector('.rc-save');
        saveBtn.disabled = true;
        saveBtn.textContent = 'Validating…';
        _msg(msg, '', 'clear');
        try {
            const resp = await API.post(`/api/${code}/auth/connect`, body);
            _msg(msg, (resp && resp.message) || `${spec.label} reconnected.`, 'ok');
            // "...and sync": re-validate the session + kick a fresh poll.
            try { await API.post(`/api/${code}/poll/trigger`, {}); } catch (e) { /* best effort */ }
            try { await API.triggerSessionCheck(); } catch (e) { /* best effort */ }
            if (window.toast) window.toast.success(`${spec.label} reconnected — syncing…`);
            setTimeout(() => {
                close();
                if (window.NotificationCenter && NotificationCenter.poll) NotificationCenter.poll();
                if (window.PlatformHealth && PlatformHealth.fetchOnce) PlatformHealth.fetchOnce();
            }, 800);
        } catch (err) {
            _msg(msg, _parseErr(err), 'err');
            saveBtn.disabled = false;
            saveBtn.textContent = 'Save & sync';
        }
    }

    function _parseErr(err) {
        let t = (err && err.message) || 'Something went wrong — check the credentials and try again.';
        t = t.replace(/^API \d+:\s*/, '');
        try { const j = JSON.parse(t); if (j && j.detail) t = j.detail; } catch (e) { /* plain text */ }
        return t;
    }

    function _msg(el, text, kind) {
        if (!el) return;
        if (kind === 'clear' || !text) { el.hidden = true; el.textContent = ''; return; }
        el.hidden = false;
        el.textContent = text;
        el.className = 'rc-msg ' + (kind === 'ok' ? 'is-ok' : 'is-err');
    }

    return { open, canReconnect };
})();
