/* People — the registry as a page (3.11.0 as Artists; People since 4.6.0).
 *
 * 4.6.0: the registry holds everyone, not only artists — commissioners, the
 * owners of characters, collaborators — with two things per row that the
 * artist-only page had no room for: a persona link ("this person is me", so a
 * self-drawn piece posts no credit line and carries your booru tag) and a
 * per-handle MENTION switch. An `@` on X / Bluesky / Instagram / Telegram /
 * Itaku is a mention that notifies; a `:iconname:` on FA, `[name]` on Inkbunny
 * or `<!~login>` on Weasyl is a profile link (4.6.1 wording). Off by default,
 * per site, because names are free and links are consent. See
 * docs/specs/people_registry.md.
 *
 * Until now the registry was only reachable THROUGH a piece: open a work, open
 * the artist picker, edit from there. That is the right place to answer "who
 * drew this", but it is the wrong place to answer "is this artist's DeviantArt
 * link still correct" or "which of them have no handles yet" — the questions
 * that come up when you are maintaining the credits rather than applying one.
 *
 * So: one row per artist, their handles per platform, their warnings, and how
 * many pieces credit them. Everything editable in place.
 *
 * Renaming is the reason this page needed real care. `masterpiece.json` stores
 * the artist's name INLINE, so the registry and the works have to move together
 * or every piece keeps crediting the old spelling. The rename therefore previews
 * first and lists exactly which pieces it will rewrite, because rewriting
 * artwork metadata is never done blind.
 */
