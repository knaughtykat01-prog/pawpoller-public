/* ── Bookshelf — the Library (concept-layer Slice A · "Atelier") ──────────────
 *
 * A cover-forward, editorial take on the works library: your stories + artwork
 * as a shelf of covers ("the cover speaks the truth" — publish status reads off
 * each spine), plus a rich per-work detail page (big cover · per-platform
 * "published to" list with live counts · chapter × platform reach).
 *
 * THE single works hub (2.155.0, backlog L). It began as one of three
 * overlapping hubs — Library + Stories (#/posting) + Artwork (#/artwork) — which
 * showed largely the SAME records: /api/works already returns both kinds behind a
 * `content_type` discriminator, so "Stories" was /api/works filtered to stories
 * with no sort/search, and "Artwork" was /api/works filtered to artwork plus a
 * discovered-tile surface. Both are now segments here and their hub routes
 * redirect in. Deep-link a segment with #/library/type/{story|artwork|
 * masterpiece|discovered}.
 *
 * Reuses the real endpoints, adds almost no backend —
 *   - list          → API.getWorks()            (GET /api/works)
 *   - story detail  → API.getPostingStory(name) (GET /api/posting/stories/{name})
 *   - masterpieces  → Masterpieces.renderGrid()     (its own managed surface)
 *   - discovered    → Submissions.renderDiscoveredInto()  (the review surface)
 * DETAIL routes are deliberately untouched — merging the hubs doesn't merge the
 * pages behind them. Artwork keeps #/artwork/image/{name}; only the richer STORY
 * detail is rebuilt here (the one with chapters + per-platform).
 *
 * Template-string rendering + a document-level click delegate for filters, to
 * match the rest of the SPA (no build step, CSP-safe — no inline handlers).
 */
