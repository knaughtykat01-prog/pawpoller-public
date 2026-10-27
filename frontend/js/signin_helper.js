// @ts-check
/* Sign in for a server (spec 059).
 *
 * Two halves, one file:
 *   • codePanel — on a server or seat, "Sign in with the PawPoller app" shows a one-time
 *     code with a 10:00 countdown and watches for the hand-over (POST /api/settings/signin-codes,
 *     then GET …/{id} every 2 s).
 *   • openDesktop — in the PawPoller desktop app, the "Sign in for a server" screen: the
 *     server's address and the code go to pywebview.api.helper_signin, which asks the person
 *     in a system dialog, opens the site's own sign-in page and hands the result over.
 *
 * Everything that came from a server is set with textContent, never as markup (FR-SEC-012).
 * No innerHTML anywhere in this file; a test holds that.
 */
(function () {
    'use strict';

    const DOWNLOAD = 'https://pawpoller.com/download';
    const LAST_SERVER = 'pawpoller.signinHelper.server';

    function el(tag, attrs, ...kids) {
        const n = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs || {})) {
            if (k === 'style') n.style.cssText = v;
            else if (k === 'text') n.textContent = v;
            else n.setAttribute(k, v);
        }
        for (const k of kids) if (k) n.append(k);
        return n;
    }

    function detail(err) {
        let d = String((err && err.message) || err || '').replace(/^API \d+:\s*/, '');
        try { d = JSON.parse(d).detail || d; } catch (e) { /* plain text */ }
        return d;
    }

    // ── the server's code panel ─────────────────────────────────────────────

    async function codePanel(panel, opts) {
        if (panel._signinTimer) clearInterval(panel._signinTimer);
        panel.replaceChildren();
        panel.style.cssText = 'margin-top:12px;padding:12px;border:1px solid var(--border);border-radius:8px;max-width:420px';
        const line = el('p', { style: 'margin:0;font-size:13px', text: 'Making a code…' });
        panel.append(line);
        let made;
        try {
            made = await API.makeSigninCode(opts.platform, opts.accountId == null ? null : opts.accountId, opts.username || '');
        } catch (err) {
            line.textContent = detail(err);
            return;
        }
        panel.replaceChildren();
        const code = el('div', { style: 'font:600 28px/1.2 ui-monospace,monospace;letter-spacing:6px;margin:4px 0 8px',
                                 text: made.code, 'aria-label': 'Sign-in code ' + made.code.split('').join(' ') });
        const steps = el('ol', { style: 'margin:0 0 8px 18px;padding:0;font-size:13px' },
            el('li', { text: 'Open the PawPoller desktop app on your computer (not this page in a browser).' }),
            el('li', { text: 'Choose "Sign in for a server" (Accounts page, or the tray menu).' }),
            el('li', { text: 'Type this server\'s address and the code above.' }));
        const left = el('p', { style: 'margin:0;font-size:12px;color:var(--text-muted)' });
        const status = el('p', { style: 'margin:8px 0 0;font-size:13px', role: 'status', 'aria-live': 'polite' });
        const get = el('a', { href: DOWNLOAD, target: '_blank', rel: 'noopener', text: 'Don\'t have the app? Get it here' });
        panel.append(code, steps, left, status, el('p', { style: 'margin:8px 0 0;font-size:12px' }, get));

        const ends = Date.now() + made.expires_in * 1000;
        const again = () => {
            const b = el('button', { class: 'btn btn-secondary btn-sm', style: 'margin-top:8px', text: 'Make a new code' });
            b.addEventListener('click', () => codePanel(panel, opts));
            panel.append(b);
        };
        const stop = () => { clearInterval(panel._signinTimer); panel._signinTimer = null; };
        const tick = async () => {
            const s = Math.max(0, Math.round((ends - Date.now()) / 1000));
            left.textContent = `Code works for ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} more.`;
            let st;
            try { st = await API.getSigninCode(made.id); } catch (e) { return; }
            if (st.state === 'done') {
                stop();
                code.style.opacity = '0.4';
                left.textContent = '';
                status.textContent = st.handle ? `Connected as ${st.handle} ✓` : 'Connected ✓';
                if (opts.onDone) setTimeout(opts.onDone, 1200);
            } else if (st.state !== 'waiting' || s === 0) {
                stop();
                code.style.opacity = '0.4';
                left.textContent = '';
                status.textContent = st.message || 'That code ran out. Make a new one.';
                again();
            }
        };
        panel._signinTimer = setInterval(tick, 2000);
        tick();
    }

    // ── the desktop's "Sign in for a server" screen ──────────────────────────

    function hasDesktop() {
        return !!(window.pywebview && window.pywebview.api && window.pywebview.api.helper_signin);
    }

    function openDesktop() {
        document.querySelector('.signin-helper-overlay')?.remove();
        const ov = el('div', { class: 'modal-overlay open signin-helper-overlay', role: 'dialog', 'aria-modal': 'true',
                               'aria-labelledby': 'signin-helper-title' });
        const panel = el('div', { class: 'modal-panel', style: 'max-width:460px' });
        const close = () => { document.removeEventListener('keydown', onKey); clearInterval(ov._t); ov.remove(); };
        const onKey = (e) => { if (e.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
        panel.append(el('h3', { id: 'signin-helper-title', style: 'margin-top:0', text: 'Sign in for a server' }));
        if (!hasDesktop()) {
            panel.append(el('p', { text: 'This works in the PawPoller desktop app. Open PawPoller on your computer and choose "Sign in for a server" there.' }));
            const ok = el('button', { class: 'btn btn-primary', text: 'Close' });
            ok.addEventListener('click', close);
            panel.append(ok);
            ov.append(panel);
            document.body.append(ov);
            return;
        }
        panel.append(el('p', { style: 'font-size:13px;color:var(--text-muted)',
            text: 'Your server shows a code when you press "Sign in with the PawPoller app" on a site. Type its address and the code here. PawPoller opens the site\'s own sign-in page; nothing is kept on this computer afterwards.' }));
        let last = '';
        try { last = localStorage.getItem(LAST_SERVER) || ''; } catch (e) { /* private window */ }
        const server = el('input', { type: 'url', class: 'search-input', placeholder: 'https://your-server.example', autocomplete: 'off',
                                     style: 'width:100%', 'aria-label': 'Server address' });
        server.value = last;
        const code = el('input', { type: 'text', class: 'search-input', placeholder: 'Code (6 characters)', autocomplete: 'off',
                                   maxlength: '8', style: 'width:100%;margin-top:8px;letter-spacing:3px;text-transform:uppercase',
                                   'aria-label': 'Sign-in code' });
        const go = el('button', { class: 'btn btn-primary', style: 'margin-top:12px', text: 'Sign in' });
        const cancel = el('button', { class: 'btn btn-secondary', style: 'margin-top:12px;margin-left:8px', text: 'Close' });
        const out = el('p', { style: 'margin:12px 0 0;font-size:13px', role: 'status', 'aria-live': 'polite' });
        const extra = el('div');
        cancel.addEventListener('click', close);
        panel.append(server, code, go, cancel, out, extra);
        ov.append(panel);
        document.body.append(ov);
        (last ? code : server).focus();

        const run = async () => {
            extra.replaceChildren();
            const addr = server.value.trim(), c = code.value.trim();
            if (!addr || !c) { out.textContent = 'Type the server address and the code.'; return; }
            try { localStorage.setItem(LAST_SERVER, addr); } catch (e) { /* fine */ }
            go.disabled = true;
            out.textContent = 'Checking the code… A window asks you to confirm, then the site\'s sign-in page opens.';
            let r;
            try { r = await window.pywebview.api.helper_signin(addr, c); } catch (e) { r = { ok: false, message: String(e) }; }
            go.disabled = false;
            out.textContent = (r && r.message) || 'Something went wrong. Try again.';
            if (r && r.needs_http_ok) {
                extra.append(el('p', { style: 'font-size:13px',
                    text: 'Plain http means your site sign-in crosses your home network unencrypted. Anyone else on that network could copy it and post as you. Only say yes for a server in your own home, and only use it when you are at home.' }));
                const yes = el('button', { class: 'btn btn-secondary', text: 'Yes, allow plain http for this server' });
                yes.addEventListener('click', async () => {
                    await window.pywebview.api.helper_http_ok(addr, true);
                    run();
                });
                extra.append(yes);
            }
        };
        go.addEventListener('click', run);
        code.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
        // A hand-over waiting for an unreachable server reports here when it lands or runs out.
        ov._t = setInterval(async () => {
            try {
                const s = await window.pywebview.api.helper_status();
                if (s && s.message && s.message !== ov._lastMsg) { ov._lastMsg = s.message; out.textContent = s.message; }
            } catch (e) { /* window closing */ }
        }, 3000);
    }

    window.SigninHelper = { codePanel, openDesktop, hasDesktop };
})();
