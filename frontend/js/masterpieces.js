/* ── Masterpieces — the managed master-record-per-image surface (Phase 2) ──────
 *
 * A Masterpiece is the image analog of a story's MASTER.md: one canonical image
 * + masterpiece.json, and (Phase 1) a membership table linking every site-upload
 * of that image so their stats pool. See docs/specs/masterpieces.md.
 *
 *   - renderGrid(gridEl, filters)  — the managed grid, shown inside Library under
 *                                    the "Masterpieces" segment (bookshelf.js).
 *   - renderDetail(name)           — the #/masterpieces/{name} detail view.
 *
 * Phase 3 adds membership management to the detail view: same-image **suggestions**
 * (native perceptual-hash, no AI) with one-click **attach**, and **detach** on each
 * linked location. Editing the canonical metadata + Sync-all still land in Phase 5.
 * Rendering mirrors collections.js (template strings + a document-level click
 * delegate, CSP-safe — no inline handlers) and reuses Charts.aggregateLine.
 */
window.Masterpieces = {
    _personas: {},          // persona_id -> {name, color}
    _personasLoaded: false,
    _cache: null,           // [] of masterpiece list rows, per Library session
    _current: null,         // name of the Masterpiece the detail view is showing
    _wired: false,          // document click delegate attached once
    // Platforms whose poster can't edit in place (supports_edit=False, mirrors the
    // backend) — Sync skips them; they render "post-only" in the Locations table.
    // 'da' is here for ARTWORK only — DeviantArt edits literature fine, but its
    // API has no image-deviation update. This table is artwork-only, so the
    // badge is correct here and must NOT be copied to the story matrix.
    _POST_ONLY: new Set(['bsky', 'ig', 'fn', 'da', 'tg', 'tw']),

    /* Drop the list cache so the next grid render refetches (called on each
       Library open by bookshelf.render). Also leaves the junk-bin view, so a
       fresh Library visit always starts on the normal grid. */
    resetCache() { this._cache = null; this._junkView = false; },

    /* ── small shared helpers (same shape as collections.js) ── */
    esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _fmt(n) {
        if (n == null) return '—';   // platform doesn't track this metric
        return (window.Utils && Utils.formatNumber) ? Utils.formatNumber(n) : String(n);
    },
    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '', color: '#888' };
    },
    /* Route platform thumbnails through the backend relays (FA/IB/Pixiv); others
       are hotlinkable. Identical to collections.js._thumbSrc / artwork.js. */
    _thumbSrc(platform, url) {
        if (!url) return '';
        if (platform === 'fa' && Utils.faThumbUrl) return Utils.faThumbUrl(url);
        if (platform === 'ib' && Utils.thumbUrl) return Utils.thumbUrl(url);
        if (platform === 'pix' && Utils.pixThumbUrl) return Utils.pixThumbUrl(url);
        return url;
    },
    /* The canonical local image is served from the artwork archive by name+file. */
    _canonUrl(name, file) {
        if (!file) return '';
        return `/api/artwork/image?name=${encodeURIComponent(name)}&file=${encodeURIComponent(file)}`;
    },

    async _loadPersonas() {
        if (this._personasLoaded) return;
        try {
            const d = await API.getPersonas();
            const arr = Array.isArray(d) ? d : ((d && d.personas) || []);
            // Keyed on persona_id, NOT id: /api/personas returns rows straight from
            // the table (PK persona_id). `p.id` was undefined, so every persona
            // landed under the key "undefined" and the chips never rendered.
            // bookshelf.js legitimately uses p.id — it reads /api/works, which
            // re-keys to {id, name, color}. Two endpoints, two shapes.
            arr.forEach(p => { this._personas[p.persona_id] = { name: p.name, color: p.color || 'var(--accent)' }; });
        } catch { /* personas are decorative here — never block the view */ }
        this._personasLoaded = true;
    },
    _personaChips(ids, cls) {
        return (ids || []).map(id => {
            const p = this._personas[id];
            if (!p) return '';
            return `<span class="mp-persona" title="${this.esc(p.name)}"><span class="mp-persona-dot" `
                + `style="background:${this.esc(p.color)}"></span>${this.esc(p.name)}</span>`;
        }).join('');
    },

    /* ── Grid (rendered into Library's #shelf-grid) ── */

    _junkView: false,       // grid shows junked pieces instead of active ones
    _lastGrid: null,         // {el, filters} so the Junk toggle can re-render

    async renderGrid(gridEl, filters) {
        if (!gridEl) return;
        // Tear down the previous window's scroll observer before re-rendering
        // (filter change / junk toggle re-enters here).
        if (this._gridObserver) { this._gridObserver.disconnect(); this._gridObserver = null; }
        filters = filters || {};
        this._lastGrid = { el: gridEl, filters };
        await this._loadPersonas();
        if (this._cache === null) {
            gridEl.className = '';
            gridEl.innerHTML = `<div class="loading-spinner">Loading your masterpieces…</div>`;
            try {
                const d = await API.getMasterpieces();
                this._cache = (d && d.masterpieces) || [];
            } catch (err) {
                gridEl.className = '';
                gridEl.innerHTML = `<div class="card error">Couldn't load masterpieces: ${this.esc(err.message)}</div>`;
                return;
            }
        }

        // Junk split (2.149.0): junked pieces are kept but live behind the Junk view.
        const junked = this._cache.filter(m => m.status === 'junk');
        let list = (this._junkView ? junked : this._cache.filter(m => m.status !== 'junk')).slice();
        const persona = filters.persona || 0;
        const q = (filters.search || '').toLowerCase();
        const sort = filters.sort || 'recent';
        if (persona) list = list.filter(m => ((m.summary && m.summary.persona_ids) || []).includes(persona));
        if (q) list = list.filter(m => (m.title || '').toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q));
        if (sort === 'title') list.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
        else if (sort === 'platforms') list.sort((a, b) =>
            (((b.summary && b.summary.platforms) || []).length) - (((a.summary && a.summary.platforms) || []).length));
        else list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));

        const newBtn = `<a class="btn btn-primary btn-sm" href="#/artwork/new"
            title="Upload a new image, describe it once, and publish it across sites">＋ New Masterpiece</a>`;
        const dupBtn = `<a class="btn btn-sm" href="#/masterpieces/duplicates"
            title="Find Masterpieces of the same image and merge them into one">🔍 Find duplicates</a>`;
        // The Junk toggle appears once anything is junked (or while viewing the bin).
        const junkBtn = (junked.length || this._junkView)
            ? `<button class="btn btn-sm${this._junkView ? ' btn-primary' : ''}" data-mp-junkview type="button"
                title="Pulled art you've binned — kept on disk, hidden from the grid, restorable">
                🗑 Junk (${junked.length})</button>` : '';
        const junkBanner = this._junkView
            ? `<div class="card muted" style="margin:.4rem 0 .8rem;padding:.5rem .8rem">Showing the junk bin —
                these stay on disk and keep their site-links, they're just hidden from the grid.
                <strong>♻ Restore</strong> puts one back.</div>` : '';
        const bar = `<div class="mp-gridbar">${newBtn}${dupBtn}${junkBtn}</div>${junkBanner}`;
        gridEl.className = '';
        if (!list.length) {
            gridEl.innerHTML = `${bar}
                <div class="empty-state"><h3>${this._junkView ? 'The junk bin is empty' : 'No masterpieces yet'}</h3>
                <p class="muted">${this._junkView
                    ? 'Nothing junked. Use 🗑 Junk on a masterpiece’s page to move it here.'
                    : 'Every artwork folder is a masterpiece. Create one, or promote a gallery image (★ Master) to link its copies across sites and pool their stats.'}</p></div>`;
        } else {
            // Windowed render (perf guardrail): only the first page of cards goes
            // into the DOM; the rest stream in as you scroll. Keeps a 1000s-piece
            // library from building thousands of image nodes up front. The data is
            // already fully fetched + filtered above — this only paces the DOM.
            gridEl.innerHTML = `${bar}
                <div class="mp-grid"></div>
                <div class="mp-grid-sentinel" aria-hidden="true" style="height:1px"></div>`;
            this._windowInto(gridEl.querySelector('.mp-grid'),
                             gridEl.querySelector('.mp-grid-sentinel'), list);
        }
        this._wireGridBar(gridEl);
    },

    /* Stream `list` into `grid` a page at a time, appending the next page when
     * `sentinel` nears the viewport. Renders the first page synchronously so the
     * grid is never empty. */
    _windowInto(grid, sentinel, list) {
        const PAGE = 60;
        let i = 0;
        const renderNext = () => {
            const slice = list.slice(i, i + PAGE);
            if (slice.length) {
                grid.insertAdjacentHTML('beforeend', slice.map(m => this._card(m)).join(''));
                i += slice.length;
            }
            if (i >= list.length) {
                if (this._gridObserver) { this._gridObserver.disconnect(); this._gridObserver = null; }
                if (sentinel) sentinel.remove();
            }
        };
        renderNext();                                   // first page, synchronously
        if (i < list.length && 'IntersectionObserver' in window) {
            this._gridObserver = new IntersectionObserver(entries => {
                if (entries.some(e => e.isIntersecting)) renderNext();
            }, { rootMargin: '600px' });                // prefetch before it's visible
            this._gridObserver.observe(sentinel);
        } else if (i < list.length) {
            // No IntersectionObserver (very old browser) — render the rest now.
            while (i < list.length) renderNext();
        }
    },

    _wireGridBar(gridEl) {
        const toggle = gridEl.querySelector('[data-mp-junkview]');
        if (toggle) toggle.addEventListener('click', () => {
            this._junkView = !this._junkView;
            const g = this._lastGrid || {};
            this.renderGrid(g.el || gridEl, g.filters);
        });
        // Restore is delegated (once per grid element) so cards streamed in later
        // by _windowInto still get it. gridEl persists across re-renders, hence the
        // guard against stacking listeners.
        if (!gridEl.dataset.mpRestoreWired) {
            gridEl.dataset.mpRestoreWired = '1';
            gridEl.addEventListener('click', async (e) => {
                const btn = e.target.closest('[data-mp-restore]');
                if (!btn || !gridEl.contains(btn)) return;
                e.preventDefault(); e.stopPropagation();   // card is an <a> — don't navigate
                btn.disabled = true;
                try {
                    await API.setMasterpieceStatus(btn.dataset.name, '');
                    this._toast('success', 'Restored to the grid');
                    this._cache = null;
                    const g = this._lastGrid || {};
                    this.renderGrid(g.el || gridEl, g.filters);
                } catch (err) {
                    btn.disabled = false;
                    this._toast('error', 'Restore failed: ' + (err.message || err));
                }
            });
        }
    },

    /* ── Duplicate finder / merge (2.144.0) ─────────────────────
     * The same image can become two separate Masterpieces (imported as two
     * folders). This scans hero images by perceptual hash, groups look-alikes,
     * and lets the user merge each group into one survivor (folding the others'
     * site-links in and deleting the redundant records). */
    async renderDuplicates() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>Tidy up Masterpieces</h1>
                <p class="muted"><a href="#/masterpieces">← Back to Masterpieces</a> · Two ways your library ends up
                with more cards than pieces — the same image posted to several sites, and the same piece in different
                renders (rough/final, SFW/NSFW). Review each below and fold them into one.</p>
            </div>

            <h2 class="mp-sec-h">Same piece, different renders <span class="muted mp-sec-sub">grouped by title</span></h2>
            <p class="muted mp-sec-note">A rough sketch and the finished colour aren't the same <em>image</em>, so the
            duplicate scan can't catch them — but the titles line up. Folding one in keeps every image as a labeled
            variant, each with its own stats.</p>
            <div id="mp-variants"><div class="loading-spinner">Looking for variant families…</div></div>

            <h2 class="mp-sec-h" style="margin-top:2rem">Same image, more than one Masterpiece <span class="muted mp-sec-sub">by image match</span></h2>
            <p class="muted mp-sec-note">Pick the one to keep and merge the rest into it — their site-links move over
            and the duplicate record is removed (the image is identical, so nothing is lost).</p>
            <div id="mp-dups"><div class="loading-spinner">Scanning your images…</div></div>

            <h2 class="mp-sec-h" style="margin-top:2rem">Wrong links <span class="muted mp-sec-sub">by image fingerprint</span></h2>
            <p class="muted mp-sec-note">Finds site-links whose image doesn't match the piece's own image — a link
            pointing at the <em>wrong</em> upload, which then pools a different piece's stats. It compares by image
            fingerprint (no AI); nothing changes until you unlink.</p>
            <div id="mp-mislinks"><button class="btn btn-sm" id="mp-mislink-scan">Scan for wrong links</button></div>`;
        this._loadVariantSuggestions();
        this._loadDuplicates();
        document.getElementById('mp-mislink-scan')?.addEventListener('click', () => this._loadMislinks());
    },

    /* Wrong-link auditor (2.192.0) — the perceptual-hash cross-check as a button.
     * Native/offline: compares each site-link's stored image hash to the piece's
     * LOCAL images; flags any that don't match. Read-only until you unlink. */
    async _loadMislinks() {
        const wrap = document.getElementById('mp-mislinks');
        if (!wrap) return;
        wrap.innerHTML = `<div class="loading-spinner">Fingerprinting your images…</div>`;
        let flagged;
        try {
            const d = await API.masterpieceMislinkAudit();
            flagged = (d && d.flagged) || [];
        } catch (err) {
            wrap.innerHTML = `<div class="card error">Scan failed: ${this.esc(err.message)}</div>`;
            return;
        }
        if (!flagged.length) {
            wrap.innerHTML = `<div class="empty-state"><p class="muted">No wrong links found — every site-link matches its piece. ✅</p></div>`;
            return;
        }
        wrap.innerHTML = flagged.map(f => {
            const p = this._plat(f.platform);
            const thumbUrl = this._thumbSrc(f.platform, f.thumbnail_url);
            const thumb = thumbUrl
                ? `<img class="mp-loc-thumb" src="${this.esc(thumbUrl)}" alt="" loading="lazy">`
                : `<span class="mp-loc-thumb mp-loc-thumb--none"></span>`;
            const open = f.view_url
                ? `<a class="pub-open" href="${this.esc(Utils.safeUrl(f.view_url) || '#')}" target="_blank" rel="noopener">open ↗</a>` : '';
            return `<div class="mp-mislink-row" data-name="${this.esc(f.name)}" data-platform="${this.esc(f.platform)}" data-sid="${this.esc(f.submission_id)}">
                ${thumb}
                <div class="mp-mislink-info">
                    <a href="#/masterpieces/${encodeURIComponent(f.name)}">${this.esc(f.title)}</a>
                    <div class="muted">linked to ${this.esc(p.label)} #${this.esc(f.submission_id)}${f.member_title ? ' — “' + this.esc(f.member_title) + '”' : ''} · looks different (distance ${f.distance})</div>
                </div>
                <div class="mp-mislink-acts">${open}
                    <button class="btn btn-sm btn-danger" data-mp-unlink>Unlink</button>
                </div>
            </div>`;
        }).join('');
        wrap.querySelectorAll('[data-mp-unlink]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const row = btn.closest('.mp-mislink-row');
                const title = row.querySelector('a').textContent;
                if (!window.confirm(`Unlink ${row.dataset.platform} #${row.dataset.sid} from “${title}”?\n\n`
                    + `The upload stays live on the platform — it's just detached from this piece so it stops pooling the wrong stats.`)) return;
                try {
                    await API.removeMasterpieceMember(row.dataset.name, row.dataset.platform, row.dataset.sid);
                    this._cache = null;
                    row.remove();
                    this._toast('success', 'Unlinked');
                    if (!wrap.querySelector('.mp-mislink-row')) {
                        wrap.innerHTML = `<div class="empty-state"><p class="muted">All cleared. ✅</p></div>`;
                    }
                } catch (err) { this._toast('error', 'Unlink failed: ' + (err.message || err)); }
            });
        });
    },

    /* ── Variant families (by title, 2.160.0) ─────────────────────────────────
     * The complement to the hash duplicate finder. suggest_families already
     * derived a hero + per-member key/label from each title, so unlike the dup
     * screen's "Variants of one piece" (which prompts for every label), this
     * folds a whole family in one click with the labels pre-filled. */
    async _loadVariantSuggestions() {
        const wrap = document.getElementById('mp-variants');
        if (!wrap) return;
        let families;
        try {
            const d = await API.getVariantSuggestions();
            families = (d && d.families) || [];
        } catch (err) {
            wrap.innerHTML = `<div class="card error">Couldn’t look for variants: ${this.esc(err.message)}</div>`;
            return;
        }
        if (!families.length) {
            wrap.innerHTML = `<div class="empty-state"><h3>No variant families found</h3>
                <p class="muted">No two Masterpieces share a title once render tags like “(Rough)” are set aside.</p></div>`;
            return;
        }
        wrap.innerHTML = families.map((f, fi) => this._variantFamily(f, fi)).join('');
        wrap.querySelectorAll('[data-varmerge]').forEach(btn =>
            btn.addEventListener('click', () => this._mergeVariantFamily(parseInt(btn.dataset.varmerge, 10), families)));
        wrap.querySelectorAll('[data-varnot]').forEach(btn =>
            btn.addEventListener('click', () => this._notVariantFamily(parseInt(btn.dataset.varnot, 10), families)));
    },

    _variantFamily(fam, fi) {
        const cards = fam.members.map((m, i) => {
            const src = m.cover_thumb
                ? this._thumbSrc(m.cover_platform, m.cover_thumb)
                : this._canonUrl(m.name, m.image);
            const thumb = src
                ? `<img class="mp-dup-thumb" src="${this.esc(src)}" alt="" loading="lazy">`
                : `<span class="mp-dup-thumb mp-dup-thumb--none">🖼️</span>`;
            // The suggested hero is pre-checked; any member can be chosen instead.
            const heroPick = `<label class="mp-dup-pick"><input type="radio" name="var-keep-${fi}" value="${i}"${m.is_hero ? ' checked' : ''}> keep as main</label>`;
            const tag = m.is_hero ? '' : `<span class="mp-var-key">${this.esc(m.label)}</span>`;
            return `
                <div class="mp-dup-card${m.is_hero ? ' is-keep' : ''}" data-idx="${i}">
                    ${thumb}
                    <div class="mp-dup-meta">
                        <div class="mp-dup-title">${this.esc(m.title || m.name)} ${tag}</div>
                        <div class="mp-dup-stats muted">${this._fmt(m.views)} views</div>
                        ${heroPick}
                    </div>
                </div>`;
        }).join('');
        return `
            <div class="mp-dup-group card" data-varfam="${fi}">
                <div class="mp-dup-row">${cards}</div>
                <div class="mp-dup-actions">
                    <button class="btn btn-primary btn-sm" data-varmerge="${fi}">Fold ${fam.members.length} into one piece</button>
                    <button class="btn btn-sm" data-varnot="${fi}"
                        title="These are separate pieces that happen to share a title — stop suggesting them">✗ Not variants</button>
                    <span class="mp-dup-msg muted" data-varmsg="${fi}"></span>
                </div>
            </div>`;
    },

    async _mergeVariantFamily(fi, families) {
        const fam = families[fi];
        const groupEl = document.querySelector(`.mp-dup-group[data-varfam="${fi}"]`);
        const msg = groupEl ? groupEl.querySelector(`[data-varmsg="${fi}"]`) : null;
        let keepIdx = fam.members.findIndex(m => m.is_hero);
        const picked = groupEl && groupEl.querySelector(`input[name="var-keep-${fi}"]:checked`);
        if (picked) keepIdx = parseInt(picked.value, 10);
        if (keepIdx < 0) keepIdx = 0;
        const keep = fam.members[keepIdx];
        const absorbs = fam.members.filter((_m, i) => i !== keepIdx);
        if (!window.confirm(`Fold ${absorbs.length} piece${absorbs.length === 1 ? '' : 's'} into “${keep.title || keep.name}” `
            + `as labeled variants? Each image moves into one Masterpiece and keeps its own stats. This can’t be undone.`)) return;
        if (msg) msg.textContent = 'Folding in…';
        let ok = 0, fail = 0;
        for (const a of absorbs) {
            // The chosen keeper takes the primary slot; every absorbed piece uses
            // the key/label suggest_families derived from its title suffix. If the
            // user re-picked the hero, the ex-hero has key '' → fall back to a slug.
            const key = a.key || (a.label || 'variant').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'variant';
            try {
                await API.mergeAsVariant({ keep: keep.name, absorb: a.name, key, label: a.label || key });
                ok++;
            } catch (e) { fail++; }
        }
        this._cache = null;   // grid is stale after folding
        if (msg) msg.textContent = fail ? `Folded ${ok}, ${fail} failed` : 'Folded into one ✓';
        if (groupEl) groupEl.style.opacity = '.55';
        this._toast(fail ? 'error' : 'success',
            fail ? `Folded ${ok}, ${fail} failed` : `${keep.title || keep.name} now has ${ok} variant${ok === 1 ? '' : 's'}`);
    },

    async _notVariantFamily(fi, families) {
        const fam = families[fi];
        const groupEl = document.querySelector(`.mp-dup-group[data-varfam="${fi}"]`);
        const msg = groupEl ? groupEl.querySelector(`[data-varmsg="${fi}"]`) : null;
        if (msg) msg.textContent = 'Remembering…';
        try {
            await API.dismissVariantFamily(fam.members.map(m => m.name));
            if (groupEl) { groupEl.style.opacity = '.5'; groupEl.querySelectorAll('button').forEach(b => b.disabled = true); }
            if (msg) msg.textContent = 'Won’t suggest these again ✓';
            this._toast('success', 'Marked as separate pieces');
        } catch (err) {
            if (msg) msg.textContent = 'Failed: ' + err.message;
        }
    },

    async _loadDuplicates() {
        const wrap = document.getElementById('mp-dups');
        if (!wrap) return;
        let groups;
        try {
            const d = await API.getMasterpieceDuplicates();
            groups = (d && d.groups) || [];
        } catch (err) {
            wrap.innerHTML = `<div class="card error">Scan failed: ${this.esc(err.message)}</div>`;
            return;
        }
        if (!groups.length) {
            wrap.innerHTML = `<div class="empty-state"><h3>No duplicates found 🎉</h3>
                <p class="muted">No two Masterpieces share the same image.</p></div>`;
            return;
        }
        wrap.innerHTML = groups.map((g, gi) => this._dupGroup(g, gi)).join('');
        wrap.querySelectorAll('[data-merge]').forEach(btn =>
            btn.addEventListener('click', () => this._mergeGroup(parseInt(btn.dataset.merge, 10), groups)));
        wrap.querySelectorAll('[data-notdup]').forEach(btn =>
            btn.addEventListener('click', () => this._notDuplicate(parseInt(btn.dataset.notdup, 10), groups)));
        wrap.querySelectorAll('[data-vardup]').forEach(btn =>
            btn.addEventListener('click', () => this._mergeGroupAsVariants(parseInt(btn.dataset.vardup, 10), groups)));
    },

    /* "Not the same" — remember that this group's images are actually different,
     * so the finder stops flagging them. Persisted server-side (2.145.0). */
    async _notDuplicate(gi, groups) {
        const items = groups[gi];
        const groupEl = document.querySelector(`.mp-dup-group[data-group="${gi}"]`);
        const msg = groupEl ? groupEl.querySelector(`[data-msg="${gi}"]`) : null;
        if (msg) msg.textContent = 'Remembering…';
        try {
            await API.dismissMasterpieceDuplicate(items.map(m => m.name));
            if (groupEl) { groupEl.style.opacity = '.5'; groupEl.querySelectorAll('button').forEach(b => b.disabled = true); }
            if (msg) msg.textContent = 'Won’t flag these again ✓';
            this._toast('success', 'Marked as different — won’t be flagged again');
        } catch (err) {
            if (msg) msg.textContent = 'Failed: ' + err.message;
        }
    },

    /* "Variants of one piece" (2.158.0) — the dup-finder's third option. Folds
     * every non-keep member of the group into the keeper as a LABELED variant:
     * image copied in, members re-keyed (stats stay attributed), record removed. */
    async _mergeGroupAsVariants(gi, groups) {
        const items = groups[gi];
        const groupEl = document.querySelector(`.mp-dup-group[data-group="${gi}"]`);
        const msg = groupEl ? groupEl.querySelector(`[data-msg="${gi}"]`) : null;
        let keepIdx = 0;
        const picked = groupEl && groupEl.querySelector(`input[name="dup-keep-${gi}"]:checked`);
        if (picked) keepIdx = parseInt(picked.value, 10);
        const keep = items[keepIdx];
        const absorbs = items.filter((_m, i) => i !== keepIdx);
        const jobs = [];
        for (const a of absorbs) {
            const label = window.prompt(
                `Variant label for “${a.title || a.name}” (e.g. NSFW, Censored, No BG)?`, 'NSFW');
            if (label === null) return;   // user backed out — do nothing at all
            const key = label.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'variant';
            jobs.push({ absorb: a.name, key, label: label.trim() || key });
        }
        if (!window.confirm(`Fold ${absorbs.length} piece${absorbs.length === 1 ? '' : 's'} into “${keep.title || keep.name}” `
            + `as labeled variants? Their images move in and their site-links keep their own stats.`)) return;
        if (msg) msg.textContent = 'Folding in…';
        let ok = 0, fail = 0;
        for (const j of jobs) {
            try { await API.mergeAsVariant({ keep: keep.name, absorb: j.absorb, key: j.key, label: j.label }); ok++; }
            catch (e) { fail++; }
        }
        this._cache = null;
        if (msg) msg.textContent = fail ? `Folded ${ok}, ${fail} failed` : 'Folded into one cohort ✓';
        if (groupEl) groupEl.style.opacity = '.55';
        this._toast(fail ? 'error' : 'success',
            fail ? `Folded ${ok}, ${fail} failed` : `${keep.title || keep.name} now has ${ok} labeled variant${ok === 1 ? '' : 's'}`);
    },

    _dupGroup(items, gi) {
        // items[0] is the recommended survivor (most views, then most sites).
        const cards = items.map((m, i) => {
            const cover = m.cover_thumb
                ? `<img class="mp-dup-thumb" src="${this.esc(this._thumbSrc(m.cover_platform, m.cover_thumb))}" alt="" loading="lazy">`
                : (this._canonUrl(m.name, m.image)
                    ? `<img class="mp-dup-thumb" src="${this.esc(this._canonUrl(m.name, m.image))}" alt="" loading="lazy">`
                    : `<span class="mp-dup-thumb mp-dup-thumb--none">🖼️</span>`);
            const keepTag = i === 0
                ? `<span class="mp-dup-keep">✓ keeps</span>`
                : `<label class="mp-dup-pick"><input type="radio" name="dup-keep-${gi}" value="${i}"> keep this instead</label>`;
            return `
                <div class="mp-dup-card${i === 0 ? ' is-keep' : ''}" data-idx="${i}">
                    ${cover}
                    <div class="mp-dup-meta">
                        <div class="mp-dup-title">${this.esc(m.title || m.name)}</div>
                        <div class="mp-dup-stats muted">${this._fmt(m.views)} views · ${m.sites} site${m.sites === 1 ? '' : 's'}</div>
                        ${keepTag}
                    </div>
                </div>`;
        }).join('');
        return `
            <div class="mp-dup-group card" data-group="${gi}">
                <div class="mp-dup-row">${cards}</div>
                <div class="mp-dup-actions">
                    <button class="btn btn-primary btn-sm" data-merge="${gi}">Merge ${items.length} into one</button>
                    <button class="btn btn-sm" data-vardup="${gi}"
                        title="Different renders of ONE piece (SFW/NSFW, censored/clean…) — fold them into one Masterpiece as labeled variants, each keeping its own stats">🖇 Variants of one piece</button>
                    <button class="btn btn-sm" data-notdup="${gi}"
                        title="These are different images — don't flag them as duplicates again">✗ Not the same</button>
                    <span class="mp-dup-msg muted" data-msg="${gi}"></span>
                </div>
            </div>`;
    },

    async _mergeGroup(gi, groups) {
        const items = groups[gi];
        const groupEl = document.querySelector(`.mp-dup-group[data-group="${gi}"]`);
        const msg = groupEl ? groupEl.querySelector(`[data-msg="${gi}"]`) : null;
        // Survivor = the radio the user picked, else the recommended items[0].
        let keepIdx = 0;
        const picked = groupEl && groupEl.querySelector(`input[name="dup-keep-${gi}"]:checked`);
        if (picked) keepIdx = parseInt(picked.value, 10);
        const keep = items[keepIdx];
        const drops = items.filter((_m, i) => i !== keepIdx);
        if (!window.confirm(`Merge ${drops.length} duplicate${drops.length === 1 ? '' : 's'} into “${keep.title || keep.name}”? `
            + `Their site-links move over and the duplicate records are deleted. This can't be undone.`)) return;
        const btn = groupEl && groupEl.querySelector('[data-merge]');
        if (btn) btn.disabled = true;
        if (msg) msg.textContent = 'Merging…';
        let ok = 0, fail = 0;
        for (const d of drops) {
            try { await API.mergeMasterpieces(keep.name, d.name); ok++; }
            catch (e) { fail++; }
        }
        this._cache = null;   // grid is stale after a merge
        if (msg) msg.textContent = fail ? `Merged ${ok}, ${fail} failed` : 'Merged ✓';
        if (groupEl) { groupEl.style.opacity = '.55'; }
        this._toast(fail ? 'error' : 'success',
            fail ? `Merged ${ok}, ${fail} failed` : `Merged into ${keep.title || keep.name}`);
    },

    _cover(m, cls) {
        const canon = this._canonUrl(m.name, m.image);
        if (canon) return `<img class="${cls}" src="${this.esc(canon)}" alt="" loading="lazy">`;
        const s = m.summary || {};
        if (s.cover_thumb) return `<img class="${cls}" src="${this.esc(this._thumbSrc(s.cover_platform, s.cover_thumb))}" alt="" loading="lazy">`;
        return `<div class="mp-cover-ph">🖼️</div>`;
    },

    _card(m) {
        const s = m.summary || {};
        const t = s.totals || {};
        const nSites = s.member_count || 0;
        // Live member platforms if we have them, else the master's configured targets.
        const plats = (s.platforms && s.platforms.length ? s.platforms : (m.platforms || []));
        const badges = plats.slice(0, 8).map(c =>
            `<span class="mp-plat" title="${this.esc(this._plat(c).label)}">${this._plat(c).emoji || c}</span>`).join('');
        const personas = this._personaChips(s.persona_ids);
        // In the junk view every card carries a one-click Restore.
        const restore = this._junkView
            ? `<button class="btn btn-sm" data-mp-restore data-name="${this.esc(m.name)}"
                style="margin-top:.35rem" type="button">♻ Restore</button>` : '';
        // Raw slug in the href (folder names are [\w-] slugs); the API layer
        // encodes once when fetching — mirrors Bookshelf's #/library/work/{name}.
        return `
            <a class="mp-card" href="#/masterpieces/${this.esc(m.name)}">
                <div class="mp-cover" data-rating="${this.esc((m.rating || '').toLowerCase())}">${this._cover(m, 'mp-cover-img')}</div>
                <div class="mp-body">
                    <div class="mp-name" title="${this.esc(m.title || m.name)}">${this.esc(m.title || m.name)}</div>
                    <div class="mp-meta">${badges}<span class="muted">· ${nSites} site${nSites === 1 ? '' : 's'}</span></div>
                    <div class="mp-stats">👁 ${this._fmt(t.views)} · ❤ ${this._fmt(t.favorites)} · 💬 ${this._fmt(t.comments)}</div>
                    ${personas ? `<div class="mp-personas-inline">${personas}</div>` : ''}
                    ${restore}
                </div>
            </a>`;
    },

    /* ── Detail (#/masterpieces/{name}) ── */

    async renderDetail(name) {
        this._current = name;
        this._init();
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="work-back"><a href="#/library">&larr; Library</a></div>
            <div id="mp-detail"><div class="loading-spinner">Opening the masterpiece…</div></div>`;
        await this._loadPersonas();
        let m;
        try {
            m = await API.getMasterpiece(name);
        } catch (err) {
            const status = (err && /404/.test(err.message)) ? 'This masterpiece no longer exists.' : this.esc(err.message);
            document.getElementById('mp-detail').innerHTML =
                `<div class="card error">Couldn't open this masterpiece: ${status}</div>`;
            return;
        }
        this._paintDetail(name, m);
        this._renderDetailNav(name);
    },

    /* Prev/next navigation across the grid list (2.190.0). Uses the cached grid
     * order (active pieces); on a deep link with no cache, fetches it. Arrows +
     * a position counter go in the top back-bar; ←/→ keys step too. */
    async _renderDetailNav(name) {
        let list = this._cache;
        if (!list) {
            try { list = ((await API.getMasterpieces()) || {}).masterpieces || []; this._cache = list; }
            catch (e) { return; }
        }
        // Active pieces in grid order; if the open piece is junk, fall back to all.
        let names = list.filter(m => m.status !== 'junk').map(m => m.name);
        let idx = names.indexOf(name);
        if (idx === -1) { names = list.map(m => m.name); idx = names.indexOf(name); }
        if (idx === -1) return;
        const prev = idx > 0 ? names[idx - 1] : null;
        const next = idx < names.length - 1 ? names[idx + 1] : null;
        this._navPrev = prev; this._navNext = next;
        const back = document.querySelector('.work-back');
        if (!back || back.querySelector('.mp-nav')) return;
        back.classList.add('mp-detail-topnav');
        const btn = (n, cls, label, title) => n
            ? `<a class="btn btn-sm ${cls}" href="#/masterpieces/${encodeURIComponent(n)}" title="${title}">${label}</a>`
            : `<span class="btn btn-sm is-disabled" aria-disabled="true">${label}</span>`;
        back.insertAdjacentHTML('beforeend', `
            <span class="mp-nav">
                ${btn(prev, 'mp-nav-prev', '&lsaquo; Prev', 'Previous (←)')}
                <span class="mp-nav-pos muted">${idx + 1} / ${names.length}</span>
                ${btn(next, 'mp-nav-next', 'Next &rsaquo;', 'Next (→)')}
            </span>`);
    },

    /* ←/→ step through pieces while on a masterpiece detail (not while typing). */
    _onNavKey(e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        // Both routes render this page (2.193.0), so arrow-stepping works on either.
        const h = location.hash || '';
        if (!/^#\/masterpieces\/[^/]/.test(h) && !/^#\/artwork\/image\/[^/]/.test(h)) return;
        if (e.target && e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
        const to = e.key === 'ArrowLeft' ? this._navPrev : this._navNext;
        if (to) { e.preventDefault(); location.hash = `#/masterpieces/${encodeURIComponent(to)}`; }
    },

    _ratingCls(r) {
        const v = (r || '').toLowerCase();
        if (v === 'adult' || v === 'explicit') return 'mp-rating mp-rating--adult';
        if (v === 'mature') return 'mp-rating mp-rating--mature';
        return 'mp-rating';
    },

    /* Which variant the URL asked for (2.193.0): '#/…/My_Art?v=nsfw' → 'nsfw'.
     * Rides a query tail rather than a path segment because artwork names may
     * contain '/', which would make '…/v/nsfw' ambiguous with the name itself.
     * Clicking a variant tile in the Library lands here, and the page opens on
     * THAT render with every sibling still in the strip beside it. */
    _variantFromHash() {
        const h = window.location.hash || '';
        const q = h.indexOf('?');
        if (q === -1) return '';
        try { return new URLSearchParams(h.slice(q + 1)).get('v') || ''; }
        catch (e) { return ''; }
    },

    /* The page is a COMPOSITION (4.4.0, C2 spec §6.2): a hero and three
     * columns of cards, each card a small renderer returning a string. The ids
     * and data-* names that _init()'s delegate and the four post-paint fills
     * depend on are unchanged — the markup around them moved, the names did
     * not (spec §2, §4). The SFW blur keys on `img.mp-hero-img[data-rating]`
     * and `.mp-alts[data-rating]` (app.js / safe_mode.css); _heroHtml emits
     * both byte-for-byte. */
    _paintDetail(name, m) {
        const root = document.getElementById('mp-detail');
        if (!root) return;
        // Kept for the delegated handlers (_applyOverrides needs the existing
        // per-platform tag map so an override doesn't clobber the others).
        this._detail = m;
        const v = this._detailView(name, m);

        root.innerHTML = `
            <div class="board-wrap">
                ${this._heroHtml(name, m, v)}
                <div class="board">
                    <div class="board-col">${this._canonicalHtml(name, m)}${this._tagsHtml(m)}${this._budgetHtml()}</div>
                    <div class="board-col">${this._publishHtml(m)}${this._linkHtml()}${this._foldHtml()}</div>
                    <div class="board-col board-col--3">${this._locationsHtml(m)}${this._growthHtml()}${this._bestHtml(m)}${this._rendersHtml(name, m, v)}</div>
                </div>
            </div>`;

        // Open on the selected render (2.193.0) — patched after innerHTML because
        // the chip list, and therefore the selection, is computed after `hero`.
        if (v.mainUrl && v.mainUrl !== v.heroUrl) {
            const heroImg = document.getElementById('mp-hero-img');
            const bg = document.getElementById('mp-stage-bg');
            if (heroImg) heroImg.src = v.mainUrl;
            if (bg) bg.src = v.mainUrl;
        }

        // The post-paint fills, as before: the shared platform rows, the
        // same-image suggestions, the per-site tag budget (which also styles
        // the chips), the combined chart (≥ 2 points). Chips render at once
        // from the textarea so the tags are never blank while the budget loads.
        this._wireDetailPublish(name, m);
        this._loadSuggestions();
        this._tagChips();
        this._loadTagBudget();
        this._loadChart(name);
        this._foldTarget = null;      // reset the "fold into" choice per detail open
    },

    /* The values several renderers share: which render is selected, its URL,
     * the rating that drives the SFW blur. Computed once. */
    _detailView(name, m) {
        const heroUrl = this._canonUrl(name, m.image);
        const rating = this.esc((m.rating || '').toLowerCase());  // drives SFW blur
        const imgs = m.images || [];
        const variants = m.variants || [];
        // Variant chips (2.158.0): declared variants render labeled with their OWN
        // stats (the cohort total stays in the headline); pieces without declared
        // variants fall back to the 2.152 unlabeled gallery of folder images.
        const chips = variants.length
            ? variants.map(v => ({
                u: this._canonUrl(name, v.image),
                label: v.label || v.key || 'Primary',
                st: `👁 ${this._fmt((v.totals || {}).views)} · ❤ ${this._fmt((v.totals || {}).favorites)}`
                    + ` · 💬 ${this._fmt((v.totals || {}).comments)} · ${v.member_count || 0} site${(v.member_count || 0) === 1 ? '' : 's'}`,
            }))
            : imgs.map((f, i) => ({ u: this._canonUrl(name, f), label: i === 0 ? 'Primary' : `Alt ${i}`, st: '' }));
        // Which chip opens selected (2.193.0). A '?v=<key>' from a Library variant
        // tile preselects that render; otherwise the hero (index 0) as before.
        const wantKey = this._variantFromHash();
        let selIdx = 0;
        if (wantKey && variants.length) {
            const i = variants.findIndex(v => (v.key || '') === wantKey);
            if (i >= 0) selIdx = i;
        }
        const mainUrl = (chips[selIdx] && chips[selIdx].u) || heroUrl;
        const selLabel = selIdx > 0 && chips[selIdx] ? chips[selIdx].label : '';
        return { heroUrl, rating, imgs, variants, chips, selIdx, mainUrl, selLabel, isJunk: m.status === 'junk' };
    },

    /* Canonical tags = core + auxiliary, in that order (core carries the 20-25
     * that platforms with a tag budget actually receive; auxiliary is the long
     * tail). `default` is the pre-split flat list, still read so folders that
     * haven't been re-saved keep working. Falls back to the union across
     * platforms if a work only has per-platform lists. */
    _canonicalTagList(m) {
        const ct = m.canonical_tags || {};
        const seenTag = new Set();
        let tagList = [];
        ['core', 'default', 'auxiliary'].forEach(k => (ct[k] || []).forEach(x => {
            if (!seenTag.has(x.toLowerCase())) { seenTag.add(x.toLowerCase()); tagList.push(x); }
        }));
        if (!tagList.length) {
            const seen = new Set();
            Object.values(ct).forEach(arr => (arr || []).forEach(x => seen.add(x)));
            tagList = [...seen];
        }
        return tagList;
    },

    /* Attribution line (3.5.2). Almost every piece here is someone else's
     * work, and the credit posted to each site is built from this field — so
     * its absence is a warning, not a blank. `author` is the posting persona
     * and is a different thing entirely. 3.10.0 — editable, and "no artist" is
     * three states rather than one warning. */
    _artistLineHtml(m) {
        const _art = m.artist || null;
        const _astatus = m.artist_status || '';
        const _nh = Object.keys((_art && _art.handles) || {}).length;
        let artistBody;
        if (_art && _art.name) {
            artistBody = `Art by <strong>${this.esc(_art.name)}</strong>` +
                (_nh ? ` <span class="muted">· ${_nh} linked account${_nh === 1 ? '' : 's'}</span>`
                     : ` <span class="muted">· no linked accounts</span>`);
        } else if (_astatus === 'own') {
            artistBody = `<span class="muted">Drawn by you — nothing to credit</span>`;
        } else if (_astatus === 'unknown') {
            artistBody = `<span class="muted">Artist unknown</span>`;
        } else {
            artistBody = `⚠ No artist recorded`;
        }
        return `
            <div class="mp-artist${(!(_art && _art.name) && !_astatus) ? ' mp-artist--missing' : ''}">
                <span id="mp-artist-body">${artistBody}</span>
                <button class="btn btn-sm" data-mp-artist-edit type="button"
                    title="Set who drew this. A name already in the registry fills in every handle that was verified for them.">✎ ${_art && _art.name ? 'Edit' : 'Set'} artist</button>
            </div>`;
    },

    /* ── Hero (§5.2) ─────────────────────────────────────────────────────── */
    _heroHtml(name, m, v) {
        const t = m.totals || {};
        const hero = v.heroUrl
            ? `<img class="mp-hero-img" id="mp-hero-img" data-rating="${v.rating}" src="${this.esc(v.heroUrl)}" alt="${this.esc(m.alt_text || m.title || name)}">`
            : `<div class="mp-hero-ph">🖼️</div>`;
        // The tile is a BUTTON so the full-size render is reachable without a
        // mouse (§13). The lightbox reads the CURRENT hero src, so it follows
        // the selected variant.
        const tile = v.heroUrl
            ? `<button type="button" class="board-hero-tile" data-mp-lightbox title="Open full size">${hero}</button>`
            : `<div class="board-hero-tile">${hero}</div>`;
        const gallery = v.chips.length > 1
            ? `<div class="mp-alts" data-rating="${v.rating}">${v.chips.map((c, i) => `
                <div class="mp-altwrap${i === v.selIdx ? ' is-active' : ''}" data-mp-img="${this.esc(c.u)}"
                     data-vstats="${this.esc(c.st)}" role="button" tabindex="0" title="${this.esc(c.label)}">
                    <img class="mp-alt" src="${this.esc(c.u)}" alt="" loading="lazy">
                    <div class="mp-alt-label">${this.esc(c.label)}</div>
                </div>`).join('')}</div>`
            : '';
        const vstatsLine = v.chips.length > 1
            ? `<div class="mp-vstats muted" id="mp-vstats">${this.esc(v.chips[v.selIdx].st)}</div>` : '';
        const rating = m.rating ? `<span class="${this._ratingCls(m.rating)}">${this.esc(m.rating)}</span>` : '';
        const personas = this._personaChips(m.persona_ids);
        const liveN = (m.locations || []).length;
        const livePill = liveN ? `<span class="pill pill--live">live on ${liveN}</span>` : '';
        const junkBadge = v.isJunk
            ? `<span class="mp-role" title="Hidden from the grid — restore to bring it back">🗑 junk</span>` : '';
        const junkBtn = `<button class="btn btn-sm" data-mp-junk data-junk="${v.isJunk ? '' : 'junk'}" type="button"
            title="${v.isJunk ? 'Put this back in the Masterpieces grid'
                : 'Hide from the grid without deleting — the folder and site-links are kept'}">
            ${v.isJunk ? '♻ Restore' : '🗑 Junk'}</button>`;
        const firstPosted = m.first_posted
            ? `<div class="mp-firstposted muted" title="${m.first_posted_source === 'title'
                ? 'Matched to a site upload by its title — link the upload to confirm'
                : 'The date the Library sorts this piece by'}">First posted ${m.first_posted_source === 'title' ? '≈ ' : ''}${Utils.formatDate(m.first_posted)}</div>`
            : `<div class="mp-firstposted muted" title="No site upload linked, so no post date — the Library sorts it by when it was added">Not posted anywhere PawPoller knows of</div>`;
        return `
            <div class="board-hero">
                ${v.heroUrl ? `<img class="mp-stage-bg board-hero-bg" id="mp-stage-bg" src="${this.esc(v.heroUrl)}" alt="" aria-hidden="true">` : ''}
                ${tile}
                <div class="board-hero-mid">
                    <h1 class="mp-title">${this.esc(m.title || name)}${v.selLabel
                        ? ` <span class="muted mp-selvariant" style="font-weight:400;font-size:.75em">— ${this.esc(v.selLabel)}</span>`
                        : ''}</h1>
                    ${this._artistLineHtml(m)}
                    ${firstPosted}
                    <div class="chip-rows">
                        <div class="chip-row">${rating}${livePill}${junkBadge}${personas ? `<span class="mp-personas">${personas}</span>` : ''}</div>
                        ${gallery ? `<div class="chip-row">${gallery}${vstatsLine}</div>` : ''}
                    </div>
                    <div class="board-stats">
                        <div class="n">${this._fmt(t.views)}<small>Views</small></div>
                        <div class="n">${this._fmt(t.favorites)}<small>Favourites</small></div>
                        <div class="n">${this._fmt(t.comments)}<small>Comments</small></div>
                        <div class="n">${t.locations || 0}<small>Sites</small></div>
                    </div>
                </div>
                <div class="board-hero-actions">
                    <button class="btn btn-sm btn-primary" data-mp-sync type="button"
                        title="Push this record to every editable site (metadata only — never re-uploads the image)">↑ Sync to sites</button>
                    <button class="btn btn-sm" data-add-collection data-mtype="masterpiece"
                        data-mref="${this.esc(name)}" data-label="${this.esc(m.title || name)}"
                        title="Bundle this piece (with its companion story / announcement posts) into a Collection">＋ Add to Collection</button>
                    <div class="pair">${junkBtn}<button class="btn btn-sm btn-danger" data-mp-delete type="button"
                        title="Delete this piece from your library. Already-published posts stay live on each platform. Prefer 🗑 Junk if you only want it out of the grid.">Delete</button></div>
                </div>
            </div>`;
    },

    /* ── Column 1 — the record (§5.3) ───────────────────────────────────── */
    _canonicalHtml(name, m) {
        const curRating = (m.rating || '').toLowerCase();
        const ratingOpts = ['general', 'mature', 'adult'].map(r =>
            `<option value="${r}"${curRating === r ? ' selected' : ''}>${r[0].toUpperCase() + r.slice(1)}</option>`).join('');
        const charsStr = (m.characters || []).join(', ');
        return `
            <section class="card" aria-labelledby="mp-sec-canon">
                <div class="sec-title"><h2 id="mp-sec-canon">Canonical record</h2>
                    <button class="btn btn-primary btn-sm" data-mp-save type="button">Save</button></div>
                <p class="sec-note">Edit once, then sync to every editable site.</p>
                <div class="mp-edit">
                    <label class="mp-field"><span>Title</span>
                        <input class="mp-input" id="mp-e-title" value="${this.esc(m.title || '')}"></label>
                    <label class="mp-field"><span>Description</span>
                        <textarea class="mp-input" id="mp-e-desc" rows="4">${this.esc(m.description || '')}</textarea></label>
                    <label class="mp-field"><span>Alt text
                            <span class="muted">(for screen readers; used as the Bluesky image description)</span></span>
                        <input class="mp-input" id="mp-e-alt" value="${this.esc(m.alt_text || '')}"
                            placeholder="e.g. A grey wolf in a red jacket grins at the viewer"></label>
                    <div class="mp-field-row">
                        <label class="mp-field"><span>Rating</span>
                            <select class="mp-input" id="mp-e-rating">${ratingOpts}</select></label>
                        <label class="mp-field"><span>Characters <span class="muted">(comma-separated)</span></span>
                            <input class="mp-input" id="mp-e-chars" value="${this.esc(charsStr)}"></label>
                    </div>
                    <div class="mp-edit-actions"><span class="mp-edit-msg muted" id="mp-edit-msg"></span></div>
                </div>
            </section>`;
    },

    /* Tags as chips — a VIEW over #mp-e-tags (§5.3). The hidden textarea keeps
     * holding the comma list, so _readCanonical and _saveCanonical are
     * unchanged; chips edit it and re-render. */
    _tagsHtml(m) {
        const tagList = this._canonicalTagList(m);
        return `
            <section class="card" aria-labelledby="mp-sec-tags">
                <div class="sec-title"><h2 id="mp-sec-tags">Tags</h2>
                    <button class="btn btn-sm btn-browse" data-mp-tagbrowse type="button" title="Pick from the tag library">🏷️ Browse library</button></div>
                <p class="sec-note">Tag it fully — each site takes what it can.</p>
                <div class="tagblock">
                    <div class="tagbar"><span class="tagcount"><b id="mp-tagcount">${tagList.length}</b> tags · <b id="mp-corecount">–</b> core</span></div>
                    <textarea id="mp-e-tags" class="mp-input" hidden aria-hidden="true">${this.esc(tagList.join(', '))}</textarea>
                    <ul class="tagchips" id="mp-tagchips" role="list" aria-label="Canonical tags"></ul>
                    <div class="tag-legend">
                        <span><span class="tagchip tagchip--core"><b>core</b></span> kept everywhere</span>
                        <span><span class="tagchip tagchip--cut"><b>trimmed</b></span> dropped on at least one site</span>
                    </div>
                </div>
            </section>`;
    },

    _budgetHtml() {
        return `
            <section class="card" aria-labelledby="mp-sec-budget">
                <div class="sec-title"><h2 id="mp-sec-budget">What each site gets</h2></div>
                <p class="sec-note">Trimming drops from the tail — core tags always survive.</p>
                <div id="mp-tagbudget" class="mp-tagbudget"><div class="card-skel"></div></div>
            </section>`;
    },

    /* ── Column 2 — where it goes (§5.4) ────────────────────────────────── */
    _publishHtml(m) {
        if (m.status === 'junk') {
            // Read-only for a junked piece (§11): every other card stays.
            return `
            <section class="card" aria-labelledby="mp-sec-pub">
                <div class="sec-title"><h2 id="mp-sec-pub">Publish to more</h2></div>
                <p class="board-readonly">Restore this piece to publish it.</p>
            </section>`;
        }
        return `
            <section class="card" aria-labelledby="mp-sec-pub">
                <div class="sec-title"><h2 id="mp-sec-pub">Publish to more</h2>
                    <span class="acts">
                        <button class="btn btn-sm" data-mp-schedule-toggle type="button">&#128340; Schedule&hellip;</button>
                        <button class="btn btn-primary btn-sm" data-mp-publish type="button">Publish now</button>
                    </span></div>
                <p class="sec-note">Sites this piece isn't on yet.</p>
                <div id="mp-detail-platforms"></div>
                <div class="mp-edit-actions"><span class="mp-edit-msg muted" id="mp-pub-msg"></span></div>
                <div class="schedule-form" id="mp-schedule-form" style="display:none">
                    <div class="schedule-form-inner">
                        <label class="schedule-label" for="mp-schedule-datetime">Publish the ticked platforms at:</label>
                        <input type="datetime-local" class="schedule-datetime" id="mp-schedule-datetime">
                        <div class="schedule-form-actions">
                            <button class="btn btn-sm btn-primary" data-mp-schedule-confirm type="button">Confirm schedule</button>
                            <button class="btn btn-sm btn-outline" data-mp-schedule-cancel type="button">Cancel</button>
                        </div>
                    </div>
                </div>
                <div class="schedule-pending" id="mp-scheduled-list"></div>
            </section>`;
    },

    _linkHtml() {
        return `
            <section class="card" aria-labelledby="mp-sec-link">
                <div class="sec-title"><h2 id="mp-sec-link">Link the same image elsewhere</h2></div>
                <p class="sec-note">Attach copies already uploaded to other sites.</p>
                <div class="acts" style="display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px">
                    <button class="btn btn-sm" data-mp-scan type="button"
                        title="Hash platform thumbnails to find this exact image on other sites (native, no AI)">↻ Scan</button>
                    <button class="btn btn-sm" data-mp-linkpick type="button"
                        title="Pick a discovered post/upload to link by hand — for when the auto-scan misses it">🔍 By hand…</button>
                    <button class="btn btn-sm" data-mp-linkurl type="button"
                        title="Paste the post's URL. The picker can only offer posts with no publication row, so a post already filed under its own site title is invisible to it — this reaches those.">🔗 Paste link…</button>
                </div>
                <div id="mp-suggest-body"><div class="muted">Looking for the same image on other sites…</div></div>
            </section>`;
    },

    _foldHtml() {
        return `
            <section class="card" aria-labelledby="mp-sec-fold">
                <div class="sec-title"><h2 id="mp-sec-fold">Same piece as another?</h2></div>
                <p class="sec-note">Fold <strong>this</strong> piece into another Masterpiece.</p>
                <div class="mp-fold">
                    <div class="mp-fold-pick">
                        <button class="btn btn-sm" data-mp-fold-pick type="button">🔍 Choose a piece…</button>
                        <span class="mp-fold-chosen muted" id="mp-fold-chosen">No piece chosen yet</span>
                    </div>
                    <div class="mp-fold-kinds">
                        <label><input type="radio" name="mp-fold-kind" value="dup" checked> It's a <strong>duplicate</strong>
                            <span class="muted">(same image — this copy is removed)</span></label>
                        <label><input type="radio" name="mp-fold-kind" value="var"> It's a <strong>variant</strong>
                            <span class="muted">(different render — this image is kept as an alternate)</span></label>
                    </div>
                    <label class="mp-field mp-fold-vlabel" id="mp-fold-vlabel-wrap" style="display:none">
                        <span>Variant label</span>
                        <input class="mp-input" id="mp-fold-vlabel" placeholder="e.g. NSFW, Rough, Sketch">
                    </label>
                    <div class="mp-edit-actions">
                        <button class="btn btn-primary btn-sm" data-mp-fold type="button">Fold this piece in</button>
                        <span class="mp-edit-msg muted" id="mp-fold-msg"></span>
                    </div>
                </div>
            </section>`;
    },

    /* ── Column 3 — what happened (§5.5) ────────────────────────────────── */
    _healthDot(code) {
        const PH = window.PlatformHealth;
        if (!PH || !PH.classify) return '';
        // No entry at all (health not fetched yet, or a profile with no
        // sessions) is "not checked", not "unconfigured": classify(null) says
        // the latter, and a page of red dots because a fetch has not happened
        // is the §55 mistake — a dot that asserts a fault it has not seen.
        const state = PH.get(code) ? PH.classify(code) : 'unknown';
        const labels = {
            healthy: 'Polling normally', stale: 'Last poll is overdue', throttled: 'Rate-limited by the site',
            error: 'Last poll failed — see Settings', running: 'Polling now', unknown: 'Not polled yet',
            unconfigured: 'No credentials for this site',
        };
        const text = labels[state] || state;
        return `<span class="health-dot health-dot--${this.esc(state)}" title="${this.esc(text)}" aria-label="${this.esc(text)}"></span>`;
    },

    _locationsHtml(m) {
        const locs = m.locations || [];
        const rows = locs.map(l => {
            const p = this._plat(l.platform);
            const st = l.stats || {};
            const thumbUrl = this._thumbSrc(l.platform, l.thumbnail_url);
            const thumb = thumbUrl
                ? `<img class="thumb-sq" src="${this.esc(thumbUrl)}" alt="" loading="lazy">`
                : `<span class="thumb-sq thumb-sq--none"></span>`;
            const roleCls = l.role === 'primary' ? 'mp-role mp-role--primary' : 'mp-role';
            const role = l.role ? `<span class="${roleCls}">${this.esc(l.role)}</span>` : '';
            // Platforms whose poster can't edit in place are Sync-exempt (§0-A1).
            const postOnly = this._POST_ONLY.has(l.platform)
                ? `<span class="mp-role mp-role--postonly" title="This site can't be edited in place — re-post to update">post-only</span>` : '';
            const safe = window.Utils && Utils.safeUrl ? Utils.safeUrl(l.url) : l.url;
            const link = safe ? `<a class="btn btn-sm" href="${this.esc(safe)}" target="_blank" rel="noopener">open&nbsp;&#8599;</a>` : '<span></span>';
            const sub = [l.account_label || l.account || '', l.title || ''].filter(Boolean).map(x => this.esc(x)).join(' · ');
            return `
                <div class="loc-row">
                    ${thumb}
                    <div class="loc-site">
                        <span class="name">${this._healthDot(l.platform)}${p.emoji || ''} ${this.esc(p.label)} ${role}${postOnly}</span>
                        ${sub ? `<span class="sub">${sub}</span>` : ''}
                    </div>
                    <span class="loc-stats" title="views · favourites · comments">${this._fmt(st.views)} · ${this._fmt(st.favorites)} · ${this._fmt(st.comments)}</span>
                    ${link}
                    <button class="mp-loc-detach" title="Unlink this upload from the Masterpiece" aria-label="Unlink"
                        data-mp-detach data-platform="${this.esc(l.platform)}" data-sid="${this.esc(l.submission_id)}">✕</button>
                </div>`;
        }).join('');
        // Publishing has auto-linked since 2.128.0 (`manager.post_artwork` upserts
        // a member on each successful post), so this fills in on its own.
        const body = locs.length
            ? `<div class="loc-list">${rows}</div>`
            : `<div class="mp-empty">Not posted anywhere yet. <strong>Publish to more</strong> links a site
               automatically as you post; <strong>Link the same image elsewhere</strong> attaches copies
               already uploaded.</div>`;
        return `
            <section class="card" aria-labelledby="mp-sec-loc">
                <div class="sec-title"><h2 id="mp-sec-loc">Published to</h2></div>
                <p class="sec-note">Where this piece already lives.</p>
                ${body}
            </section>`;
    },

    _growthHtml() {
        return `
            <section class="card" id="mp-chart-card" style="display:none" aria-labelledby="mp-sec-growth">
                <div class="sec-title"><h2 id="mp-sec-growth">Combined growth</h2></div>
                <p class="sec-note">Summed across every site.</p>
                <div class="mp-chart-wrap"><canvas id="mp-combined-chart"></canvas></div>
            </section>`;
    },

    /* Client-side from locations[].stats — no new API (spec §10). */
    _bestHtml(m) {
        const locs = (m.locations || []).filter(l => l.platform);
        if (!locs.length) return '';
        const views = locs.map(l => ({ code: l.platform, v: Number((l.stats || {}).views) || 0 }));
        const total = views.reduce((a, b) => a + b.v, 0);
        const top = views.reduce((a, b) => (b.v > a.v ? b : a), views[0]);
        const share = total ? Math.round(top.v / total * 100) : 0;
        const avg = Math.round(total / views.length);
        const p = this._plat(top.code);
        return `
            <section class="card" aria-labelledby="mp-sec-best">
                <div class="sec-title"><h2 id="mp-sec-best">Best performer</h2></div>
                <p class="sec-note">${total ? `${this.esc(p.label)} carries ${share}% of this piece's views.` : 'No views recorded yet.'}</p>
                <div class="best-grid">
                    <div class="stat-card"><div class="label">Top site</div><div class="value">${p.emoji || ''} ${this.esc(p.label)}</div></div>
                    <div class="stat-card"><div class="label">Per-site average</div><div class="value">${this._fmt(avg)}</div></div>
                </div>
            </section>`;
    },

    /* The variant manager (2.189.0) plus Replace / Add variant, collapsed at the
     * foot of column 3 (§5.2): rarely used, and it was the largest thing above
     * the fold. The chips in the hero stay the viewer. */
    _rendersHtml(name, m, v) {
        const variants = v.variants, imgs = v.imgs;
        const declaredRows = !variants.length ? '' : variants.map(x => {
            const isPrimary = !x.key;
            const sites = (x.member_count || 0);
            return `<div class="mp-vrow" data-vkey="${this.esc(x.key)}">
                <span class="mp-vname">${this.esc(x.label || x.key || 'Primary')}${isPrimary
                    ? ' <span class="muted mp-vprimary">primary</span>'
                    : ` <code class="mp-vkey" title="Internal key — set when the variant was created and kept on rename, so it can differ from the label">${this.esc(x.key)}</code>`}</span>
                <span class="muted mp-vmeta">${sites} site${sites === 1 ? '' : 's'}</span>
                <span class="mp-vacts">
                    <button class="btn btn-sm" type="button" data-mp-vrename="${this.esc(x.key)}" title="Rename this variant">&#9998; Rename</button>
                    ${isPrimary ? '' : `<button class="btn btn-sm" type="button" data-mp-vsplit="${this.esc(x.key)}"
                        title="Separate this variant into its own Masterpiece — its image and site-links go with it">&#10548; Separate</button>`}
                </span>
            </div>`;
        }).join('');
        // Undeclared alts (3.36.0): a multi-image import attaches every image
        // from one source post to a single record, so these can be entirely
        // different artworks. Same Separate, which declares + splits in one call.
        const altRows = (!variants.length && imgs.length > 1) ? imgs.slice(1).map((f, i) => `
            <div class="mp-vrow" data-vimage="${this.esc(f)}">
                <span class="mp-vname">Alt ${i + 1} <code class="mp-vkey" title="The file in this piece's folder">${this.esc(f)}</code></span>
                <span class="muted mp-vmeta">no site links of its own</span>
                <span class="mp-vacts">
                    <button class="btn btn-sm" type="button" data-mp-isplit="${this.esc(f)}"
                        title="Separate this image into its own Masterpiece — this piece keeps its links and stats">&#10548; Separate</button>
                </span>
            </div>`).join('') : '';
        const manageNote = variants.length
            ? `Separating undoes a variant merge: the image moves to a new Masterpiece and its site-links follow, keeping their stats.`
            : `These extra images came in together from one source post, so some may be different pieces entirely. Separating moves one into its own Masterpiece; this piece keeps every site link and stat, because none of them belong to the alt. The new record is hashed, so “Link the same image elsewhere” can find its real uploads.`;
        const count = variants.length || imgs.length || 1;
        const manager = (declaredRows || altRows)
            ? `<div class="mp-vadmin-body">${declaredRows}${altRows}<p class="muted mp-vadmin-note">${manageNote}</p></div>`
            : `<p class="muted" style="font-size:12.5px;margin:0">One render. Add a variant to keep another version of this piece alongside it.</p>`;
        return `
            <details class="card renders-card">
                <summary>Renders <span class="muted">— ${count} image${count === 1 ? '' : 's'}; replace, rename, separate</span></summary>
                <div class="body">
                    ${manager}
                    <div class="renders-acts">
                        <label class="btn btn-sm" title="Swap in a better/higher-res version — keeps this record, its tags and every site link. The old file stays as a gallery alternate.">
                            ⇪ Replace image
                            <input type="file" id="mp-replace-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
                        </label>
                        <label class="btn btn-sm" title="Upload another render (SFW/NSFW/rough…) straight in as a labeled variant of this piece.">
                            ＋ Add variant
                            <input type="file" id="mp-addvariant-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
                        </label>
                        <span id="mp-replace-msg" class="muted"></span>
                    </div>
                </div>
            </details>`;
    },

    /* ── Tag chips (§5.3, §13) ──────────────────────────────────────────── */
    /* Render the chips from #mp-e-tags. Core / cut styling comes from the last
     * tag-preview (_loadTagBudget → this._budget); with none yet, plain chips:
     * the tags are real data the page already holds, and hiding them would
     * lose more than the annotation is worth (§11). */
    _tagChips() {
        const ta = document.getElementById('mp-e-tags');
        const host = document.getElementById('mp-tagchips');
        if (!ta || !host) return;
        const tags = ta.value.split(',').map(x => x.trim()).filter(Boolean);
        const d = this._budget || null;
        const core = new Set(((d && d.canonical) ? d.canonical.slice(0, d.core_count || 0) : []).map(x => String(x).toLowerCase()));
        const cutBy = {};
        ((d && d.platforms) || []).forEach(p => (p.dropped || []).forEach(t => {
            const k = String(t).toLowerCase();
            (cutBy[k] = cutBy[k] || []).push(this._PLATFORM_LABELS[p.platform] || p.platform);
        }));
        const chips = tags.map(t => {
            const k = t.toLowerCase();
            const cls = 'tagchip' + (core.has(k) ? ' tagchip--core' : '') + (cutBy[k] ? ' tagchip--cut' : '');
            const title = cutBy[k] ? ` title="Cut on ${this.esc(cutBy[k].join(', '))}"`
                : (core.has(k) ? ' title="Core — kept on every site"' : '');
            return `<li class="${cls}"${title}><b>${this.esc(t)}</b><button type="button" class="x" data-mp-chip-x="${this.esc(t)}" aria-label="Remove tag ${this.esc(t)}">×</button></li>`;
        }).join('');
        host.innerHTML = chips
            + (tags.length ? '' : `<li class="tag-empty">No tags yet — add some, or browse the library.</li>`)
            + `<li class="tagchip-slot"><button type="button" class="tagchip-add" data-mp-chip-add>+ add tag</button>
               <input type="text" class="tagchip-input" id="mp-tag-add" placeholder="tag, another tag" hidden aria-label="Add tags"></li>`;
        const n = document.getElementById('mp-tagcount'); if (n) n.textContent = String(tags.length);
        const c = document.getElementById('mp-corecount'); if (c) c.textContent = d ? String(d.core_count || 0) : '–';
    },

    _tagsFromTextarea() {
        const ta = document.getElementById('mp-e-tags');
        return ta ? ta.value.split(',').map(x => x.trim()).filter(Boolean) : [];
    },

    _setTags(list) {
        const ta = document.getElementById('mp-e-tags');
        if (!ta) return;
        ta.value = list.join(', ');
        this._tagChips();
    },

    _addTagsFromInput(input) {
        const add = input.value.split(',').map(x => x.trim()).filter(Boolean);
        input.value = '';
        input.hidden = true;
        if (add.length) {
            const cur = this._tagsFromTextarea();
            const seen = new Set(cur.map(x => x.toLowerCase()));
            add.forEach(t => { if (!seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); cur.push(t); } });
            this._setTags(cur);
        }
        const btn = document.querySelector('[data-mp-chip-add]');
        if (btn) btn.focus();
    },

    _removeTag(tag) {
        const cur = this._tagsFromTextarea();
        const i = cur.findIndex(x => x.toLowerCase() === String(tag).toLowerCase());
        if (i === -1) return;
        cur.splice(i, 1);
        this._setTags(cur);
        // Focus the next chip's ×, or + add when it was the last — otherwise
        // focus falls to <body> and a keyboard user loses their place (§13).
        const xs = document.querySelectorAll('#mp-tagchips [data-mp-chip-x]');
        const next = xs[Math.min(i, xs.length - 1)] || document.querySelector('[data-mp-chip-add]');
        if (next) next.focus();
    },

    /* The hero tile's full-size view (§5.2). Carries the hero's class and
     * data-rating so SFW mode blurs it exactly as it blurs the tile. */
    _openLightbox() {
        const img = document.getElementById('mp-hero-img');
        if (!img || !img.src) return;
        const ov = document.createElement('div');
        ov.className = 'modal-overlay open mp-lightbox';
        ov.setAttribute('role', 'dialog');
        ov.setAttribute('aria-label', 'Full-size render');
        ov.innerHTML = `<img class="mp-hero-img" data-rating="${this.esc(img.dataset.rating || '')}" src="${this.esc(img.src)}" alt="${this.esc(img.alt || '')}">`;
        const close = () => { document.removeEventListener('keydown', onKey); ov.remove(); img.closest('[data-mp-lightbox]')?.focus(); };
        const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } };
        ov.addEventListener('click', close);
        document.addEventListener('keydown', onKey);
        document.body.appendChild(ov);
    },

    /* ── Publish / schedule / delete (ported from the Artwork detail, 2.193.0) ──
     * The old Artwork page owned these four capabilities and nothing else the
     * Masterpiece page didn't already do better, so they move here and this
     * becomes the single art detail page.
     *
     * Two latent bugs are fixed in the move:
     *  1. The old code queried '.art-plat-row' but _renderPlatformRows emits
     *     'artwork-plat-row', so the "already posted → dim and disable" pass had
     *     never once fired. Corrected selector below.
     *  2. The per-platform "Override tags" inputs were rendered but never read by
     *     the publish or schedule paths — typed overrides were silently dropped.
     *     _platformOverrides() now collects them for both. */
    async _wireDetailPublish(name, m) {
        const host = document.getElementById('mp-detail-platforms');
        if (!host || !window.Artwork) return;

        // Same shared rows as the artwork form, seeded with this piece's
        // saved Telegram options so they show what is actually stored.
        const _live = [...new Set([
            ...(m.publications || []).filter(p => p.status === 'posted' && p.external_url).map(p => p.platform),
            ...(m.locations || []).filter(l => l.url).map(l => l.platform),
        ])].filter(Boolean);
        const cats = (this._detail || {}).categories || {};
        const hasDescs = !!(m.descriptions && typeof m.descriptions === 'object');
        const optsByCode = {}, extraByCode = {};
        // One panel per announcer (Telegram 4.0.10/4.3.0; X and Bluesky 4.3.7).
        // The text box only where the record exposes descriptions, so a save can
        // never clobber the others; each panel's link list leaves itself out.
        (window.Artwork._ANNOUNCERS || ['tg']).forEach(code => {
            optsByCode[code] = cats[code] || {};
            extraByCode[code] = { code,
                desc: hasDescs ? (m.descriptions[code] || '') : undefined,
                live: _live.filter(c => c !== code) };
        });
        window.Artwork._renderPlatformRows(host, optsByCode, extraByCode);

        // Dim + disable platforms this piece is already posted to. Both the
        // publications list and the resolved member locations count as "posted",
        // since a linked upload IS a live post on that site.
        const posted = new Set([
            ...(m.publications || []).filter(p => p.status === 'posted').map(p => p.platform),
            ...(m.locations || []).map(l => l.platform),
        ].filter(Boolean));
        host.querySelectorAll('.artwork-plat-row').forEach(row => {
            const platform = row.dataset.platform;
            if (!posted.has(platform)) return;
            row.style.opacity = '.5';
            const cb = row.querySelector('.art-plat-check');
            if (cb) { cb.disabled = true; cb.title = 'Already on this site'; }
            // A dimmed row is a dead end when the upload no longer exists:
            // PawPoller cannot see a deletion made on the site, so a post you
            // removed upstream (wrong account, wrong file) keeps this platform
            // locked forever. "Forget" clears the local records; the toast
            // hands back the URL so it can be re-linked. (3.17.0)
            if (row.querySelector('.mp-forget-pub')) return;
            const btn = document.createElement('button');
            btn.className = 'btn-tiny mp-forget-pub';
            btn.type = 'button';
            btn.textContent = 'Forget';
            btn.title = 'Deleted this post on the site? Clear it here to post again.';
            btn.onclick = () => this._forgetPublication(name, platform);
            row.appendChild(btn);
        });
        await window.Artwork._populateAccountSelectors();
        this._loadScheduled(name);
    },

    /* Clear the local record of a post that no longer exists upstream.
       Confirmed because it discards `first_posted_at` and the posted title for
       that platform; the toast returns the URL, and "Link a post by URL" takes
       it straight back, so the action is undoable rather than destructive. */
    async _forgetPublication(name, platform) {
        const P = platform.toUpperCase();
        if (!confirm(
            `Forget that this piece is on ${P}?\n\n`
            + `Use this when you deleted the post on ${P} itself. It clears `
            + `PawPoller's record so you can post again — it does NOT delete `
            + `anything on ${P}.`)) return;
        try {
            const r = await API.forgetMasterpiecePublication(name, platform);
            this._toast('success', r.external_url
                ? `${P} cleared — re-link with ${r.external_url}`
                : `${P} cleared — you can post it again`);
            await this.renderDetail(name);
        } catch (err) {
            this._toast('error', 'Could not clear: ' + (err.message || err));
        }
    },

    /* Ticked platforms + their chosen account + any per-platform tag override. */
    _publishSelection() {
        const host = document.getElementById('mp-detail-platforms');
        if (!host) return { platforms: [], accountIds: {}, overrides: {} };
        const platforms = Array.from(host.querySelectorAll('.art-plat-check:checked'))
            .map(c => c.value);
        const accountIds = {};
        host.querySelectorAll('.art-acct-select').forEach(sel => {
            if (platforms.includes(sel.dataset.platform)) {
                accountIds[sel.dataset.platform] = parseInt(sel.value, 10);
            }
        });
        const overrides = {};
        host.querySelectorAll('.art-plat-tags').forEach(inp => {
            const p = inp.dataset.platform;
            if (!platforms.includes(p)) return;
            const tags = (inp.value || '').split(',').map(s => s.trim()).filter(Boolean);
            if (tags.length) overrides[p] = tags;
        });
        return { platforms, accountIds, overrides };
    },

    /* Persist per-platform tag overrides BEFORE publishing.
     *
     * There is no tag_overrides parameter on POST /api/artwork/publish — the
     * documented mechanism is the per-platform tag map in masterpiece.json, which
     * save_artwork_metadata preserves and every poster cascades from. Writing the
     * override there (rather than inventing a request field the backend would
     * ignore) is what makes it actually take effect, and it makes the override
     * durable and visible on the record instead of a one-shot. */
    async _applyOverrides(name, overrides) {
        const keys = Object.keys(overrides || {});
        const A = window.Artwork;
        const codes = (A && A._ANNOUNCERS) || ['tg'];
        const detail = this._detail || {};
        const oldCats = detail.categories || {};
        const oldDescs = (detail.descriptions && typeof detail.descriptions === 'object') ? detail.descriptions : null;

        // Per-announcer options (Telegram since 4.0.10; X and Bluesky 4.3.7).
        // Only panels ON THE PAGE are read, so a platform with no panel keeps
        // whatever the record holds for it.
        let categories = null;
        codes.forEach(code => {
            if (!(A && A._collectPlatOpts) || !document.querySelector(`.art-tg-opt[data-platform="${code}"]`)) return;
            const o = A._collectPlatOpts(code);
            const had = !!oldCats[code];
            if (!Object.keys(o).length && !had) return;
            categories = categories || { ...oldCats };
            if (Object.keys(o).length) categories[code] = o; else delete categories[code];
        });
        // Stored per-announcer text (4.3.0) \u2014 merged into descriptions, never
        // replacing the map, and only when the record exposed it (else no box).
        let descriptions = null;
        if (oldDescs && A && A._collectPlatDesc) {
            const next = { ...oldDescs };
            codes.forEach(code => {
                const text = A._collectPlatDesc(code);
                if (text === null) return;
                if (text) next[code] = text; else delete next[code];
            });
            if (JSON.stringify(next) !== JSON.stringify(oldDescs)) descriptions = next;
        }
        if (!keys.length && !categories && !descriptions) return;

        const payload = {};
        if (descriptions) payload.descriptions = descriptions;
        if (keys.length) {
            const tags = { ...(detail.canonical_tags || {}) };
            keys.forEach(p => { tags[p] = overrides[p]; });
            payload.tags = tags;
        }
        // Options travel the same route as tag overrides: written to the record
        // before publishing, so the poster reads them from masterpiece.json
        // rather than needing a request field the backend has no parameter for.
        if (categories) payload.categories = categories;
        if (Object.keys(payload).length) await API.updateArtwork(name, payload);
    },

    async _publishNow(name) {
        const msg = document.getElementById('mp-pub-msg');
        const { platforms, accountIds, overrides } = this._publishSelection();
        if (!platforms.length) { if (msg) msg.textContent = 'Tick at least one site.'; return; }
        // Before _applyOverrides: that WRITES tag overrides into the record,
        // and a cancel after it would have silently changed the piece.
        const title = ((this._detail || {}).title) || name.replace(/_/g, ' ');
        const thumb = (document.getElementById('mp-hero-img') || {}).src || '';
        const personaId = window.Artwork ? window.Artwork._personaId('#mp-detail-platforms') : null;
        const conf = await Components.confirmPublish({
            title, thumb, subtitle: 'Masterpiece',
            persona: window.Artwork ? window.Artwork._personaLabel('#mp-detail-platforms') : '',
            targets: window.Artwork
                ? window.Artwork._confirmTargets('#mp-detail-platforms', platforms, accountIds)
                : platforms.map(code => ({ code, label: code })),
            textBoxes: window.Artwork ? window.Artwork._pubTextBoxes(platforms) : [],
        });
        if (!conf) { if (msg) msg.textContent = ''; return; }
        if (msg) msg.textContent = 'Publishing…';
        try {
            await this._applyOverrides(name, overrides);
            const res = await API.publishArtwork({
                artwork_name: name, platforms, account_ids: accountIds,
                persona_id: personaId,
                description_overrides: window.Artwork ? window.Artwork._pubDescOverrides(conf) : undefined,
                confirm_live: true,
            });
            const ok = res.successes || 0;
            const fail = Components.showPublishResults(msg, res.results);
            this._toast(fail ? 'error' : 'success',
                fail ? `${fail} of ${ok + fail} sites failed — see below` : `Published to ${ok} site${ok === 1 ? '' : 's'}`);
            if (!fail) this.renderDetail(name);
            else if (msg) msg.textContent = 'Some sites failed:';
        } catch (err) {
            if (msg) msg.textContent = 'Publish failed: ' + err.message;
        }
    },

    async _confirmSchedule(name) {
        const msg = document.getElementById('mp-pub-msg');
        const val = (document.getElementById('mp-schedule-datetime') || {}).value;
        if (!val) { if (msg) msg.textContent = 'Pick a date and time.'; return; }
        const when = new Date(val);
        if (isNaN(when.getTime())) { if (msg) msg.textContent = 'Invalid date/time.'; return; }
        if (when.getTime() < Date.now()) { if (msg) msg.textContent = 'Pick a time in the future.'; return; }
        const { platforms, accountIds, overrides } = this._publishSelection();
        if (!platforms.length) { if (msg) msg.textContent = 'Tick at least one site.'; return; }

        // datetime-local is LOCAL; toISOString() hands the backend a UTC instant,
        // so 8pm AEST fires at 8pm AEST.
        const isoStr = when.toISOString();
        if (msg) msg.textContent = 'Scheduling…';
        await this._applyOverrides(name, overrides);
        let ok = 0, fail = 0;
        for (const platform of platforms) {
            try {
                await API.scheduleArtwork({
                    artwork_name: name, platform, scheduled_at: isoStr,
                    account_id: accountIds[platform],
                });
                ok++;
            } catch (err) { fail++; console.warn('Schedule failed for', platform, err); }
        }
        this._toast(fail ? 'error' : 'success',
            `Scheduled ${ok} site${ok === 1 ? '' : 's'} for ${when.toLocaleString()}`
            + (fail ? `, ${fail} failed` : ''));
        const form = document.getElementById('mp-schedule-form');
        if (form) form.style.display = 'none';
        if (msg) msg.textContent = '';
        this._loadScheduled(name);
    },

    async _loadScheduled(name) {
        const box = document.getElementById('mp-scheduled-list');
        if (!box) return;
        let items = [];
        try {
            const resp = await API.getArtworkScheduled(name);
            items = (resp.items || []).filter(i => i.status === 'pending' && i.scheduled_at);
        } catch (e) { return; }
        if (!items.length) { box.innerHTML = ''; return; }
        items.sort((a, b) => (a.scheduled_at || '').localeCompare(b.scheduled_at || ''));
        let html = '<div class="schedule-pending-header">Scheduled</div>';
        for (const it of items) {
            // Stored 'YYYY-MM-DD HH:MM:SS' is UTC; make it a real instant, then localise.
            const when = new Date(it.scheduled_at.replace(' ', 'T') + 'Z').toLocaleString();
            const plat = (window.PLATFORMS || []).find(p => p.code === it.platform);
            html += '<div class="schedule-pending-item">'
                + '<span class="schedule-pending-icon">&#128340;</span> '
                + this.esc(plat ? plat.name : it.platform) + ' &mdash; ' + this.esc(when)
                + ' <button class="btn btn-xs btn-outline" data-mp-sched-cancel="' + it.queue_id + '">Cancel</button>'
                + '</div>';
        }
        box.innerHTML = html;
    },

    async _deletePiece(name) {
        if (!confirm('Delete this piece from your library?\n\nAny already-published posts stay live on '
            + 'each platform. If you only want it out of the grid, use 🗑 Junk instead — that keeps '
            + 'the folder and every site link.')) return;
        try {
            await API.deleteArtwork(name);
            this._toast('success', 'Deleted');
            window.location.hash = '#/library/type/artwork';
        } catch (err) {
            this._toast('error', 'Delete failed: ' + err.message);
        }
    },

    /* ── Membership management (Phase 3) ── */

    _init() {
        if (this._wired) return;
        this._wired = true;
        document.addEventListener('keydown', (e) => this._onNavKey(e));
        // The add-tag box (§13): Enter commits a comma list, Escape cancels,
        // Backspace on an empty box removes the last chip.
        document.addEventListener('keydown', (e) => {
            const inp = e.target && e.target.id === 'mp-tag-add' ? e.target : null;
            if (!inp) return;
            if (e.key === 'Enter') { e.preventDefault(); this._addTagsFromInput(inp); }
            else if (e.key === 'Escape') {
                e.preventDefault(); inp.value = ''; inp.hidden = true;
                const b = document.querySelector('[data-mp-chip-add]'); if (b) b.focus();
            } else if (e.key === 'Backspace' && !inp.value) {
                e.preventDefault();
                const xs = document.querySelectorAll('#mp-tagchips [data-mp-chip-x]');
                const last = xs[xs.length - 1];
                if (last) this._removeTag(last.dataset.mpChipX);
                const again = document.getElementById('mp-tag-add');
                if (again) { again.hidden = false; again.focus(); }
            }
        });
        // Clicking away from a half-typed tag keeps it rather than losing it.
        document.addEventListener('focusout', (e) => {
            if (e.target && e.target.id === 'mp-tag-add' && e.target.value.trim()) this._addTagsFromInput(e.target);
        });
        document.addEventListener('click', (e) => {
            // Tag chips (4.4.0) — a view over #mp-e-tags; see _tagChips.
            const chipX = e.target.closest('[data-mp-chip-x]');
            if (chipX) { e.preventDefault(); this._removeTag(chipX.dataset.mpChipX); return; }
            const chipAdd = e.target.closest('[data-mp-chip-add]');
            if (chipAdd) {
                e.preventDefault();
                const inp = document.getElementById('mp-tag-add');
                if (inp) { inp.hidden = false; inp.focus(); }
                return;
            }
            const lb = e.target.closest('[data-mp-lightbox]');
            if (lb) { e.preventDefault(); this._openLightbox(); return; }
            const bre = e.target.closest('[data-mp-budget-retry]');
            if (bre) { e.preventDefault(); this._loadTagBudget(); return; }
            const save = e.target.closest('[data-mp-save]');
            if (save) { e.preventDefault(); this._saveCanonical(); return; }
            const sync = e.target.closest('[data-mp-sync]');
            if (sync) { e.preventDefault(); this._syncAll(sync); return; }
            const tb = e.target.closest('[data-mp-tagbrowse]');
            if (tb) { e.preventDefault(); this._openTagBrowse(); return; }
            const scan = e.target.closest('[data-mp-scan]');
            if (scan) { e.preventDefault(); this._scanForMatches(scan); return; }
            const linkpick = e.target.closest('[data-mp-linkpick]');
            if (linkpick) { e.preventDefault(); this._pickLinkTarget(); return; }
            const linkurl = e.target.closest('[data-mp-linkurl]');
            if (linkurl) { e.preventDefault(); this._linkByUrl(); return; }
            const att = e.target.closest('[data-mp-attach]');
            if (att) { e.preventDefault(); this._attach(att); return; }
            const det = e.target.closest('[data-mp-detach]');
            if (det) { e.preventDefault(); this._detach(det.dataset.platform, det.dataset.sid); return; }
            const junk = e.target.closest('[data-mp-junk]');
            if (junk) { e.preventDefault(); this._setJunk(junk); return; }
            // Publish / schedule / delete — ported from the Artwork detail (2.193.0).
            const pub = e.target.closest('[data-mp-publish]');
            if (pub) { e.preventDefault(); this._publishNow(this._current); return; }
            const schedT = e.target.closest('[data-mp-schedule-toggle]');
            if (schedT) {
                e.preventDefault();
                const form = document.getElementById('mp-schedule-form');
                const input = document.getElementById('mp-schedule-datetime');
                if (form) {
                    const showing = form.style.display !== 'none';
                    form.style.display = showing ? 'none' : '';
                    if (!showing && input && !input.value && window.Artwork) {
                        input.value = window.Artwork._defaultScheduleLocal();
                    }
                }
                return;
            }
            const schedC = e.target.closest('[data-mp-schedule-cancel]');
            if (schedC) {
                e.preventDefault();
                const form = document.getElementById('mp-schedule-form');
                if (form) form.style.display = 'none';
                return;
            }
            const schedOk = e.target.closest('[data-mp-schedule-confirm]');
            if (schedOk) { e.preventDefault(); this._confirmSchedule(this._current); return; }
            const schedX = e.target.closest('[data-mp-sched-cancel]');
            if (schedX) {
                e.preventDefault();
                const qid = parseInt(schedX.dataset.mpSchedCancel, 10);
                API.cancelArtworkScheduled(this._current, qid)
                    .then(() => this._loadScheduled(this._current))
                    .catch(err => this._toast('error', 'Cancel failed: ' + err.message));
                return;
            }
            const del = e.target.closest('[data-mp-delete]');
            if (del) { e.preventDefault(); this._deletePiece(this._current); return; }
            const aedit = e.target.closest('[data-mp-artist-edit]');
            if (aedit) { e.preventDefault(); this._openArtistPicker(); return; }
            const tbe = e.target.closest('[data-mp-tbedit]');
            if (tbe) { e.preventDefault(); this._editPlatformTags(tbe.dataset.mpTbedit); return; }
            const foldPick = e.target.closest('[data-mp-fold-pick]');
            if (foldPick) { e.preventDefault(); this._pickFoldTarget(); return; }
            const fold = e.target.closest('[data-mp-fold]');
            if (fold) { e.preventDefault(); this._foldIntoAnother(); return; }
            const vren = e.target.closest('[data-mp-vrename]');
            if (vren) { e.preventDefault(); this._renameVariant(vren.dataset.mpVrename); return; }
            const vsplit = e.target.closest('[data-mp-vsplit]');
            if (vsplit) { e.preventDefault(); this._splitVariant(vsplit.dataset.mpVsplit); return; }
            const isplit = e.target.closest('[data-mp-isplit]');
            if (isplit) { e.preventDefault(); this._splitImage(isplit.dataset.mpIsplit); return; }
            const vsave = e.target.closest('[data-mp-vsave]');
            if (vsave) { e.preventDefault(); this._saveVariantName(vsave.dataset.mpVsave); return; }
            const vcancel = e.target.closest('[data-mp-vcancel]');
            if (vcancel) { e.preventDefault(); this.renderDetail(this._current); return; }
            const alt = e.target.closest('[data-mp-img]');
            if (alt) {
                e.preventDefault();
                const heroImg = document.getElementById('mp-hero-img');
                if (heroImg) heroImg.src = alt.dataset.mpImg;
                // The giant ambient backdrop follows the focused variant (2.158.0).
                const bg = document.getElementById('mp-stage-bg');
                if (bg) bg.src = alt.dataset.mpImg;
                const vs = document.getElementById('mp-vstats');
                if (vs) vs.textContent = alt.dataset.vstats || '';
                document.querySelectorAll('.mp-altwrap, .mp-alt').forEach(x =>
                    x.classList.toggle('is-active', x === alt));
                return;
            }
        });

        // Replace-image picker. Delegated (not bound in renderDetail) because
        // _init runs once; `change` bubbles, and this._current tracks the open
        // Masterpiece.
        document.addEventListener('change', (e) => {
            if (!e.target) return;
            if (e.target.id === 'mp-replace-file') {
                const f = e.target.files && e.target.files[0];
                if (f && this._current) this._replaceImage(this._current, f);
                return;
            }
            if (e.target.id === 'mp-addvariant-file') {
                const f = e.target.files && e.target.files[0];
                if (f && this._current) this._addVariantUpload(this._current, f);
                e.target.value = '';   // allow re-picking the same file
                return;
            }
            // Fold picker: show the label field only for the "variant" choice.
            if (e.target.name === 'mp-fold-kind') {
                const wrap = document.getElementById('mp-fold-vlabel-wrap');
                if (wrap) wrap.style.display = e.target.value === 'var' ? '' : 'none';
            }
        });
    },

    /* Choose the target piece via the visual WorkPicker (2.162.0) — replaces the
     * old type-a-title datalist. Masterpieces only (you fold a piece into another
     * PIECE); single-select. Stores {name,title} + reflects it in the UI. */
    _pickFoldTarget() {
        if (!window.WorkPicker) { this._toast('error', 'Picker unavailable'); return; }
        WorkPicker.open({
            title: 'Fold this piece into…',
            confirmLabel: 'Choose',
            multi: false,
            filters: ['masterpiece'],
            onConfirm: (items) => {
                const it = items[0];
                if (!it || it.member_ref === this._current) {
                    if (it) this._toast('info', "That's this same piece.");
                    return;
                }
                this._foldTarget = { name: it.member_ref, title: it.title };
                const chosen = document.getElementById('mp-fold-chosen');
                if (chosen) { chosen.textContent = `→ ${it.title}`; chosen.classList.remove('muted'); }
            },
        });
    },

    /* Fold THIS piece into a chosen other Masterpiece — the per-piece counterpart
     * of the bulk tidy-up screen. Duplicate → /merge (this image removed, same as
     * the target); Variant → /merge-as-variant (this image kept as an alternate).
     * Either way "this" folder is absorbed, so we navigate to the target after. */
    async _foldIntoAnother() {
        const msg = document.getElementById('mp-fold-msg');
        const set = t => { if (msg) msg.textContent = t || ''; };
        if (!this._foldTarget) { set('Choose a piece to fold into first.'); return; }
        const target = this._foldTarget.name;
        if (target === this._current) { set("That's this same piece."); return; }
        const kindEl = document.querySelector('input[name="mp-fold-kind"]:checked');
        const kind = kindEl ? kindEl.value : 'dup';

        if (kind === 'dup') {
            if (!window.confirm(`Fold “${this._current}” into “${target}” as a DUPLICATE?\n\n`
                + `This piece's site-links move over and THIS copy is removed (its image is the same as the other). `
                + `This can't be undone.`)) return;
            set('Merging…');
            try {
                await API.mergeMasterpieces(target, this._current);   // keep=target, drop=this
                this._cache = null;
                this._toast('success', `Merged into ${target}`);
                window.location.hash = `#/masterpieces/${encodeURIComponent(target)}`;
            } catch (e) { set('Failed: ' + (e.message || e)); }
            return;
        }

        // Variant.
        const vlabel = (document.getElementById('mp-fold-vlabel') || {}).value || '';
        const label = vlabel.trim();
        if (!label) { set('Give the variant a label (e.g. NSFW, Rough).'); return; }
        const key = label.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'variant';
        if (!window.confirm(`Fold “${this._current}” into “${target}” as the “${label}” VARIANT?\n\n`
            + `This piece moves into that Masterpiece as a labeled alternate, keeping its stats. If it has its own `
            + `variants, they come across too. You can Separate it back out later.`)) return;
        set('Folding in…');
        try {
            const r = await API.mergeAsVariant({ keep: target, absorb: this._current, key, label });
            this._cache = null;
            const extra = (r && r.variants_added > 1) ? ` (${r.variants_added} variants carried over)` : '';
            this._toast('success', `Folded into ${target} as “${label}”${extra}`);
            window.location.hash = `#/masterpieces/${encodeURIComponent(target)}`;
        } catch (e) { set('Failed: ' + (e.message || e)); }
    },

    /* Swap the canonical image for a better/higher-res version (2.153.0).
     * Non-destructive: the record, its tags and every site link survive, and the
     * OLD file stays in the folder as a gallery alternate. */
    async _replaceImage(name, file) {
        const msg = document.getElementById('mp-replace-msg');
        const set = t => { if (msg) msg.textContent = t || ''; };
        set('Uploading…');
        try {
            const res = await API.replaceMasterpieceImage(name, file);
            this._cache = null;                      // grid cover is now stale
            this._toast('success', `Image replaced (was ${res.previous})`);
            set('');
            await this.renderDetail(name);           // re-render with the new hero + gallery
        } catch (err) {
            set('Replace failed: ' + (err.message || err));
        }
    },

    /* Upload a fresh image straight in as a labeled variant (2.190.2). Prompts
       for a label, then POSTs the file to /variants/upload and re-renders. */
    async _addVariantUpload(name, file) {
        const label = (window.prompt('Label for this variant (e.g. NSFW, Rough, PFP):', '') || '').trim();
        if (!label) { this._toast('info', 'Add-variant cancelled'); return; }
        const msg = document.getElementById('mp-replace-msg');
        const set = t => { if (msg) msg.textContent = t || ''; };
        set('Uploading variant…');
        try {
            const res = await API.uploadMasterpieceVariant(name, file, label);
            this._cache = null;
            this._toast('success', `Added variant “${res.label || res.key}”`);
            set('');
            await this.renderDetail(name);
        } catch (err) {
            set('');
            this._toast('error', 'Add variant failed: ' + (err.message || err));
        }
    },

    /* Junk / restore from the detail page (2.149.0). Junking keeps the folder +
       members; it only hides the piece behind the grid's Junk view. */
    async _setJunk(btn) {
        if (!this._current) return;
        const toJunk = btn.dataset.junk === 'junk';
        if (toJunk && !window.confirm('Move this masterpiece to the junk bin? It stays on disk with all its '
            + 'site-links and can be restored any time — it just leaves the grid.')) return;
        btn.disabled = true;
        try {
            await API.setMasterpieceStatus(this._current, toJunk ? 'junk' : '');
            this._cache = null;   // grid split is stale
            this._toast('success', toJunk ? 'Moved to junk' : 'Restored to the grid');
            await this.renderDetail(this._current);
        } catch (err) {
            btn.disabled = false;
            this._toast('error', 'Failed: ' + (err.message || err));
        }
    },

    /* ── Variant management (2.189.0) ── */

    /* Swap the name cell for an inline input — no modal, CSP-safe. */
    _renameVariant(key) {
        const row = document.querySelector(`.mp-vrow[data-vkey="${CSS.escape(key)}"]`);
        if (!row || row.querySelector('.mp-vedit')) return;
        const nameEl = row.querySelector('.mp-vname');
        const current = (nameEl.textContent || '').replace(/\s*primary\s*$/i, '').trim();
        nameEl.innerHTML = `<input class="mp-vedit" type="text" value="${this.esc(current)}"
            maxlength="60" aria-label="Variant label">`;
        row.querySelector('.mp-vacts').innerHTML =
            `<button class="btn btn-sm btn-primary" type="button" data-mp-vsave="${this.esc(key)}">Save</button>
             <button class="btn btn-sm" type="button" data-mp-vcancel>Cancel</button>`;
        const inp = row.querySelector('.mp-vedit');
        inp.focus(); inp.select();
        inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this._saveVariantName(key); }
            if (e.key === 'Escape') { e.preventDefault(); this.renderDetail(this._current); }
        });
    },

    async _saveVariantName(key) {
        const row = document.querySelector(`.mp-vrow[data-vkey="${CSS.escape(key)}"]`);
        const inp = row && row.querySelector('.mp-vedit');
        const label = (inp ? inp.value : '').trim();
        if (!label) { this._toast('error', 'Give the variant a label.'); return; }
        try {
            await API.renameMasterpieceVariant(this._current, key, { label });
            this._toast('success', 'Variant renamed');
            await this.renderDetail(this._current);
        } catch (err) { this._toast('error', 'Rename failed: ' + (err.message || err)); }
    },

    /* Separate an UNDECLARED alt into its own Masterpiece (3.36.0).

       The multi-image-import case: several unrelated artworks share one record as
       bare files. One call declares the image as a variant and splits it, so a
       failure cannot strand a half-declared variant. Asks for a title up front
       because the default ("<piece> (alt_2)") is never what you want. */
    async _splitImage(image) {
        if (!this._current) return;
        const title = window.prompt(
            `Separate “${image}” into its own Masterpiece.\n\n`
            + `This piece keeps all of its site links and stats — none of them belong `
            + `to this image.\n\nTitle for the new piece (blank = name it after this one):`, '');
        if (title === null) return;
        try {
            const body = title.trim() ? { new_name: title.trim() } : {};
            const r = await API.splitMasterpieceImage(this._current, image, body);
            this._cache = null;   // the grid gained a record
            this._toast('success', `Separated into “${r.new_name}”`);
            if (window.confirm(`Created “${r.new_name}”. Open it now?`)) {
                location.hash = `#/masterpieces/${encodeURIComponent(r.new_name)}`;
            } else {
                await this.renderDetail(this._current);
            }
        } catch (err) { this._toast('error', 'Separate failed: ' + (err.message || err)); }
    },

    /* Separate a variant back out into its own Masterpiece — undoes a merge. */
    async _splitVariant(key) {
        if (!this._current) return;
        const row = document.querySelector(`.mp-vrow[data-vkey="${CSS.escape(key)}"]`);
        const label = row ? (row.querySelector('.mp-vname').textContent || key).trim() : key;
        if (!window.confirm(`Separate “${label}” into its own Masterpiece?\n\n`
            + `Its image moves to a new record and its site-links go with it, keeping their stats. `
            + `This piece keeps everything else.`)) return;
        try {
            const r = await API.splitMasterpieceVariant(this._current, key);
            this._cache = null;   // the grid gained a record
            this._toast('success', `Separated into “${r.new_name}” (${r.members_moved} site-link${r.members_moved === 1 ? '' : 's'} moved)`);
            if (window.confirm(`Created “${r.new_name}”. Open it now?`)) {
                location.hash = `#/masterpieces/${encodeURIComponent(r.new_name)}`;
            } else {
                await this.renderDetail(this._current);
            }
        } catch (err) { this._toast('error', 'Separate failed: ' + (err.message || err)); }
    },

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
        else if (window.toast && window.toast.info) window.toast.info(msg);
    },

    /* ── Canonical edit + Sync-all (Phase 5) ── */

    _readCanonical() {
        const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
        const list = (s) => s.split(',').map(x => x.trim()).filter(Boolean);
        return {
            title: val('mp-e-title').trim(),
            description: val('mp-e-desc'),
            rating: val('mp-e-rating'),
            characters: list(val('mp-e-chars')),
            tags: list(val('mp-e-tags')),
            alt_text: val('mp-e-alt').trim(),
        };
    },

    _msg(text, isErr) {
        const el = document.getElementById('mp-edit-msg');
        if (el) { el.textContent = text; el.className = 'mp-edit-msg ' + (isErr ? 'mp-err' : 'muted'); }
    },

    async _saveCanonical() {
        if (!this._current) return;
        this._msg('Saving…', false);
        try {
            await API.patchMasterpiece(this._current, this._readCanonical());
            this._toast('success', 'Canonical record saved');
            await this.renderDetail(this._current);   // reflect the new title/rating in the header
        } catch (err) {
            this._msg('Save failed: ' + (err.message || err), true);
        }
    },

    // ── Per-platform tag budgets (3.12.0) ────────────────────────────────
    //
    // The canonical set is meant to be RICH and `core` is a priority ORDER, not
    // the posted subset: every site is offered the whole list and trims from the
    // tail, so the tags that matter most survive everywhere. What was missing is
    // any way to SEE that happening — and to override it on the sites where the
    // automatic trim keeps the wrong ones.

    _PLATFORM_LABELS: {
        fa: 'FurAffinity', ib: 'Inkbunny', e621: 'e621', sf: 'SoFurry',
        ws: 'Weasyl', da: 'DeviantArt', fn: 'FurryNetwork', ik: 'Itaku', fbr: 'Furbooru',
    },

    async _loadTagBudget() {
        const box = document.getElementById('mp-tagbudget');
        if (!box || !this._current) return;
        let d;
        try {
            d = await API.getMasterpieceTagPreview(this._current);
        } catch (err) {
            // One line and a retry; the chips stay, plain (§11).
            box.innerHTML = `<div class="muted">Couldn't load the per-site view.
                <button class="btn btn-sm" type="button" data-mp-budget-retry>Retry</button></div>`;
            return;
        }
        this._budget = d;
        const rows = (d.platforms || []).map(p => {
            const label = this._PLATFORM_LABELS[p.platform] || p.platform;
            const trimmed = p.dropped.length > 0;
            const cls = p.override ? 'is-override' : (trimmed ? 'is-trimmed' : '');
            const detail = p.override
                ? `<span class="mp-tb-tag">curated</span> ${p.sent} tags`
                : trimmed
                    ? `<strong>${p.sent}</strong> of ${p.total} — <span class="mp-tb-cut">${p.dropped.length} cut</span>`
                    : `all ${p.sent}`;
            return `<div class="mp-tb-row ${cls}">
                        <span class="mp-tb-site">${this.esc(label)}</span>
                        <span class="mp-tb-lim">${this.esc(p.limit)}</span>
                        <span class="mp-tb-got">${detail}</span>
                        <button class="btn btn-sm" data-mp-tbedit="${p.platform}" type="button"
                            title="${p.override
                                ? 'Edit the curated list for this site, or clear it to go back to the automatic trim'
                                : 'Pick exactly what this site gets, instead of the automatic trim'}">${
                            p.override ? 'Edit' : 'Override'}</button>
                    </div>`;
        }).join('');
        box.innerHTML = rows || `<div class="muted">No site budgets to show.</div>`;
        this._tagChips();      // core / cut styling comes from this call (4.4.0)
    },

    /* Curating one platform. Prefilled with what that site WOULD get, so the
       starting point is the automatic answer and you only change what's wrong. */
    _editPlatformTags(platform) {
        const d = this._budget || {};
        const row = (d.platforms || []).find(p => p.platform === platform);
        if (!row || !window.TagPicker) return;
        const current = row.override
            ? null                                    // fetched below
            : (d.canonical || []).filter(t => !row.dropped.includes(t));
        const label = this._PLATFORM_LABELS[platform] || platform;

        const openWith = (selected) => TagPicker.open({
            title: `${label} tags`,
            selected,
            onConfirm: async (names) => {
                try {
                    // An empty list means "stop overriding" rather than "post no
                    // tags" — posting nothing is never what someone wants, and
                    // clearing has to be reachable from here.
                    await API.patchMasterpiece(this._current, {
                        platform_tags: { [platform]: names && names.length ? names : null },
                    });
                    this._toast('success', names && names.length
                        ? `${label} will get ${names.length} tags`
                        : `${label} is back to the automatic trim`);
                    await this._loadTagBudget();
                } catch (err) {
                    this._toast('error', 'Could not save: ' + (err.message || err));
                }
            },
        });

        if (!row.override) return openWith(current);
        API.getMasterpiece(this._current)
            .then(m => openWith(((m.canonical_tags || {})[platform]) || []))
            .catch(() => openWith([]));
    },

    // ── Artist picker (3.10.1) ───────────────────────────────────────────
    //
    // 3.10.0 shipped this as an inline form. It worked, but it sat apart from
    // the tag browser next to it on the same page — a different shape for the
    // same job. It is now the same modal, so "who drew this" and "what is it
    // tagged" are picked the same way.

    _openArtistPicker() {
        if (!this._current) return;
        if (!window.ArtistPicker) { this._toast('info', 'Artist picker unavailable'); return; }
        const m = this._detail || {};
        ArtistPicker.open({
            artist: m.artist || null,
            status: m.artist_status || '',
            onConfirm: ({ artist, status }) => this._saveArtist(artist, status),
        });
    },

    async _saveArtist(artist, status) {
        if (!this._current) return;
        try {
            // Both fields always travel: choosing an artist has to clear a stale
            // "my own work", and choosing "my own work" has to clear the artist.
            await API.patchMasterpiece(this._current, { artist, artist_status: status || '' });
            this._toast('success',
                artist ? `Artist set to ${artist.name}`
                : status === 'own' ? 'Marked as your own work'
                : status === 'unknown' ? 'Marked as artist unknown'
                : 'Artist cleared');
            await this.renderDetail(this._current);
        } catch (err) {
            this._toast('error', 'Could not save the artist: ' + (err.message || err));
        }
    },

    async _syncAll(btn) {
        if (!this._current) return;
        // 4.2.0 (spec §10 Q4): sync gets the shared dialog. It creates nothing
        // but OVERWRITES title/description/tags/rating on every live upload,
        // so it lists the sites it will rewrite; post-only ones show struck.
        const m = this._detail || {};
        const seen = new Set();
        const targets = (m.locations || [])
            .filter(l => l.platform && !seen.has(l.platform) && seen.add(l.platform))
            .map(l => {
                const p = this._plat(l.platform);
                return { code: l.platform, label: p.label, emoji: p.emoji,
                         disabled: this._POST_ONLY.has(l.platform), reason: 'post-only — can\'t be edited in place' };
            });
        if (!(await Components.confirmPublish({
            title: m.title || this._current.replace(/_/g, ' '),
            thumb: (document.getElementById('mp-hero-img') || {}).src || '',
            subtitle: 'Sync canonical record', verb: 'Sync', noun: 'sites', targets,
            warning: 'Overwrites the title, description, tags and rating on each live upload. Nothing is re-uploaded.',
        }))) return;
        btn.disabled = true;
        const msg = document.getElementById('mp-edit-msg');
        try {
            await API.patchMasterpiece(this._current, this._readCanonical());   // save first, then push
            this._msg('Syncing…', false);
            const res = await API.syncMasterpiece(this._current, { confirm_live: true });
            const fail = Components.showPublishResults(msg, res.results, { okText: 'Synced' });
            const parts = [`synced ${res.synced}`];
            if (res.skipped) parts.push(`${res.skipped} post-only`);
            if (fail) parts.push(`${fail} failed`);
            this._toast(fail ? 'warn' : 'success', 'Sync: ' + parts.join(' · '));
            this._msg('Sync: ' + parts.join(' · '), !!fail);
        } catch (err) {
            this._msg('Sync failed: ' + (err.message || err), true);
        } finally {
            btn.disabled = false;
        }
    },

    _openTagBrowse() {
        const input = document.getElementById('mp-e-tags');
        if (!input || !window.TagPicker) { this._toast('info', 'Tag browser unavailable'); return; }
        const selected = input.value.split(',').map(x => x.trim()).filter(Boolean);
        TagPicker.open({
            title: 'Canonical tags',
            selected,
            onConfirm: (names) => { input.value = (names || []).join(', '); this._tagChips(); },
        });
    },

    async _loadSuggestions() {
        const body = document.getElementById('mp-suggest-body');
        if (!body || !this._current) return;
        let sug = [];
        try {
            const d = await API.getMasterpieceSuggestions(this._current);
            sug = (d && d.suggestions) || [];
        } catch { body.innerHTML = `<div class="muted">Couldn't load suggestions.</div>`; return; }
        if (!sug.length) {
            body.innerHTML = `<div class="muted">No matches found yet. If you've uploaded this image elsewhere,
                hit <strong>Scan for matches</strong> above to hash platform thumbnails and look again.</div>`;
            return;
        }
        body.className = '';
        body.innerHTML = `<div class="mp-suggest-grid">${sug.map(s => this._suggestCard(s)).join('')}</div>`;
    },

    _suggestCard(s) {
        const p = this._plat(s.platform);
        const thumbUrl = this._thumbSrc(s.platform, s.thumbnail_url);
        const thumb = thumbUrl
            ? `<img class="mp-suggest-thumb" src="${this.esc(thumbUrl)}" alt="" loading="lazy">`
            : `<div class="mp-suggest-thumb"></div>`;
        const pct = Math.round((s.similarity || 0) * 100);
        return `
            <div class="mp-suggest">
                ${thumb}
                <div class="mp-suggest-body">
                    <div class="mp-suggest-title" title="${this.esc(s.title || '')}">${this.esc(s.title || ('#' + s.submission_id))}</div>
                    <div class="mp-suggest-meta">${p.emoji || ''} ${this.esc(p.label)} · ${pct}% match</div>
                    <button class="btn btn-sm btn-primary" data-mp-attach
                        data-platform="${this.esc(s.platform)}" data-sid="${this.esc(s.submission_id)}"
                        data-account="${s.account_id != null ? this.esc(s.account_id) : ''}">＋ Link</button>
                </div>
            </div>`;
    },

    async _scanForMatches(btn) {
        const orig = btn.textContent;
        btn.disabled = true; btn.textContent = 'Scanning…';
        try {
            if (API.scanImageHashes) await API.scanImageHashes();
            await this._loadSuggestions();
            this._toast('success', 'Scan complete');
        } catch (err) {
            this._toast('error', 'Scan failed: ' + (err.message || err));
        } finally {
            btn.disabled = false; btn.textContent = orig;
        }
    },

    /* Manually link a discovered submission as a same-image member — the picker
     * counterpart to the pHash auto-suggestions (2.162.0). For when the scan
     * misses a copy (thumbnail not hashed, cropped, etc.). */
    _pickLinkTarget() {
        if (!this._current) return;
        if (!window.WorkPicker) { this._toast('error', 'Picker unavailable'); return; }
        const name = this._current;
        WorkPicker.open({
            title: 'Link a copy from another site',
            confirmLabel: 'Link',
            multi: false,
            filters: ['discovered'],
            onConfirm: async (items) => {
                const it = items[0];
                if (!it) return;
                const idx = it.member_ref.indexOf(':');
                const platform = it.member_ref.slice(0, idx);
                const sid = it.member_ref.slice(idx + 1);
                await API.addMasterpieceMember(name, { platform, submission_id: sid, linked_via: 'manual' });
                this._toast('success', 'Linked');
                await this.renderDetail(name);
            },
        });
    },

    /* Link a site upload from a pasted URL (3.14.0).
     *
     * Two-step on purpose: resolve and REPORT, then link on confirmation. A URL
     * can resolve to an id nobody has stored, or to a post already attached
     * elsewhere, and both of those are worth saying out loud — the second is the
     * common case that sends people looking for this feature ("it IS on FA, why
     * isn't it here"), and silently linking would hide the real answer. */
    async _linkByUrl() {
        if (!this._current) return;
        const name = this._current;
        const url = prompt('Paste the post URL (FurAffinity, e621, Inkbunny, SoFurry, Weasyl, Itaku, DeviantArt, X, Bluesky…):', '');
        if (!url || !url.trim()) return;
        let d;
        try {
            d = await API.linkMasterpieceByUrl(name, { url: url.trim() });
        } catch (err) {
            this._toast('error', err.message || String(err));
            return;
        }
        const b = d && d.best;
        if (!b) { this._toast('error', 'Could not read that link'); return; }
        const where = this._plat(b.platform);
        if (!b.known) {
            this._toast('error',
                `${where.label} ${b.submission_id} isn't in the database yet — poll ${where.label} first.`);
            return;
        }
        if (b.linked_to && b.linked_to !== name) {
            this._toast('error', `Already linked to "${b.linked_to}" — unlink it there first.`);
            return;
        }
        if (b.linked_to === name) { this._toast('info', 'Already linked to this piece.'); return; }
        // Say what was found, including the record it is currently filed under —
        // that is the diagnosis, not just a confirmation prompt.
        const filed = b.publication_of && b.publication_of !== name
            ? `

Note: currently recorded as its own work, "${b.publication_of}".`
            : '';
        const ok = confirm(
            `Link this upload?

${where.label} · ${b.title || ('#' + b.submission_id)}${filed}`);
        if (!ok) return;
        try {
            await API.linkMasterpieceByUrl(name, { url: url.trim(), confirm: true });
            this._toast('success', 'Linked');
            await this.renderDetail(name);
        } catch (err) {
            this._toast('error', err.message || String(err));
        }
    },

    async _attach(btn) {
        if (!this._current) return;
        const platform = btn.dataset.platform, sid = btn.dataset.sid;
        const account = btn.dataset.account;
        btn.disabled = true; btn.textContent = 'Linking…';
        try {
            const body = { platform, submission_id: sid, linked_via: 'phash' };
            if (account) body.account_id = parseInt(account, 10);
            await API.addMasterpieceMember(this._current, body);
            this._toast('success', 'Linked');
            await this.renderDetail(this._current);   // re-pool stats + refresh suggestions
        } catch (err) {
            btn.disabled = false; btn.textContent = '＋ Link';
            this._toast('error', 'Link failed: ' + (err.message || err));
        }
    },

    async _detach(platform, sid) {
        if (!this._current) return;
        try {
            await API.removeMasterpieceMember(this._current, platform, sid);
            this._toast('success', 'Unlinked');
            await this.renderDetail(this._current);
        } catch (err) {
            this._toast('error', 'Unlink failed: ' + (err.message || err));
        }
    },

    async _loadChart(name) {
        try {
            const snap = await API.getMasterpieceSnapshots(name);
            const rows = (snap && snap.snapshots) || [];
            if (rows.length > 1 && window.Charts) {
                const card = document.getElementById('mp-chart-card');
                if (card) card.style.display = '';
                Charts.aggregateLine('mp-combined-chart', rows, ['views', 'favorites_count', 'comments_count']);
            }
        } catch { /* chart is best-effort */ }
    },
};
