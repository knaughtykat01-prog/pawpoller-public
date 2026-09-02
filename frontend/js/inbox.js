/* Inbox (gap G3) — the unified cross-platform comment feed.
 *
 * One place to see every comment on your work across every platform PawPoller
 * captures content for (IB + FA from their legacy tables; Bluesky / Mastodon /
 * e621 / DeviantArt from Stage-A1 capture), newest-first, with:
 *   - ✓ handled toggling (per comment, persisted server-side),
 *   - native Reply where the platform supports it (bsky / mast / e621),
 *   - "Open ↗" permalinks for everything else (reply on-site).
 *
 * Data: GET /api/inbox · POST /api/inbox/handled · POST /api/inbox/reply.
 */
window.Inbox = {
    _showHandled: false,
    _platform: '',

    esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '💬', color: '#888' };
    },

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = '<div class="loading">Loading inbox…</div>';
        let data;
        try {
            const q = new URLSearchParams();
            if (this._platform) q.set('platform', this._platform);
            if (!this._showHandled) q.set('unhandled', 'true');
            const resp = await fetch('/api/inbox?' + q.toString());
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            data = await resp.json();
        } catch (err) {
            app.innerHTML = `<div class="card error">Inbox failed to load: ${this.esc(err.message || err)}</div>`;
            return;
        }
        const items = data.items || [];
        const plats = [...new Set(items.map(i => i.platform))].sort();
        const platOpts = ['<option value="">All platforms</option>']
            .concat(plats.map(p =>
                `<option value="${this.esc(p)}" ${p === this._platform ? 'selected' : ''}>${this.esc(this._plat(p).label)}</option>`))
            .join('');

        app.innerHTML = `
            <div class="page-header">
                <h2>💬 Inbox <span class="muted" style="font-size:.6em">${data.unhandled_count || 0} to answer</span></h2>
                <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
                    <select id="inbox-plat" class="search-input" style="max-width:180px">${platOpts}</select>
                    <label style="font-size:12px;color:var(--text-muted);cursor:pointer">
                        <input type="checkbox" id="inbox-show-handled" ${this._showHandled ? 'checked' : ''}> show handled
                    </label>
                </div>
            </div>
            ${items.length ? '' : `<div class="empty-state"><h3>Inbox zero 🎉</h3>
                <p>No ${this._showHandled ? '' : 'unanswered '}comments${this._platform ? ' on ' + this.esc(this._plat(this._platform).label) : ''}.
                Comments are captured as the pollers run.</p></div>`}
            <div id="inbox-list">${items.map(i => this._card(i)).join('')}</div>`;

        document.getElementById('inbox-plat')?.addEventListener('change', (e) => {
            this._platform = e.target.value; this.render();
        });
        document.getElementById('inbox-show-handled')?.addEventListener('change', (e) => {
            this._showHandled = e.target.checked; this.render();
        });
        this._wire();
    },

    _card(i) {
        const p = this._plat(i.platform);
        const when = i.commented_at || i.first_seen_at || '';
        const key = `${this.esc(i.platform)}|${this.esc(String(i.comment_id))}`;
        return `
            <div class="card inbox-card ${i.handled ? 'inbox-handled' : ''}" data-inbox-key="${key}"
                 style="margin-bottom:10px;${i.handled ? 'opacity:.55' : ''}">
                <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
                    <span title="${this.esc(p.label)}">${p.emoji || '💬'}</span>
                    <strong>${this.esc(i.author || 'someone')}</strong>
                    <span class="muted" style="font-size:12px">on</span>
                    <span style="font-size:13px">${this.esc(i.submission_title || i.submission_id)}</span>
                    <span class="muted" style="font-size:11px;margin-left:auto">${this.esc(when)}</span>
                </div>
                <div style="margin:6px 0 8px;white-space:pre-wrap;font-size:14px">${this.esc(i.body || '')}</div>
                <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
                    ${i.can_reply ? `<button class="btn btn-sm btn-primary" data-inbox-reply>Reply</button>` : ''}
                    ${i.permalink ? `<a class="btn btn-sm btn-outline" href="${this.esc(i.permalink)}" target="_blank" rel="noopener">Open ↗${i.can_reply ? '' : ' (reply on-site)'}</a>` : ''}
                    <button class="btn btn-sm ${i.handled ? 'btn-outline' : ''}" data-inbox-handled="${i.handled ? '0' : '1'}">
                        ${i.handled ? '↩ Unhandle' : '✓ Handled'}</button>
                    <span class="inbox-msg muted" style="font-size:12px"></span>
                </div>
                ${i.can_reply ? `
                <div class="inbox-replybox" style="display:none;margin-top:8px">
                    <textarea class="search-input inbox-reply-text" rows="2" style="width:100%"
                        placeholder="Reply as your ${this.esc(p.label)} account…"></textarea>
                    <div style="display:flex;gap:6px;margin-top:6px">
                        <button class="btn btn-sm btn-primary" data-inbox-send>Send reply</button>
                        <button class="btn btn-sm btn-outline" data-inbox-replycancel>Cancel</button>
                    </div>
                </div>` : ''}
            </div>`;
    },

    _wire() {
        document.querySelectorAll('.inbox-card').forEach(card => {
            const [platform, commentId] = (card.dataset.inboxKey || '').split('|');
            const msg = card.querySelector('.inbox-msg');

            card.querySelector('[data-inbox-handled]')?.addEventListener('click', async (e) => {
                const handled = e.currentTarget.dataset.inboxHandled === '1';
                try {
                    const r = await fetch('/api/inbox/handled', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ platform, comment_id: commentId, handled }),
                    });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    this.render();
                } catch (err) {
                    if (msg) msg.textContent = 'Failed: ' + (err.message || err);
                }
            });

            card.querySelector('[data-inbox-reply]')?.addEventListener('click', () => {
                const box = card.querySelector('.inbox-replybox');
                if (box) { box.style.display = box.style.display === 'none' ? '' : 'none'; box.querySelector('textarea')?.focus(); }
            });
            card.querySelector('[data-inbox-replycancel]')?.addEventListener('click', () => {
                const box = card.querySelector('.inbox-replybox');
                if (box) box.style.display = 'none';
            });
            card.querySelector('[data-inbox-send]')?.addEventListener('click', async (e) => {
                const text = card.querySelector('.inbox-reply-text')?.value.trim();
                if (!text) { if (msg) msg.textContent = 'Write a reply first.'; return; }
                const btn = e.currentTarget;
                btn.disabled = true; btn.textContent = 'Sending…';
                try {
                    const r = await fetch('/api/inbox/reply', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ platform, comment_id: commentId, text }),
                    });
                    const d = await r.json().catch(() => ({}));
                    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
                    if (window.toast) window.toast.success('Reply posted ✓');
                    this.render();   // reply auto-marks handled server-side
                } catch (err) {
                    btn.disabled = false; btn.textContent = 'Send reply';
                    if (msg) msg.textContent = 'Reply failed: ' + (err.message || err);
                }
            });
        });
    },
};
