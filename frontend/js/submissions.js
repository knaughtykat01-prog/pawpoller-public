/* ── Submissions hub (unified works library) ─────────────────────
 *
 * The central place to see every WORK — stories + artwork — grouped per work,
 * with All / Stories / Artwork subtabs, a persona filter, search, and sort.
 * Cards link to the existing per-work detail (story detail / artwork detail).
 * Read-only aggregation served by /api/works. Phase 1 of the Submissions hub
 * spec (docs/specs/submissions-hub.md). Dispatched from the router on
 * #/submissions.
 *
 * Filtering is client-side over a single fetched list for snappy controls; the
 * /api/works endpoint also accepts the same params for direct/API use.
 */
window.Submissions = {
    _works: [],
    _personas: [],
    _type: 'all',     // all | story | artwork
    _persona: 0,      // 0 = all personas
    _search: '',
    _sort: 'recent',  // recent | title | platforms

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '', color: '#888' };
    },

    _inputStyle:
        'padding:.45rem .65rem;border-radius:8px;border:1px solid var(--card-border-inner);' +
        'background:var(--bg-elev,transparent);color:inherit;font:inherit;',

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
    },

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Submissions</h1>
                    <p class="muted">Everything you've made — stories and artwork — in one place.
                    Filter by type or persona, then open any work for its full per-platform detail.</p>
                </div>
                <a class="btn" id="subs-disc-link" href="#/submissions/discovered" style="flex-shrink:0;">Discovered &rarr;</a>
            </div>
            <div id="subs-suggest"></div>
            <div id="subs-controls"></div>
            <div id="subs-grid"><div class="loading-spinner">Loading…</div></div>`;

        let data;
        try {
            data = await API.getWorks();
        } catch (err) {
            document.getElementById('subs-grid').innerHTML =
                `<div class="card error">Failed to load submissions: ${this.esc(err.message)}</div>`;
            return;
        }
        this._works = (data && data.works) || [];
        this._personas = (data && data.personas) || [];
        // Discovered (polled-but-unmanaged) posts — used for the "import art"
        // suggestion + the Discovered link count. Best-effort; never blocks.
        try {
            const disc = await API.getDiscovered();
            this._discovered = (disc && disc.discovered) || [];
        } catch { this._discovered = []; }
        this._discoveredArt = this._discovered.filter(d => d.kind === 'art' && d.thumbnail_url);
        const dl = document.getElementById('subs-disc-link');
        if (dl && this._discovered.length) dl.textContent = `Discovered (${this._discovered.length}) →`;
        this._renderSuggest();
        this._renderControls();
        this._paint();
    },

    /* Suggestion banner: offer a one-click import of all discovered art so
       polled pieces become managed works (and show up in this grid). */
    _renderSuggest() {
        const el = document.getElementById('subs-suggest');
        if (!el) return;
        const n = (this._discoveredArt || []).length;
        if (!n) { el.innerHTML = ''; return; }
        const one = n === 1;
        el.innerHTML = `
            <div class="subs-suggest-banner">
                <div><strong>${n} discovered art piece${one ? '' : 's'}</strong> from your polling
                ${one ? "isn't" : "aren't"} in your library yet — import ${one ? 'it' : 'them'}
                to manage ${one ? 'it' : 'them'} here.</div>
                <div style="display:flex;gap:.5rem;flex-shrink:0;">
                    <button class="btn btn-primary" id="subs-import-art">Import all art</button>
                    <a class="btn" href="#/submissions/discovered">Review &rarr;</a>
                </div>
            </div>`;
        const b = document.getElementById('subs-import-art');
        if (b) b.addEventListener('click', () => this._importAllArt());
    },

    async _importAllArt() {
        const b = document.getElementById('subs-import-art');
        if (b) { b.disabled = true; b.textContent = 'Importing…'; }
        try {
            const res = await API.importDiscoveredArt();
            const bits = [`imported ${res.imported}`];
            if (res.skipped) bits.push(`skipped ${res.skipped}`);
            if (res.failed) bits.push(`${res.failed} failed (FA art needs the desktop app)`);
            this._toast(res.imported ? 'success' : (res.failed ? 'warn' : 'info'),
                `Discovered art: ${bits.join(', ')}`);
            await this.render();   // reload works + refresh the suggestion
        } catch (err) {
            this._toast('error', `Import failed: ${this.esc(err.message)}`);
            if (b) { b.disabled = false; b.textContent = 'Import all art'; }
        }
    },

    _renderControls() {
        const seg = (val, label) => `
            <button class="btn ${this._type === val ? 'btn-primary' : ''}" data-type="${val}"
                style="border-radius:0;border:none;">${label}</button>`;
        // Persona filter only shown when there's more than one persona to choose.
        const personaSel = this._personas.length > 1 ? `
            <select id="subs-persona" style="${this._inputStyle}max-width:180px;">
                <option value="0">All personas</option>
                ${this._personas.map(p =>
                    `<option value="${p.id}">${this.esc(p.name)}</option>`).join('')}
            </select>` : '';
        document.getElementById('subs-controls').innerHTML = `
            <div style="display:flex;gap:.75rem;flex-wrap:wrap;align-items:center;margin-bottom:1.25rem;">
                <div style="display:inline-flex;border:1px solid var(--card-border-inner);border-radius:8px;overflow:hidden;">
                    ${seg('all', 'All')}${seg('story', 'Stories')}${seg('artwork', 'Artwork')}
                </div>
                ${personaSel}
                <input id="subs-search" type="search" placeholder="Search…"
                    style="${this._inputStyle}max-width:220px;" value="${this.esc(this._search)}">
                <select id="subs-sort" style="${this._inputStyle}max-width:160px;margin-left:auto;">
                    <option value="recent">Most recent</option>
                    <option value="title">Title A–Z</option>
                    <option value="platforms">Most platforms</option>
                </select>
            </div>`;

        document.querySelectorAll('#subs-controls [data-type]').forEach(b =>
            b.addEventListener('click', () => {
                this._type = b.dataset.type;
                this._renderControls();   // refresh active segment
                this._paint();
            }));
        const ps = document.getElementById('subs-persona');
        if (ps) ps.addEventListener('change', () => {
            this._persona = parseInt(ps.value) || 0; this._paint();
        });
        const se = document.getElementById('subs-search');
        if (se) se.addEventListener('input', () => { this._search = se.value; this._paint(); });
        const so = document.getElementById('subs-sort');
        if (so) { so.value = this._sort; so.addEventListener('change', () => { this._sort = so.value; this._paint(); }); }
    },

    _filtered() {
        let list = this._works.slice();
        if (this._type !== 'all') list = list.filter(w => w.content_type === this._type);
        if (this._persona) list = list.filter(w => (w.persona_ids || []).includes(this._persona));
        if (this._search) {
            const q = this._search.toLowerCase();
            list = list.filter(w =>
                (w.title || '').toLowerCase().includes(q) || (w.name || '').toLowerCase().includes(q));
        }
        if (this._sort === 'title') list.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
        else if (this._sort === 'platforms') list.sort((a, b) => (b.platforms || []).length - (a.platforms || []).length);
        else list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        return list;
    },

    _paint() {
        const grid = document.getElementById('subs-grid');
        if (!grid) return;
        const list = this._filtered();
        if (!list.length) {
            grid.className = '';
            const nArt = (this._discoveredArt || []).length;
            if (this._type === 'artwork' && nArt) {
                const one = nArt === 1;
                grid.innerHTML = `<div class="empty-state"><h3>No imported artwork yet</h3>
                    <p class="muted">${nArt} discovered art piece${one ? '' : 's'} from polling
                    ${one ? 'is' : 'are'} waiting.
                    <a href="#/submissions/discovered">Import ${one ? 'it' : 'them'} &rarr;</a></p></div>`;
            } else {
                grid.innerHTML = `<div class="empty-state"><h3>Nothing here yet</h3>
                    <p class="muted">No works match this filter.</p></div>`;
            }
            return;
        }
        grid.className = 'story-card-grid';
        grid.innerHTML = list.map(w => this._card(w)).join('');
    },

    _card(w) {
        const _cover = Utils.cssUrl(w.thumb_url);
        const cover = _cover
            ? `<div class="story-card-cover" style="background-image:url('${_cover}')"></div>`
            : `<div class="story-card-cover" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);">no image</div>`;
        const typeChip = `<span class="chip" style="text-transform:capitalize;">${this.esc(w.content_type)}</span>`;
        const rating = w.rating ? `<span class="chip">${this.esc(w.rating)}</span>` : '';
        const plats = (w.platforms || []).map(c =>
            `<span title="${this.esc(this._plat(c).label)}">${this._plat(c).emoji || c}</span>`).join(' ');
        const persona = (w.persona_names && w.persona_names.length)
            ? `<div class="muted" style="font-size:.78rem;margin-top:.3rem;">${this.esc(w.persona_names.join(', '))}</div>` : '';
        const meta = w.meta ? `<div class="story-card-stats">${this.esc(w.meta)}</div>` : '';
        // "Add to Collection" — a role=button span (interactive content can't be a
        // real <button> inside the card's <a>); the global Collections delegated
        // handler catches [data-add-collection] and preventDefaults the nav.
        const addColl = `<span class="btn btn-sm coll-add-btn" role="button" tabindex="0"
            data-add-collection data-mtype="work" data-mref="${this.esc(w.content_type + ':' + w.name)}"
            data-label="${this.esc(w.title || w.name)}" title="Add to a collection">＋ Collection</span>`;
        return `
            <a class="story-card" href="${w.detail_route}">
                ${cover}
                <div class="story-card-body">
                    <div class="story-card-title">${this.esc(w.title || w.name)}</div>
                    <div class="story-card-meta">${typeChip}${rating}</div>
                    ${meta}
                    <div class="story-card-platforms" style="margin-top:.4rem;">${plats}</div>
                    ${persona}
                    <div style="margin-top:.5rem;">${addColl}</div>
                </div>
            </a>`;
    },

    /* ── Discovered (unlinked) bucket + link-to-work (Phase 2) ──── */

    /* Same discovered review surface, painted into a caller-supplied element
     * instead of owning the page — this is how the Library's "Discovered"
     * segment shows it (2.155.0). Everything below (_discRow, link/import/
     * ★ Master/Ignore, the per-platform bulk bar) is shared: _paintDiscovered
     * targets #disc-list, so we just need that id to exist inside `target`.
     * The standalone page keeps working; it's this plus a header. */
    async renderDiscoveredInto(target) {
        if (!target) return;
        target.className = '';
        target.innerHTML = `
            <div style="display:flex;justify-content:flex-end;margin-bottom:.6rem;">
                <a class="btn btn-primary btn-sm" href="#/submissions/triage"
                    title="Review discovered items one at a time, with keyboard shortcuts">⚡ Triage one-by-one</a>
            </div>
            <div id="disc-list"><div class="loading-spinner">Loading…</div></div>`;
        let disc;
        try {
            disc = await API.getDiscovered();
        } catch (err) {
            const el = document.getElementById('disc-list');
            if (el) el.innerHTML = `<div class="card error">Failed to load: ${this.esc(err.message)}</div>`;
            return;
        }
        this._discItems = (disc && disc.discovered) || [];
        this._paintDiscovered();
    },

    async renderDiscovered() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Discovered submissions</h1>
                    <p class="muted">Posts the pollers found on your platforms that aren't linked to a
                    local work yet. Link one to an existing work to fold it into the hub.</p>
                </div>
                <div style="display:flex;gap:.5rem;flex-shrink:0;">
                    <a class="btn btn-primary" href="#/submissions/triage" title="Review discovered items one at a time, with keyboard shortcuts">⚡ Triage one-by-one</a>
                    <a class="btn" href="#/artwork/ignored">Ignored</a>
                    <a class="btn" href="#/library">&larr; Library</a>
                </div>
            </div>
            <div id="disc-list"><div class="loading-spinner">Loading…</div></div>`;

        let disc;
        try {
            disc = await API.getDiscovered();
        } catch (err) {
            document.getElementById('disc-list').innerHTML =
                `<div class="card error">Failed to load: ${this.esc(err.message)}</div>`;
            return;
        }
        this._discItems = (disc && disc.discovered) || [];
        this._paintDiscovered();
    },

    _paintDiscovered() {
        const el = document.getElementById('disc-list');
        if (!el) return;
        if (!this._discItems.length) {
            el.innerHTML = `<div class="empty-state"><h3>Nothing unlinked</h3>
                <p class="muted">Every discovered submission is already linked or imported.</p></div>`;
            return;
        }
        // Per-platform bulk-import bar. Counts only rows that CAN import as
        // artwork (2.157.0): it counted every row, so X offered "Import all 54"
        // when all 54 were text tweets with no image to download — 54 guaranteed
        // failures behind one button.
        const counts = {};
        this._discItems.filter(d => this._canImportArt(d))
            .forEach(d => { counts[d.platform] = (counts[d.platform] || 0) + 1; });
        const bulk = Object.keys(counts).sort().map(p =>
            `<button class="btn" data-bulk="${p}">Import all ${counts[p]} from ${this.esc(this._plat(p).label)}</button>`).join(' ');
        const artBar = bulk ? `
            <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:1rem;">
                <span class="muted" style="font-size:.85rem;">Bulk import as artwork:</span> ${bulk}
            </div>` : '';
        // The text side: microblog posts with no image belong in Posts.
        const nPosts = this._discItems.filter(d => this._canImportPost(d)).length;
        const postBar = nPosts ? `
            <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:1rem;">
                <span class="muted" style="font-size:.85rem;">Text posts (no image):</span>
                <button class="btn" id="disc-bulk-posts">Import all ${nPosts} into Posts</button>
            </div>` : '';
        el.innerHTML = `
            ${artBar}${postBar}
            ${this._discItems.map((d, i) => this._discRow(d, i)).join('')}`;
        this._discItems.forEach((_d, i) => {
            const lbtn = document.getElementById(`disc-link-btn-${i}`);
            if (lbtn) lbtn.addEventListener('click', () => this._linkOne(i));
            const ibtn = document.getElementById(`disc-import-btn-${i}`);
            if (ibtn) ibtn.addEventListener('click', () => this._importOne(i));
            const gbtn = document.getElementById(`disc-ignore-btn-${i}`);
            if (gbtn) gbtn.addEventListener('click', () => this._ignoreOne(i));
            const mbtn = document.getElementById(`disc-master-btn-${i}`);   // art rows only
            if (mbtn) mbtn.addEventListener('click', () => this._masterOne(i));
            const pbtn = document.getElementById(`disc-post-btn-${i}`);     // text microblog only
            if (pbtn) pbtn.addEventListener('click', () => this._postOne(i));
        });
        document.querySelectorAll('#disc-list [data-bulk]').forEach(b =>
            b.addEventListener('click', () => this._importAll(b.dataset.bulk)));
        const bp = document.getElementById('disc-bulk-posts');
        if (bp) bp.addEventListener('click', () => this._importAllPosts());
    },

    async _importAll(platform) {
        const btns = [...document.querySelectorAll(`#disc-list [data-bulk="${platform}"]`)];
        btns.forEach(b => { b.disabled = true; b.textContent = 'Importing…'; });
        try {
            const res = await API.importBulk(platform);
            this._toast('success',
                `${platform.toUpperCase()}: imported ${res.imported}, skipped ${res.skipped}, failed ${res.failed}`);
            const disc = await API.getDiscovered();
            this._discItems = (disc && disc.discovered) || [];
            this._paintDiscovered();
        } catch (err) {
            this._toast('error', `Bulk import failed: ${err.message}`);
            this._paintDiscovered();
        }
    },

    async _importOne(i) {
        const d = this._discItems[i];
        const btn = document.getElementById(`disc-import-btn-${i}`);
        if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }
        try {
            const res = await API.importArtwork(d.platform, d.submission_id);
            this._toast('success', res.status === 'already_imported'
                ? `Already imported as ${res.name}` : `Imported as ${res.name}`);
            this._discItems.splice(i, 1);
            this._paintDiscovered();
        } catch (err) {
            this._toast('error', `Import failed: ${err.message}`);
            if (btn) { btn.disabled = false; btn.textContent = 'Import'; }
        }
    },

    _discRow(d, i) {
        const thumb = d.thumbnail_url
            ? `<img src="${this.esc(d.thumbnail_url)}" alt="" style="width:56px;height:56px;object-fit:cover;border-radius:8px;flex-shrink:0;">`
            : `<div style="width:56px;height:56px;border-radius:8px;background:var(--bg-elev);flex-shrink:0;"></div>`;
        const plat = this._plat(d.platform);
        return `
            <div class="card" style="display:flex;gap:1rem;align-items:center;padding:.85rem 1rem;margin-bottom:.6rem;flex-wrap:wrap;">
                ${thumb}
                <div style="flex:1;min-width:160px;">
                    <div style="font-weight:600;">${this.esc(d.title)}</div>
                    <div class="muted" style="font-size:.8rem;">
                        <span title="${this.esc(plat.label)}">${plat.emoji || ''} ${this.esc(plat.label)}</span>${d.type ? ` &middot; ${this.esc(d.type)}` : ''}
                        &middot; <a href="${this.esc(Utils.safeUrl(d.url) || '#')}" target="_blank" rel="noopener">view &#8599;</a>
                    </div>
                </div>
                <button class="btn btn-primary" id="disc-link-btn-${i}"
                    title="Link this to one of your works — opens the visual picker">🔗 Link to work…</button>
                ${this._canImportArt(d) ? `<button class="btn" id="disc-import-btn-${i}" title="Download the image + metadata as a new artwork">Import</button>` : ''}
                ${this._canImportPost(d) ? `<button class="btn" id="disc-post-btn-${i}" title="Bring this in as a post — it's a microblog post with no image, so there's no artwork to make">→ Posts</button>` : ''}
                ${this._canMaster(d) ? `<button class="btn" id="disc-master-btn-${i}" title="Promote to a Masterpiece — the master record for this image">★ Master</button>` : ''}
                <button class="btn" id="disc-ignore-btn-${i}" title="Hide this — not something you want here (e.g. an image from a tweet). Reversible from the Ignored view.">🚫 Ignore</button>
            </div>`;
    },

    /* Import-as-artwork downloads an image and mints an artwork folder, so it
     * needs an image. It used to show on EVERY row (2.157.0) — including the 54
     * text tweets, where there was nothing to download and the button could only
     * fail. Image → artwork; text → post (below); neither → Link or Ignore. */
    _canImportArt(d) {
        return !!d && !!d.thumbnail_url;
    },

    /* A tweet is a POST, not an artwork. Microblog platforms only: a SquidgeWorld
     * text work or a thumbnail-less DeviantArt piece is a story/artwork that
     * happens to lack an image, NOT a post. Mirrors post_importer's gate.
     *
     * Deliberately does NOT check `kind`: the backend's classifier lists "post"
     * among its ART hints (so image-bearing microblog posts are catchable by the
     * artwork import), which tags EVERY Bluesky post as art regardless of content.
     * No image = nothing for the artwork path to download, so on a microblog it's
     * a text post whatever `kind` says. */
    _MICROBLOG: ['tw', 'bsky', 'mast', 'thr', 'tum'],
    _canImportPost(d) {
        return !!d && this._MICROBLOG.includes(d.platform) && !d.thumbnail_url;
    },

    /* Bulk: every discovered text post → Posts. Refetches rather than splicing —
     * a batch can partially fail, and the server's exclusion set is the truth
     * about what's left in the queue. */
    async _importAllPosts() {
        const b = document.getElementById('disc-bulk-posts');
        if (b) { b.disabled = true; b.textContent = 'Importing…'; }
        try {
            const r = await API.importDiscoveredPosts();
            const bits = [`imported ${r.imported}`];
            if (r.skipped) bits.push(`${r.skipped} already in`);
            if (r.failed) bits.push(`${r.failed} failed`);
            this._toast(r.imported ? 'success' : (r.failed ? 'warn' : 'info'),
                `Posts: ${bits.join(', ')}`);
            const disc = await API.getDiscovered();
            this._discItems = (disc && disc.discovered) || [];
            this._paintDiscovered();
        } catch (err) {
            if (b) { b.disabled = false; b.textContent = 'Import all into Posts'; }
            this._toast('error', 'Import failed: ' + (err.message || err));
        }
    },

    /* Import one discovered text post into the Posts module. Idempotent server-
     * side; the row leaves the queue because its post_publications row is one of
     * the discovered exclusion sets. */
    async _postOne(i) {
        const d = this._discItems[i];
        if (!d) return;
        const btn = document.getElementById(`disc-post-btn-${i}`);
        const orig = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }
        try {
            const r = await API.importDiscoveredPost(d.platform, d.submission_id);
            this._toast(r.status === 'skipped' ? 'info' : 'success',
                r.status === 'skipped' ? 'Already in Posts' : 'Imported into Posts');
            this._discItems.splice(i, 1);
            this._paintDiscovered();
        } catch (err) {
            if (btn) { btn.disabled = false; btn.textContent = orig; }
            this._toast('error', 'Import failed: ' + (err.message || err));
        }
    },

    /* A Masterpiece is the master record for ONE IMAGE, so the row needs an image
     * and must not be a text work. Deliberately NOT gated on the old Artwork hub's
     * `_PLATFORMS` allowlist: that list omits X/Threads, which is precisely what
     * hid the Ignore buttons until 2.143.0 — this is the surface where discovered
     * items actually get reviewed, and tweet art is a real source of Masterpieces. */
    _canMaster(d) {
        return !!d && !!d.thumbnail_url && d.kind !== 'text';
    },

    /* ★ Master — ported from the retired Artwork hub (2.155.0). It lived ONLY on
     * that hub's discovered tiles, so redirecting #/artwork here would have
     * silently killed it. Same flow, image rows only.
     *
     * Stop duplicates forming (2.151.0, backlog M): if this image already IS a
     * Masterpiece, offer to link into it rather than mint a second record. Only
     * ever a PROMPT — near-identical hashes aren't proof (an SFW/NSFW pair of one
     * ref sheet hashes the same), so the call is the user's. The match check is
     * best-effort: if it fails, fall through to a normal promote. */
    async _masterOne(i) {
        const d = this._discItems[i];
        if (!d) return;
        const btn = document.getElementById(`disc-master-btn-${i}`);
        const orig = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Mastering…'; }
        const platform = d.platform, sid = d.submission_id;
        try {
            let match = null;
            try {
                const r = await API.matchMasterpiece(platform, sid);
                match = r && r.match;
            } catch { /* no opinion — fall through to a normal promote */ }

            if (match && window.confirm(
                `This looks like your existing Masterpiece “${match.title}”.\n\n`
                + `OK — link this piece into it (no duplicate created).\n`
                + `Cancel — make a separate Masterpiece anyway (e.g. an SFW/NSFW variant).`)) {
                await API.addMasterpieceMember(match.name, { platform, submission_id: sid });
                this._toast('success', `Linked into “${match.title}”`);
                window.location.hash = `#/masterpieces/${encodeURIComponent(match.name)}`;
                return;
            }
            const res = await API.promoteMasterpiece(platform, sid);
            this._toast('success', 'Made a Masterpiece — opening it');
            window.location.hash = `#/masterpieces/${encodeURIComponent(res.name)}`;
        } catch (err) {
            if (btn) { btn.disabled = false; btn.textContent = orig; }
            this._toast('error', 'Make Masterpiece failed: ' + (err.message || err));
        }
    },

    async _ignoreOne(i) {
        const d = this._discItems[i];
        const btn = document.getElementById(`disc-ignore-btn-${i}`);
        if (btn) btn.disabled = true;
        try {
            await API.ignoreDiscovered(d.platform, d.submission_id);
            this._toast('success', 'Ignored — hidden from discovered');
            this._discItems.splice(i, 1);
            this._paintDiscovered();
        } catch (err) {
            this._toast('error', `Ignore failed: ${err.message}`);
            if (btn) btn.disabled = false;
        }
    },

    /* Link a discovered submission to an existing work — via the visual picker
     * (2.162.0). Works/masterpieces only: you link a tweet TO a work, not to
     * another tweet, so the discovered filter is deliberately omitted. */
    async _linkOne(i) {
        const d = this._discItems[i];
        if (!d) return;
        if (!window.WorkPicker) { this._toast('error', 'Picker unavailable'); return; }
        WorkPicker.open({
            title: `Link “${d.title || d.submission_id}” to…`,
            confirmLabel: 'Link',
            multi: false,
            filters: ['story', 'artwork', 'masterpiece'],
            onConfirm: async (items) => {
                const it = items[0];
                if (!it) return;
                // Work items carry member_ref "content_type:name"; a masterpiece
                // carries a bare name (its folder == an artwork work of that name).
                let content_type, name;
                if (it.member_type === 'masterpiece') {
                    content_type = 'artwork';
                    name = it.member_ref;
                } else {
                    const p = it.member_ref.split(':');
                    content_type = p[0];
                    name = p.slice(1).join(':');
                }
                await API.linkSubmission({
                    platform: d.platform, submission_id: d.submission_id,
                    content_type, name, title: d.title, url: d.url,
                });
                this._toast('success', `Linked to ${name}`);
                const idx = this._discItems.indexOf(d);
                if (idx >= 0) this._discItems.splice(idx, 1);
                this._paintDiscovered();
            },
        });
    },

    /* ── Triage inbox (backlog V) ────────────────────────────────
     * The discovered queue, one card at a time with big/quick actions +
     * keyboard shortcuts — a fast keep / →Posts / master / link / ignore flow
     * instead of scanning a long list. Reuses the list view's endpoints;
     * forward-only over a snapshot (acted items are done server-side, so we
     * just advance the cursor). */
    async renderTriage() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Triage</h1>
                    <p class="muted">Clear your discovered queue one at a time — keep it, send it to Posts,
                    make it a Masterpiece, link it to a work, or ignore it, then on to the next.</p>
                </div>
                <a class="btn" href="#/submissions/discovered" style="flex-shrink:0;">&#9776; List view</a>
            </div>
            <div id="triage-body"><div class="loading-spinner">Loading…</div></div>`;
        let disc;
        try { disc = await API.getDiscovered(); }
        catch (err) {
            const b = document.getElementById('triage-body');
            if (b) b.innerHTML = `<div class="card error">Failed to load: ${this.esc(err.message)}</div>`;
            return;
        }
        this._triageItems = (disc && disc.discovered) || [];
        this._triageIdx = 0;
        this._triageCount = 0;
        this._wireTriageKeys();
        this._paintTriage();
    },

    /* Keyboard triage: attach once. The handler self-removes when the triage DOM
     * is gone (route changed), so it never acts off-screen or double-binds. */
    _wireTriageKeys() {
        if (this._triageKeyHandler) return;
        this._triageKeyHandler = (e) => {
            if (!document.getElementById('triage-body')) {
                document.removeEventListener('keydown', this._triageKeyHandler);
                this._triageKeyHandler = null;
                return;
            }
            if (e.target && e.target.matches && e.target.matches('input,textarea,select')) return;
            const d = this._triageItems[this._triageIdx];
            if (!d) return;
            const k = (e.key || '').toLowerCase();
            if (k === 'i' || k === 'x') { e.preventDefault(); this._triageAct('ignore'); }
            else if (k === 'p' && this._canImportPost(d)) { e.preventDefault(); this._triageAct('post'); }
            else if (k === 'a' && this._canImportArt(d)) { e.preventDefault(); this._triageAct('art'); }
            else if (k === 'm' && this._canMaster(d)) { e.preventDefault(); this._triageMaster(); }
            else if (k === 'l') { e.preventDefault(); this._triageLink(); }
            else if (k === 'arrowright' || k === 's') { e.preventDefault(); this._triageAct('skip'); }
        };
        document.addEventListener('keydown', this._triageKeyHandler);
    },

    /* No image + not a microblog → almost certainly a story/writing submission
     * (SF/DA/AO3/WS text), not art. Surfaces the "link, don't import as art" hint
     * the backlog called for. */
    _looksLikeWriting(d) {
        return !!d && !d.thumbnail_url && !this._MICROBLOG.includes(d.platform);
    },

    _paintTriage() {
        const body = document.getElementById('triage-body');
        if (!body) return;
        const total = this._triageItems.length;
        if (this._triageIdx >= total) {
            body.innerHTML = `<div class="empty-state" style="text-align:center;">
                <h3>${this._triageCount ? '✅ Inbox cleared' : 'Nothing to triage'}</h3>
                <p class="muted">${this._triageCount
                    ? `You triaged ${this._triageCount} item${this._triageCount === 1 ? '' : 's'}. Nice.`
                    : 'No unlinked discovered submissions right now.'}</p>
                <a class="btn btn-primary" href="#/library">&larr; Back to Library</a></div>`;
            return;
        }
        const d = this._triageItems[this._triageIdx];
        const plat = this._plat(d.platform);
        const hero = d.thumbnail_url
            ? `<img src="${this.esc(d.thumbnail_url)}" alt="" style="max-width:100%;max-height:360px;border-radius:10px;object-fit:contain;">`
            : `<div style="font-size:3rem;opacity:.5;padding:2.5rem 0;">${plat.emoji || '📄'}</div>`;
        const writingFlag = this._looksLikeWriting(d)
            ? `<div style="background:color-mix(in srgb, var(--accent) 10%, var(--surface));border-radius:8px;padding:.5rem .8rem;font-size:.85rem;margin:.7rem 0 0;">
                📝 Looks like a <strong>writing submission</strong> (no image) — link it to a story rather than importing as art.</div>`
            : '';

        const btn = (id, cls, label, title) =>
            `<button class="btn ${cls}" data-triage="${id}" title="${this.esc(title)}">${label}</button>`;
        const actions = [
            this._canImportArt(d) ? btn('art', 'btn-primary', 'Import as art <kbd>A</kbd>', 'Download the image + metadata as a new artwork') : '',
            this._canImportPost(d) ? btn('post', '', '&rarr; Posts <kbd>P</kbd>', 'Bring this in as a microblog post') : '',
            this._canMaster(d) ? btn('master', '', '★ Master <kbd>M</kbd>', 'Promote to a Masterpiece') : '',
            btn('link', '', '🔗 Link <kbd>L</kbd>', 'Link this to one of your works'),
            btn('ignore', 'btn-danger', '🚫 Ignore <kbd>I</kbd>', 'Hide this — reversible from Ignored'),
            btn('skip', 'btn-outline', 'Skip <kbd>→</kbd>', 'Leave it and move on'),
        ].filter(Boolean).join(' ');
        const keyHint = `${this._canImportArt(d) ? '<kbd>A</kbd> import · ' : ''}`
            + `${this._canImportPost(d) ? '<kbd>P</kbd> posts · ' : ''}`
            + `${this._canMaster(d) ? '<kbd>M</kbd> master · ' : ''}<kbd>L</kbd> link · <kbd>I</kbd> ignore · <kbd>→</kbd> skip`;

        body.innerHTML = `
            <div style="max-width:560px;margin:0 auto;">
                <div class="muted" style="text-align:center;margin-bottom:.5rem;">
                    ${this._triageIdx + 1} of ${total}${this._triageCount ? ` · ${this._triageCount} triaged` : ''}</div>
                <div class="card" style="text-align:center;padding:1.2rem;">
                    ${hero}
                    <div style="font-weight:700;font-size:1.1rem;margin-top:.7rem;">${this.esc(d.title || d.submission_id)}</div>
                    <div class="muted" style="font-size:.85rem;margin-top:.2rem;">
                        ${plat.emoji || ''} ${this.esc(plat.label)}${d.type ? ` · ${this.esc(d.type)}` : ''}
                        · <a href="${this.esc(Utils.safeUrl(d.url) || '#')}" target="_blank" rel="noopener">view ↗</a></div>
                    ${writingFlag}
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:.5rem;justify-content:center;margin-top:.9rem;">${actions}</div>
                <div class="muted" style="text-align:center;font-size:.78rem;margin-top:.8rem;">Keyboard: ${keyHint}</div>
            </div>`;

        body.querySelectorAll('[data-triage]').forEach(b =>
            b.addEventListener('click', () => {
                const a = b.dataset.triage;
                if (a === 'master') this._triageMaster();
                else if (a === 'link') this._triageLink();
                else this._triageAct(a);
            }));
    },

    async _triageAct(kind) {
        const d = this._triageItems[this._triageIdx];
        if (!d) return;
        try {
            if (kind === 'ignore') { await API.ignoreDiscovered(d.platform, d.submission_id); this._toast('success', 'Ignored'); }
            else if (kind === 'post') { await API.importDiscoveredPost(d.platform, d.submission_id); this._toast('success', 'Sent to Posts'); }
            else if (kind === 'art') { const r = await API.importArtwork(d.platform, d.submission_id); this._toast('success', `Imported as ${r.name}`); }
            // 'skip' does nothing server-side.
            if (kind !== 'skip') this._triageCount++;
            this._triageIdx++;
            this._paintTriage();
        } catch (err) {
            this._toast('error', `${kind} failed: ${err.message || err}`);
        }
    },

    async _triageMaster() {
        const d = this._triageItems[this._triageIdx];
        if (!d) return;
        const platform = d.platform, sid = d.submission_id;
        try {
            let match = null;
            try { const r = await API.matchMasterpiece(platform, sid); match = r && r.match; } catch { /* no opinion */ }
            if (match && window.confirm(
                `This looks like your existing Masterpiece “${match.title}”.\n\n`
                + `OK — link this piece into it (no duplicate).\nCancel — make a separate Masterpiece anyway.`)) {
                await API.addMasterpieceMember(match.name, { platform, submission_id: sid });
                this._toast('success', `Linked into “${match.title}”`);
            } else {
                const res = await API.promoteMasterpiece(platform, sid);
                this._toast('success', `Made a Masterpiece: ${res.name}`);
            }
            this._triageCount++;
            this._triageIdx++;
            this._paintTriage();
        } catch (err) {
            this._toast('error', 'Make Masterpiece failed: ' + (err.message || err));
        }
    },

    _triageLink() {
        const d = this._triageItems[this._triageIdx];
        if (!d) return;
        if (!window.WorkPicker) { this._toast('error', 'Picker unavailable'); return; }
        WorkPicker.open({
            title: `Link “${d.title || d.submission_id}” to…`,
            confirmLabel: 'Link',
            multi: false,
            filters: ['story', 'artwork', 'masterpiece'],
            onConfirm: async (items) => {
                const it = items[0];
                if (!it) return;
                let content_type, name;
                if (it.member_type === 'masterpiece') { content_type = 'artwork'; name = it.member_ref; }
                else { const p = it.member_ref.split(':'); content_type = p[0]; name = p.slice(1).join(':'); }
                await API.linkSubmission({
                    platform: d.platform, submission_id: d.submission_id,
                    content_type, name, title: d.title, url: d.url,
                });
                this._toast('success', `Linked to ${name}`);
                this._triageCount++;
                this._triageIdx++;
                this._paintTriage();
            },
        });
    },
};