window.Artists = {
    _all: [],
    _q: '',
    _filter: 'all',

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },

    PLATFORMS: [
        ['fa', 'FurAffinity'], ['e621', 'e621'], ['da', 'DeviantArt'], ['tw', 'X / Twitter'],
        ['bsky', 'Bluesky'], ['ib', 'Inkbunny'], ['ws', 'Weasyl'], ['sf', 'SoFurry'],
        ['fn', 'FurryNetwork'], ['ik', 'Itaku'], ['ig', 'Instagram'], ['tg', 'Telegram'],
    ],

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>People</h1>
                    <p class="muted">Artists, commissioners, character owners, collaborators — and you. Handles here
                    are what gets rendered on every platform — fix one and every piece naming them is fixed.
                    <em>mention</em> switches whether a handle is <strong>linked</strong> on that site when they are
                    named on a piece — a real @-mention that notifies them where the site has one (X, Bluesky,
                    Instagram, Telegram, Itaku), a profile link elsewhere (FA, Inkbunny, Weasyl, e621, DeviantArt).
                    Off, the post carries just their name. The artist's own credit is always linked.</p>
                </div>
                <button class="btn btn-primary" data-ar-new type="button">+ Add person</button>
            </div>
            <div class="ar-bar">
                <input type="search" id="ar-search" class="ar-search"
                       placeholder="Search by name, alias or handle…" autocomplete="off">
                <div class="ar-chips" id="ar-chips"></div>
            </div>
            <div id="ar-list"><div class="loading-spinner">Loading…</div></div>`;
        this._wire();
        await this._load();
    },

    async _load() {
        try {
            // with_counts reads every folder in the archive — worth it here (the
            // "how many pieces" column is half the point of the page) and
            // deliberately off for the picker, which opens on a keystroke.
            const d = await API.listArtists('', true);
            this._all = d.artists || [];
            this._personas = d.personas || [];
        } catch (err) {
            document.getElementById('ar-list').innerHTML =
                `<div class="empty-state">Could not load the registry: ${this.esc(err.message || err)}</div>`;
            return;
        }
        this._draw();
    },

    _visible() {
        const q = this._q.toLowerCase();
        return this._all.filter(a => {
            if (this._filter === 'flagged' && !(a.warnings || []).length) return false;
            if (this._filter === 'nohandles' && Object.keys(a.handles || {}).length) return false;
            if (this._filter === 'unused' && (a.works || 0) > 0) return false;
            if (this._filter === 'me' && a.persona_id == null) return false;
            if (!q) return true;
            return a.name.toLowerCase().includes(q)
                || (a.aliases || []).some(x => String(x).toLowerCase().includes(q))
                || Object.values(a.handles || {}).some(h => String(h).toLowerCase().includes(q));
        });
    },

    _draw() {
        const chips = [
            ['all', 'All'], ['me', 'You'], ['flagged', '⚠ Warnings'],
            ['nohandles', 'No handles'], ['unused', 'No pieces'],
        ];
        const q = this._q.toLowerCase();
        const hit = (a) => !q || a.name.toLowerCase().includes(q)
            || (a.aliases || []).some(x => String(x).toLowerCase().includes(q))
            || Object.values(a.handles || {}).some(h => String(h).toLowerCase().includes(q));
        const counts = { all: 0, me: 0, flagged: 0, nohandles: 0, unused: 0 };
        for (const a of this._all) {
            if (!hit(a)) continue;
            counts.all++;
            if (a.persona_id != null) counts.me++;
            if ((a.warnings || []).length) counts.flagged++;
            if (!Object.keys(a.handles || {}).length) counts.nohandles++;
            if (!(a.works || 0)) counts.unused++;
        }
        document.getElementById('ar-chips').innerHTML = chips.map(([k, label]) =>
            `<button type="button" class="ar-chip${k === this._filter ? ' is-active' : ''}"
                     data-ar-filter="${k}">${label} <span class="ar-chip-n">${counts[k]}</span></button>`).join('');

        const rows = this._visible().map(a => this._row(a)).join('');
        document.getElementById('ar-list').innerHTML = rows
            || '<div class="empty-state">No artists match.</div>';
    },

    _personaName(id) {
        const p = (this._personas || []).find(x => String(x.persona_id) === String(id));
        return p ? p.name : '';
    },

    _row(a) {
        const handles = a.handles || {};
        const mention = a.mention || {};
        const n = Object.keys(handles).length;
        const pname = a.persona_id != null ? this._personaName(a.persona_id) : '';
        const you = a.persona_id != null
            ? `<span class="ar-badge ar-you" title="One of your personas — a piece they drew posts no credit line when that persona posts it">you${pname ? ' · ' + this.esc(pname) : ''}</span>` : '';
        const pOpts = `<option value=""${a.persona_id == null ? ' selected' : ''}>Not me</option>` +
            (this._personas || []).map(p =>
                `<option value="${this.esc(p.persona_id)}"${String(p.persona_id) === String(a.persona_id) ? ' selected' : ''}>me · ${this.esc(p.name)}</option>`).join('');
        const warn = (a.warnings || []).length
            ? `<div class="ar-warn">${a.warnings.map(w => `<div>⚠ ${this.esc(w)}</div>`).join('')}</div>` : '';
        const ctx = (a.context || []).length
            ? `<div class="ar-ctx">${a.context.map(w => `<div>${this.esc(w)}</div>`).join('')}</div>` : '';
        const aliases = (a.aliases || []).length
            ? `<span class="ar-alias">also: ${a.aliases.map(x => this.esc(x)).join(', ')}</span>` : '';
        const fields = this.PLATFORMS.map(([code, label]) => {
            const v = handles[code] || '';
            const rm = v
                ? `<button type="button" class="ar-h-rm" data-ar-rm="${a.key}|${code}"
                           title="Forget this handle for ${this.esc(a.name)}">&times;</button>` : '';
            // Mention (4.6.0): may this handle be linked on this site when they
            // are named on a piece? A link notifies — off until they say yes.
            const men = v
                ? `<label class="ar-mention" title="Link them on this site when named on a piece — an @-mention that notifies them where the site has one, a profile link elsewhere">
                        <input type="checkbox" data-ar-mention="${a.key}|${code}"${mention[code] ? ' checked' : ''}> mention</label>` : '';
            return `<div class="ar-h">
                        <span>${label}${rm}</span>
                        <input type="text" data-ar-handle="${a.key}|${code}"
                               value="${this.esc(v)}" placeholder="—" autocomplete="off" spellcheck="false"
                               aria-label="${this.esc(a.name)} on ${label}">
                        ${men}
                    </div>`;
        }).join('');
        return `
            <div class="ar-card" data-ar-key="${this.esc(a.key)}">
                <div class="ar-head">
                    <div class="ar-id">
                        <span class="ar-name">${this.esc(a.name)}</span>
                        ${aliases}
                    </div>
                    <div class="ar-meta">
                        ${you}
                        <span class="ar-badge">${n} handle${n === 1 ? '' : 's'}</span>
                        <span class="ar-badge${(a.works || 0) ? '' : ' ar-badge-zero'}">${a.works || 0} piece${(a.works || 0) === 1 ? '' : 's'}</span>
                    </div>
                    <div class="ar-acts">
                        <select class="ar-persona" data-ar-persona="${this.esc(a.key)}" title="Is this person one of your personas?" aria-label="Persona">${pOpts}</select>
                        <button class="btn btn-sm" data-ar-rename="${this.esc(a.key)}" type="button">Rename</button>
                        <button class="btn btn-sm btn-primary" data-ar-save="${this.esc(a.key)}" type="button">Save</button>
                    </div>
                </div>
                ${warn}${ctx}
                <div class="ar-handles">${fields}</div>
                <div class="ar-msg" data-ar-msg="${this.esc(a.key)}"></div>
            </div>`;
    },

    _wire() {
        const search = document.getElementById('ar-search');
        let t = null;
        search?.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => { this._q = search.value.trim(); this._draw(); }, 180);
        });
        if (this._wired) return;
        this._wired = true;
        document.addEventListener('click', (e) => {
            const chip = e.target.closest('[data-ar-filter]');
            if (chip) { this._filter = chip.dataset.arFilter; this._draw(); return; }
            const save = e.target.closest('[data-ar-save]');
            if (save) { e.preventDefault(); this._saveHandles(save.dataset.arSave); return; }
            const rm = e.target.closest('[data-ar-rm]');
            if (rm) { e.preventDefault(); this._removeHandle(rm.dataset.arRm); return; }
            const ren = e.target.closest('[data-ar-rename]');
            if (ren) { e.preventDefault(); this._rename(ren.dataset.arRename); return; }
            const add = e.target.closest('[data-ar-new]');
            if (add) { e.preventDefault(); this._addArtist(); return; }
        });
    },

    _msg(key, text, bad) {
        const el = document.querySelector(`[data-ar-msg="${CSS.escape(key)}"]`);
        if (el) { el.textContent = text || ''; el.className = 'ar-msg' + (bad ? ' ar-msg-bad' : ''); }
    },

    async _saveHandles(key) {
        const a = this._all.find(x => x.key === key);
        if (!a) return;
        const handles = {};
        document.querySelectorAll('[data-ar-handle]').forEach(el => {
            const [k, code] = el.dataset.arHandle.split('|');
            if (k !== key) return;
            const v = (el.value || '').trim();
            if (v) handles[code] = v;
        });
        // Mention per handle and the persona link travel with the save (4.6.0).
        const mention = {};
        document.querySelectorAll('[data-ar-mention]').forEach(el => {
            const [k, code] = el.dataset.arMention.split('|');
            if (k === key) mention[code] = !!el.checked;
        });
        const psel = document.querySelector(`[data-ar-persona="${CSS.escape(key)}"]`);
        this._msg(key, 'Saving…');
        try {
            // Upserts merge, so this adds and corrects. Emptying a box does NOT
            // remove — that is what the x is for, and why it is a separate call.
            const body = { name: a.name, handles, mention };
            if (psel) body.persona_id = psel.value ? Number(psel.value) : null;
            const updated = await API.saveArtist(body);
            a.handles = updated.handles || {};
            a.mention = updated.mention || {};
            a.persona_id = updated.persona_id == null ? null : updated.persona_id;
            // Redraw THIS card only (the badge, the ticked boxes): a full
            // _draw() would discard edits in progress on every other card.
            const card = document.querySelector(`.ar-card[data-ar-key="${CSS.escape(key)}"]`);
            if (card) card.outerHTML = this._row(a);
            this._msg(key, 'Saved');
            setTimeout(() => this._msg(key, ''), 2500);
        } catch (err) {
            this._msg(key, 'Save failed: ' + (err.message || err), true);
        }
    },

    async _removeHandle(ref) {
        const [key, platform] = ref.split('|');
        const a = this._all.find(x => x.key === key);
        if (!a) return;
        try {
            const updated = await API.deleteArtistHandle(key, platform);
            a.handles = updated.handles || {};
            this._draw();
        } catch (err) {
            this._msg(key, 'Could not remove: ' + (err.message || err), true);
        }
    },

    async _addArtist() {
        const name = prompt('Name');
        if (!name || !name.trim()) return;
        try {
            await API.saveArtist({ name: name.trim() });
            await this._load();
        } catch (err) {
            alert('Could not add: ' + (err.message || err));
        }
    },

    /* Rename is two operations that must not drift apart: the registry row, and
       the name written inline on every masterpiece.json crediting them. The
       preview exists so the second one is never a surprise. */
    async _rename(key) {
        const a = this._all.find(x => x.key === key);
        if (!a) return;
        const next = prompt(`Rename “${a.name}” to:`, a.name);
        if (!next || !next.trim() || next.trim() === a.name) return;

        let preview;
        try {
            preview = await API.renameArtist(key, next.trim(), false);
        } catch (err) {
            alert('Could not check the rename: ' + (err.message || err));
            return;
        }
        if (preview.conflict) {
            alert(`An artist called “${next.trim()}” already exists.\n\n` +
                  `Renaming onto them would have to merge two sets of handles and ` +
                  `flags — move the handles across deliberately instead.`);
            return;
        }
        const works = preview.works || [];
        const listed = works.slice(0, 12).map(w => '  • ' + w).join('\n');
        const more = works.length > 12 ? `\n  …and ${works.length - 12} more` : '';
        const ok = confirm(
            `Rename “${preview.from}” to “${preview.to}”.\n\n` +
            (works.length
                ? `This also rewrites the credit on ${works.length} piece${works.length === 1 ? '' : 's'}:\n${listed}${more}\n\n`
                : 'No pieces currently credit them, so only the registry changes.\n\n') +
            `“${preview.from}” is kept as an alias so searches still find them.`);
        if (!ok) return;

        try {
            const r = await API.renameArtist(key, next.trim(), true);
            await this._load();
            const failed = (r.works_failed || []).length;
            if (failed) {
                alert(`Renamed, but ${failed} piece${failed === 1 ? '' : 's'} could not be updated:\n` +
                      (r.works_failed || []).join(', '));
            }
        } catch (err) {
            alert('Rename failed: ' + (err.message || err));
        }
    },
};
