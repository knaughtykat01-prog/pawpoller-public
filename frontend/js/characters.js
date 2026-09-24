/* Characters — the registry as a page (4.33.0, backlog CHARREG).
 *
 * People answers "who drew this"; this answers "who is in it". Same page, different
 * noun — deliberately the People page's markup and classes (`.ar-*`), because two
 * registries that look alike are two registries somebody already knows how to use.
 *
 * Four things per row that the picker has no room for: the owner (a People row, so a
 * character whose owner has handles inherits them), the booru tag that travels to e621
 * and Furbooru, the species, and free notes.
 *
 * Renaming gets the same care it gets on People and for the same reason:
 * `masterpiece.json` writes the character's NAME inline so the archive reads without a
 * database, which makes a rename two jobs — the registry, then every piece naming
 * them. It previews first and lists exactly which pieces it would rewrite.
 */
window.Characters = {
    _all: [],
    _people: [],
    _q: '',
    _filter: 'all',

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Characters</h1>
                    <p class="muted">Everyone who appears <em>in</em> your art — yours and other people's.
                    Naming a character here once means every piece spells them the same way, and the
                    <strong>booru tag</strong> you give them is added automatically when a piece featuring
                    them posts to e621 or Furbooru. The <strong>owner</strong> is a row in
                    <a href="#/artists">People</a>, so crediting whoever the character belongs to uses the
                    handles already saved there.</p>
                </div>
                <button class="btn btn-primary" data-ch-new type="button">+ Add character</button>
            </div>
            <div class="ar-bar">
                <input type="search" id="ch-search" class="ar-search"
                       placeholder="Search by name, tag or species…" autocomplete="off">
                <div class="ar-chips" id="ch-chips"></div>
            </div>
            <div id="ch-list"><div class="loading-spinner">Loading…</div></div>`;
        this._wire();
        await this._load();
    },

    async _load() {
        try {
            // with_counts reads every folder — worth it here (the "how many pieces"
            // column is half the point of the page) and off for the picker.
            const [d, people] = await Promise.all([
                API.listCharacters('', true),
                API.listArtists('', false).catch(() => ({ artists: [] })),
            ]);
            this._all = d.characters || [];
            this._people = people.artists || [];
        } catch (err) {
            document.getElementById('ch-list').innerHTML =
                `<div class="empty-state">Could not load the registry: ${this.esc(err.message || err)}</div>`;
            return;
        }
        this._draw();
    },

    _hit(c) {
        const q = this._q.toLowerCase();
        if (!q) return true;
        return c.name.toLowerCase().includes(q)
            || (c.booru_tag || '').toLowerCase().includes(q)
            || (c.species || '').toLowerCase().includes(q)
            || (c.owner && c.owner.name.toLowerCase().includes(q))
            || (c.aliases || []).some(x => String(x).toLowerCase().includes(q));
    },

    _draw() {
        const chips = [
            ['all', 'All'], ['owned', 'Has an owner'], ['notag', 'No booru tag'],
            ['unused', 'No pieces'],
        ];
        const counts = { all: 0, owned: 0, notag: 0, unused: 0 };
        for (const c of this._all) {
            if (!this._hit(c)) continue;
            counts.all++;
            if (c.owner) counts.owned++;
            if (!c.booru_tag) counts.notag++;
            if (!(c.works || 0)) counts.unused++;
        }
        document.getElementById('ch-chips').innerHTML = chips.map(([k, label]) =>
            `<button type="button" class="ar-chip${k === this._filter ? ' is-active' : ''}"
                     data-ch-filter="${k}">${label} <span class="ar-chip-n">${counts[k]}</span></button>`).join('');

        const rows = this._all.filter(c => {
            if (!this._hit(c)) return false;
            if (this._filter === 'owned') return !!c.owner;
            if (this._filter === 'notag') return !c.booru_tag;
            if (this._filter === 'unused') return !(c.works || 0);
            return true;
        }).map(c => this._row(c)).join('');
        document.getElementById('ch-list').innerHTML = rows
            || '<div class="empty-state">No characters match.</div>';
    },

    _row(c) {
        const k = this.esc(c.key);
        const owner = c.owner
            ? `<span class="ar-badge">${this.esc(c.owner.name)}’s</span>` : '';
        const works = c.works || 0;
        const aliases = (c.aliases || []).length
            ? `<span class="ar-alias">also: ${c.aliases.map(x => this.esc(x)).join(', ')}</span>` : '';
        // "You" first and labelled (4.34.1). The list is People rows by name, and
        // nothing said which one is YOU — so the commonest answer, "this is my
        // character", meant knowing which of your own names you filed yourself under.
        // A People row is you when it carries a persona link ("this person is me").
        const _mine = this._people.filter(p => p.persona_id != null);
        const _others = this._people.filter(p => p.persona_id == null);
        const _opt = (p, mine) =>
            `<option value="${this.esc(p.key)}"${p.key === c.owner_key ? ' selected' : ''}>`
            + `${mine ? 'you · ' : ''}${this.esc(p.name)}</option>`;
        const ownerOpts = `<option value=""${c.owner_key ? '' : ' selected'}>Owner unknown</option>`
            + _mine.map(p => _opt(p, true)).join('')
            + _others.map(p => _opt(p, false)).join('');
        const field = (label, prop, placeholder, hint) => `
            <div class="ar-h">
                <span${hint ? ` title="${this.esc(hint)}"` : ''}>${label}</span>
                <input type="text" data-ch-field="${k}|${prop}" value="${this.esc(c[prop] || '')}"
                       placeholder="${this.esc(placeholder)}" autocomplete="off" spellcheck="false"
                       aria-label="${this.esc(c.name)} — ${label}">
            </div>`;
        return `
            <div class="ar-card" data-ch-key="${k}">
                <div class="ar-head">
                    <div class="ar-id">
                        <span class="ar-name">${this.esc(c.name)}</span>
                        ${aliases}
                    </div>
                    <div class="ar-meta">
                        ${owner}
                        ${c.species ? `<span class="ar-badge">${this.esc(c.species)}</span>` : ''}
                        <span class="ar-badge${works ? '' : ' ar-badge-zero'}">${works} piece${works === 1 ? '' : 's'}</span>
                    </div>
                    <div class="ar-acts">
                        <select class="ar-persona" data-ch-owner="${k}" title="Who does this character belong to? Rows come from People." aria-label="Owner">${ownerOpts}</select>
                        <button class="btn btn-sm" data-ch-rename="${k}" type="button">Rename</button>
                        <button class="btn btn-sm btn-primary" data-ch-save="${k}" type="button">Save</button>
                        <button class="btn btn-sm ar-del" data-ch-del="${k}" type="button"
                            title="Remove this character from the registry. Pieces keep the name — only the owner link and the booru tag go.">Delete</button>
                    </div>
                </div>
                <div class="ar-handles">
                    ${field('Booru tag', 'booru_tag', 'name_(owner)',
                            'The tag this character already uses on e621 / Furbooru — lowercase, underscores for spaces, the owner in brackets. Added automatically when a piece featuring them posts there. Leave it empty and nothing is added.')}
                    ${field('Species', 'species', '—', '')}
                    ${field('Notes', 'notes', '—', 'Yours to read — never posted anywhere.')}
                </div>
                <div class="ar-msg" data-ch-msg="${k}"></div>
            </div>`;
    },

    _wire() {
        const search = document.getElementById('ch-search');
        let t = null;
        search?.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => { this._q = search.value.trim(); this._draw(); }, 180);
        });
        if (this._wired) return;
        this._wired = true;
        document.addEventListener('click', (e) => {
            const chip = e.target.closest('[data-ch-filter]');
            if (chip) { this._filter = chip.dataset.chFilter; this._draw(); return; }
            const save = e.target.closest('[data-ch-save]');
            if (save) { e.preventDefault(); this._save(save.dataset.chSave); return; }
            const ren = e.target.closest('[data-ch-rename]');
            if (ren) { e.preventDefault(); this._rename(ren.dataset.chRename); return; }
            const del = e.target.closest('[data-ch-del]');
            if (del) { e.preventDefault(); this._delete(del.dataset.chDel); return; }
            const add = e.target.closest('[data-ch-new]');
            if (add) { e.preventDefault(); this._add(); return; }
        });
    },

    _msg(key, text, bad) {
        const el = document.querySelector(`[data-ch-msg="${CSS.escape(key)}"]`);
        if (el) { el.textContent = text || ''; el.className = 'ar-msg' + (bad ? ' ar-msg-bad' : ''); }
    },

    async _save(key) {
        const c = this._all.find(x => x.key === key);
        if (!c) return;
        const body = { name: c.name };
        document.querySelectorAll('[data-ch-field]').forEach(el => {
            const [k, prop] = el.dataset.chField.split('|');
            if (k === key) body[prop] = (el.value || '').trim();
        });
        const osel = document.querySelector(`[data-ch-owner="${CSS.escape(key)}"]`);
        if (osel) body.owner_key = osel.value;
        this._msg(key, 'Saving…');
        try {
            const updated = await API.saveCharacter(body);
            // The row comes back without its resolved owner (the upsert has no reason to
            // read People), so fill it from the list already in hand.
            const owner = this._people.find(p => p.key === updated.character.owner_key);
            Object.assign(c, updated.character, {
                works: c.works,
                owner: owner ? { key: owner.key, name: owner.name } : null,
            });
            // Redraw THIS card only — a full _draw() would discard edits in progress
            // on every other card.
            const card = document.querySelector(`.ar-card[data-ch-key="${CSS.escape(key)}"]`);
            if (card) card.outerHTML = this._row(c);
            this._msg(key, 'Saved');
            setTimeout(() => this._msg(key, ''), 2500);
        } catch (err) {
            this._msg(key, 'Save failed: ' + (err.message || err), true);
        }
    },

    async _add() {
        const name = prompt('Character name');
        if (!name || !name.trim()) return;
        try {
            await API.saveCharacter({ name: name.trim() });
            await this._load();
        } catch (err) {
            alert('Could not add them: ' + (err.message || err));
        }
    },

    /* Two operations that must not drift apart: the registry row, and the name written
       inline on every masterpiece.json featuring them. The preview exists so the second
       one is never a surprise. */
    async _rename(key) {
        const c = this._all.find(x => x.key === key);
        if (!c) return;
        const next = prompt(`Rename “${c.name}” to:`, c.name);
        if (!next || !next.trim() || next.trim() === c.name) return;

        let preview;
        try {
            preview = await API.renameCharacter(key, next.trim(), false);
        } catch (err) {
            alert('Could not check the rename: ' + (err.message || err));
            return;
        }
        if (preview.conflict) {
            alert(`A character called “${next.trim()}” already exists.\n\n` +
                  `Renaming onto them would have to merge two owners and two tags — ` +
                  `move the details across deliberately instead.`);
            return;
        }
        const works = preview.works || [];
        const listed = works.slice(0, 12).map(w => '  • ' + w).join('\n');
        const more = works.length > 12 ? `\n  …and ${works.length - 12} more` : '';
        const ok = confirm(
            `Rename “${preview.from}” to “${preview.to}”.\n\n` +
            (works.length
                ? `This also rewrites the name on ${works.length} piece${works.length === 1 ? '' : 's'}:\n${listed}${more}\n\n`
                : 'No pieces currently feature them, so only the registry changes.\n\n') +
            `“${preview.from}” is kept as an alias so searches still find them.`);
        if (!ok) return;

        try {
            const r = await API.renameCharacter(key, next.trim(), true);
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

    /* Nothing in the archive is touched: the name is written inline on each
       masterpiece.json, so those pieces keep it and simply stop resolving it to an
       owner or a tag afterwards. The card already carries the piece count, so ask with
       it in hand rather than firing a request to be refused. */
    async _delete(key) {
        const c = this._all.find(x => x.key === key);
        if (!c) return;
        const n = c.works || 0;
        const ok = confirm(
            `Remove “${c.name}” from Characters?\n\n`
            + (n
                ? `${n} piece${n === 1 ? '' : 's'} feature them. Those pieces keep the name — only the `
                  + 'owner link and the booru tag go, so posts stop tagging them automatically.\n\n'
                : 'No pieces feature them, so only this registry entry goes.\n\n')
            + 'Nothing is removed from your archive, and adding them again under the same name '
            + 'brings the details back.');
        if (!ok) return;
        try {
            await API.deleteCharacter(key, true);
            await this._load();
        } catch (err) {
            alert('Could not remove them: ' + (err.message || err));
        }
    },
};
