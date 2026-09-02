/* ── Artwork hub (PostyBirb-style image posting) ─────────────────
 *
 * A standalone image uploader parallel to the Stories hub: drop in an image,
 * fill per-platform metadata, publish to multiple art sites at once. Posted art
 * is tracked in the same analytics as stories (pollers auto-discover it).
 * Renders into #app, dispatched from the SPA router on #/artwork.
 *
 * Works on both runtimes: the browser file input + FormData upload works
 * everywhere; on the desktop app a native file-dialog bridge
 * (window.pywebview.api.open_image_dialog) lets you pick a local file by path.
 */
window.Artwork = {

    /* Image-capable platforms the hub posts to (v1), in display order. */
    _PLATFORMS: ['ib', 'fa', 'sf', 'bsky', 'ik', 'ws', 'da', 'e621', 'ig', 'fn'],

    _pendingFile: null,    // browser File awaiting upload
    _pendingPath: null,    // desktop local path awaiting copy
    _previewUrl: null,     // object URL for the live preview (revoked on re-pick)


    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '', color: '#888' };
    },

    _isDesktop() {
        return !!(window.pywebview && window.pywebview.api
            && window.pywebview.api.open_image_dialog);
    },

    _imgUrl(name, file) {
        return `/api/artwork/image?name=${encodeURIComponent(name)}&file=${encodeURIComponent(file)}`;
    },

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
    },

    /* ── Hub: grid of artworks (library + discovered) ───────────
     *
     * Two sources merged into one grid, "like Stories":
     *   • Library    — art you uploaded/imported into PawPoller. Clickable to the
     *                  per-work detail, badged with the platforms it's posted to.
     *   • Discovered — art the pollers found on your art accounts that isn't in
     *                  your library yet. Badged with its source platform + views,
     *                  with View ↗ + Import actions. Filtered to visual work via
     *                  the backend `kind` tag + the art-capable platform set.
     */
    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Artwork</h1>
                    <p class="muted">Everything you've got — art you've uploaded here plus pieces the
                    pollers found on your accounts — in one place. Upload new work to publish it to
                    multiple art sites at once.</p>
                </div>
                <div style="display:flex;gap:.5rem;flex-shrink:0;">
                    <a class="btn" href="#/artwork/ignored">Ignored</a>
                    <a class="btn" href="#/artwork/log">History</a>
                    <a class="btn btn-primary" href="#/artwork/new">+ New artwork</a>
                </div>
            </div>
            <div id="artwork-filters" class="artwork-filters" hidden></div>
            <div id="artwork-grid"><div class="loading-spinner">Loading…</div></div>`;

        const grid = document.getElementById('artwork-grid');

        // Library is essential; discovered is additive — never block the grid on it.
        let library = [];
        try {
            const data = await API.getArtworks();
            library = (data && data.artworks) || [];
        } catch (err) {
            grid.innerHTML = `<div class="card error">Failed to load artworks: ${this.esc(err.message)}</div>`;
            return;
        }
        let discovered = [];
        try {
            const dd = await API.getDiscovered();
            discovered = ((dd && dd.discovered) || []).filter(d => this._isArt(d));
        } catch (e) { /* show the library regardless of discovered errors */ }

        // Cross-platform links fold same-piece discovered tiles into one master.
        // A master IS a generic submission_link whose members happen to be art
        // tiles — additive and best-effort, so plain tiles show if links fail.
        let links = [];
        try {
            const ld = await API.getLinks();
            links = (ld && ld.links) || [];
        } catch (e) { /* masters are additive; fall back to ungrouped tiles */ }

        const { masters, standalone } = this._foldMasters(discovered, links);

        if (!library.length && !masters.length && !standalone.length) {
            grid.className = '';
            grid.innerHTML = `
                <div class="empty-state">
                    <h3>No artwork yet</h3>
                    <p class="muted">Upload your first image, or once the pollers have run they'll
                    surface art from your connected accounts here.</p>
                    <a class="btn btn-primary" href="#/artwork/new">+ New artwork</a>
                </div>`;
            return;
        }

        // Merge into one list, newest first. Library cards link to their detail;
        // master cards pool a linked set; discovered cards carry View ↗ + Import.
        // Each item carries a lowercased title for the search filter.
        const merged = [
            ...library.map(a => ({ _src: 'lib', _date: a.created_at || '', _title: (a.title || a.name || '').toLowerCase(), a })),
            ...masters.map(m => ({ _src: 'master', _date: m._date || '', _title: this._masterTitle(m.members).toLowerCase(), m })),
            ...standalone.map(d => ({ _src: 'disc', _date: d.posted_at || '', _title: (d.title || '').toLowerCase(), d })),
        ].sort((x, y) => (y._date || '').localeCompare(x._date || ''));
        this._hubItems = merged;
        // "In library" = uploaded/imported here; "Discovered" = found by the
        // pollers (masters + standalone discovered tiles).
        this._hubSeg = this._hubSeg || 'all';
        this._hubSearch = '';

        // Filter bar (client-side over the already-loaded set).
        const counts = {
            all: merged.length,
            lib: merged.filter(m => m._src === 'lib').length,
            disc: merged.filter(m => m._src !== 'lib').length,
        };
        const filterBar = document.getElementById('artwork-filters');
        filterBar.hidden = false;
        filterBar.innerHTML = this._hubFilterBar(counts);
        filterBar.querySelectorAll('[data-seg]').forEach(btn =>
            btn.addEventListener('click', () => {
                this._hubSeg = btn.dataset.seg;
                filterBar.querySelectorAll('[data-seg]').forEach(b => b.classList.toggle('is-active', b === btn));
                this._applyHubFilters();
            }));
        const searchEl = filterBar.querySelector('#artwork-search');
        if (searchEl) searchEl.addEventListener('input', () => {
            this._hubSearch = searchEl.value.trim().toLowerCase();
            this._applyHubFilters();
        });

        grid.className = 'artwork-grid';
        this._applyHubFilters();

        grid.addEventListener('click', e => {
            // Delete-from-card takes priority over the card's navigation and over
            // select mode (library cards aren't selectable anyway).
            const del = e.target.closest('[data-art-del]');
            if (del) { e.preventDefault(); e.stopPropagation(); this._deleteFromHub(del.dataset.artDel); return; }
            // Existing masters (dormant cross-platform links) stay expandable/splittable
            // for read-only display; the Gallery no longer MINTS new ones (Masterpieces
            // Phase 7 — use "★ Master" instead).
            const split = e.target.closest('.art-master-split');
            if (split) { e.preventDefault(); this._splitMaster(split.dataset.linkId); return; }
            const tog = e.target.closest('.art-master-toggle');
            if (tog) { e.preventDefault(); this._toggleMaster(tog); return; }
            const imp = e.target.closest('.art-import-btn');
            if (imp) { e.preventDefault(); this._importDiscovered(imp); return; }
            // Promote a discovered piece straight into a Masterpiece (import + seed
            // its primary member), then open the master's detail view.
            const mk = e.target.closest('.art-make-mp-btn');
            if (mk) { e.preventDefault(); this._makeMasterpiece(mk); return; }
            // Ignore a discovered tile — hide it from the hub (reversible).
            const ig = e.target.closest('.art-ignore-btn');
            if (ig) { e.preventDefault(); this._ignoreDiscovered(ig); }
        });
    },

    /* Ignore a discovered tile: persist it to the Ignore list and drop the card
     * from the grid immediately (also from the cached _hubItems so a filter
     * re-render doesn't bring it back). Reversible via the Ignored view. */
    async _ignoreDiscovered(btn) {
        const platform = btn.dataset.platform;
        const sid = btn.dataset.sid;
        btn.disabled = true;
        try {
            await API.ignoreDiscovered(platform, sid);
            const key = this._key(platform, sid);
            this._hubItems = (this._hubItems || []).filter(m =>
                !(m._src === 'disc' && this._key(m.d.platform, m.d.submission_id) === key));
            const card = btn.closest('.artwork-card');
            if (card) card.remove();
            this._toast('success', 'Ignored — hidden from the hub');
        } catch (err) {
            btn.disabled = false;
            this._toast('error', 'Could not ignore: ' + err.message);
        }
    },

    /* Segmented filter + search bar for the Artwork hub. Purely client-side over
     * the loaded set — separates uploaded/imported library work from art the
     * pollers discovered, and lets the user title-search across both. */
    _hubFilterBar(counts) {
        const seg = (id, label, n) =>
            `<button type="button" class="art-seg${this._hubSeg === id ? ' is-active' : ''}" data-seg="${id}">`
            + `${label} <span class="art-seg-count">${n}</span></button>`;
        return `
            <div class="art-seg-group" role="group" aria-label="Filter artwork">
                ${seg('all', 'All', counts.all)}
                ${seg('lib', 'In library', counts.lib)}
                ${seg('disc', 'Discovered', counts.disc)}
            </div>
            <input type="search" id="artwork-search" class="art-search" placeholder="Search titles…"
                value="${this.esc(this._hubSearch || '')}" autocomplete="off">`;
    },

    /* Re-render just the grid from this._hubItems using the current segment +
     * search. Cheap (no refetch); the grid's delegated click handler survives
     * because it's bound to the container, not the cards. */
    _applyHubFilters() {
        const grid = document.getElementById('artwork-grid');
        if (!grid) return;
        const seg = this._hubSeg || 'all';
        const q = this._hubSearch || '';
        const items = (this._hubItems || []).filter(m => {
            if (seg === 'lib' && m._src !== 'lib') return false;
            if (seg === 'disc' && m._src === 'lib') return false;
            if (q && !(m._title || '').includes(q)) return false;
            return true;
        });
        if (!items.length) {
            grid.className = '';
            grid.innerHTML = `<div class="empty-state"><p class="muted">No artwork matches this filter.</p></div>`;
            return;
        }
        grid.className = 'artwork-grid';
        grid.innerHTML = items.map(m =>
            m._src === 'lib' ? this._card(m.a)
                : m._src === 'master' ? this._masterCard(m.m)
                    : this._discoveredCard(m.d)).join('');
    },

    /* ── Masters (unify) ────────────────────────────────────────
     *
     * A "master" coalesces the same artwork posted to several sites — each with
     * its own per-site submission id — into one tile with pooled stats. It reuses
     * the generic cross-platform link tables (submission_links /
     * submission_link_members): an artwork master is simply a link whose members
     * are art tiles. See prototype/docs/ARTWORK_UNIFY.md.
     */

    _key(platform, sid) { return String(platform) + ':' + String(sid); },
    _unkey(k) {
        const i = k.indexOf(':');
        return { platform: k.slice(0, i), submission_id: k.slice(i + 1) };
    },

    /* Group discovered art tiles that share a submission_link into masters,
       returning the masters plus the still-standalone tiles. A link becomes a
       master only when 2+ of its members are art tiles present in this gallery,
       so story links (and links with a single art member here) fall through and
       their members stay as plain tiles. */
    _foldMasters(discovered, links) {
        const byKey = new Map();
        discovered.forEach(d => byKey.set(this._key(d.platform, d.submission_id), d));
        const claimed = new Set();
        const masters = [];
        (links || []).forEach(link => {
            const members = (link.members || [])
                .map(m => byKey.get(this._key(m.platform, m.submission_id)))
                .filter(Boolean);
            if (members.length < 2) return;
            members.forEach(d => claimed.add(this._key(d.platform, d.submission_id)));
            const _date = members.map(d => d.posted_at || '').sort().pop() || '';
            masters.push({ link_id: link.link_id, members, _date });
        });
        const standalone = discovered.filter(
            d => !claimed.has(this._key(d.platform, d.submission_id)));
        return { masters, standalone };
    },

    _masterCover(members) {
        const withThumb = members.find(d => this._thumbSrc(d));
        const src = withThumb ? this._thumbSrc(withThumb) : '';
        return src
            ? `<div class="artwork-card-cover" style="background-image:url('${this.esc(src)}')"></div>`
            : `<div class="artwork-card-cover artwork-card-cover--empty">no image</div>`;
    },

    _masterTitle(members) {
        const m = members.find(d => d.title);
        return (m && m.title) || ('#' + members[0].submission_id);
    },

    _masterCard(m) {
        const members = m.members;
        const cover = this._masterCover(members);
        const totalViews = members.reduce((s, d) => s + (Number(d.views) || 0), 0);
        const emojis = members.map(d => {
            const p = this._plat(d.platform);
            return `<span class="artwork-plat" title="${this.esc(p.label)}">${p.emoji || this.esc(p.label)}</span>`;
        }).join('');
        const rows = members.map(d => {
            const p = this._plat(d.platform);
            const v = (d.views != null)
                ? `<span class="artwork-master-member-stat">${Utils.formatNumber(d.views)} views</span>` : '';
            return `
                <div class="artwork-master-member">
                    <span class="artwork-plat" title="${this.esc(p.label)}">${p.emoji || this.esc(p.label)}</span>
                    <span class="artwork-master-member-title">${this.esc(d.title || ('#' + d.submission_id))}</span>
                    ${v}
                    <a class="btn btn-sm" href="${this.esc(Utils.safeUrl(d.url) || '#')}" target="_blank" rel="noopener">View ↗</a>
                </div>`;
        }).join('');
        return `
            <div class="artwork-card artwork-card--master" data-link-id="${this.esc(m.link_id)}">
                <button type="button" class="art-master-toggle" data-link-id="${this.esc(m.link_id)}"
                    title="Expand this master">
                    ${cover}
                    <span class="artwork-disc-badge artwork-master-badge">${members.length} sites</span>
                </button>
                <div class="artwork-card-body">
                    <div class="artwork-card-title">${this.esc(this._masterTitle(members))}</div>
                    <div class="artwork-card-meta">
                        <span class="artwork-plats">${emojis}</span>
                        <span class="artwork-disc-stat" style="margin-left:auto;">${Utils.formatNumber(totalViews)} views</span>
                    </div>
                    <div class="artwork-master-panel">
                        ${rows}
                        <div class="artwork-master-panel-actions">
                            <button type="button" class="btn btn-sm btn-danger art-master-split"
                                data-link-id="${this.esc(m.link_id)}">Split</button>
                        </div>
                    </div>
                </div>
            </div>`;
    },

    _toggleMaster(btn) {
        const card = btn.closest('.artwork-card--master');
        if (card) card.classList.toggle('expanded');
    },

    async _splitMaster(linkId) {
        if (!confirm('Split this master? The pieces go back to separate tiles — nothing is deleted from any site.')) return;
        try {
            await API.deleteLink(linkId);
            this._toast('success', 'Split');
            const y = window.scrollY;
            await this.render();
            window.scrollTo(0, y);
        } catch (err) {
            this._toast('error', 'Split failed: ' + (err.message || err));
        }
    },

    /* Keep only art-capable, visual (non-text), thumbnailed discovered items. */
    _isArt(d) {
        return !!d && this._PLATFORMS.includes(d.platform)
            && d.kind !== 'text' && !!d.thumbnail_url;
    },

    /* FA / Inkbunny / Pixiv thumbnails can't be hotlinked from the browser
       (CORS + mixed-content), so they must go through the backend relay
       endpoints — same as the submissions tables do via Utils.*ThumbUrl.
       Other platforms' thumbnails load directly. Without this the discovered
       cards for those three platforms render as blank tiles. */
    _thumbSrc(d) {
        const url = d && d.thumbnail_url;
        if (!url) return '';
        if (d.platform === 'fa') return Utils.faThumbUrl(url);
        if (d.platform === 'ib') return Utils.thumbUrl(url);
        if (d.platform === 'pix') return Utils.pixThumbUrl(url);
        return url;
    },

    _discoveredCard(d) {
        const plat = this._plat(d.platform);
        const src = this._thumbSrc(d);
        const cover = src
            ? `<div class="artwork-card-cover" style="background-image:url('${this.esc(src)}')"></div>`
            : `<div class="artwork-card-cover artwork-card-cover--empty">no image</div>`;
        const views = (d.views != null)
            ? `<span class="artwork-disc-stat">${Utils.formatNumber(d.views)} views</span>` : '';
        const key = this._key(d.platform, d.submission_id);
        return `
            <div class="artwork-card artwork-card--disc artwork-card--selectable" data-key="${this.esc(key)}">
                <span class="artwork-select-check" aria-hidden="true">✓</span>
                <a href="${this.esc(Utils.safeUrl(d.url) || '#')}" target="_blank" rel="noopener" class="artwork-card-coverlink">
                    ${cover}
                    <span class="artwork-disc-badge" title="Found on ${this.esc(plat.label)}">${plat.emoji || this.esc(plat.label)}</span>
                </a>
                <div class="artwork-card-body">
                    <div class="artwork-card-title">${this.esc(d.title || ('#' + d.submission_id))}</div>
                    <div class="artwork-card-meta">${views}</div>
                    <div class="artwork-disc-actions">
                        <a class="btn btn-sm" href="${this.esc(Utils.safeUrl(d.url) || '#')}" target="_blank" rel="noopener">View ↗</a>
                        <button class="btn btn-sm btn-primary art-import-btn"
                            data-platform="${this.esc(d.platform)}" data-sid="${this.esc(d.submission_id)}">Import</button>
                        <button class="btn btn-sm art-make-mp-btn"
                            title="Import this image and make it a Masterpiece — the master record you sync across every site"
                            data-platform="${this.esc(d.platform)}" data-sid="${this.esc(d.submission_id)}">★ Master</button>
                        <button class="btn btn-sm art-ignore-btn"
                            title="Hide this — not real artwork you want (e.g. scraped from a tweet). Reversible from the Ignored view."
                            data-platform="${this.esc(d.platform)}" data-sid="${this.esc(d.submission_id)}">🚫 Ignore</button>
                    </div>
                </div>
            </div>`;
    },

    async _importDiscovered(btn) {
        const platform = btn.dataset.platform, sid = btn.dataset.sid;
        btn.disabled = true; btn.textContent = 'Importing…';
        try {
            await API.importArtwork(platform, sid);
            this._toast('success', 'Imported to your library');
            /* Re-render so the piece leaves Discovered and becomes a library
             * card — but preserve scroll. render() rebuilds #app from the top
             * (loading spinner), which otherwise jumps the viewport to the top
             * mid-list while importing down a long Discovered grid. (2.51.7) */
            const scrollY = window.scrollY;
            await this.render();
            window.scrollTo(0, scrollY);
        } catch (err) {
            btn.disabled = false; btn.textContent = 'Import';
            this._toast('error', 'Import failed: ' + (err.message || err));
        }
    },

    /* Promote a discovered piece into a Masterpiece: imports the image (full-res
     * where the platform allows), seeds the source as the primary member, hashes
     * the canonical image, then opens the master's detail view where lookalikes on
     * other sites can be linked in. */
    async _makeMasterpiece(btn) {
        const platform = btn.dataset.platform, sid = btn.dataset.sid;
        btn.disabled = true; const orig = btn.textContent; btn.textContent = 'Mastering…';
        try {
            // Stop duplicates forming (2.151.0): if this image already IS a
            // Masterpiece, offer to link this upload into it instead of minting a
            // second record. Only ever a prompt — near-identical hashes aren't
            // proof (an SFW/NSFW pair of one ref sheet hashes the same), so the
            // call is the user's. Best-effort: a failed check never blocks the promote.
            let match = null;
            try {
                const r = await API.matchMasterpiece(platform, sid);
                match = r && r.match;
            } catch (e) { /* no opinion — fall through to a normal promote */ }

            if (match && window.confirm(
                `This looks like your existing Masterpiece “${match.title}”.\n\n`
                + `OK — link this upload into it (no duplicate created).\n`
                + `Cancel — make a separate Masterpiece anyway (e.g. an SFW/NSFW variant).`)) {
                await API.addMasterpieceMember(match.name, { platform, submission_id: sid });
                this._toast('success', `Linked into “${match.title}”`);
                window.location.hash = `#/masterpieces/${encodeURIComponent(match.name)}`;
                return;
            }

            const res = await API.promoteMasterpiece(platform, sid);
            this._toast('success', 'Made a Masterpiece — opening it');
            window.location.hash = `#/masterpieces/${res.name}`;
        } catch (err) {
            btn.disabled = false; btn.textContent = orig;
            this._toast('error', 'Make Masterpiece failed: ' + (err.message || err));
        }
    },

    _card(a) {
        // data-rating drives the SFW/safe-mode blur (safe_mode.css); unknown → blurred.
        const rAttr = ` data-rating="${this.esc((a.rating || '').toLowerCase())}"`;
        const cover = a.image
            ? `<div class="artwork-card-cover"${rAttr} style="background-image:url('${this._imgUrl(a.name, a.image)}')"></div>`
            : `<div class="artwork-card-cover artwork-card-cover--empty"${rAttr}>no image</div>`;
        const rating = a.rating
            ? `<span class="artwork-badge artwork-badge--${this.esc(a.rating)}">${this.esc(a.rating)}</span>` : '';
        const plats = (a.platforms || []).map(c =>
            `<span class="artwork-plat" title="${this.esc(this._plat(c).label)}">${this._plat(c).emoji || c}</span>`).join('');
        return `
            <a class="artwork-card" href="#/artwork/image/${encodeURIComponent(a.name)}">
                ${cover}
                <span class="artwork-card-del" role="button" tabindex="-1"
                    data-art-del="${this.esc(a.name)}" title="Delete artwork"
                    aria-label="Delete ${this.esc(a.title || a.name)}">🗑</span>
                <div class="artwork-card-body">
                    <div class="artwork-card-title">${this.esc(a.title || a.name)}</div>
                    <div class="artwork-card-meta">${rating}<span class="artwork-plats">${plats}</span></div>
                </div>
            </a>`;
    },

    /* Delete a saved artwork straight from a hub card. The detail page has had a
     * Delete button all along, but it was buried — surfacing it here is the fix
     * for "artwork upload screen missing remove artwork" (a discoverability gap). */
    async _deleteFromHub(name) {
        if (!confirm(`Delete "${(name || '').replace(/_/g, ' ')}" from your library?\nAny already-published posts stay live on each platform.`)) return;
        try {
            await API.deleteArtwork(name);
            this._toast('success', 'Deleted');
            const y = window.scrollY;
            await this.render();
            window.scrollTo(0, y);
        } catch (err) {
            this._toast('error', 'Delete failed: ' + (err.message || err));
        }
    },

    /* ── Create / upload flow ───────────────────────────────── */

    async renderUpload() {
        this._pendingFile = null;
        this._pendingPath = null;
        if (this._previewUrl) { URL.revokeObjectURL(this._previewUrl); this._previewUrl = null; }

        const app = document.getElementById('app');
        const desktopBtn = this._isDesktop()
            ? `<button type="button" class="btn" id="art-pick-local">Choose local file…</button>` : '';

        app.innerHTML = `
            <div class="page-header">
                <h1>New artwork</h1>
                <p class="muted"><a href="#/library/type/artwork">← Back to Library</a>
                    · <a href="#/artwork/quick">⚡ Quick publish</a> for the one-screen version</p>
            </div>
            <div class="artwork-upload">
                <div class="artwork-upload-col">
                    <div id="art-drop" class="artwork-drop">
                        <div class="artwork-drop-inner" id="art-drop-inner">
                            <div class="artwork-drop-icon">&#128444;</div>
                            <p>Drag an image here, or</p>
                            <label class="btn btn-primary">Choose image
                                <input type="file" id="art-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
                            </label>
                            ${desktopBtn}
                            <p class="muted artwork-drop-hint">PNG, JPG, GIF or WebP</p>
                        </div>
                        <img id="art-preview" class="artwork-preview" alt="preview" hidden>
                    </div>
                    <button type="button" id="art-remove" class="btn btn-sm artwork-remove" hidden>&#10005; Remove image</button>
                </div>
                <div class="artwork-upload-col">
                    <div class="card">
                        <label class="field">Title
                            <input type="text" id="art-title" placeholder="Artwork title">
                        </label>
                        <label class="field">Description
                            <textarea id="art-desc" rows="4" placeholder="Caption / description"></textarea>
                        </label>
                        <label class="field">Alt text <span class="muted">(describes the image for screen readers; used on Bluesky)</span>
                            <input type="text" id="art-alt" placeholder="e.g. A grey wolf in a red jacket grins at the viewer">
                        </label>
                        <div class="field-row">
                            <label class="field">Rating
                                <select id="art-rating">
                                    <option value="general">General</option>
                                    <option value="mature">Mature</option>
                                    <option value="adult" selected>Adult</option>
                                </select>
                            </label>
                        </div>
                        <label class="field">Tags <span class="muted">(comma-separated, used as the default for every platform)</span>
                            <textarea id="art-tags" rows="2" placeholder="tag one, tag two, tag three"></textarea>
                        </label>
                        <button type="button" class="btn btn-sm" id="art-tag-browse">🏷️ Browse tag library</button>
                    </div>
                    <div class="card">
                        <h3>Publish to</h3>
                        <p class="muted" style="margin:.2rem 0 .6rem;">Tick a platform to publish, pick the account,
                        and optionally override its tags.</p>
                        <div id="art-platforms"></div>
                    </div>
                    <div class="artwork-actions">
                        <button class="btn" id="art-save">Save to library</button>
                        <button class="btn btn-primary" id="art-publish">Save &amp; publish</button>
                        <span id="art-msg" class="muted"></span>
                    </div>
                </div>
            </div>`;

        this._renderPlatformRows(document.getElementById('art-platforms'));
        this._wireUpload();
        await this._populateAccountSelectors();
    },

    _renderPlatformRows(el) {
        el.innerHTML = this._PLATFORMS.map(code => {
            const p = this._plat(code);
            return `
            <div class="artwork-plat-row" data-platform="${code}">
                <label class="artwork-plat-toggle">
                    <input type="checkbox" class="art-plat-check" value="${code}">
                    <span class="artwork-plat-emoji">${p.emoji || ''}</span>
                    <span class="artwork-plat-name">${this.esc(p.label)}</span>
                </label>
                <span class="art-acct-slot" data-platform="${code}"></span>
                <details class="artwork-plat-adv">
                    <summary>Override</summary>
                    <label class="field">Tags for ${this.esc(p.label)}
                        <input type="text" class="art-plat-tags" data-platform="${code}" placeholder="(defaults to the tags above)">
                    </label>
                </details>
            </div>`;
        }).join('');
    },

    async _populateAccountSelectors() {
        for (const code of this._PLATFORMS) {
            const slot = document.querySelector(`.art-acct-slot[data-platform="${code}"]`);
            if (!slot) continue;
            try {
                const data = await API.getAccounts(code);
                const accts = (data.accounts || []).filter(a => a.enabled);
                if (accts.length < 2) continue;   // single account → no picker needed
                const opts = accts.map(a =>
                    `<option value="${a.account_id}"${a.is_default ? ' selected' : ''}>` +
                    `${this.esc(a.label || a.handle || ('account ' + a.account_id))}</option>`).join('');
                slot.innerHTML = `<label class="art-acct">as <select class="art-acct-select" data-platform="${code}">${opts}</select></label>`;
            } catch (e) { /* default account on any failure */ }
        }
    },

    _wireUpload() {
        const fileInput = document.getElementById('art-file');
        const drop = document.getElementById('art-drop');
        fileInput.addEventListener('change', () => {
            if (fileInput.files && fileInput.files[0]) this._setFile(fileInput.files[0]);
        });
        ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
            e.preventDefault(); drop.classList.add('dragover');
        }));
        ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
            e.preventDefault(); drop.classList.remove('dragover');
        }));
        drop.addEventListener('drop', e => {
            const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) this._setFile(f);
        });

        const localBtn = document.getElementById('art-pick-local');
        if (localBtn) localBtn.addEventListener('click', () => this._pickDesktopFile());

        const removeBtn = document.getElementById('art-remove');
        if (removeBtn) removeBtn.addEventListener('click', () => this._clearFile());

        document.getElementById('art-save').addEventListener('click', () => this._save(false));
        document.getElementById('art-publish').addEventListener('click', () => this._save(true));

        // Tag-library browser — same picker chrome the story editor uses, so
        // artwork tags come from the canonical 4,600-tag database, not free typing.
        const tagBrowseBtn = document.getElementById('art-tag-browse');
        if (tagBrowseBtn) tagBrowseBtn.addEventListener('click', () => this._openTagLibrary());

        // Auto-fill the title from the filename if the user hasn't typed one.
        const titleInput = document.getElementById('art-title');
        titleInput.dataset.touched = '';
        titleInput.addEventListener('input', () => { titleInput.dataset.touched = '1'; });
    },

    _setFile(file) {
        if (!/\.(png|jpe?g|gif|webp)$/i.test(file.name)) {
            this._toast('error', 'Please choose a PNG, JPG, GIF or WebP image.');
            return;
        }
        this._pendingFile = file;
        this._pendingPath = null;
        if (this._previewUrl) URL.revokeObjectURL(this._previewUrl);
        this._previewUrl = URL.createObjectURL(file);
        this._showPreview(this._previewUrl, file.name);
    },

    async _pickDesktopFile() {
        try {
            const result = await window.pywebview.api.open_image_dialog();
            const path = Array.isArray(result) ? result[0] : result;
            if (!path) return;
            this._pendingPath = path;
            this._pendingFile = null;
            // Desktop can't object-URL a disk path; show the filename + serve via
            // a transient name? Simplest: show the basename and a generic icon.
            const base = String(path).split(/[\\/]/).pop();
            this._showPreview('', base);
        } catch (e) {
            this._toast('error', 'File dialog failed: ' + (e.message || e));
        }
    },

    _showPreview(url, label) {
        const img = document.getElementById('art-preview');
        const inner = document.getElementById('art-drop-inner');
        const titleInput = document.getElementById('art-title');
        if (titleInput && !titleInput.dataset.touched && !titleInput.value) {
            titleInput.value = (label || '').replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
        }
        if (url) {
            img.src = url; img.hidden = false; inner.style.display = 'none';
        } else {
            img.hidden = true; inner.style.display = '';
            inner.querySelector('.artwork-drop-hint').textContent = 'Selected: ' + (label || 'file');
        }
        const removeBtn = document.getElementById('art-remove');
        if (removeBtn) removeBtn.hidden = false;
    },

    /* Clear the chosen image and restore the empty drop zone so a different
     * file can be picked (the upload screen had no way to undo a selection). */
    _clearFile() {
        this._pendingFile = null;
        this._pendingPath = null;
        if (this._previewUrl) { URL.revokeObjectURL(this._previewUrl); this._previewUrl = null; }
        const img = document.getElementById('art-preview');
        const inner = document.getElementById('art-drop-inner');
        const fileInput = document.getElementById('art-file');
        const removeBtn = document.getElementById('art-remove');
        if (img) { img.hidden = true; img.src = ''; }
        if (inner) {
            inner.style.display = '';
            const hint = inner.querySelector('.artwork-drop-hint');
            if (hint) hint.textContent = 'PNG, JPG, GIF or WebP';
        }
        if (fileInput) fileInput.value = '';
        if (removeBtn) removeBtn.hidden = true;
    },

    _parseTags(s) {
        if (!s) return [];
        const sep = s.indexOf(',') >= 0 ? ',' : /\s/;
        return s.split(sep).map(t => t.trim()).filter(Boolean);
    },

    /* Open the shared TagPicker pre-loaded with whatever is already in the
     * default-tags box, and write the confirmed selection straight back. The
     * picker preserves free-typed tags that aren't in the library, so this is
     * lossless — it only ever adds discoverability, never drops a tag. */
    _openTagLibrary(targetId = 'art-tags') {
        if (!window.TagPicker) { this._toast('error', 'Tag library unavailable'); return; }
        const box = document.getElementById(targetId);
        if (!box) return;
        TagPicker.open({
            title: 'Tag library',
            selected: this._parseTags(box.value),
            onConfirm: (names) => { box.value = names.join(', '); },
        });
    },

    _collectMetadata() {
        const title = document.getElementById('art-title').value.trim();
        const description = document.getElementById('art-desc').value;
        const altText = document.getElementById('art-alt')?.value.trim() || '';
        const rating = document.getElementById('art-rating').value;
        const defaultTags = this._parseTags(document.getElementById('art-tags').value);

        const tags = {};
        if (defaultTags.length) tags.default = defaultTags;

        const checked = Array.from(document.querySelectorAll('.art-plat-check:checked')).map(c => c.value);
        // Per-platform tag overrides
        document.querySelectorAll('.art-plat-tags').forEach(inp => {
            const code = inp.dataset.platform;
            if (!checked.includes(code)) return;
            const over = this._parseTags(inp.value);
            if (over.length) tags[code] = over;
        });

        const accountIds = {};
        document.querySelectorAll('.art-acct-select').forEach(sel => {
            if (checked.includes(sel.dataset.platform)) accountIds[sel.dataset.platform] = parseInt(sel.value, 10);
        });

        return {
            title, description, rating, tags,
            alt_text: altText,
            platforms: checked,
            _accountIds: accountIds,
        };
    },

    async _save(publish) {
        const msg = document.getElementById('art-msg');
        const meta = this._collectMetadata();
        if (!this._pendingFile && !this._pendingPath) {
            msg.textContent = 'Choose an image first.'; return;
        }
        if (!meta.title) { msg.textContent = 'Enter a title.'; return; }
        if (publish && !meta.platforms.length) { msg.textContent = 'Tick at least one platform to publish.'; return; }

        const accountIds = meta._accountIds;
        delete meta._accountIds;

        const saveBtn = document.getElementById('art-save');
        const pubBtn = document.getElementById('art-publish');
        saveBtn.disabled = pubBtn.disabled = true;
        msg.textContent = 'Saving…';

        let name;
        try {
            if (this._pendingPath) {
                const r = await API.createArtworkFromPath({ path: this._pendingPath, metadata: meta });
                name = r.name;
            } else {
                const r = await API.uploadArtwork(this._pendingFile, meta, null,
                    pct => { msg.textContent = `Uploading… ${pct}%`; });
                name = r.name;
            }
        } catch (err) {
            msg.textContent = 'Save failed: ' + err.message;
            saveBtn.disabled = pubBtn.disabled = false;
            return;
        }

        if (!publish) {
            this._toast('success', 'Saved to library');
            window.location.hash = `#/artwork/image/${encodeURIComponent(name)}`;
            return;
        }

        msg.textContent = 'Publishing…';
        try {
            const res = await API.publishArtwork({
                artwork_name: name,
                platforms: meta.platforms,
                account_ids: accountIds,
            });
            const ok = res.successes || 0, fail = res.failures || 0;
            this._toast(fail ? 'error' : 'success', `Published: ${ok} ok, ${fail} failed`);
            window.location.hash = `#/artwork/image/${encodeURIComponent(name)}`;
        } catch (err) {
            msg.textContent = 'Saved, but publish failed: ' + err.message;
            saveBtn.disabled = pubBtn.disabled = false;
        }
    },

    /* ── Detail ─────────────────────────────────────────────── */

    /* UNIFIED ART DETAIL (2.193.0).
     *
     * There used to be two pages for one record: this one (reached from
     * #/artwork/image/{name}) and the Masterpiece detail (#/masterpieces/{name}).
     * They were never two entities — masterpiece.json is a back-compatible
     * SUPERSET of artwork.json, both endpoints load through the same
     * artwork_reader.load_artwork(), and list_masterpieces adopts every artwork
     * folder into the index. There is no discriminator anywhere in the data.
     *
     * The Masterpiece renderer was the superset of the two (variants + per-variant
     * stats, members, pHash linking, sync-to-sites, growth chart, collections,
     * junk, replace-image, fold, prev/next), so it won. The four things only this
     * page had — publish now, schedule, delete, alt text — were ported into it.
     *
     * BOTH routes stay live and render the same page, deliberately: detail_route
     * is authored server-side in submissions_api.py and consumed by the Library,
     * showcase, queue, commissions and Image Tool, so a redirect would have meant
     * repointing all of them for no user-visible gain.
     *
     * The old renderer below is kept, unreachable, only as the port reference for
     * anything that turns out to have been missed; delete it once this has had a
     * release in the wild. */
    async renderDetail(name) {
        if (window.Masterpieces) return window.Masterpieces.renderDetail(name);
        return this._renderDetailLegacy(name);
    },

    async _renderDetailLegacy(name) {
        const app = document.getElementById('app');
        app.innerHTML = `<div class="loading-spinner">Loading…</div>`;
        let data;
        try {
            data = await API.getArtwork(name);
        } catch (err) {
            app.innerHTML = `<div class="card error">Artwork not found: ${this.esc(name)}</div>`;
            return;
        }
        const cover = data.image
            ? `<img class="artwork-detail-img" id="art-detail-img" data-rating="${this.esc((data.rating || '').toLowerCase())}" src="${this._imgUrl(name, data.image)}" alt="${this.esc(data.title)}">` : '';
        // Variant strip (2.190.0): declared variants render labeled; a piece with
        // only undeclared alt files falls back to the raw folder images. Clicking
        // a thumbnail swaps the main image. Read-only — managed in Masterpieces.
        const _variants = data.variants || [];
        const _chips = _variants.length
            ? _variants.map(v => ({ img: v.image, label: v.label || v.key || 'Primary' }))
            : (data.images || []).map((f, i) => ({ img: f, label: i === 0 ? 'Primary' : `Alt ${i}` }));
        const altStrip = _chips.length > 1
            ? `<div class="artwork-detail-alts" data-rating="${this.esc((data.rating || '').toLowerCase())}">
                ${_chips.map((c, i) => `
                    <button type="button" class="artwork-alt${i === 0 ? ' is-active' : ''}"
                        data-art-alt="${this._imgUrl(name, c.img)}" title="${this.esc(c.label)}">
                        <img src="${this._imgUrl(name, c.img)}" alt="" loading="lazy">
                        <span class="artwork-alt-label">${this.esc(c.label)}</span>
                    </button>`).join('')}
               </div>` : '';
        const pubRows = (data.publications || []).map(p => {
            const plat = this._plat(p.platform);
            const st = p.stats || {};
            const link = p.external_url
                ? `<a href="${this.esc(Utils.safeUrl(p.external_url) || '#')}" target="_blank" rel="noopener">view ↗</a>` : '—';
            const stats = p.stats
                ? `${Utils.formatNumber(st.views || 0)} views · ${Utils.formatNumber(st.favorites_count || 0)} faves · ${Utils.formatNumber(st.comments_count || 0)} comments`
                : '<span class="muted">not yet polled</span>';
            return `<tr>
                <td>${plat.emoji || ''} ${this.esc(plat.label)}</td>
                <td><span class="artwork-status artwork-status--${this.esc(p.status)}">${this.esc(p.status)}</span></td>
                <td>${stats}</td>
                <td>${link}</td>
            </tr>`;
        }).join('');

        // Editable canonical metadata (rating / title / description / tags). The
        // rating select is pre-selected to the current value; the tags box shows
        // the default tag list (the cascade source for every platform).
        const curRating = (data.rating || '').toLowerCase();
        const ratingOpts = ['general', 'mature', 'adult'].map(r =>
            `<option value="${r}"${curRating === r ? ' selected' : ''}>${r[0].toUpperCase() + r.slice(1)}</option>`).join('');
        const tagsObj = data.tags || {};
        const defaultTags = (tagsObj.default && tagsObj.default.length)
            ? tagsObj.default
            : [...new Set(Object.values(tagsObj).flat())];
        const defaultTagsStr = defaultTags.join(', ');

        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>${this.esc(data.title || name)}</h1>
                    <p class="muted"><a href="#/library/type/artwork">← Back to Library</a></p>
                </div>
                <div style="display:flex;gap:.5rem;flex-shrink:0;">
                    <button class="btn btn-danger" id="art-delete">Delete</button>
                </div>
            </div>
            <div class="artwork-detail">
                <div class="artwork-detail-col">${cover}${altStrip}</div>
                <div class="artwork-detail-col">
                    <div class="card">
                        <h3>Details <span class="muted" style="font-weight:400;font-size:.8rem">— edit the canonical record</span></h3>
                        <label class="field">Title
                            <input type="text" id="art-edit-title" value="${this.esc(data.title || '')}" placeholder="Artwork title">
                        </label>
                        <label class="field">Description
                            <textarea id="art-edit-desc" rows="4" placeholder="Caption / description">${this.esc(data.description || '')}</textarea>
                        </label>
                        <label class="field">Alt text <span class="muted">(for screen readers; used on Bluesky)</span>
                            <input type="text" id="art-edit-alt" value="${this.esc(data.alt_text || '')}" placeholder="e.g. A grey wolf in a red jacket grins at the viewer">
                        </label>
                        <div class="field-row">
                            <label class="field">Rating
                                <select id="art-edit-rating">${ratingOpts}</select>
                            </label>
                        </div>
                        <label class="field">Tags <span class="muted">(comma-separated default)</span>
                            <textarea id="art-edit-tags" rows="2" placeholder="tag one, tag two">${this.esc(defaultTagsStr)}</textarea>
                        </label>
                        <div class="artwork-actions">
                            <button type="button" class="btn btn-sm" id="art-edit-tagbrowse">🏷️ Browse tag library</button>
                            <button type="button" class="btn btn-primary btn-sm" id="art-edit-save">Save changes</button>
                            <span id="art-edit-msg" class="muted"></span>
                        </div>
                    </div>
                    <div class="card">
                        <h3>Published</h3>
                        ${pubRows
                            ? `<table class="data-table"><thead><tr><th>Platform</th><th>Status</th><th>Stats</th><th></th></tr></thead><tbody>${pubRows}</tbody></table>`
                            : '<p class="muted">Not published anywhere yet.</p>'}
                    </div>
                    <div class="card">
                        <h3>Publish to more</h3>
                        <div id="art-detail-platforms"></div>
                        <div class="artwork-actions">
                            <button class="btn btn-primary" id="art-detail-publish">Publish now</button>
                            <button class="btn btn-outline" id="art-detail-schedule-toggle">&#128340; Schedule&hellip;</button>
                            <span id="art-detail-msg" class="muted"></span>
                        </div>
                        <div class="schedule-form" id="art-schedule-form" style="display:none">
                            <div class="schedule-form-inner">
                                <label class="schedule-label" for="art-schedule-datetime">Publish the ticked platforms at:</label>
                                <input type="datetime-local" class="schedule-datetime" id="art-schedule-datetime">
                                <div class="schedule-form-actions">
                                    <button class="btn btn-sm btn-primary" id="art-schedule-confirm">Confirm schedule</button>
                                    <button class="btn btn-sm btn-outline" id="art-schedule-cancel">Cancel</button>
                                </div>
                            </div>
                        </div>
                        <div class="schedule-pending" id="art-scheduled-list"></div>
                    </div>
                </div>
            </div>`;

        // A compact platform picker for "publish to more", excluding already-posted.
        const posted = new Set((data.publications || [])
            .filter(p => p.status === 'posted').map(p => p.platform));
        this._renderPlatformRows(document.getElementById('art-detail-platforms'));
        // Pre-disable already-posted platforms.
        document.querySelectorAll('#art-detail-platforms .art-plat-row').forEach(row => {
            if (posted.has(row.dataset.platform)) {
                row.style.opacity = '.5';
                const cb = row.querySelector('.art-plat-check');
                if (cb) { cb.disabled = true; cb.title = 'Already posted'; }
            }
        });
        await this._populateAccountSelectors();

        document.getElementById('art-delete').addEventListener('click', () => this._delete(name));
        // Variant strip: swap the main image (2.190.0).
        document.querySelectorAll('.artwork-detail-alts .artwork-alt').forEach(btn =>
            btn.addEventListener('click', () => {
                const main = document.getElementById('art-detail-img');
                if (main) main.src = btn.dataset.artAlt;
                document.querySelectorAll('.artwork-detail-alts .artwork-alt')
                    .forEach(b => b.classList.toggle('is-active', b === btn));
            }));
        document.getElementById('art-detail-publish').addEventListener('click', () => this._publishMore(name));
        document.getElementById('art-edit-save').addEventListener('click', () => this._saveMeta(name, data));
        document.getElementById('art-edit-tagbrowse').addEventListener('click', () => this._openTagLibrary('art-edit-tags'));

        // Scheduling: toggle the picker, confirm, and list what's already queued.
        const schedForm = document.getElementById('art-schedule-form');
        const schedInput = document.getElementById('art-schedule-datetime');
        document.getElementById('art-detail-schedule-toggle').addEventListener('click', () => {
            const showing = schedForm.style.display !== 'none';
            schedForm.style.display = showing ? 'none' : '';
            if (!showing && !schedInput.value) schedInput.value = this._defaultScheduleLocal();
        });
        document.getElementById('art-schedule-cancel').addEventListener('click', () => {
            schedForm.style.display = 'none';
        });
        document.getElementById('art-schedule-confirm').addEventListener('click', () => this._confirmSchedule(name));
        this._loadArtScheduled(name);
    },

    /* datetime-local wants 'YYYY-MM-DDTHH:MM' in LOCAL time. Default to one
     * hour out so the picker opens on a sane, in-the-future value. */
    _defaultScheduleLocal() {
        // Persona preferred posting time (gap-wave-3 §1): tomorrow at HH:MM.
        const opt = this._qpState && this._qpState.options
            && this._qpState.options.find(o => o.id === this._qpState.presetId);
        const t = opt && opt.defaults && opt.defaults.time;
        if (t && /^\d{2}:\d{2}$/.test(t)) {
            const d = new Date();
            d.setDate(d.getDate() + 1);
            d.setHours(parseInt(t.slice(0, 2), 10), parseInt(t.slice(3), 10), 0, 0);
            const pad = n => String(n).padStart(2, '0');
            return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
                + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
        }
        const d = new Date(Date.now() + 60 * 60 * 1000);
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
            `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },

    /* Schedule the ticked (not-yet-posted) platforms for a future time. One
     * request per platform — the immediate publish fans out the same way. The
     * datetime-local value is LOCAL; toISOString() hands the backend a UTC
     * instant, so a schedule set at 8pm AEST fires at 8pm AEST. */
    async _confirmSchedule(name) {
        const msg = document.getElementById('art-detail-msg');
        const val = document.getElementById('art-schedule-datetime').value;
        if (!val) { msg.textContent = 'Pick a date and time.'; return; }
        const when = new Date(val);
        if (isNaN(when.getTime())) { msg.textContent = 'Invalid date/time.'; return; }
        if (when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }

        const checked = Array.from(document.querySelectorAll('#art-detail-platforms .art-plat-check:checked'))
            .map(c => c.value);
        if (!checked.length) { msg.textContent = 'Tick at least one platform.'; return; }
        const accountIds = {};
        document.querySelectorAll('#art-detail-platforms .art-acct-select').forEach(sel => {
            if (checked.includes(sel.dataset.platform)) accountIds[sel.dataset.platform] = parseInt(sel.value, 10);
        });

        const isoStr = when.toISOString();
        msg.textContent = 'Scheduling…';
        let ok = 0, fail = 0;
        for (const platform of checked) {
            try {
                await API.scheduleArtwork({
                    artwork_name: name, platform, scheduled_at: isoStr,
                    account_id: accountIds[platform],
                });
                ok++;
            } catch (err) {
                fail++;
                console.warn('Schedule failed for', platform, err);
            }
        }
        this._toast(fail ? 'error' : 'success',
            `Scheduled ${ok} platform${ok === 1 ? '' : 's'} for ${when.toLocaleString()}` +
            (fail ? `, ${fail} failed` : ''));
        document.getElementById('art-schedule-form').style.display = 'none';
        msg.textContent = '';
        this._loadArtScheduled(name);
    },

    async _loadArtScheduled(name) {
        const box = document.getElementById('art-scheduled-list');
        if (!box) return;
        let items = [];
        try {
            const resp = await API.getArtworkScheduled(name);
            items = (resp.items || []).filter(i => i.status === 'pending' && i.scheduled_at);
        } catch { return; }
        if (!items.length) { box.innerHTML = ''; return; }
        items.sort((a, b) => (a.scheduled_at || '').localeCompare(b.scheduled_at || ''));
        let html = '<div class="schedule-pending-header">Scheduled</div>';
        for (const it of items) {
            // Stored 'YYYY-MM-DD HH:MM:SS' is UTC; make it a real instant then localise.
            const when = new Date(it.scheduled_at.replace(' ', 'T') + 'Z').toLocaleString();
            const plat = (window.PLATFORMS || []).find(p => p.code === it.platform);
            html += '<div class="schedule-pending-item">' +
                '<span class="schedule-pending-icon">&#128340;</span> ' +
                this.esc(plat ? plat.name : it.platform) + ' &mdash; ' + this.esc(when) +
                ' <button class="btn btn-xs btn-outline" data-art-sched-cancel="' + it.queue_id + '">Cancel</button>' +
                '</div>';
        }
        box.innerHTML = html;
        box.querySelectorAll('[data-art-sched-cancel]').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    await API.cancelArtworkScheduled(name, parseInt(btn.dataset.artSchedCancel, 10));
                    this._loadArtScheduled(name);
                } catch (err) {
                    this._toast('error', 'Cancel failed: ' + err.message);
                }
            });
        });
    },

    /* Save canonical metadata edits (rating / title / description / tags) on a
     * standalone artwork. Merges the new default tags into the existing per-
     * platform tag dict so overrides survive, then PATCHes /images/{name}. This
     * updates the local record only — use "Sync" on a Masterpiece to push edits
     * out to already-published sites. */
    async _saveMeta(name, data) {
        const msg = document.getElementById('art-edit-msg');
        const btn = document.getElementById('art-edit-save');
        const title = document.getElementById('art-edit-title').value.trim();
        if (!title) { msg.textContent = 'Enter a title.'; return; }
        const tags = { ...(data.tags || {}) };
        const def = this._parseTags(document.getElementById('art-edit-tags').value);
        if (def.length) tags.default = def; else delete tags.default;
        const updates = {
            title,
            description: document.getElementById('art-edit-desc').value,
            rating: document.getElementById('art-edit-rating').value,
            alt_text: document.getElementById('art-edit-alt')?.value.trim() || '',
            tags,
        };
        btn.disabled = true;
        msg.textContent = 'Saving…';
        try {
            await API.updateArtwork(name, updates);
            this._toast('success', 'Saved');
            this.renderDetail(name);
        } catch (err) {
            msg.textContent = 'Save failed: ' + err.message;
            btn.disabled = false;
        }
    },

    async _publishMore(name) {
        const msg = document.getElementById('art-detail-msg');
        const checked = Array.from(document.querySelectorAll('#art-detail-platforms .art-plat-check:checked'))
            .map(c => c.value);
        if (!checked.length) { msg.textContent = 'Tick at least one platform.'; return; }
        const accountIds = {};
        document.querySelectorAll('#art-detail-platforms .art-acct-select').forEach(sel => {
            if (checked.includes(sel.dataset.platform)) accountIds[sel.dataset.platform] = parseInt(sel.value, 10);
        });
        msg.textContent = 'Publishing…';
        try {
            const res = await API.publishArtwork({ artwork_name: name, platforms: checked, account_ids: accountIds });
            const ok = res.successes || 0, fail = res.failures || 0;
            this._toast(fail ? 'error' : 'success', `Published: ${ok} ok, ${fail} failed`);
            this.renderDetail(name);
        } catch (err) {
            msg.textContent = 'Publish failed: ' + err.message;
        }
    },

    async _delete(name) {
        if (!confirm('Delete this artwork from your library? Any already-published posts stay live on each platform.')) return;
        try {
            await API.deleteArtwork(name);
            this._toast('success', 'Deleted');
            window.location.hash = '#/library/type/artwork';
        } catch (err) {
            this._toast('error', 'Delete failed: ' + err.message);
        }
    },

    /* ── Log ────────────────────────────────────────────────── */

    async renderLog() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>Artwork history</h1>
                <p class="muted"><a href="#/library/type/artwork">← Back to Library</a></p>
            </div>
            <div id="art-log"><div class="loading-spinner">Loading…</div></div>`;
        let data;
        try {
            data = await API.getArtworkLog();
        } catch (err) {
            document.getElementById('art-log').innerHTML =
                `<div class="card error">Failed to load: ${this.esc(err.message)}</div>`;
            return;
        }
        const rows = (data.log || []).map(e => `
            <tr>
                <td>${this.esc(e.created_at || '')}</td>
                <td>${this._plat(e.platform).emoji || ''} ${this.esc(this._plat(e.platform).label)}</td>
                <td>${this.esc(e.story_name || '')}</td>
                <td>${this.esc(e.action || '')}</td>
                <td><span class="artwork-status artwork-status--${this.esc(e.status)}">${this.esc(e.status)}</span></td>
                <td class="muted">${this.esc(e.error_message || '')}</td>
            </tr>`).join('');
        document.getElementById('art-log').innerHTML = rows
            ? `<table class="data-table"><thead><tr><th>When</th><th>Platform</th><th>Artwork</th><th>Action</th><th>Status</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`
            : '<div class="empty-state"><p class="muted">No artwork posts yet.</p></div>';
    },

    /* Manage the Ignore list — the discovered tiles the user hid from the hub.
     * Each row can be restored (un-ignored), which brings it back to the hub on
     * the next load. Keeps Ignore from being a one-way trap. */
    async renderIgnored() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>Ignored artwork</h1>
                <p class="muted"><a href="#/library/type/artwork">← Back to Library</a> · Hidden discovered tiles. Restore any to
                bring it back to the hub.</p>
            </div>
            <div id="art-ignored"><div class="loading-spinner">Loading…</div></div>`;
        const wrap = document.getElementById('art-ignored');
        let data;
        try {
            data = await API.getIgnoredDiscovered();
        } catch (err) {
            wrap.innerHTML = `<div class="card error">Failed to load: ${this.esc(err.message)}</div>`;
            return;
        }
        const items = data.ignored || [];
        if (!items.length) {
            wrap.innerHTML = '<div class="empty-state"><p class="muted">Nothing ignored. '
                + 'Use 🚫 Ignore on a discovered tile to hide art you don\'t want here.</p></div>';
            return;
        }
        const rows = items.map(e => `
            <tr>
                <td>${this._plat(e.platform).emoji || ''} ${this.esc(this._plat(e.platform).label)}</td>
                <td class="muted">#${this.esc(e.submission_id)}</td>
                <td class="muted">${this.esc(e.ignored_at || '')}</td>
                <td><button class="btn btn-sm art-unignore-btn"
                    data-platform="${this.esc(e.platform)}" data-sid="${this.esc(e.submission_id)}">↩ Restore</button></td>
            </tr>`).join('');
        wrap.innerHTML = `<table class="data-table"><thead><tr><th>Platform</th><th>Submission</th>`
            + `<th>Ignored</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
        wrap.addEventListener('click', async e => {
            const btn = e.target.closest('.art-unignore-btn');
            if (!btn) return;
            btn.disabled = true;
            try {
                await API.unignoreDiscovered(btn.dataset.platform, btn.dataset.sid);
                const tr = btn.closest('tr');
                if (tr) tr.remove();
                this._toast('success', 'Restored to the hub');
                if (!wrap.querySelector('tbody tr')) this.renderIgnored();
            } catch (err) {
                btn.disabled = false;
                this._toast('error', 'Restore failed: ' + err.message);
            }
        });
    },

    /* ── Quick Publish (#/artwork/quick) ─────────────────────────
     * The 80% case on one screen: drop an image, pick a persona, publish.
     * A persona IS the preset — its accounts define which art sites to post to
     * and as which account. Rating + tags (and any platforms you switch off) are
     * remembered per persona in localStorage, so the second time you pick a
     * persona it comes back configured. Reuses the same upload + publish
     * endpoints as the full form; the full form (#/artwork/new) stays the escape
     * hatch for per-platform overrides.
     */

    _qpState: null,   // { map: {presetId: {platform: account_id}}, personas, presetId }

    _qpPresetKey(id) { return 'pp-quickpub-preset:' + id; },

    _qpLoadPreset(id) {
        try { return JSON.parse(localStorage.getItem(this._qpPresetKey(id))) || {}; }
        catch (e) { return {}; }
    },
    _qpSavePreset(id, data) {
        try { localStorage.setItem(this._qpPresetKey(id), JSON.stringify(data)); } catch (e) { /* quota */ }
    },

    /* Group enabled art accounts into per-persona presets + an "All accounts"
     * catch-all. Each preset maps a platform → the account to post as (the
     * persona's default account on that platform, else its first). */
    _qpBuildMap(accounts) {
        const artset = new Set(this._PLATFORMS);
        const enabled = (accounts || []).filter(a => a.enabled && artset.has(a.platform));
        const per = {};        // presetId -> {platform: account_id}
        const all = {};
        const put = (bag, a) => {
            if (!(a.platform in bag) || a.is_default) bag[a.platform] = a.account_id;
        };
        for (const a of enabled) {
            const pid = a.persona_id ? ('p' + a.persona_id) : null;
            if (pid) { (per[pid] = per[pid] || {}); put(per[pid], a); }
            put(all, a);
        }
        return { per, all };
    },

    async renderQuick() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>⚡ Quick publish</h1>
                <p class="muted">Drop an image, pick who's posting, go. Need per-site tweaks?
                    <a href="#/artwork/new">Use the full form</a>.</p>
            </div>
            <div class="qp-wrap" style="max-width:640px;display:flex;flex-direction:column;gap:1rem;">
                <div class="card">
                    <div id="qp-drop" class="artwork-drop" tabindex="0">
                        <img id="qp-preview" hidden style="max-width:100%;max-height:340px;border-radius:8px;">
                        <div id="qp-drop-inner" class="artwork-drop-inner">
                            <div class="artwork-drop-ico">🖼️</div>
                            <div>Drop an image here or <label for="qp-file" style="text-decoration:underline;cursor:pointer;color:var(--accent);">choose a file</label>
                                ${this._isDesktop() ? '· <button type="button" class="btn btn-sm" id="qp-pick-local">Pick from computer</button>' : ''}</div>
                            <div class="artwork-drop-hint muted">PNG, JPG, GIF or WebP</div>
                        </div>
                        <input type="file" id="qp-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
                    </div>
                    <button type="button" class="btn btn-sm" id="qp-remove" hidden style="margin-top:.5rem;">Remove image</button>
                    <label class="field" style="margin-top:.6rem;">Title
                        <input type="text" id="qp-title" placeholder="(defaults to the file name)">
                    </label>
                </div>

                <div class="card">
                    <h3 style="margin-top:0;">Publish as</h3>
                    <div id="qp-personas" class="qp-personas" style="display:flex;flex-wrap:wrap;gap:.4rem;"></div>
                    <div style="margin-top:.7rem;">
                        <div class="muted" style="font-size:.85rem;margin-bottom:.3rem;">Going to (tap to toggle):</div>
                        <div id="qp-platforms" class="qp-platforms" style="display:flex;flex-wrap:wrap;gap:.4rem;"></div>
                    </div>
                    <div class="field-row" style="margin-top:.7rem;display:flex;gap:.8rem;flex-wrap:wrap;">
                        <label class="field" style="flex:0 0 auto;">Rating
                            <select id="qp-rating">
                                <option value="general">General</option>
                                <option value="mature">Mature</option>
                                <option value="adult" selected>Adult</option>
                            </select>
                        </label>
                        <label class="field" style="flex:1 1 200px;">Tags <span class="muted">(comma-separated)</span>
                            <textarea id="qp-tags" rows="2" placeholder="tag one, tag two"></textarea>
                        </label>
                    </div>
                    <button type="button" class="btn btn-sm" id="qp-tag-browse">🏷️ Browse tag library</button>
                </div>

                <div style="display:flex;align-items:center;gap:.8rem;">
                    <button class="btn btn-primary" id="qp-go" style="padding:.6rem 1.4rem;font-size:1.02rem;">Publish now</button>
                    <button class="btn btn-outline" id="qp-schedule">🕐 Schedule…</button>
                    <span id="qp-msg" class="muted"></span>
                </div>
                <div class="schedule-form" id="qp-schedule-form" style="display:none">
                    <div class="schedule-form-inner">
                        <label class="schedule-label" for="qp-schedule-dt">Publish at:</label>
                        <input type="datetime-local" class="schedule-datetime" id="qp-schedule-dt">
                        <div class="schedule-form-actions">
                            <button class="btn btn-sm btn-primary" id="qp-schedule-confirm">Confirm schedule</button>
                            <button class="btn btn-sm btn-outline" id="qp-schedule-cancel">Cancel</button>
                        </div>
                    </div>
                </div>
            </div>`;

        this._pendingFile = this._pendingPath = null;
        this._wireQuick();
        await this._loadQuickPresets();
    },

    _wireQuick() {
        const file = document.getElementById('qp-file');
        const drop = document.getElementById('qp-drop');
        file.addEventListener('change', () => {
            if (file.files && file.files[0]) this._qpSetFile(file.files[0]);
        });
        ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
            e.preventDefault(); drop.classList.add('dragover');
        }));
        ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
            e.preventDefault(); drop.classList.remove('dragover');
        }));
        drop.addEventListener('drop', e => {
            const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) this._qpSetFile(f);
        });
        const local = document.getElementById('qp-pick-local');
        if (local) local.addEventListener('click', () => this._qpPickDesktop());
        document.getElementById('qp-remove').addEventListener('click', () => this._qpClearFile());
        document.getElementById('qp-tag-browse').addEventListener('click', () => this._openTagLibrary('qp-tags'));
        document.getElementById('qp-go').addEventListener('click', () => this._qpPublish(null));

        const sform = document.getElementById('qp-schedule-form');
        const sdt = document.getElementById('qp-schedule-dt');
        document.getElementById('qp-schedule').addEventListener('click', () => {
            const showing = sform.style.display !== 'none';
            sform.style.display = showing ? 'none' : '';
            if (!showing && !sdt.value) sdt.value = this._defaultScheduleLocal();
        });
        document.getElementById('qp-schedule-cancel').addEventListener('click', () => { sform.style.display = 'none'; });
        document.getElementById('qp-schedule-confirm').addEventListener('click', () => this._qpPublish(sdt.value));

        const title = document.getElementById('qp-title');
        title.dataset.touched = '';
        title.addEventListener('input', () => { title.dataset.touched = '1'; });
    },

    _qpSetFile(f) {
        if (!/\.(png|jpe?g|gif|webp)$/i.test(f.name)) {
            this._toast('error', 'Please choose a PNG, JPG, GIF or WebP image.'); return;
        }
        this._pendingFile = f; this._pendingPath = null;
        if (this._previewUrl) URL.revokeObjectURL(this._previewUrl);
        this._previewUrl = URL.createObjectURL(f);
        this._qpShowPreview(this._previewUrl, f.name);
    },
    async _qpPickDesktop() {
        try {
            const r = await window.pywebview.api.open_image_dialog();
            const path = Array.isArray(r) ? r[0] : r;
            if (!path) return;
            this._pendingPath = path; this._pendingFile = null;
            this._qpShowPreview('', String(path).split(/[\\/]/).pop());
        } catch (e) { this._toast('error', 'File dialog failed: ' + (e.message || e)); }
    },
    _qpShowPreview(url, label) {
        const img = document.getElementById('qp-preview');
        const inner = document.getElementById('qp-drop-inner');
        const title = document.getElementById('qp-title');
        if (title && !title.dataset.touched && !title.value) {
            title.value = (label || '').replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
        }
        if (url) { img.src = url; img.hidden = false; inner.style.display = 'none'; }
        else {
            img.hidden = true; inner.style.display = '';
            inner.querySelector('.artwork-drop-hint').textContent = 'Selected: ' + (label || 'file');
        }
        document.getElementById('qp-remove').hidden = false;
    },
    _qpClearFile() {
        this._pendingFile = this._pendingPath = null;
        if (this._previewUrl) { URL.revokeObjectURL(this._previewUrl); this._previewUrl = null; }
        const img = document.getElementById('qp-preview');
        const inner = document.getElementById('qp-drop-inner');
        if (img) { img.hidden = true; img.src = ''; }
        if (inner) {
            inner.style.display = '';
            const h = inner.querySelector('.artwork-drop-hint');
            if (h) h.textContent = 'PNG, JPG, GIF or WebP';
        }
        document.getElementById('qp-file').value = '';
        document.getElementById('qp-remove').hidden = true;
    },

    async _loadQuickPresets() {
        const chipBox = document.getElementById('qp-personas');
        let personas = [], accounts = [];
        try {
            const [pRes, aRes] = await Promise.all([API.getPersonas(), API.getAccounts()]);
            personas = (pRes && pRes.personas) || [];
            accounts = (aRes && aRes.accounts) || [];
        } catch (e) { /* fall through to empty */ }

        const { per, all } = this._qpBuildMap(accounts);
        // Presets to offer: each persona that has ≥1 art account, then "All accounts".
        const options = [];
        for (const p of personas) {
            const id = 'p' + p.persona_id;
            if (per[id] && Object.keys(per[id]).length) {
                options.push({
                    id, label: p.name, color: p.color || '#6c8cff', map: per[id],
                    // Server-side persona posting defaults (gap-wave-3 §1) —
                    // seed the preset when this browser has no saved one.
                    defaults: {
                        platforms: (p.default_platforms || '').split(',').filter(Boolean),
                        rating: p.default_rating || '',
                        time: p.preferred_post_time || '',
                    },
                });
            }
        }
        if (Object.keys(all).length) {
            options.push({ id: 'all', label: 'All accounts', color: '#888', map: all });
        }

        if (!options.length) {
            chipBox.innerHTML = `<p class="muted">No art accounts connected yet.
                <a href="#/accounts">Connect an account</a> to publish, or
                <a href="#/artwork/new">use the full form</a>.</p>`;
            document.getElementById('qp-go').disabled = true;
            document.getElementById('qp-schedule').disabled = true;
            return;
        }

        this._qpState = { options, presetId: null };
        chipBox.innerHTML = options.map(o =>
            `<button type="button" class="qp-persona-chip" data-preset="${o.id}"
                style="display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .7rem;border-radius:999px;
                border:1px solid var(--border);background:var(--surface);cursor:pointer;">
                <span style="width:10px;height:10px;border-radius:50%;background:${this.esc(o.color)};"></span>
                ${this.esc(o.label)}
                <span class="muted" style="font-size:.8rem;">${Object.keys(o.map).length}</span>
            </button>`).join('');
        chipBox.querySelectorAll('[data-preset]').forEach(btn =>
            btn.addEventListener('click', () => this._qpSelectPreset(btn.dataset.preset)));

        // Restore the last-used preset, else the first option.
        let last = null;
        try { last = localStorage.getItem('pp-quickpub-last'); } catch (e) { /* ignore */ }
        const start = options.find(o => o.id === last) ? last : options[0].id;
        this._qpSelectPreset(start);
    },

    _qpSelectPreset(presetId) {
        const st = this._qpState;
        const opt = st && st.options.find(o => o.id === presetId);
        if (!opt) return;
        st.presetId = presetId;

        document.querySelectorAll('#qp-personas .qp-persona-chip').forEach(c => {
            const on = c.dataset.preset === presetId;
            c.style.borderColor = on ? 'var(--accent)' : 'var(--border)';
            c.style.background = on ? 'color-mix(in srgb, var(--accent) 16%, var(--surface))' : 'var(--surface)';
            c.style.fontWeight = on ? '600' : '400';
        });

        const saved = this._qpLoadPreset(presetId);
        const pdef = opt.defaults || {};
        // localStorage (this browser's memory) wins; the persona's synced
        // server-side defaults seed a fresh browser (gap-wave-3 §1).
        const rating = saved.rating || pdef.rating;
        if (rating) document.getElementById('qp-rating').value = rating;
        if (typeof saved.tags === 'string') document.getElementById('qp-tags').value = saved.tags;
        let off = new Set(saved.off || []);
        if (!saved.off && (pdef.platforms || []).length) {
            // No browser preset: default ON = the persona's chosen platforms.
            off = new Set(Object.keys(opt.map).filter(c => !pdef.platforms.includes(c)));
        }

        // Platform toggle chips = this preset's platforms, in the hub's display order.
        const codes = this._PLATFORMS.filter(c => c in opt.map);
        const box = document.getElementById('qp-platforms');
        box.innerHTML = codes.map(code => {
            const p = this._plat(code);
            const on = !off.has(code);
            return `<button type="button" class="qp-plat-chip" data-plat="${code}" data-on="${on ? '1' : '0'}"
                style="display:inline-flex;align-items:center;gap:.35rem;padding:.3rem .65rem;border-radius:999px;
                border:1px solid ${on ? 'var(--accent)' : 'var(--border)'};
                background:${on ? 'color-mix(in srgb, var(--accent) 16%, var(--surface))' : 'var(--surface)'};
                opacity:${on ? '1' : '.5'};cursor:pointer;">
                ${p.emoji || ''} ${this.esc(p.label)}</button>`;
        }).join('');
        box.querySelectorAll('[data-plat]').forEach(chip =>
            chip.addEventListener('click', () => {
                const on = chip.dataset.on !== '1';
                chip.dataset.on = on ? '1' : '0';
                chip.style.borderColor = on ? 'var(--accent)' : 'var(--border)';
                chip.style.background = on ? 'color-mix(in srgb, var(--accent) 16%, var(--surface))' : 'var(--surface)';
                chip.style.opacity = on ? '1' : '.5';
            }));
    },

    _qpCheckedPlatforms() {
        return Array.from(document.querySelectorAll('#qp-platforms .qp-plat-chip'))
            .filter(c => c.dataset.on === '1').map(c => c.dataset.plat);
    },

    /* Publish (or schedule when a datetime-local value is passed). Reuses the
     * artwork upload + publish/schedule endpoints — the persona's map supplies
     * the per-platform account. */
    async _qpPublish(scheduledLocal) {
        const msg = document.getElementById('qp-msg');
        const st = this._qpState;
        if (!st || !st.presetId) { msg.textContent = 'Pick who’s posting.'; return; }
        if (!this._pendingFile && !this._pendingPath) { msg.textContent = 'Choose an image first.'; return; }
        const platforms = this._qpCheckedPlatforms();
        if (!platforms.length) { msg.textContent = 'Keep at least one site ticked.'; return; }

        let scheduledIso = null;
        if (scheduledLocal) {
            const when = new Date(scheduledLocal);
            if (isNaN(when.getTime())) { msg.textContent = 'Invalid date/time.'; return; }
            if (when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }
            scheduledIso = when.toISOString();
        }

        const opt = st.options.find(o => o.id === st.presetId);
        const rating = document.getElementById('qp-rating').value;
        const tagsRaw = document.getElementById('qp-tags').value;
        const tags = this._parseTags(tagsRaw);
        // Always send a real title — fall back to the file name so an emptied
        // field can't create an untitled folder.
        let title = (document.getElementById('qp-title').value || '').trim();
        if (!title) {
            const fname = this._pendingFile ? this._pendingFile.name
                : (this._pendingPath ? String(this._pendingPath).split(/[\\/]/).pop() : '');
            title = (fname || '').replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim() || 'Untitled';
        }
        const meta = {
            title, description: '', rating,
            tags: tags.length ? { default: tags } : {}, platforms,
        };
        const accountIds = {};
        for (const code of platforms) accountIds[code] = opt.map[code];

        const go = document.getElementById('qp-go');
        const sch = document.getElementById('qp-schedule');
        go.disabled = sch.disabled = true;
        msg.textContent = scheduledIso ? 'Scheduling…' : 'Publishing…';

        let name;
        try {
            if (this._pendingPath) {
                name = (await API.createArtworkFromPath({ path: this._pendingPath, metadata: meta })).name;
            } else {
                name = (await API.uploadArtwork(this._pendingFile, meta, null,
                    pct => { msg.textContent = `Uploading… ${pct}%`; })).name;
            }
        } catch (err) {
            msg.textContent = 'Upload failed: ' + err.message;
            go.disabled = sch.disabled = false;
            return;
        }

        // Remember this preset's choices + that it was the last one used.
        this._qpSavePreset(st.presetId, {
            rating, tags: tagsRaw,
            off: this._PLATFORMS.filter(c => (c in opt.map) && !platforms.includes(c)),
        });
        try { localStorage.setItem('pp-quickpub-last', st.presetId); } catch (e) { /* ignore */ }

        try {
            if (scheduledIso) {
                let ok = 0, fail = 0;
                for (const code of platforms) {
                    try {
                        await API.scheduleArtwork({ artwork_name: name, platform: code,
                            scheduled_at: scheduledIso, account_id: accountIds[code] });
                        ok++;
                    } catch (e) { fail++; }
                }
                this._toast(fail ? 'error' : 'success',
                    `Scheduled ${ok} site${ok === 1 ? '' : 's'}` + (fail ? `, ${fail} failed` : ''));
            } else {
                const res = await API.publishArtwork({ artwork_name: name, platforms, account_ids: accountIds });
                const ok = res.successes || 0, fail = res.failures || 0;
                this._toast(fail ? 'error' : 'success', `Published: ${ok} ok, ${fail} failed`);
            }
            window.location.hash = `#/artwork/image/${encodeURIComponent(name)}`;
        } catch (err) {
            msg.textContent = 'Saved, but publish failed: ' + err.message;
            go.disabled = sch.disabled = false;
        }
    },
};