window.Bookshelf = {
    _works: [],
    _personas: [],
    _type: 'all',      // all | story | artwork | masterpiece | discovered
    _persona: 0,       // 0 = all
    _search: '',
    _sort: 'recent',   // recent | title | platforms
    _status: 'all',    // all | posted | drafts — filter by publish state
    _discCount: 0,     // discovered-segment badge (filled by _loadDiscovered)

    /* Valid #/library/type/{t} targets — guards the deep-link + the redirects
       from the retired hubs against typos silently showing an empty shelf. */
    TYPES: ['all', 'story', 'artwork', 'masterpiece', 'discovered', 'unfiled'],

    esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '', color: '#888' };
    },

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
        else if (window.toast && window.toast.info) window.toast.info(msg);
    },

    _num(n) {
        return (window.Utils && Utils.formatNumber) ? Utils.formatNumber(n || 0) : String(n || 0);
    },

    /* Per-platform metric names differ (views/hits/reads, faves/kudos/votes);
       pull the first present. */
    _pick(stats, keys) {
        if (!stats) return 0;
        for (const k of keys) if (stats[k] != null) return Number(stats[k]) || 0;
        return 0;
    },
    _views(s) { return this._pick(s, ['views', 'hits', 'reads']); },
    _faves(s) { return this._pick(s, ['favorites_count', 'kudos', 'votes', 'favorites']); },
    _comments(s) { return this._pick(s, ['comments_count', 'comments']); },

    /* ── Library home ──────────────────────────────────────────── */

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="shelf-topbar">
                <div class="shelf-head">
                    <div class="shelf-eyebrow">Your works</div>
                    <h1 class="shelf-title">Library</h1>
                    <p class="shelf-sub">Every story and piece you've made, on the shelf — each cover
                    carries its own truth: where it's live, and where it isn't yet.</p>
                </div>
                <div class="shelf-topbar-actions">
                    <button class="btn btn-secondary btn-sm" id="shelf-view-btn" type="button"
                        title="Switch to the animated shelf view — the Library will open there until you switch back">▤ Shelf view</button>
                    <a class="btn btn-secondary shelf-laurels" href="#/laurels" title="Your milestones, medals and trophies">
                        <span aria-hidden="true">🏅</span> Laurels
                    </a>
                    <a class="btn btn-secondary btn-sm" href="#/artwork/ignored" title="Discovered pieces you've dismissed">Ignored</a>
                    <a class="btn btn-secondary btn-sm" href="#/artwork/log" title="Artwork posting history">History</a>
                </div>
            </div>
            <div id="shelf-discovered"></div>
            <div id="shelf-controls"></div>
            <div id="shelf-grid"><div class="loading-spinner">Loading your shelf…</div></div>`;

        // "▤ Shelf view" — switch to the Showcase AND remember it as the
        // Library's opening view (2.158.0; "✕ Classic view" remembers back).
        const shelfBtn = document.getElementById('shelf-view-btn');
        if (shelfBtn) shelfBtn.addEventListener('click', () => {
            try { localStorage.setItem('pp_library_view', 'shelves'); } catch { /* still switches */ }
            if (window.Showcase) {
                try { history.replaceState(null, '', '#/library'); } catch { /* non-fatal */ }
                window.Showcase.renderLibrary();
            }
        });

        let data;
        try {
            data = await API.getWorks();
        } catch (err) {
            document.getElementById('shelf-grid').innerHTML =
                `<div class="card error">Couldn't open the library: ${this.esc(err.message)}</div>`;
            return;
        }
        this._works = (data && data.works) || [];
        this._personas = (data && data.personas) || [];
        // Fresh masterpiece data per Library open (the grid is lazy-loaded on first
        // switch to the Masterpieces segment; this just drops any stale cache).
        if (window.Masterpieces && Masterpieces.resetCache) Masterpieces.resetCache();
        this._renderControls();
        this._paint();
        this._loadDiscovered();   // discovered-art import banner (moved from Submissions)
    },

    /* Discovered-art import banner — ported from the retired Submissions hub.
     * Also feeds the Discovered segment's count badge. Best-effort; never blocks
     * the shelf (a failed fetch just leaves the banner and badge off).
     *
     * Banner vs segment: the banner is the NUDGE plus its one bulk action; the
     * segment is the review surface. So the banner counts importable ART, while
     * the badge counts EVERY discovered item — the segment shows stories too. */
    async _loadDiscovered() {
        const slot = document.getElementById('shelf-discovered');
        if (!slot) return;
        let art = [];
        try {
            const disc = await API.getDiscovered();
            const all = (disc && disc.discovered) || [];
            this._discCount = all.length;
            art = all.filter(d => d.kind === 'art' && d.thumbnail_url);
        } catch { return; }
        // Patch just the badge, not the whole control bar: _renderControls()
        // rebuilds the search input, which would steal focus and drop the caret
        // if this fetch lands while you're already typing.
        this._paintDiscCount();
        if (!art.length) { slot.innerHTML = ''; return; }
        const one = art.length === 1;
        slot.innerHTML = `
            <div class="shelf-discovered-banner">
                <div><strong>${art.length} discovered art piece${one ? '' : 's'}</strong> from your polling
                ${one ? "isn't" : "aren't"} in your library yet — import ${one ? 'it' : 'them'} to manage and re-post.</div>
                <div class="shelf-discovered-actions">
                    <button class="btn btn-primary btn-sm" id="shelf-import-art">Import all art</button>
                    <button class="btn btn-sm" id="shelf-review-disc" type="button">Review →</button>
                </div>
            </div>`;
        const b = document.getElementById('shelf-import-art');
        if (b) b.addEventListener('click', () => this._importAllArt());
        const r = document.getElementById('shelf-review-disc');
        if (r) r.addEventListener('click', () => this.switchType('discovered'));
    },

    async _importAllArt() {
        const b = document.getElementById('shelf-import-art');
        if (b) { b.disabled = true; b.textContent = 'Importing…'; }
        try {
            const res = await API.importDiscoveredArt();
            const bits = [`imported ${res.imported}`];
            if (res.failed) bits.push(`${res.failed} failed`);
            this._toast(res.imported ? 'success' : (res.failed ? 'warn' : 'info'),
                `Discovered art: ${bits.join(', ')}`);
            await this.render();   // refresh shelf + banner
        } catch (err) {
            this._toast('error', `Import failed: ${this.esc(err.message || err)}`);
            if (b) { b.disabled = false; b.textContent = 'Import all art'; }
        }
    },

    _discLabel() {
        return this._discCount
            ? `Discovered <span class="shelf-seg-count">${this._discCount}</span>` : 'Discovered';
    },

    /* Write _discCount into the Discovered segment in place. Safe to call before
       the controls exist (they render the badge from _discCount anyway). */
    _paintDiscCount() {
        const b = document.querySelector('[data-shelf-type="discovered"]');
        if (b) b.innerHTML = this._discLabel();
    },

    _renderControls() {
        const el = document.getElementById('shelf-controls');
        if (!el) return;
        const seg = (val, label) => `
            <button class="shelf-seg ${this._type === val ? 'is-active' : ''}" data-shelf-type="${val}"
                type="button">${label}</button>`;
        // Discovered is a REVIEW queue, not a shelf of your works: it renders its
        // own rows and its own per-platform bulk bar, so the shelf's persona /
        // search / sort controls don't apply and would just be dead inputs.
        // Search / sort / status filter operate on the cached works list, which
        // neither of these segments renders — showing controls that do nothing
        // is worse than showing none.
        const isDisc = this._type === 'discovered' || this._type === 'unfiled';
        const personaSel = (!isDisc && this._personas.length > 1) ? `
            <select id="shelf-persona" class="shelf-input">
                <option value="0">All personas</option>
                ${this._personas.map(p => `<option value="${p.id}"${p.id === this._persona ? ' selected' : ''}>${this.esc(p.name)}</option>`).join('')}
            </select>` : '';
        // The Junk option appears only once something IS junked (or while you are
        // looking at the bin) - an always-present filter for an empty bin is noise.
        // Mirrors the Masterpieces grid's `(junked.length || this._junkView)`.
        const junkCount = this._works.filter(w => w.is_junk).length;
        const junkOpt = (junkCount || this._status === 'junk')
            ? `<option value="junk">🗑 Junk (${junkCount})</option>` : '';
        const shelfControls = isDisc ? '' : `
                ${personaSel}
                <input id="shelf-search" class="shelf-input" type="search" placeholder="Search — try tag:white_tiger -tag:cum artist:…" title="Bare words match title/name. Fields: tag: platform: artist: persona: rating: type: series: status:  —  prefix with - (or use tag_exclude:) to exclude, comma for or, * for wildcard, quotes for spaces. e.g. tag:white_tiger -tag:cum status:draft" value="${this.esc(this._search)}">
                <select id="shelf-sort" class="shelf-input shelf-sort">
                    <option value="recent">Recently posted</option>
                    <option value="added">Recently added</option>
                    <option value="title">Title A–Z</option>
                    <option value="platforms">Most platforms</option>
                    <option value="views">Most viewed</option>
                    <option value="favorites">Most favourited</option>
                    <option value="comments">Most comments</option>
                    <option value="series">Series</option>
                </select>
                <select id="shelf-status" class="shelf-input shelf-sort" title="Filter by publish state">
                    <option value="all">All works</option>
                    <option value="posted">Posted</option>
                    <option value="drafts">Drafts</option>
                    <option value="unattributed">Missing artist</option>
                    ${junkOpt}
                </select>`;
        el.innerHTML = `
            <div class="shelf-controls">
                <div class="shelf-segs">${seg('all', 'All')}${seg('story', 'Stories')}${seg('artwork', 'Artwork')}${seg('masterpiece', 'Masterpieces')}${seg('discovered', this._discLabel())}${seg('unfiled', 'Unfiled')}</div>
                ${shelfControls}
            </div>`;

        el.querySelectorAll('[data-shelf-type]').forEach(b =>
            b.addEventListener('click', () => this.switchType(b.dataset.shelfType)));
        const ps = el.querySelector('#shelf-persona');
        if (ps) ps.addEventListener('change', () => { this._persona = parseInt(ps.value) || 0; this._paint(); });
        const se = el.querySelector('#shelf-search');
        if (se) se.addEventListener('input', () => { this._search = se.value; this._paint(); });
        const so = el.querySelector('#shelf-sort');
        if (so) { so.value = this._sort; so.addEventListener('change', () => { this._sort = so.value; this._paint(); }); }
        const st = el.querySelector('#shelf-status');
        if (st) { st.value = this._status; st.addEventListener('change', () => { this._status = st.value; this._paint(); }); }
    },

    /* Switch segment IN PLACE — no re-fetch, no router round-trip (the works are
     * already cached in _works). replaceState, not location.hash: assigning the
     * hash would fire hashchange → route() → a full render() and another
     * /api/works call just to show a filter of data we're already holding.
     * replaceState still leaves a URL you can refresh, bookmark or share. */
    switchType(t) {
        if (!this.TYPES.includes(t)) return;
        this._type = t;
        try {
            // 'all' writes /browse, not bare #/library — the bare route is the
            // Showcase shelves (2.158.0); this keeps refreshes on the classic grid.
            const url = t === 'all' ? '#/library/browse' : `#/library/type/${t}`;
            history.replaceState(null, '', url);
        } catch { /* non-fatal — the segment still switches */ }
        this._renderControls();
        this._paint();
    },

    _filtered() {
        let list = this._works.slice();
        if (this._type !== 'all') list = list.filter(w => w.content_type === this._type);
        if (this._persona) list = list.filter(w => (w.persona_ids || []).includes(this._persona));
        // Parsed once, up front: the junk rule below needs to know whether the
        // query itself asked for junk (3.14.0), so it can't wait for the search
        // step at the bottom.
        const parsed = (this._search && window.SearchQuery)
            ? SearchQuery.parse(this._search) : null;

        // Junk (3.13.1): 'junk' means kept-but-HIDDEN — the folder and members
        // survive, the grid stops showing it. The Masterpieces grid has worked
        // this way since 2.149.0; the Library never did, so junking a piece hid
        // it from one surface and left it sitting in the other. Junked works are
        // excluded from EVERY view except the explicit Junk filter, including
        // Posted/Drafts/Missing artist — a hidden piece should not reappear
        // because you narrowed to drafts.
        //
        // The one other way in is asking for it by name: `status:junk` in the
        // search box (3.14.0). Typing that and getting nothing back would be the
        // filter lying to you, so the hide stands aside for it.
        if (this._status === 'junk') list = list.filter(w => w.is_junk);
        else if (!(parsed && SearchQuery.wantsJunk(parsed))) list = list.filter(w => !w.is_junk);
        // Publish-state filter (2.199.0): a work is "posted" once it's live on ≥1
        // platform, else it's a local draft. Uses publication_count already on
        // each work — no extra fetch.
        if (this._status === 'posted') list = list.filter(w => (w.publication_count || 0) > 0);
        else if (this._status === 'drafts') list = list.filter(w => (w.publication_count || 0) === 0);
        // Attribution filter (3.5.2). Only artwork can be unattributed — a story
        // has an author, not an artist — so stories are excluded rather than
        // shown as permanently "missing".
        else if (this._status === 'unattributed') {
            list = list.filter(w => w.content_type !== 'story' && w.needs_artist);
        }
        // Field-scoped search (3.14.0): `tag:`, `-tag:`/`tag_exclude:`, `artist:`,
        // `platform:`, `persona:`, `rating:`, `type:`, `series:`, `status:`, with
        // bare words still meaning title/name. Falls back to the old substring
        // match if the module failed to load, so a bad deploy degrades to the
        // previous behaviour rather than to a shelf that ignores what you type.
        if (this._search) {
            if (parsed) {
                list = list.filter(w => SearchQuery.match(w, parsed));
            } else {
                const q = this._search.toLowerCase();
                list = list.filter(w => (w.title || '').toLowerCase().includes(q) || (w.name || '').toLowerCase().includes(q));
            }
        }
        if (this._sort === 'title') list.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
        else if (this._sort === 'platforms') list.sort((a, b) => (b.platforms || []).length - (a.platforms || []).length);
        // Group by series (gap-wave-5 §2): series-less works sink to the bottom,
        // then within a series they order by index, then title.
        else if (this._sort === 'series') list.sort((a, b) => {
            const sa = a.series || '', sb = b.series || '';
            if (!sa && !sb) return (a.title || '').localeCompare(b.title || '');
            if (!sa) return 1;
            if (!sb) return -1;
            const byName = sa.localeCompare(sb);
            if (byName) return byName;
            const ia = a.series_index || 0, ib = b.series_index || 0;
            if (ia !== ib) return ia - ib;
            return (a.title || '').localeCompare(b.title || '');
        });
        // Performance sorts — pooled across every platform the work is live on
        // (backend supplies w.stats; 2.147.0). Feeds the Overview stat-card links.
        else if (['views', 'favorites', 'comments'].includes(this._sort)) {
            const k = this._sort;
            list.sort((a, b) => ((b.stats || {})[k] || 0) - ((a.stats || {})[k] || 0));
        }
        // "Recently added" is when PawPoller met the piece (created_at). "Recently
        // posted" is when it was actually published, which is what "most recent"
        // always meant to the user — but until 4.0.12 both were created_at, so a
        // bulk import of old work sorted to the top in walk order. A piece with
        // no known post date falls back to created_at rather than off the end.
        else if (this._sort === 'added') list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        else list.sort((a, b) => (b.original_posted_at || b.created_at || '').localeCompare(a.original_posted_at || a.created_at || ''));
        return list;
    },

    _paint() {
        const grid = document.getElementById('shelf-grid');
        if (!grid) return;
        // Tear down the previous window's scroll observer (segment/filter change).
        if (this._gridObserver) { this._gridObserver.disconnect(); this._gridObserver = null; }
        // Masterpieces are their own managed surface (master-record-per-image, from
        // /api/masterpieces) — hand the grid to the Masterpieces module, passing the
        // shared shelf filters so persona/search/sort keep working across segments.
        if (this._type === 'masterpiece') {
            if (window.Masterpieces) {
                Masterpieces.renderGrid(grid, { persona: this._persona, search: this._search, sort: this._sort });
            } else {
                grid.className = '';
                grid.innerHTML = `<div class="empty-state"><h3>Masterpieces unavailable</h3></div>`;
            }
            return;
        }
        // Discovered — the polled-but-unlinked review queue, folded in from the
        // retired Artwork hub (2.155.0). Submissions owns the rows AND their
        // actions (link · import · ★ Master · 🚫 Ignore · per-platform bulk);
        // we hand it the grid rather than reimplement any of that here.
        // Unfiled posts (3.16.0): publications whose work no longer exists on
        // disk. Invisible on every other surface by construction — works lists
        // are built from folders, and the discovered list excludes anything that
        // HAS a publication row — so without this segment a post can be polled,
        // recorded, and unreachable.
        if (this._type === 'unfiled') { this._paintUnfiled(grid); return; }
        if (this._type === 'discovered') {
            if (window.Submissions) {
                Submissions.renderDiscoveredInto(grid);
            } else {
                grid.className = '';
                grid.innerHTML = `<div class="empty-state"><h3>Discovered unavailable</h3></div>`;
            }
            return;
        }
        const list = this._filtered();
        if (!list.length) {
            grid.className = '';
            // The bin says what it is: "no works match" reads like a broken
            // filter when the honest answer is "nothing is junked".
            grid.innerHTML = this._status === 'junk'
                ? `<div class="empty-state"><h3>The junk bin is empty</h3>
                    <p class="muted">Nothing is hidden. Junk a piece from its page to move it here.</p></div>`
                : `<div class="empty-state"><h3>An empty shelf</h3>
                    <p class="muted">No works match this filter yet.</p></div>`;
            return;
        }
        grid.className = 'shelf-grid';
        grid.innerHTML = '';
        this._windowInto(grid, list);
    },

    async _paintUnfiled(grid) {
        grid.className = '';
        grid.innerHTML = `<div class="muted" style="padding:.6rem">Looking for unfiled posts…</div>`;
        let d;
        try {
            d = await API.getUnfiledPosts();
        } catch (err) {
            grid.innerHTML = `<div class="empty-state"><h3>Couldn't load unfiled posts</h3>
                <p class="muted">${this.esc(err.message || String(err))}</p></div>`;
            return;
        }
        const list = (d && d.unfiled) || [];
        if (!list.length) {
            grid.innerHTML = `<div class="empty-state"><h3>Nothing unfiled</h3>
                <p class="muted">Every post on record belongs to a work that still exists.</p></div>`;
            return;
        }
        const rows = list.map(g => {
            const posts = (g.posts || []).map(p => {
                const plat = String(p.platform || '').toUpperCase();
                const url = p.external_url || '';
                const link = url
                    ? `<a href="${this.esc(url)}" target="_blank" rel="noopener">${this.esc(p.title_used || p.external_id)} &#8599;</a>`
                    : this.esc(p.title_used || p.external_id);
                // The distinction that matters: is the upload still pooling into
                // some piece, or does this post count for nothing?
                const pooled = p.linked_to
                    ? `<span class="muted">counts toward ${this.esc(p.linked_to)}</span>`
                    : `<span style="color:var(--danger,#c33)">counts toward nothing</span>`;
                return `<li>${this.esc(plat)} · ${link} — ${pooled}</li>`;
            }).join('');
            return `<div class="card" style="margin:.5rem 0;padding:.7rem .9rem">
                <div><strong>${this.esc(g.story_name)}</strong>
                    <span class="muted" style="font-size:.8rem">— ${this.esc(g.content_type)}, no folder on disk</span></div>
                <ul style="margin:.4rem 0 0 1rem;font-size:.86rem">${posts}</ul>
            </div>`;
        }).join('');
        grid.innerHTML = `
            <div class="card muted" style="margin:.2rem 0 .6rem;padding:.55rem .85rem">
                <strong>${d.works} record${d.works === 1 ? '' : 's'}, ${d.posts} post${d.posts === 1 ? '' : 's'}</strong>
                — posted work whose local folder is gone. Usually a piece that was folded into
                another (fixed in 3.16.0) or a folder you deleted, which deliberately keeps the
                record because the art still exists on the platform.
                To re-attach one: open the piece it belongs to and use <strong>🔗 Paste a link…</strong>.
            </div>${rows}`;
    },

    /* Stream books into the shelf grid a page at a time (perf guardrail): the
     * first page renders now, the rest as you scroll — so a 1000s-work library
     * doesn't build every cover node up front. The sentinel is a full-row grid
     * item so it never steals a book's cell. */
    _windowInto(grid, list) {
        const PAGE = 60;
        let i = 0;
        const sentinel = document.createElement('div');
        sentinel.setAttribute('aria-hidden', 'true');
        sentinel.style.cssText = 'grid-column:1/-1;height:1px';
        const renderNext = () => {
            const slice = list.slice(i, i + PAGE);
            if (slice.length) {
                sentinel.insertAdjacentHTML('beforebegin', slice.map(w => this._book(w)).join(''));
                i += slice.length;
            }
            if (i >= list.length) {
                if (this._gridObserver) { this._gridObserver.disconnect(); this._gridObserver = null; }
                sentinel.remove();
            }
        };
        grid.appendChild(sentinel);
        renderNext();                                   // first page, synchronously
        if (i < list.length && 'IntersectionObserver' in window) {
            this._gridObserver = new IntersectionObserver(es => {
                if (es.some(e => e.isIntersecting)) renderNext();
            }, { rootMargin: '600px' });
            this._gridObserver.observe(sentinel);
        } else {
            while (i < list.length) renderNext();       // no observer → render all
        }
    },

    /* A single "book" on the shelf. The cover is the hero; a small gilt ribbon
       tells the truth (how many platforms it's live on, or "Draft"). Stories
       open the rich Library detail; artwork keeps its own detail route. */
    _book(w) {
        const isStory = w.content_type === 'story';
        const href = isStory ? `#/library/work/${w.name}` : (w.detail_route || '#/library');
        // Truth-telling: a gilt ribbon only when a work is actually out there —
        // "N live" (platforms it's posted to), or "published" when we know it has
        // publications but no posted-status platforms. Unpublished works stay
        // clean (no cover ribbon), marked only by a quiet "Draft" in the meta.
        const nPlat = (w.platforms || []).length;
        let ribbon = '';
        if (nPlat) ribbon = `<span class="book-ribbon" title="Live on ${nPlat} platform${nPlat === 1 ? '' : 's'}">${nPlat} live</span>`;
        else if (w.publication_count) ribbon = `<span class="book-ribbon" title="Published">published</span>`;
        const draftTag = (!nPlat && !w.publication_count) ? `<span class="book-draft">Draft</span>` : '';
        // Attribution warning (3.5.2). The owner's standing rule is that credit is
        // always present, so a piece with no artist recorded is a problem to
        // surface, not a neutral state — most of all before it posts. Stories
        // are exempt: they have an author, not an artist.
        const noArtist = (w.content_type !== 'story' && w.needs_artist)
            ? `<span class="book-noartist" title="No artist recorded — add one before posting">no artist</span>`
            : '';
        const initials = this.esc((w.title || w.name || '?').trim().charAt(0).toUpperCase());
        // data-rating drives the SFW/safe-mode blur (safe_mode.css). Lower-cased
        // so "General" matches; missing/unknown → blurred by default in safe mode.
        const rAttr = ` data-rating="${this.esc((w.rating || '').toLowerCase())}"`;
        const cover = w.thumb_url
            ? `<div class="book-cover"${rAttr} style="background-image:url('${this.esc(w.thumb_url)}')">${ribbon}</div>`
            : `<div class="book-cover book-cover--blank"${rAttr}><span class="book-initial">${initials}</span>${ribbon}</div>`;
        const rating = w.rating ? `<span class="book-rating">${this.esc(w.rating)}</span>` : '';
        // The date "Recently posted" sorts by, on the card — a sort key nobody
        // can see cannot be checked (4.3.1). ≈ = matched to an upload by title;
        // linking the upload makes it exact. No date → the import date, muted.
        const postedLine = w.original_posted_at
            ? `<div class="book-posted" title="${w.posted_date_source === 'title'
                ? 'Matched to a site upload by its title — link the upload to confirm the date'
                : 'First posted'}">${w.posted_date_source === 'title' ? '≈ ' : ''}Posted ${Utils.formatDate(w.original_posted_at)}</div>`
            : (w.created_at
                ? `<div class="book-posted muted" title="No site upload linked, so no post date — sorts by when it was added">Added ${Utils.formatDate(w.created_at)}</div>`
                : '');
        const plats = (w.platforms || []).slice(0, 8).map(c =>
            `<span class="book-plat" title="${this.esc(this._plat(c).label)}">${this._plat(c).emoji || c}</span>`).join('');
        // Carried over from the retired Stories hub (2.155.0) so folding it in
        // costs nothing: a ⚠ warnings tooltip, a category chip and a short blurb.
        const warns = (w.warnings || []).length
            ? ` <span class="book-warn" title="${this.esc(w.warnings.join(', '))}">⚠</span>` : '';
        const category = w.category ? `<span class="book-category">${this.esc(w.category)}</span>` : '';
        // Series badge (gap-wave-5 §2) — "📚 Series #n"; index shown only when set.
        const series = w.series
            ? `<span class="book-series" title="Series: ${this.esc(w.series)}">📚 ${this.esc(w.series)}${w.series_index ? ' #' + w.series_index : ''}</span>`
            : '';
        const blurb = w.description
            ? `<div class="book-blurb">${this.esc(w.description.slice(0, 120))}${w.description.length > 120 ? '…' : ''}</div>`
            : '';
        // ＋ Collection — same affordance the (now-retired) Submissions hub had.
        // The global collections.js click delegate handles [data-add-collection]
        // and preventDefaults the card's own navigation.
        const collect = `<span class="book-collect" role="button" tabindex="-1"
            data-add-collection data-mtype="work" data-mref="${this.esc(w.content_type + ':' + w.name)}"
            data-label="${this.esc(w.title || w.name)}" title="Add to a collection">＋ Collection</span>`;
        return `
            <a class="book" href="${this.esc(href)}">
                ${cover}
                ${collect}
                <div class="book-spine">
                    <div class="book-title">${this.esc(w.title || w.name)}${warns}</div>
                    <div class="book-meta">${w.meta ? this.esc(w.meta) : (isStory ? 'Story' : 'Artwork')}${rating ? ' · ' : ''}${rating}${category ? ' ' : ''}${category}${draftTag ? ' ' + draftTag : ''}${noArtist ? ' ' + noArtist : ''}</div>
                    ${postedLine}
                    ${series ? `<div class="book-series-line">${series}</div>` : ''}
                    ${blurb}
                    <div class="book-plats">${plats}</div>
                </div>
            </a>${this._variantBooks(w)}`;
    },

    /* A tile per non-primary variant of an artwork work (2.190.1), rendered right
     * after its master card so the Library shows every render, not just the
     * master.
     *
     * 2.193.0: each tile now links to its OWN variant via the '?v=<key>' selector
     * the backend puts on v.detail_route. Before this the key was dropped and
     * every variant tile opened the master's hero image, which is exactly what
     * made the two detail pages feel inconsistent. Falls back to the master route
     * if an older payload has no per-variant route. */
    _variantBooks(w) {
        const vs = w.variants || [];
        if (!vs.length || !w.detail_route) return '';
        return vs.map(v => {
            const rAttr = ` data-rating="${this.esc((v.rating || w.rating || '').toLowerCase())}"`;
            const cover = v.thumb_url
                ? `<div class="book-cover"${rAttr} style="background-image:url('${this.esc(v.thumb_url)}')"><span class="book-vbadge">variant</span></div>`
                : `<div class="book-cover book-cover--blank"${rAttr}><span class="book-vbadge">variant</span></div>`;
            return `
            <a class="book book--variant" href="${this.esc(v.detail_route || w.detail_route)}"
               title="${this.esc(v.label || v.key)} — a variant of ${this.esc(w.title || w.name)}">
                ${cover}
                <div class="book-spine">
                    <div class="book-title">${this.esc(w.title || w.name)}</div>
                    <div class="book-meta"><span class="book-vlabel">${this.esc(v.label || v.key)}</span></div>
                </div>
            </a>`;
        }).join('');
    },

    /* The work detail page (renderWork / _paintWork / _wMedal) was deleted in
     * 4.5.0: #/library/work/ is the story board (story_board.js). The chapter
     * reach and achievements it drew live on there. */
};
