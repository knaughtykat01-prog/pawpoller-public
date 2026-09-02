/* Collections hub + detail — a curated master container for one piece across
 * every platform it lives on (gallery works + microblog posts + optional story),
 * with pooled analytics / merged tags / all locations. See docs/specs/collections.md.
 *
 * CSP-safe: no inline handlers — a delegated click listener keyed on data-*
 * attributes, mirroring the rest of the app. */
window.Collections = {
    _personas: {},   // id -> {name, color}

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _fmt(n) {
        n = Number(n || 0);
        if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
        return String(n);
    },
    _toast(kind, msg) { if (window.toast && window.toast[kind]) window.toast[kind](msg); },
    _plat(code) { return (window.platformByCode && window.platformByCode(code)) || { code, label: (code || '').toUpperCase(), emoji: '' }; },

    async _loadPersonas() {
        try {
            const d = await API.getPersonas();
            this._personas = {};
            (d.personas || []).forEach(p => { this._personas[p.persona_id] = { name: p.name, color: p.color || 'var(--accent)' }; });
        } catch (e) { /* personas are optional decoration */ }
    },

    _personaChips(ids) {
        return (ids || []).map(id => {
            const p = this._personas[id];
            if (!p) return '';
            return `<span class="coll-persona" title="${this.esc(p.name)}"><span class="coll-persona-dot" style="background:${this.esc(p.color)}"></span>${this.esc(p.name)}</span>`;
        }).join('');
    },

    _platBadges(codes) {
        return (codes || []).map(c => {
            const p = this._plat(c);
            return `<span class="coll-plat" title="${this.esc(p.label)}">${p.emoji || this.esc((c || '').toUpperCase())}</span>`;
        }).join('');
    },

    // ── Hub grid ────────────────────────────────────────────────
    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Collections</h1>
                    <p class="muted">A master folder per piece — every place it's posted (gallery + microblog),
                    pooled analytics, all tags and links, plus an optional companion story.</p>
                </div>
                <button class="btn btn-primary" data-coll-new>+ New collection</button>
            </div>
            <div id="coll-suggest"></div>
            <div id="coll-grid"><div class="loading-spinner">Loading…</div></div>`;
        this._renderSuggestions();
        await this._loadPersonas();
        let items = [];
        try {
            const d = await API.getCollections();
            items = d.collections || [];
        } catch (err) {
            document.getElementById('coll-grid').innerHTML =
                `<div class="card error">Failed to load collections: ${this.esc(err.message)}</div>`;
            return;
        }
        const grid = document.getElementById('coll-grid');
        if (!items.length) {
            grid.innerHTML = `
                <div class="empty-state">
                    <h3>No collections yet</h3>
                    <p class="muted">Group a piece's posts across every platform into one master — with pooled stats,
                    all its links and tags, and a companion story if it has one.</p>
                    <button class="btn btn-primary" data-coll-new>+ New collection</button>
                </div>`;
            return;
        }
        grid.className = 'coll-grid';
        grid.innerHTML = items.map(c => this._card(c)).join('');
    },

    /* FA / Inkbunny / Pixiv thumbnails need the backend relay (CORS + mixed
     * content); everything else loads directly. Mirrors artwork.js._thumbSrc. */
    _thumbSrc(platform, url) {
        if (!url) return '';
        if (platform === 'fa') return Utils.faThumbUrl(url);
        if (platform === 'ib') return Utils.thumbUrl(url);
        if (platform === 'pix') return Utils.pixThumbUrl(url);
        return url;
    },

    _card(c) {
        const t = c.totals || {};
        const cover = (c.cover_kind === 'url' && c.cover_ref)
            ? `<img class="coll-cover-img" src="${this.esc(c.cover_ref)}" alt="" loading="lazy">`
            : (c.cover_thumb
                ? `<img class="coll-cover-img" src="${this.esc(this._thumbSrc(c.cover_platform, c.cover_thumb))}" alt="" loading="lazy">`
                : `<div class="coll-cover-ph">🗂️</div>`);
        return `
            <a class="coll-card" href="#/collections/${c.id}">
                <div class="coll-cover">${cover}</div>
                <div class="coll-body">
                    <div class="coll-name">${this.esc(c.name)}</div>
                    <div class="coll-meta">${this._platBadges(c.platforms)} <span class="muted">· ${t.locations || 0} location${(t.locations === 1) ? '' : 's'}</span></div>
                    <div class="coll-stats muted">👁 ${this._fmt(t.views)} · ❤ ${this._fmt(t.favorites)} · 💬 ${this._fmt(t.comments)}</div>
                    <div class="coll-personas">${this._personaChips(c.persona_ids)}</div>
                </div>
            </a>`;
    },

    /* Suggested collections — un-grouped cross-platform lookalikes (title
     * similarity today; + perceptual-hash image similarity in Phase 4). Folded
     * in from the retired Cross-Platform Links screen. Non-fatal. */
    async _renderSuggestions() {
        const host = document.getElementById('coll-suggest');
        if (!host) return;
        let sugg = [];
        try { sugg = (await API.getCollectionSuggestions()).suggestions || []; }
        catch (e) { return; }
        this._suggestions = sugg;

        const reasonChip = (r) => {
            if (r === 'image') return '<span class="coll-reason coll-reason--image">🖼 pixel match</span>';
            if (r === 'both') return '<span class="coll-reason coll-reason--both">✓ title + pixels</span>';
            return '<span class="coll-reason">📝 title match</span>';
        };
        const rows = sugg.slice(0, 12).map((s, i) => {
            const subs = s.submissions || [];
            const chips = subs.map(m => {
                const p = this._plat(m.platform);
                return `<span class="coll-plat" title="${this.esc(p.label)}">${p.emoji || this.esc((m.platform || '').toUpperCase())}</span>`;
            }).join(' ');
            const title = this.esc((subs.find(m => m.title) || subs[0] || {}).title || 'Untitled');
            return `<div class="coll-suggest-row">
                <div class="coll-suggest-info">
                    <div class="coll-suggest-title">${title}</div>
                    <div class="coll-suggest-meta">${chips} ${reasonChip(s.reason)} <span class="muted">· ${Math.round((s.similarity || 0) * 100)}%</span></div>
                </div>
                <button class="btn btn-sm btn-primary" data-coll-suggest="${i}">Make collection</button>
            </div>`;
        }).join('');

        // Always show the card so the pixel-scan control stays discoverable, even
        // with zero current suggestions.
        const body = rows || '<p class="muted" style="margin:.2rem 0;">No lookalikes found yet. <strong>Scan images</strong> compares your art and thumbnails by pixels (no AI) to find the same piece across platforms.</p>';
        host.innerHTML = `<div class="card coll-suggest-card" style="margin-bottom:1rem;">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap;">
                <h3 style="margin:.1rem 0 .2rem;">Suggested collections</h3>
                <button class="btn btn-sm" data-coll-scan title="Hash local art + thumbnails and compare by pixels — no AI, runs locally">🔍 Scan images</button>
            </div>
            <p class="muted" style="margin:0 0 .6rem;">The same piece across platforms, not yet grouped — matched by title and by pixels. One click merges them into a new collection.</p>
            ${body}
        </div>`;
    },

    /* Run the native pixel-hash scan, then refresh suggestions. */
    async _scanImages() {
        const btn = document.querySelector('[data-coll-scan]');
        if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
        try {
            const r = await API.scanImageHashes();
            const a = (r.local_artwork || {}).hashed || 0;
            const t = (r.thumbnails || {}).hashed || 0;
            this._toast('success', `Hashed ${a + t} image${a + t === 1 ? '' : 's'} (${a} art, ${t} thumbs)`);
        } catch (err) {
            this._toast('error', err.message || err);
        }
        await this._renderSuggestions();
    },

    /* Create a new collection from a suggestion's submission set and open it. */
    async _createFromSuggestion(idx) {
        const s = (this._suggestions || [])[idx];
        if (!s) return;
        const subs = s.submissions || [];
        if (!subs.length) return;
        const name = (subs[0] && subs[0].title) || 'New collection';
        try {
            const members = subs.map(m => ({ member_type: 'submission', member_ref: `${m.platform}:${m.submission_id}` }));
            const r = await API.createCollection({ name, members });
            this._toast('success', 'Collection created');
            location.hash = `#/collections/${r.id}`;
        } catch (err) {
            this._toast('error', err.message || err);
        }
    },

    // ── Detail ──────────────────────────────────────────────────
    async renderDetail(id) {
        const app = document.getElementById('app');
        app.innerHTML = `
            <p style="margin:.2rem 0 .8rem;"><a href="#/collections">← Collections</a></p>
            <div id="coll-detail">Loading…</div>`;
        await this._loadPersonas();
        let c;
        try {
            c = await API.getCollection(id);
        } catch (err) {
            document.getElementById('coll-detail').innerHTML =
                `<div class="card error">${err.status === 404 ? 'Collection not found.' : 'Failed to load: ' + this.esc(err.message)} <a href="#/collections">Back</a></div>`;
            return;
        }
        this._current = c;
        const t = c.totals || {};
        const locs = c.locations || [];
        const locRows = locs.map(l => {
            const p = this._plat(l.platform);
            const s = l.stats || {};
            const thumb = l.thumbnail_url
                ? `<img class="coll-loc-thumb" src="${this.esc(this._thumbSrc(l.platform, l.thumbnail_url))}" alt="" loading="lazy">`
                : '<span class="coll-loc-thumb coll-loc-thumb--none"></span>';
            return `<tr>
                <td>${thumb}</td>
                <td><strong>${p.emoji || ''} ${this.esc(p.label)}</strong></td>
                <td class="muted">${this.esc(l.title || '')}</td>
                <td class="muted">${s.views == null ? '—' : this._fmt(s.views)}</td>
                <td class="muted">${s.favorites == null ? '—' : this._fmt(s.favorites)}</td>
                <td class="muted">${s.comments == null ? '—' : this._fmt(s.comments)}</td>
                <td style="text-align:right;">${l.url ? `<a class="btn btn-sm" href="${this.esc(Utils.safeUrl(l.url) || '#')}" target="_blank" rel="noopener">View ↗</a>` : ''}</td>
            </tr>`;
        }).join('');

        const memberRows = (c.members || []).map(m => `
            <tr>
                <td><span class="coll-mtype">${this.esc(m.member_type)}</span></td>
                <td class="muted">${this.esc(m.member_ref)}</td>
                <td class="muted">${this.esc(m.role || '')}</td>
                <td style="text-align:right;">
                    <button class="btn btn-sm btn-danger" data-coll-remove data-mt="${this.esc(m.member_type)}" data-mr="${this.esc(m.member_ref)}">Remove</button>
                </td>
            </tr>`).join('');

        const tags = (c.tags || []).map(tag => `<span class="coll-tag">${this.esc(tag)}</span>`).join('');

        document.getElementById('coll-detail').innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1 style="margin-bottom:.2rem;">${this.esc(c.name)}</h1>
                    <div class="coll-personas">${this._personaChips(c.persona_ids)}</div>
                    ${c.notes ? `<p class="muted" style="margin-top:.5rem;">${this.esc(c.notes)}</p>` : ''}
                </div>
                <div style="display:flex;gap:.5rem;flex-shrink:0;">
                    <button class="btn" data-coll-edit>Edit</button>
                    <button class="btn btn-danger" data-coll-delete>Delete</button>
                </div>
            </div>
            <div class="stats-grid" style="margin:1rem 0;">
                ${Components.statCard('Views', t.views || 0)}
                ${Components.statCard('Favourites', t.favorites || 0)}
                ${Components.statCard('Comments', t.comments || 0)}
                ${Components.statCard('Platforms', t.platforms || 0)}
            </div>
            <div class="card" id="coll-chart-card" style="margin:1rem 0;display:none;">
                <h3>Combined growth</h3>
                <p class="muted" style="margin:.1rem 0 .6rem;">Summed views/faves/comments across every location over time.</p>
                <div class="chart-wrap"><canvas id="coll-combined-chart"></canvas></div>
            </div>
            ${c.story ? `<div class="card" style="margin-bottom:1rem;"><h3>Companion story</h3>
                <p><a href="#/posting/story/${encodeURIComponent(c.story.name)}">${this.esc(c.story.name.replace(/_/g, ' '))}</a></p></div>` : ''}
            <div class="card" style="margin-bottom:1rem;">
                <h3>Locations</h3>
                ${locRows ? `<table class="data-table"><thead><tr><th></th><th>Platform</th><th>Title</th><th>Views</th><th>Faves</th><th>Comments</th><th></th></tr></thead><tbody>${locRows}</tbody></table>`
                          : '<p class="muted">No resolvable locations yet — add works or submissions below.</p>'}
            </div>
            ${tags ? `<div class="card" style="margin-bottom:1rem;"><h3>Tags</h3><div class="coll-tags">${tags}</div></div>` : ''}
            <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:1rem;">
                    <h3 style="margin:0;">Members</h3>
                    <button class="btn btn-sm btn-primary" data-coll-addmember>＋ Add member</button>
                </div>
                ${memberRows ? `<table class="data-table" style="margin-top:.6rem;"><tbody>${memberRows}</tbody></table>`
                             : '<p class="muted" style="margin-top:.6rem;">No members yet — add works or submissions with <strong>＋ Add member</strong>, or use "Add to Collection" from the Submissions hub.</p>'}
            </div>`;

        // Combined cross-platform growth chart — folded in from the retired
        // Cross-Platform Links screen (2.113.0). Only shown when there's a real
        // time-series (2+ points); a nicety, so failures are silent.
        try {
            const snap = await API.getCollectionSnapshots(id);
            const rows = (snap && snap.snapshots) || [];
            if (rows.length > 1 && window.Charts) {
                const card = document.getElementById('coll-chart-card');
                if (card) {
                    card.style.display = '';
                    Charts.aggregateLine('coll-combined-chart', rows, ['views', 'favorites_count', 'comments_count']);
                }
            }
        } catch (e) { /* chart is optional */ }
    },

    // ── Actions (delegated) ─────────────────────────────────────
    _init() {
        if (this._wired) return;
        this._wired = true;
        document.addEventListener('click', (e) => {
            const nw = e.target.closest('[data-coll-new]');
            if (nw) { e.preventDefault(); this._newModal(); return; }
            const rm = e.target.closest('[data-coll-remove]');
            if (rm) { e.preventDefault(); this._removeMember(rm.dataset.mt, rm.dataset.mr); return; }
            const ed = e.target.closest('[data-coll-edit]');
            if (ed) { e.preventDefault(); this._editModal(); return; }
            const del = e.target.closest('[data-coll-delete]');
            if (del) { e.preventDefault(); this._delete(); return; }
            const am = e.target.closest('[data-coll-addmember]');
            if (am) { e.preventDefault(); this._addMemberBrowser(); return; }
            const sg = e.target.closest('[data-coll-suggest]');
            if (sg) { e.preventDefault(); this._createFromSuggestion(parseInt(sg.dataset.collSuggest, 10)); return; }
            const sc = e.target.closest('[data-coll-scan]');
            if (sc) { e.preventDefault(); this._scanImages(); return; }
            // "Add to Collection" from a piece elsewhere in the app.
            const add = e.target.closest('[data-add-collection]');
            if (add) {
                e.preventDefault(); e.stopPropagation();
                this.pickAndAdd(add.dataset.mtype, add.dataset.mref, add.dataset.label || '');
                return;
            }
        });
        // Keyboard support for the role="button" add-to-collection spans.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const add = e.target.closest && e.target.closest('[data-add-collection]');
            if (add) {
                e.preventDefault(); e.stopPropagation();
                this.pickAndAdd(add.dataset.mtype, add.dataset.mref, add.dataset.label || '');
            }
        });
    },

    /* Pick a collection (existing or new) and add one piece to it. Reusable from
     * anywhere via a <button data-add-collection data-mtype data-mref data-label>. */
    async pickAndAdd(memberType, memberRef, label) {
        if (!memberType || !memberRef) return;
        let cols = [];
        try { cols = (await API.getCollections()).collections || []; } catch (e) { /* empty */ }
        const rows = cols.map(c =>
            `<button class="btn coll-pick-row" data-pick="${c.id}">${this.esc(c.name)}
                <span class="muted">· ${(c.totals && c.totals.locations) || 0} loc</span></button>`).join('');
        const wrap = this._shell('Add to collection',
            `<p class="muted" style="margin:.1rem 0 .8rem;">${this.esc(label || memberRef)}</p>
             <div class="coll-pick-list">${rows || '<p class="muted">No collections yet.</p>'}</div>
             <div class="coll-actions" style="margin-top:.9rem;">
                 <button class="btn btn-primary" id="coll-pick-new">＋ New collection</button>
                 <button class="btn" id="coll-pick-cancel">Cancel</button>
             </div>
             <div class="coll-msg" id="coll-pick-msg"></div>`);
        const close = () => wrap.remove();
        wrap.querySelector('#coll-pick-cancel').addEventListener('click', close);
        const doAdd = async (cid) => {
            try {
                await API.addCollectionMember(cid, { member_type: memberType, member_ref: memberRef });
                this._toast('success', 'Added to collection');
                close();
            } catch (err) {
                wrap.querySelector('#coll-pick-msg').textContent = err.message || err;
            }
        };
        wrap.querySelectorAll('[data-pick]').forEach(b =>
            b.addEventListener('click', () => doAdd(Number(b.dataset.pick))));
        wrap.querySelector('#coll-pick-new').addEventListener('click', async () => {
            const name = prompt('New collection name:');
            if (!name || !name.trim()) return;
            try {
                const r = await API.createCollection({ name: name.trim() });
                await doAdd(r.id);
            } catch (err) { wrap.querySelector('#coll-pick-msg').textContent = err.message || err; }
        });
    },

    /* Browse your works + discovered submissions and add them to THIS collection. */
    async _addMemberBrowser() {
        const c = this._current;
        if (!c) return;
        // Visual picker (work_picker.js) — searchable thumbnail grid that scales
        // to 1000s of works. Falls back to nothing if the component is unavailable.
        if (!window.WorkPicker) { this._toast('error', 'Picker unavailable'); return; }
        let added = 0;
        WorkPicker.open({
            title: `Add to “${c.name}”`,
            confirmLabel: 'Add selected',
            onConfirm: async (items) => {
                for (const it of items) {
                    try {
                        await API.addCollectionMember(c.id, { member_type: it.member_type, member_ref: it.member_ref });
                        added++;
                    } catch (err) { this._toast('error', `${it.title}: ${err.message || err}`); }
                }
                if (added) this._toast('success', `Added ${added} item${added === 1 ? '' : 's'}`);
                this.renderDetail(c.id);
            },
        });
    },

    /* Bare modal shell (reuses guide-modal); returns the wrapper element. */
    _shell(title, bodyHtml) {
        const wrap = document.createElement('div');
        wrap.className = 'guide-modal';
        wrap.innerHTML = `
            <div class="guide-modal-card" role="dialog" aria-modal="true">
                <div class="guide-modal-head">
                    <h3 class="guide-modal-title">${this.esc(title)}</h3>
                    <button class="guide-modal-close" type="button" aria-label="Close">&times;</button></div>
                <div class="guide-modal-body">${bodyHtml}</div>
            </div>`;
        document.body.appendChild(wrap);
        wrap.addEventListener('click', (e) => { if (e.target === wrap) wrap.remove(); });
        wrap.querySelector('.guide-modal-close').addEventListener('click', () => wrap.remove());
        return wrap;
    },

    _newModal() {
        this._modal('New collection', { name: '', notes: '' }, async (vals) => {
            const r = await API.createCollection({ name: vals.name, notes: vals.notes });
            this._toast('success', 'Collection created');
            location.hash = `#/collections/${r.id}`;
        });
    },

    _editModal() {
        const c = this._current || {};
        this._modal('Edit collection', { name: c.name || '', notes: c.notes || '' }, async (vals) => {
            await API.updateCollection(c.id, { name: vals.name, notes: vals.notes });
            this._toast('success', 'Saved');
            this.renderDetail(c.id);
        });
    },

    async _delete() {
        const c = this._current;
        if (!c || !confirm(`Delete collection "${c.name}"? Its members are just unlinked — the underlying works/posts are not deleted.`)) return;
        try {
            await API.deleteCollection(c.id);
            this._toast('success', 'Collection deleted');
            location.hash = '#/collections';
        } catch (err) { this._toast('error', 'Delete failed: ' + (err.message || err)); }
    },

    async _removeMember(mt, mr) {
        const c = this._current;
        if (!c) return;
        try {
            await API.removeCollectionMember(c.id, mt, mr);
            this.renderDetail(c.id);
        } catch (err) { this._toast('error', 'Remove failed: ' + (err.message || err)); }
    },

    /* Small self-contained modal (CSP-safe: built + wired in JS, no inline handlers). */
    _modal(title, vals, onSave) {
        const wrap = document.createElement('div');
        wrap.className = 'guide-modal';
        wrap.innerHTML = `
            <div class="guide-modal-card" role="dialog" aria-modal="true">
                <div class="guide-modal-head">
                    <h3 class="guide-modal-title">${this.esc(title)}</h3>
                    <button class="guide-modal-close" type="button" aria-label="Close">&times;</button></div>
                <div class="guide-modal-body">
                    <label class="coll-field"><span>Name</span>
                        <input class="coll-input" id="coll-f-name" type="text" value="${this.esc(vals.name)}" placeholder="e.g. Best Friends"></label>
                    <label class="coll-field"><span>Notes (optional)</span>
                        <textarea class="coll-input" id="coll-f-notes" rows="3">${this.esc(vals.notes)}</textarea></label>
                    <div class="coll-msg" id="coll-f-msg"></div>
                    <div class="coll-actions">
                        <button class="btn btn-primary" id="coll-f-save">Save</button>
                        <button class="btn" id="coll-f-cancel">Cancel</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(wrap);
        const close = () => wrap.remove();
        wrap.addEventListener('click', (e) => { if (e.target === wrap) close(); });  // backdrop = the overlay itself
        wrap.querySelector('.guide-modal-close').addEventListener('click', close);
        wrap.querySelector('#coll-f-cancel').addEventListener('click', close);
        wrap.querySelector('#coll-f-save').addEventListener('click', async () => {
            const name = wrap.querySelector('#coll-f-name').value.trim();
            const notes = wrap.querySelector('#coll-f-notes').value;
            const msg = wrap.querySelector('#coll-f-msg');
            if (!name) { msg.textContent = 'Name is required.'; return; }
            const btn = wrap.querySelector('#coll-f-save');
            btn.disabled = true; btn.textContent = 'Saving…';
            try { await onSave({ name, notes }); close(); }
            catch (err) { msg.textContent = (err.message || err); btn.disabled = false; btn.textContent = 'Save'; }
        });
    },
};
Collections._init();
