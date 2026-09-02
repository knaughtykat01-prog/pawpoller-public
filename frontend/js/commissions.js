/* Commissions hub + detail — a lightweight client/commission tracker
 * (gap-wave-5 §4). A board grouped by status, soonest-due first, with an inline
 * status-advance control. Money is data only (no payment integration).
 *
 * CSP-safe: no inline handlers — one delegated click listener keyed on data-*
 * attributes, mirroring Collections. */
window.Commissions = {
    _statuses: ['quote', 'accepted', 'wip', 'paid', 'delivered'],
    _statusLabel: {
        quote: 'Quote', accepted: 'Accepted', wip: 'In progress',
        paid: 'Paid', delivered: 'Delivered',
    },
    _current: null,

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _toast(kind, msg) { if (window.toast && window.toast[kind]) window.toast[kind](msg); },
    _plat(code) { return (window.platformByCode && window.platformByCode(code)) || { code, label: (code || '').toUpperCase(), emoji: '' }; },

    _money(c) {
        const p = Number(c.price || 0);
        if (!p) return '';
        // Keep it simple: symbol-less, currency code appended (e.g. "45 USD").
        return `${p % 1 === 0 ? p : p.toFixed(2)} ${this.esc(c.currency || 'USD')}`;
    },

    /* Due-date badge: overdue (unless delivered/paid) turns it red. */
    _dueBadge(c) {
        if (!c.due_date) return '';
        const overdue = c.status !== 'delivered' && c.status !== 'paid'
            && c.due_date < new Date().toISOString().slice(0, 10);
        return `<span class="comm-due${overdue ? ' comm-due--over' : ''}" title="Due ${this.esc(c.due_date)}">${overdue ? '⚠ ' : ''}due ${this.esc(c.due_date)}</span>`;
    },

    _platBadges(codes) {
        return (codes || []).map(c => {
            const p = this._plat(c);
            return `<span class="comm-plat" title="${this.esc(p.label)}">${p.emoji || this.esc((c || '').toUpperCase())}</span>`;
        }).join('');
    },

    _nextStatus(status) {
        const i = this._statuses.indexOf(status);
        return (i >= 0 && i < this._statuses.length - 1) ? this._statuses[i + 1] : null;
    },

    // ── Hub board ────────────────────────────────────────────────
    async render(archived = false) {
        this._archivedView = archived;
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Commissions${archived ? ' · Archived' : ''}</h1>
                    <p class="muted">Track clients and commissions — price, due date, status, attachments, and where each
                    piece was delivered. Money is just noted here; there's no payment processing.</p>
                </div>
                ${archived ? '' : '<button class="btn btn-primary" data-comm-new>+ New commission</button>'}
            </div>
            <div class="comm-toolbar" id="comm-toolbar"></div>
            <div id="comm-board"><div class="loading-spinner">Loading…</div></div>`;
        let items = [], archivedCount = 0;
        try {
            const d = await API.getCommissions(archived);
            items = d.commissions || [];
            archivedCount = d.archived_count || 0;
            if (Array.isArray(d.statuses) && d.statuses.length) this._statuses = d.statuses;
        } catch (err) {
            document.getElementById('comm-board').innerHTML =
                `<div class="card error">Failed to load commissions: ${this.esc(err.message)}</div>`;
            return;
        }
        // Toolbar — the archived toggle (only when there's an archive to see).
        const tb = document.getElementById('comm-toolbar');
        if (archived) {
            tb.innerHTML = `<a href="#/commissions" class="btn btn-sm">&larr; Back to active board</a>`;
        } else if (archivedCount > 0) {
            tb.innerHTML = `<a href="#/commissions/archived" class="btn btn-sm btn-outline">&#128230; Archived (${archivedCount})</a>`;
        }
        const board = document.getElementById('comm-board');
        if (!items.length) {
            board.className = '';
            board.innerHTML = archived
                ? `<div class="empty-state"><h3>Nothing archived</h3>
                       <p class="muted">Commissions you archive from the board collect here.</p></div>`
                : `<div class="empty-state">
                       <h3>No commissions yet</h3>
                       <p class="muted">Add a commission to track a client, a price, a due date, and where you delivered it.</p>
                       <button class="btn btn-primary" data-comm-new>+ New commission</button>
                   </div>`;
            return;
        }
        if (archived) {
            // Flat card grid — status columns don't matter once it's off the board.
            board.className = 'comm-cards-grid';
            board.innerHTML = items.map(c => this._card(c)).join('');
            return;
        }
        // Active: group by status; only render columns that have cards.
        const byStatus = {};
        items.forEach(c => { (byStatus[c.status] || (byStatus[c.status] = [])).push(c); });
        board.className = 'comm-board';
        board.innerHTML = this._statuses
            .filter(s => (byStatus[s] || []).length)
            .map(s => `
                <section class="comm-col">
                    <h2 class="comm-col-head">${this.esc(this._statusLabel[s] || s)}
                        <span class="comm-col-count">${byStatus[s].length}</span></h2>
                    <div class="comm-col-body">${byStatus[s].map(c => this._card(c)).join('')}</div>
                </section>`).join('');
    },

    _card(c) {
        const money = this._money(c);
        const next = this._nextStatus(c.status);
        const advance = (!c.archived && next)
            ? `<button class="btn btn-sm comm-advance" data-comm-advance="${c.id}" data-next="${next}"
                    title="Advance to ${this.esc(this._statusLabel[next] || next)}">→ ${this.esc(this._statusLabel[next] || next)}</button>`
            : '';
        const archiveBtn = c.archived
            ? `<button class="btn btn-sm" data-comm-unarchive="${c.id}" title="Move back to the active board">Unarchive</button>`
            : `<button class="btn btn-sm btn-outline" data-comm-archive="${c.id}" title="Move to the archive">&#128230; Archive</button>`;
        return `
            <div class="comm-card" data-comm-open="${c.id}" role="button" tabindex="0">
                <div class="comm-card-top">
                    <span class="comm-client">${this.esc(c.client_name || 'Unnamed client')}</span>
                    ${money ? `<span class="comm-price">${money}</span>` : ''}
                </div>
                ${c.description ? `<p class="comm-desc">${this.esc(c.description.slice(0, 140))}${c.description.length > 140 ? '…' : ''}</p>` : ''}
                <div class="comm-meta">
                    ${this._dueBadge(c)}
                    ${this._platBadges(c.deliver_sites)}
                </div>
                <div class="comm-card-actions">${advance}${archiveBtn}</div>
            </div>`;
    },

    // ── Detail ───────────────────────────────────────────────────
    async renderDetail(id) {
        const app = document.getElementById('app');
        app.innerHTML = `<div class="work-back"><a href="#/commissions">&larr; Commissions</a></div>
            <div id="comm-detail"><div class="loading-spinner">Loading…</div></div>`;
        let c;
        try {
            c = await API.getCommission(id);
        } catch (err) {
            document.getElementById('comm-detail').innerHTML =
                `<div class="card error">Couldn't open this commission: ${this.esc(err.message)}</div>`;
            return;
        }
        this._current = c;
        const money = this._money(c);
        const artLink = c.artwork_name
            ? `<a href="#/artwork/image/${encodeURIComponent(c.artwork_name)}">${this.esc(c.artwork_name)}</a>`
            : '<span class="muted">none linked</span>';
        const sites = (c.deliver_sites || []).length
            ? this._platBadges(c.deliver_sites) : '<span class="muted">none</span>';
        const archiveBtn = c.archived
            ? `<button class="btn btn-sm" data-comm-unarchive="${c.id}">Unarchive</button>`
            : `<button class="btn btn-sm btn-outline" data-comm-archive="${c.id}">&#128230; Archive</button>`;
        const eyebrow = (c.archived ? '📦 Archived · ' : '') + this.esc(this._statusLabel[c.status] || c.status);
        document.getElementById('comm-detail').innerHTML = `
            <article class="comm-detail-card">
                <div class="comm-detail-head">
                    <div>
                        <div class="shelf-eyebrow">${eyebrow}</div>
                        <h1 class="comm-detail-title">${this.esc(c.client_name || 'Unnamed client')}</h1>
                    </div>
                    <div class="comm-detail-actions">
                        ${archiveBtn}
                        <button class="btn btn-sm" data-comm-edit>Edit</button>
                        <button class="btn btn-sm btn-danger" data-comm-delete>Delete</button>
                    </div>
                </div>
                <dl class="comm-fields">
                    <div><dt>Price</dt><dd>${money || '<span class="muted">—</span>'}</dd></div>
                    <div><dt>Status</dt><dd>${this._statusControl(c)}</dd></div>
                    <div><dt>Due date</dt><dd>${c.due_date ? this.esc(c.due_date) : '<span class="muted">—</span>'}</dd></div>
                    <div><dt>Delivered to</dt><dd>${sites}</dd></div>
                    <div><dt>Linked artwork</dt><dd>${artLink}</dd></div>
                </dl>
                ${c.description ? `<h3 class="comm-h3">Description</h3><p class="comm-body">${this.esc(c.description)}</p>` : ''}
                ${c.notes ? `<h3 class="comm-h3">Notes</h3><p class="comm-body">${this.esc(c.notes)}</p>` : ''}

                <section class="comm-attach">
                    <h3 class="comm-h3">Attachments</h3>
                    <div class="comm-dropzone" id="comm-dropzone">
                        <input type="file" id="comm-file-input" multiple hidden>
                        <p class="comm-dropzone-main">Drag files here, or
                            <button class="btn btn-sm" id="comm-file-browse" type="button">choose files</button></p>
                        <p class="muted comm-dropzone-hint">Any file up to 25&nbsp;MB — reference sheets, WIPs, screenshots, contracts…</p>
                    </div>
                    <div class="comm-files" id="comm-files"><div class="muted">Loading attachments…</div></div>
                </section>
            </article>`;
        this._wireAttachments(c.id);
        this._loadFiles(c.id);
    },

    // ── Attachments (2.188) ──────────────────────────────────────
    _wireAttachments(cid) {
        const dz = document.getElementById('comm-dropzone');
        const input = document.getElementById('comm-file-input');
        document.getElementById('comm-file-browse')?.addEventListener('click', () => input && input.click());
        input?.addEventListener('change', () => { this._uploadFiles(cid, input.files); input.value = ''; });
        if (!dz) return;
        ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, (e) => {
            e.preventDefault(); dz.classList.add('is-drag');
        }));
        ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, (e) => {
            e.preventDefault(); dz.classList.remove('is-drag');
        }));
        dz.addEventListener('drop', (e) => {
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                this._uploadFiles(cid, e.dataTransfer.files);
            }
        });
    },

    async _uploadFiles(cid, fileList) {
        const files = [...(fileList || [])];
        if (!files.length) return;
        let ok = 0;
        for (const f of files) {
            try { await API.uploadCommissionFile(cid, f); ok++; }
            catch (err) { this._toast('error', `${f.name}: ${(err.message || err)}`); }
        }
        if (ok) this._toast('success', `Uploaded ${ok} file${ok === 1 ? '' : 's'}`);
        this._loadFiles(cid);
    },

    async _loadFiles(cid) {
        const box = document.getElementById('comm-files');
        if (!box) return;
        let files = [];
        try { files = (await API.getCommissionFiles(cid)).files || []; }
        catch (err) { box.innerHTML = `<div class="muted">Couldn't load attachments.</div>`; return; }
        if (!files.length) { box.innerHTML = `<div class="muted comm-files-empty">No attachments yet.</div>`; return; }
        box.innerHTML = files.map(f => this._fileTile(f)).join('');
        box.querySelectorAll('[data-comm-file-delete]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.preventDefault(); e.stopPropagation();
                const fn = btn.dataset.commFileDelete;
                if (!confirm(`Delete "${fn}"?`)) return;
                try { await API.deleteCommissionFile(cid, fn); this._loadFiles(cid); }
                catch (err) { this._toast('error', 'Delete failed'); }
            });
        });
    },

    _fileSize(n) {
        if (n < 1024) return `${n} B`;
        if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
        return `${(n / 1048576).toFixed(1)} MB`;
    },

    _fileTile(f) {
        const size = this._fileSize(f.size || 0);
        const del = `<button class="comm-file-del" data-comm-file-delete="${this.esc(f.filename)}" title="Delete" aria-label="Delete">&times;</button>`;
        if (f.is_image) {
            return `<figure class="comm-file comm-file--img">
                <a href="${this.esc(f.url)}" target="_blank" rel="noopener">
                    <img src="${this.esc(f.url)}" alt="${this.esc(f.filename)}" loading="lazy"></a>
                ${del}
                <figcaption title="${this.esc(f.filename)}">${this.esc(f.filename)} <span class="comm-file-size">${size}</span></figcaption>
            </figure>`;
        }
        return `<div class="comm-file comm-file--doc">
            <a class="comm-file-doc-link" href="${this.esc(f.url)}" download>
                <span class="comm-file-ico" aria-hidden="true">&#128196;</span>
                <span class="comm-file-name" title="${this.esc(f.filename)}">${this.esc(f.filename)}</span>
                <span class="comm-file-size">${size}</span></a>
            ${del}
        </div>`;
    },

    /* Inline status <select> on the detail page — changes save immediately. */
    _statusControl(c) {
        return `<select class="comm-status-select" data-comm-status="${c.id}">
            ${this._statuses.map(s =>
                `<option value="${s}"${s === c.status ? ' selected' : ''}>${this.esc(this._statusLabel[s] || s)}</option>`).join('')}
        </select>`;
    },

    // ── Wiring ───────────────────────────────────────────────────
    _init() {
        if (this._wired) return;
        this._wired = true;
        document.addEventListener('click', (e) => {
            const nw = e.target.closest('[data-comm-new]');
            if (nw) { e.preventDefault(); this._newModal(); return; }
            const adv = e.target.closest('[data-comm-advance]');
            if (adv) { e.preventDefault(); e.stopPropagation(); this._advance(Number(adv.dataset.commAdvance), adv.dataset.next); return; }
            const arch = e.target.closest('[data-comm-archive]');
            if (arch) { e.preventDefault(); e.stopPropagation(); this._setArchived(Number(arch.dataset.commArchive), true); return; }
            const unarch = e.target.closest('[data-comm-unarchive]');
            if (unarch) { e.preventDefault(); e.stopPropagation(); this._setArchived(Number(unarch.dataset.commUnarchive), false); return; }
            const ed = e.target.closest('[data-comm-edit]');
            if (ed) { e.preventDefault(); this._editModal(); return; }
            const del = e.target.closest('[data-comm-delete]');
            if (del) { e.preventDefault(); this._delete(); return; }
            const open = e.target.closest('[data-comm-open]');
            if (open) { e.preventDefault(); location.hash = `#/commissions/${open.dataset.commOpen}`; return; }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const open = e.target.closest && e.target.closest('[data-comm-open]');
            if (open) { e.preventDefault(); location.hash = `#/commissions/${open.dataset.commOpen}`; }
        });
        document.addEventListener('change', (e) => {
            const sel = e.target.closest('[data-comm-status]');
            if (sel) this._setStatus(Number(sel.dataset.commStatus), sel.value);
        });
    },

    async _advance(id, next) {
        try {
            await API.updateCommission(id, { status: next });
            this._toast('success', `Moved to ${this._statusLabel[next] || next}`);
            this.render();
        } catch (err) { this._toast('error', 'Update failed: ' + (err.message || err)); }
    },

    async _setStatus(id, status) {
        try {
            await API.updateCommission(id, { status });
            this._toast('success', 'Status updated');
            if (this._current && this._current.id === id) this._current.status = status;
        } catch (err) { this._toast('error', 'Update failed: ' + (err.message || err)); }
    },

    /* Archive / unarchive, then re-render whichever view we're on. */
    async _setArchived(id, archived) {
        try {
            await API.updateCommission(id, { archived: archived ? 1 : 0 });
            this._toast('success', archived ? 'Archived' : 'Unarchived');
            const hash = location.hash || '';
            if (/#\/commissions\/\d+$/.test(hash)) this.renderDetail(id);
            else if (hash.indexOf('/commissions/archived') !== -1) this.render(true);
            else this.render(false);
        } catch (err) { this._toast('error', 'Update failed: ' + (err.message || err)); }
    },

    async _delete() {
        const c = this._current;
        if (!c || !confirm(`Delete the commission for "${c.client_name}"? This can't be undone.`)) return;
        try {
            await API.deleteCommission(c.id);
            this._toast('success', 'Commission deleted');
            location.hash = '#/commissions';
        } catch (err) { this._toast('error', 'Delete failed: ' + (err.message || err)); }
    },

    _newModal() {
        this._modal('New commission', {
            client_name: '', description: '', price: '', currency: 'USD',
            status: 'quote', due_date: '', artwork_name: '', deliver_sites: [], notes: '',
        }, async (vals) => {
            const r = await API.createCommission(vals);
            this._toast('success', 'Commission created');
            location.hash = `#/commissions/${r.id}`;
        });
    },

    _editModal() {
        const c = this._current || {};
        this._modal('Edit commission', {
            client_name: c.client_name || '', description: c.description || '',
            price: c.price || '', currency: c.currency || 'USD', status: c.status || 'quote',
            due_date: c.due_date || '', artwork_name: c.artwork_name || '',
            deliver_sites: c.deliver_sites || [], notes: c.notes || '',
        }, async (vals) => {
            await API.updateCommission(c.id, vals);
            this._toast('success', 'Saved');
            this.renderDetail(c.id);
        });
    },

    /* All poster codes that can appear as a delivery target. Kept in sync with
     * posting/artwork_reader._ALL_POSTER_IDS. */
    _deliverCodes: ['ib', 'fa', 'ws', 'sf', 'da', 'ik', 'bsky', 'e621', 'ig'],

    /* Self-contained modal (CSP-safe: built + wired in JS, no inline handlers). */
    _modal(title, vals, onSave) {
        const wrap = document.createElement('div');
        wrap.className = 'guide-modal';
        const siteBoxes = this._deliverCodes.map(code => {
            const p = this._plat(code);
            const on = (vals.deliver_sites || []).includes(code);
            return `<label class="comm-site-box"><input type="checkbox" value="${code}"${on ? ' checked' : ''}>
                <span>${p.emoji || ''} ${this.esc(p.label)}</span></label>`;
        }).join('');
        const statusOpts = this._statuses.map(s =>
            `<option value="${s}"${s === vals.status ? ' selected' : ''}>${this.esc(this._statusLabel[s] || s)}</option>`).join('');
        wrap.innerHTML = `
            <div class="guide-modal-card comm-modal-card" role="dialog" aria-modal="true">
                <div class="guide-modal-head">
                    <h3 class="guide-modal-title">${this.esc(title)}</h3>
                    <button class="guide-modal-close" type="button" aria-label="Close">&times;</button></div>
                <div class="guide-modal-body">
                    <label class="comm-field"><span>Client name</span>
                        <input class="comm-input" id="comm-f-client" type="text" value="${this.esc(vals.client_name)}" placeholder="e.g. @someone"></label>
                    <label class="comm-field"><span>Description</span>
                        <textarea class="comm-input" id="comm-f-desc" rows="2" placeholder="What's the piece?">${this.esc(vals.description)}</textarea></label>
                    <div class="comm-field-row">
                        <label class="comm-field comm-field--sm"><span>Price</span>
                            <input class="comm-input" id="comm-f-price" type="number" min="0" step="0.01" value="${this.esc(String(vals.price))}"></label>
                        <label class="comm-field comm-field--sm"><span>Currency</span>
                            <input class="comm-input" id="comm-f-currency" type="text" maxlength="5" value="${this.esc(vals.currency)}"></label>
                        <label class="comm-field comm-field--sm"><span>Status</span>
                            <select class="comm-input" id="comm-f-status">${statusOpts}</select></label>
                    </div>
                    <div class="comm-field-row">
                        <label class="comm-field comm-field--sm"><span>Due date</span>
                            <input class="comm-input" id="comm-f-due" type="date" value="${this.esc(vals.due_date)}"></label>
                        <label class="comm-field"><span>Linked artwork (name, optional)</span>
                            <input class="comm-input" id="comm-f-art" type="text" value="${this.esc(vals.artwork_name)}" placeholder="artwork folder name"></label>
                    </div>
                    <div class="comm-field"><span>Delivered to</span>
                        <div class="comm-sites" id="comm-f-sites">${siteBoxes}</div></div>
                    <label class="comm-field"><span>Notes (optional)</span>
                        <textarea class="comm-input" id="comm-f-notes" rows="2">${this.esc(vals.notes)}</textarea></label>
                    <div class="comm-msg" id="comm-f-msg"></div>
                    <div class="comm-actions">
                        <button class="btn btn-primary" id="comm-f-save">Save</button>
                        <button class="btn" id="comm-f-cancel">Cancel</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(wrap);
        const close = () => wrap.remove();
        wrap.addEventListener('click', (e) => { if (e.target === wrap) close(); });
        wrap.querySelector('.guide-modal-close').addEventListener('click', close);
        wrap.querySelector('#comm-f-cancel').addEventListener('click', close);
        wrap.querySelector('#comm-f-save').addEventListener('click', async () => {
            const client = wrap.querySelector('#comm-f-client').value.trim();
            const msg = wrap.querySelector('#comm-f-msg');
            if (!client) { msg.textContent = 'Client name is required.'; return; }
            const sites = [...wrap.querySelectorAll('#comm-f-sites input:checked')].map(i => i.value);
            const payload = {
                client_name: client,
                description: wrap.querySelector('#comm-f-desc').value,
                price: parseFloat(wrap.querySelector('#comm-f-price').value) || 0,
                currency: wrap.querySelector('#comm-f-currency').value.trim() || 'USD',
                status: wrap.querySelector('#comm-f-status').value,
                due_date: wrap.querySelector('#comm-f-due').value,
                artwork_name: wrap.querySelector('#comm-f-art').value.trim(),
                deliver_sites: sites,
                notes: wrap.querySelector('#comm-f-notes').value,
            };
            const btn = wrap.querySelector('#comm-f-save');
            btn.disabled = true; btn.textContent = 'Saving…';
            try { await onSave(payload); close(); }
            catch (err) { msg.textContent = (err.message || err); btn.disabled = false; btn.textContent = 'Save'; }
        });
    },
};
Commissions._init();
