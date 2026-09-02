/* PawPoller notification centre — the bell (top-right) + dropdown feed.
 *
 * One self-contained widget (like the toast stack / logs panel): it owns a
 * fixed bell button with an unread badge and a dropdown panel that lists recent
 * system events — poll cycles, posts/uploads, and session-expiry — from
 * /api/notifications. This is the "see everything in one place" layer.
 *
 *   • Polls /api/notifications every 60s → unread badge.
 *   • New FAILURE / WARNING events also pop a toast (errors sticky); successes
 *     stay silent in the list. The backlog present on first load is seeded
 *     silently so a page refresh doesn't replay history as toasts.
 *   • Opening the dropdown marks everything read (clears the badge).
 *
 * Auth: /api/notifications needs a session, so App.init() calls
 * NotificationCenter.start() after the dashboard auth check (same as
 * PlatformHealth). A fallback auto-start covers legacy pages.
 */
(function () {
    const POLL_MS = 60_000;
    const LABELS = (window.PlatformHealth && window.PlatformHealth.LABELS) || {};

    let _items = [];
    let _unread = 0;
    let _open = false;
    let _seeded = false;
    const _seen = new Set();
    let _bell = null, _panel = null, _badge = null, _timer = null;

    const relTime = (iso) => (window.PlatformHealth ? window.PlatformHealth.relativePast(iso) : '');

    function escapeText(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    const key = (it) => `${it.timestamp || ''}|${it.platform || ''}|${it.summary || ''}`;
    const isFailure = (it) => it.status === 'error' || it.status === 'failed' || it.status === 'partial';

    function statusIcon(it) {
        if (isFailure(it)) return '✕';
        if (it.status === 'warn') return '⚠';
        if (it.status === 'running') return '⋯';
        if (it.status === 'success') return '✓';
        return '·';
    }
    function statusClass(it) {
        if (isFailure(it)) return 'error';
        if (it.status === 'warn') return 'warn';
        if (it.status === 'success') return 'success';
        return 'info';
    }

    function ensureEls() {
        if (_bell) return;
        _bell = document.createElement('button');
        _bell.id = 'pp-notif-bell';
        _bell.className = 'pp-notif-bell';
        _bell.setAttribute('aria-label', 'Notifications');
        _bell.innerHTML = '<span class="pp-notif-ico" aria-hidden="true">🔔</span>'
            + '<span class="pp-notif-badge" hidden>0</span>';
        _badge = _bell.querySelector('.pp-notif-badge');

        _panel = document.createElement('div');
        _panel.id = 'pp-notif-panel';
        _panel.className = 'pp-notif-panel';
        _panel.hidden = true;

        document.body.appendChild(_bell);
        document.body.appendChild(_panel);

        _bell.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
        document.addEventListener('click', (e) => {
            if (_open && !_panel.contains(e.target) && e.target !== _bell && !_bell.contains(e.target)) close();
        });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && _open) close(); });
    }

    function renderBadge() {
        if (!_badge) return;
        if (_unread > 0) {
            _badge.textContent = _unread > 99 ? '99+' : String(_unread);
            _badge.hidden = false;
            _bell.classList.add('has-unread');
        } else {
            _badge.hidden = true;
            _bell.classList.remove('has-unread');
        }
    }

    function renderPanel() {
        if (!_panel) return;
        const rows = _items.length ? _items.map((it, i) => {
            const label = LABELS[it.platform] || (it.platform || '').toUpperCase();
            const meta = escapeText([label, relTime(it.timestamp)].filter(Boolean).join(' · '));
            const detail = (it.detail && isFailure(it)) ? '<div class="pp-notif-detail"></div>' : '';
            // Session-health alerts get quick actions: Reconnect (paste fresh
            // creds, for platforms with a reconnect spec) + Mute/Unmute (auto-
            // clears on recovery, server-side — the "I know, stop nagging" one).
            let actions = '';
            if (it.kind === 'session') {
                const canRc = window.Reconnect && Reconnect.canReconnect(it.platform);
                actions = `<div class="pp-notif-actions-col">`
                    + (canRc ? `<button class="pp-notif-reconnect" type="button" data-idx="${i}">Reconnect</button>` : '')
                    + `<button class="pp-notif-mute" type="button" data-idx="${i}">${it.muted ? 'Unmute' : 'Mute'}</button>`
                    + `</div>`;
            }
            return `<div class="pp-notif-item ${it.unread ? 'is-unread' : ''} ${it.muted ? 'is-muted' : ''} pp-notif-${statusClass(it)}">
                <span class="pp-notif-item-ico" aria-hidden="true">${statusIcon(it)}</span>
                <div class="pp-notif-item-body">
                    <div class="pp-notif-summary"></div>
                    <div class="pp-notif-meta">${meta}</div>
                    ${detail}
                </div>
                ${actions}
            </div>`;
        }).join('') : '<div class="pp-notif-empty">No recent activity.</div>';

        const clearBtn = _items.length
            ? `<button class="pp-notif-clear" type="button">Clear all</button>` : '';
        _panel.innerHTML =
            `<div class="pp-notif-head">`
            + `<span class="pp-notif-title">Notifications</span>`
            + `<div class="pp-notif-actions">`
            + `<a class="pp-notif-all" href="#/">Overview →</a>`
            + clearBtn
            + `<button class="pp-notif-close" type="button" aria-label="Close">✕</button>`
            + `</div></div>`
            + `<div class="pp-notif-list">${rows}</div>`;

        // Fill dynamic text via textContent so scraped summaries/details can't inject.
        _panel.querySelectorAll('.pp-notif-item').forEach((el, i) => {
            const it = _items[i];
            el.querySelector('.pp-notif-summary').textContent = it.summary || '(event)';
            const d = el.querySelector('.pp-notif-detail');
            if (d) d.textContent = (it.detail || '').slice(0, 200);
        });
        _panel.querySelector('.pp-notif-close')?.addEventListener('click', () => close());
        _panel.querySelector('.pp-notif-clear')?.addEventListener('click', () => clearAll());
        // Close the dropdown when a link inside it is clicked (SPA navigation).
        _panel.querySelector('.pp-notif-all')?.addEventListener('click', () => close());
        // Mute / unmute a session-health alert (keeps the panel open).
        _panel.querySelectorAll('.pp-notif-mute').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const it = _items[Number(btn.dataset.idx)];
                if (!it || !it.platform) return;
                btn.disabled = true;
                try { await API.muteSessionAlert(it.platform, !it.muted); }
                catch (err) { btn.disabled = false; return; }
                await poll();                 // re-render from server truth (badge + list)
            });
        });
        // Reconnect — open the quick paste-a-token modal for this platform.
        _panel.querySelectorAll('.pp-notif-reconnect').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const it = _items[Number(btn.dataset.idx)];
                if (it && it.platform && window.Reconnect) { close(); Reconnect.open(it.platform); }
            });
        });
    }

    /* Clear the feed: empty the list optimistically, persist a server-side
     * watermark so it survives a refresh, and re-seed the toast dedup so the
     * next poll's (server-filtered) set doesn't replay as toasts. On a network
     * failure the items simply reappear on the next poll — server is truth. */
    async function clearAll() {
        _items = [];
        _unread = 0;
        _seeded = false;
        renderBadge();
        renderPanel();
        try { await API.clearNotifications(); } catch (e) { /* reappears next poll */ }
    }

    /* Pop a toast for each NEW failure/warning. Successes stay silent (they
     * live in the list only). The first poll seeds the seen-set without
     * toasting, so opening/refreshing the dashboard doesn't replay history. */
    function maybeToast(items) {
        if (!_seeded) {
            items.forEach((it) => _seen.add(key(it)));
            _seeded = true;
            return;
        }
        const fresh = items.filter((it) => !_seen.has(key(it)));
        // Oldest → newest so the newest toast ends up on top of the stack.
        fresh.slice().reverse().forEach((it) => {
            _seen.add(key(it));
            if (it.muted) return;                                                      // muted → quiet, no toast
            if (!window.toast) return;
            if (isFailure(it)) window.toast.error(it.summary || 'Something failed');   // sticky
            else if (it.status === 'warn') window.toast.warn(it.summary || 'Warning');
            // success / running / info → silent
        });
        if (_seen.size > 500) {          // keep the dedup set bounded
            _seen.clear();
            items.forEach((it) => _seen.add(key(it)));
        }
    }

    async function poll() {
        try {
            const resp = await API.getNotifications(40);
            _items = (resp && resp.items) || [];
            _unread = (resp && resp.unread) || 0;
            maybeToast(_items);
            renderBadge();
            if (_open) renderPanel();
        } catch (e) { /* retry next tick */ }
    }

    function toggle() { _open ? close() : open(); }

    async function open() {
        ensureEls();
        _open = true;
        _panel.hidden = false;
        _bell.classList.add('is-open');
        renderPanel();
        if (_unread > 0) {
            _unread = 0;                 // optimistic — clear the badge now
            renderBadge();
            try { await API.markNotificationsRead(); } catch (e) { /* ignore */ }
            _items.forEach((it) => { it.unread = false; });
            renderPanel();
        }
    }

    function close() {
        _open = false;
        if (_panel) _panel.hidden = true;
        if (_bell) _bell.classList.remove('is-open');
    }

    function start() {
        ensureEls();
        poll();
        if (!_timer) _timer = setInterval(poll, POLL_MS);
    }
    function stop() { if (_timer) clearInterval(_timer); _timer = null; }

    window.NotificationCenter = { start, stop, poll, open, close };

    if (document.readyState !== 'loading' && document.getElementById('platform-grid')) {
        start();
    }
})();
