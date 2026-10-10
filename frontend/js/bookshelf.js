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
 * discovered|unfiled}.
 *
 * ONE CARD PER PIECE (4.47.0, spec 014). The Masterpieces segment showed the same
 * pieces as Artwork a second time, and every extra version of a piece was its own
 * card. Now a piece is one card that pools every upload of it (what Masterpieces
 * pooled), and its versions — or a story's chapters — fan out from a toggle on the
 * cover as tiles placed right after it. The Masterpieces grid's tools moved here:
 * Select → post as a batch (still Masterpieces._openBatch), Find duplicates, New,
 * and Restore in the junk view. #/masterpieces and …/type/masterpiece redirect to
 * Artwork.
 *
 * Reuses the real endpoints, adds almost no backend —
 *   - list          → API.getWorks()            (GET /api/works)
 *   - story detail  → API.getPostingStory(name) (GET /api/posting/stories/{name})
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
    _type: 'all',      // all | story | artwork | discovered | unfiled
    _persona: 0,       // 0 = all
    _search: '',
    _sort: 'recent',   // recent | title | platforms
    _status: 'all',    // all | posted | drafts — filter by publish state
    _platform: '',     // '' = every platform; a code filters to works live there
    _discCount: 0,     // discovered-segment badge (filled by _loadDiscovered)

    /* Valid #/library/type/{t} targets — guards the deep-link + the redirects
       from the retired hubs against typos silently showing an empty shelf. */
    TYPES: ['all', 'story', 'artwork', 'discovered', 'unfiled'],

    /* The retired Masterpieces segment (4.47.0) — old links land on Artwork. */
    normaliseType(t) { return t === 'masterpiece' ? 'artwork' : t; },

    /* Versions & chapters (4.47.0, spec 014). `_partsAll` is the "Show versions &
       chapters" switch; `_openMap` holds per-card overrides keyed "type:name";
       `_forceOpen` is per-render — cards a search matched only through a part. */
    _PARTS_KEY: 'pp_library_parts',
    _OPEN_KEY: 'pp_library_open',
    _partsAll: false,
    _openMap: {},
    _forceOpen: new Set(),
    _MAX_CHAPTER_TILES: 4,

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

    /* _pick / _views / _faves / _comments were deleted in 4.34.4 (UNIFORMITEM
       phase 5). They read a work's per-platform stats for `_paintWork`, the work
       detail page retired in 4.5.0; nothing has called them since. story_board.js
       carries its own copies of the same three getters for the page that replaced
       it. */

    /* ── Library home ──────────────────────────────────────────── */

    async render() {
        const _rt = App._routeToken();   // route race guard (App._stale)
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
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
            <div id="shelf-grid">${Utils.skeleton('cards', 12, 'the Library')}</div>`;

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
        // The piece page's prev/next list is Masterpieces' cache; drop it so it
        // follows any change made since (4.47.0: the grid that also used it is gone).
        if (window.Masterpieces && Masterpieces.resetCache) Masterpieces.resetCache();
        this._loadPartsState();
        this._renderControls();
        this._paint();
        this._mountFab();
        this._loadDiscovered();   // discovered-art import banner (moved from Submissions)
    },

    /* ── Phone: the filters fold into a button (4.56.2, LIBFAB) ──────────────────
     * On a phone the filter block sits at the top of the Library. Scrolled past, it folds
     * into a "Filters" pill in the top bar ("Filters · Artwork · 2"); tapping the pill drops
     * the same block down from the bar as a panel. It tucks away again on a tap outside,
     * a scroll, Escape, or the pill. The block isn't moved — it turns into a fixed panel
     * while open, with a placeholder holding its space so the page doesn't jump.
     * Desktop keeps the sticky bar (4.45.1). */
    _TYPE_LABEL: { all: 'All', story: 'Stories', artwork: 'Artwork', discovered: 'Discovered', unfiled: 'Unfiled' },

    _mountFab() {
        this._unmountFab();
        const ctl = document.getElementById('shelf-controls');
        if (!ctl || !window.IntersectionObserver) return;
        const fab = document.createElement('button');
        fab.type = 'button';
        fab.id = 'shelf-fab';
        fab.className = 'shelf-fab';
        fab.setAttribute('aria-controls', 'shelf-controls');
        fab.setAttribute('aria-expanded', 'false');
        document.body.appendChild(fab);
        this._fab = fab;
        this._paintFab();
        const root = document.documentElement;
        // The bar's real height (--mbar-h is a calc() with the notch inset, so read the strip itself).
        const barH = () => (document.getElementById('mobile-bar-bg') || {}).offsetHeight || 64;
        this._fabObs = new IntersectionObserver(([e]) => {
            if (root.classList.contains('shelf-open')) return;   // the open panel isn't "scrolled past"
            root.classList.toggle('shelf-folded', !e.isIntersecting && e.boundingClientRect.top < barH());
        }, { rootMargin: `-${Math.round(barH())}px 0px 0px 0px` });
        this._fabObs.observe(ctl);
        fab.addEventListener('click', (ev) => { ev.stopPropagation(); this._toggleFab(); });
        this._fabOff = (ev) => {
            if (!root.classList.contains('shelf-open')) return;
            if (ev.type === 'keydown') { if (ev.key === 'Escape') this._toggleFab(false); return; }
            if (ev.type === 'scroll') { if (Math.abs(window.scrollY - this._fabY) > 40) this._toggleFab(false); return; }
            if (!ctl.contains(ev.target) && ev.target !== fab && !fab.contains(ev.target)) this._toggleFab(false);
        };
        document.addEventListener('click', this._fabOff, true);
        document.addEventListener('keydown', this._fabOff);
        window.addEventListener('scroll', this._fabOff, { passive: true });
        this._fabLeave = () => this._unmountFab();
        window.addEventListener('hashchange', this._fabLeave, { once: true });
    },

    _unmountFab() {
        const root = document.documentElement;
        root.classList.remove('shelf-folded', 'shelf-open');
        if (this._fabObs) { this._fabObs.disconnect(); this._fabObs = null; }
        if (this._fabOff) {
            document.removeEventListener('click', this._fabOff, true);
            document.removeEventListener('keydown', this._fabOff);
            window.removeEventListener('scroll', this._fabOff);
            this._fabOff = null;
        }
        if (this._fabLeave) { window.removeEventListener('hashchange', this._fabLeave); this._fabLeave = null; }
        const old = document.getElementById('shelf-fab');
        if (old) old.remove();
        const sp = document.getElementById('shelf-controls-spacer');
        if (sp) sp.remove();
        this._fab = null;
    },

    _toggleFab(open) {
        const root = document.documentElement;
        const ctl = document.getElementById('shelf-controls');
        if (!ctl || !this._fab) return;
        const want = open === undefined ? !root.classList.contains('shelf-open') : open;
        if (want) {
            let sp = document.getElementById('shelf-controls-spacer');
            if (!sp) { sp = document.createElement('div'); sp.id = 'shelf-controls-spacer'; ctl.before(sp); }
            sp.style.height = ctl.offsetHeight + 'px';
            this._fabY = window.scrollY;
            root.classList.add('shelf-open');
        } else {
            root.classList.remove('shelf-open');
            const sp = document.getElementById('shelf-controls-spacer');
            if (sp) sp.remove();
        }
        this._fab.setAttribute('aria-expanded', String(want));
    },

    _paintFab() {
        if (!this._fab) return;
        const n = [this._persona, (this._search || '').trim(), this._status && this._status !== 'all',
                   this._platform].filter(Boolean).length;
        this._fab.innerHTML = `<svg class="ico" aria-hidden="true" width="16" height="16"><use href="#i-filter"/></svg> Filters · ${this.esc(this._TYPE_LABEL[this._type] || 'All')}`
            + (n ? ` <span class="shelf-fab-n" aria-label="${n} more filter${n === 1 ? '' : 's'} on">${n}</span>` : '');
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
                <select id="shelf-sort" class="shelf-input shelf-sort" aria-label="Sort by">
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
                </select>
                <select id="shelf-platform" class="shelf-input shelf-sort" title="Filter by the site a work is live on">
                    <option value="">Every platform</option>
                    ${(window.visiblePlatforms ? window.visiblePlatforms() : (window.PLATFORMS || []))
                        .map(p => `<option value="${this.esc(p.code)}"${p.code === this._platform ? ' selected' : ''}>${this.esc(p.emoji ? p.emoji + ' ' + p.label : p.label)}</option>`).join('')}
                </select>`;
        // Tools row (4.47.0): the versions/chapters switch, and the Masterpieces
        // grid's tools — they act on artwork, so they show on All and Artwork.
        const arty = this._type === 'all' || this._type === 'artwork';
        const sel = !!(window.Masterpieces && Masterpieces._selMode);
        const tools = isDisc ? '' : `
            <div class="shelf-tools">
                <button type="button" class="shelf-switch" id="shelf-parts" role="switch"
                    aria-checked="${this._partsAll}">
                    <span class="shelf-switch-track" aria-hidden="true"></span>Show versions &amp; chapters</button>
                ${arty && this._status !== 'junk' ? `
                <span class="shelf-tools-sp"></span>
                <button class="btn btn-sm${sel ? ' btn-primary' : ''}" data-mp-select type="button"
                    title="Tick several pieces and post them in one go — each as its own submission, spread out over time">☑ ${sel ? 'Selecting…' : 'Select'}</button>
                <a class="btn btn-sm" href="#/masterpieces/duplicates"
                    title="Find pieces that are the same image, or the same piece in different renders, and fold them into one">🔍 Find duplicates</a>
                <a class="btn btn-primary btn-sm" href="#/artwork/new"
                    title="Upload a new image, describe it once, and publish it across sites">＋ New artwork</a>` : ''}
            </div>`;
        el.innerHTML = `
            <div class="shelf-controls">
                <div class="shelf-segs">${seg('all', 'All')}${seg('story', 'Stories')}${seg('artwork', 'Artwork')}${seg('discovered', this._discLabel())}${seg('unfiled', 'Unfiled')}</div>
                ${shelfControls}
            </div>${tools}`;
        el.querySelector('#shelf-parts')?.addEventListener('click', () => this._setPartsAll(!this._partsAll));
        el.querySelector('[data-mp-select]')?.addEventListener('click', () => {
            if (!window.Masterpieces) return;
            Masterpieces._selMode = !Masterpieces._selMode;
            if (!Masterpieces._selMode) Masterpieces._sel.clear();
            this._renderControls();
            this._paint();
        });

        el.querySelectorAll('[data-shelf-type]').forEach(b =>
            b.addEventListener('click', () => this.switchType(b.dataset.shelfType)));
        const ps = el.querySelector('#shelf-persona');
        if (ps) ps.addEventListener('change', () => { this._persona = parseInt(ps.value) || 0; this._paint(); });
        const se = el.querySelector('#shelf-search');
        if (se) se.addEventListener('input', () => { this._search = se.value; this._paint(); });
        const so = el.querySelector('#shelf-sort');
        if (so) { so.value = this._sort; so.addEventListener('change', () => { this._sort = so.value; this._paint(); }); }
        const st = el.querySelector('#shelf-status');
        // Re-renders the bar too: the junk view hides Select (there is nothing to post).
        if (st) { st.value = this._status; st.addEventListener('change', () => { this._status = st.value; this._renderControls(); this._paint(); }); }
        const pf = el.querySelector('#shelf-platform');
        if (pf) { pf.value = this._platform; pf.addEventListener('change', () => { this._platform = pf.value; this._paint(); }); }
    },

    /* Switch segment IN PLACE — no re-fetch, no router round-trip (the works are
     * already cached in _works). replaceState, not location.hash: assigning the
     * hash would fire hashchange → route() → a full render() and another
     * /api/works call just to show a filter of data we're already holding.
     * replaceState still leaves a URL you can refresh, bookmark or share. */
    switchType(t) {
        t = this.normaliseType(t);
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
        // Platform filter (4.32.3): the sites a work is actually live on. `platforms`
        // is already on every work for the "Most platforms" sort, so this costs nothing.
        if (this._platform) {
            list = list.filter(w => (w.platforms || []).includes(this._platform));
        }
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
        //
        // Parts (4.47.0, spec 014): a piece also matches when only one of its
        // versions or chapters does — "nsfw" finds the piece with an NSFW version,
        // `rating:adult` finds a General piece whose other version is Adult — and
        // that card is drawn opened to show the part.
        this._forceOpen = new Set();
        if (this._search) {
            const q = this._search.toLowerCase();
            const hit = parsed
                ? (x => SearchQuery.match(x, parsed))
                : (w => (w.title || '').toLowerCase().includes(q) || (w.name || '').toLowerCase().includes(q));
            list = list.filter(w => {
                if (hit(w)) return true;
                if (this._parts(w).some(hit)) { this._forceOpen.add(this._key(w)); return true; }
                return false;
            });
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
        // bulk import of old work sorted to the top in walk order. Posted work
        // first; never-posted pieces after it, newest-added first (4.39.1).
        else if (this._sort === 'added') list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        else list.sort((a, b) => this._recentCmp(a, b));
        return list;
    },

    /* Mirrors submissions_api.recent_key. An undated piece used to borrow its
     * added date and sit AMONG posted work, so one bulk import of never-posted
     * pieces outranked everything posted before it. */
    _recentCmp(a, b) {
        const da = a.original_posted_at || '', db = b.original_posted_at || '';
        if (!da !== !db) return da ? -1 : 1;
        return da ? db.localeCompare(da) : (b.created_at || '').localeCompare(a.created_at || '');
    },

    _paint() {
        const grid = document.getElementById('shelf-grid');
        if (!grid) return;
        // Tear down the previous window's scroll observer (segment/filter change).
        if (this._gridObserver) { this._gridObserver.disconnect(); this._gridObserver = null; }
        this._wireGrid(grid);
        // Select mode belongs to the shelf; leaving it for Discovered/Unfiled ends it.
        if (window.Masterpieces && Masterpieces._selMode
            && (this._type === 'discovered' || this._type === 'unfiled' || this._status === 'junk')) {
            Masterpieces._selMode = false; Masterpieces._sel.clear();
        }
        if (window.Masterpieces && Masterpieces._paintSelBar) Masterpieces._paintSelBar();
        this._paintFab();
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
        // "Select all shown" in the batch bar selects the art on screen.
        if (window.Masterpieces) {
            Masterpieces._lastList = list.filter(w => w.content_type === 'artwork');
            Masterpieces._paintSelBar();
        }
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

    /* ── One card per piece (4.47.0, spec 014) ─────────────────────────────── */

    _key(w) { return `${w.content_type}:${w.name}`; },

    /* A work's parts as pseudo-works the search grammar can test: versions for art
       (title = the version's label, its own rating and sites), chapters for stories. */
    _parts(w) {
        if (w.content_type === 'story') {
            return (w.chapters || []).map(c => ({ ...w, title: c.title || '', platforms: c.platforms || [] }));
        }
        return (w.variants || []).map(v => ({
            ...w, title: v.label || v.key || '', rating: v.rating || w.rating,
            platforms: v.platforms || [],
        }));
    },

    _partCount(w) {
        return w.content_type === 'story'
            ? ((w.chapters || []).length ? (w.chapter_count || w.chapters.length) : 0)
            : (w.variants || []).length;
    },

    _loadPartsState() {
        try {
            this._partsAll = localStorage.getItem(this._PARTS_KEY) === '1';
            const m = JSON.parse(localStorage.getItem(this._OPEN_KEY) || '{}');
            this._openMap = (m && typeof m === 'object') ? m : {};
        } catch { this._partsAll = false; this._openMap = {}; }   // blocked storage: closed, nothing remembered
    },

    _savePartsState() {
        try {
            localStorage.setItem(this._PARTS_KEY, this._partsAll ? '1' : '0');
            localStorage.setItem(this._OPEN_KEY, JSON.stringify(this._openMap));
        } catch { /* blocked storage: works for this visit only */ }
    },

    /* The switch opens or closes EVERY card, so it clears the per-card overrides. */
    _setPartsAll(on) {
        this._partsAll = !!on;
        this._openMap = {};
        this._savePartsState();
        const sw = document.getElementById('shelf-parts');
        if (sw) sw.setAttribute('aria-checked', String(this._partsAll));
        const grid = document.getElementById('shelf-grid');
        if (grid) grid.querySelectorAll('[data-fan]').forEach(b => this._applyOpen(grid, b.dataset.fan));
    },

    _isOpen(key) {
        if (this._forceOpen && this._forceOpen.has(key)) return true;
        return Object.prototype.hasOwnProperty.call(this._openMap, key) ? !!this._openMap[key] : this._partsAll;
    },

    _applyOpen(grid, key) {
        const open = this._isOpen(key);
        const sel = window.CSS && CSS.escape ? CSS.escape(key) : key.replace(/["\\]/g, '\\$&');
        grid.querySelectorAll(`[data-part-of="${sel}"]`).forEach(t => { t.hidden = !open; });
        const b = grid.querySelector(`[data-fan="${sel}"]`);
        if (b) {
            b.setAttribute('aria-expanded', String(open));
            b.classList.toggle('is-open', open);
            b.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + b.dataset.what);
        }
        b?.closest('.book')?.classList.toggle('is-open', open);
    },

    /* Delegated once per grid element (cards stream in later): the cover toggle,
       Restore in the junk view, and Select mode's tick-instead-of-open. */
    _wireGrid(grid) {
        if (grid.dataset.shelfWired) return;
        grid.dataset.shelfWired = '1';
        grid.addEventListener('click', async (e) => {
            const fan = e.target.closest('[data-fan]');
            if (fan && grid.contains(fan)) {
                e.preventDefault();
                const key = fan.dataset.fan;
                this._openMap[key] = !this._isOpen(key);
                if (this._forceOpen) this._forceOpen.delete(key);
                this._savePartsState();
                this._applyOpen(grid, key);
                return;
            }
            const rb = e.target.closest('[data-restore]');
            if (rb && grid.contains(rb)) {
                e.preventDefault();
                rb.disabled = true;
                try {
                    await API.setMasterpieceStatus(rb.dataset.restore, '');
                    this._toast('success', 'Restored to the Library');
                    await this.render();
                } catch (err) {
                    rb.disabled = false;
                    this._toast('error', 'Restore failed: ' + (err.message || err));
                }
                return;
            }
            if (window.Masterpieces && Masterpieces._selMode) {
                const card = e.target.closest('.book[data-mp-name]');
                if (card && grid.contains(card)) { e.preventDefault(); Masterpieces._toggleSel(card); }
            }
        });
        grid.addEventListener('keydown', (e) => {
            if (!(window.Masterpieces && Masterpieces._selMode) || (e.key !== ' ' && e.key !== 'Enter')) return;
            const card = e.target.closest('.book[data-mp-name]');
            if (!card || e.target.closest('[data-fan]')) return;
            e.preventDefault();
            Masterpieces._toggleSel(card);
        });
    },

    /* "Fri 20:00" within a week, else "4 Oct". */
    _when(at) {
        if (!at) return '';
        const d = Utils.time.parse(at);
        if (!d || isNaN(d)) return '';
        const days = (d - Date.now()) / 864e5;
        return days >= -1 && days < 7
            ? Utils.time.format(d, { weekday: 'short', hour: 'numeric', minute: '2-digit' })
            : Utils.time.format(d, { day: 'numeric', month: 'short' });
    },

    /* Pooled numbers, words beside the icons for screen readers. */
    _nums(stats, isStory) {
        const s = stats || {};
        const fmt = n => (window.Utils && Utils.formatCompact) ? Utils.formatCompact(n || 0) : String(n || 0);
        const bits = [];
        if (s.views) bits.push(`<span title="${isStory ? 'Reads' : 'Views'}"><span aria-hidden="true">👁</span> ${fmt(s.views)}<span class="sr-only"> ${isStory ? 'reads' : 'views'}</span></span>`);
        if (s.favorites) bits.push(`<span title="Favourites"><span aria-hidden="true">❤</span> ${fmt(s.favorites)}<span class="sr-only"> favourites</span></span>`);
        if (s.comments) bits.push(`<span title="Comments"><span aria-hidden="true">💬</span> ${fmt(s.comments)}<span class="sr-only"> comments</span></span>`);
        return bits.length ? `<div class="book-nums">${bits.join('')}</div>` : '';
    },

    _platIcons(codes, max = 8) {
        return (codes || []).slice(0, max).map(c =>
            `<span class="book-plat" title="${this.esc(this._plat(c).label)}">${this._plat(c).emoji || this.esc(c)}</span>`).join('');
    },

    /* The status chip on the cover (Live on N / Scheduled / Draft) and, when more is
       queued, a second chip under the title ("Ch 10–12 · Fri 20:00", "Next · Fri"). */
    _statusChips(w) {
        const st = w.status || {};
        const state = st.state || ((w.platforms || []).length ? 'live' : 'draft');
        const label = st.label || (state === 'live' ? `Live on ${(w.platforms || []).length}` : 'Draft');
        const when = this._when(st.next_at);
        const coverLabel = state === 'scheduled' && when ? `Scheduled · ${when}` : label;
        const chip = `<span class="book-ribbon book-ribbon--${this.esc(state)}">${this.esc(coverLabel)}</span>`;
        let extra = '';
        if (state === 'live' && st.next_at) {
            const what = st.scheduled_label || 'Next';
            extra = `<span class="book-chip book-chip--sched">${this.esc(what)}${when ? ' · ' + this.esc(when) : ''}</span>`;
        }
        return { chip, extra, state };
    },

    _personaLine(w) {
        if (this._personas.length < 2) return '';
        const by = {};
        this._personas.forEach(p => { by[p.id] = p; });
        const chips = (w.persona_ids || []).map(id => by[id]).filter(Boolean).map(p =>
            `<span class="book-persona"><span class="book-persona-dot" style="background:${this.esc(p.color || 'var(--accent)')}"></span>${this.esc(p.name)}</span>`).join('');
        return chips ? `<div class="book-personas">${chips}</div>` : '';
    },

    /* A single "book" on the shelf: one per piece. The cover carries the status
       chip and, when the piece has versions or chapters, the toggle that fans them
       out as tiles right after the card. Stories open the story board; artwork the
       piece page. */
    _book(w) {
        const isStory = w.content_type === 'story';
        const href = isStory ? `#/library/work/${w.name}` : (w.detail_route || '#/library');
        const key = this._key(w);
        const { chip, extra } = this._statusChips(w);
        // Attribution warning (3.5.2). The owner's standing rule is that credit is
        // always present, so a piece with no artist recorded is a problem to
        // surface, not a neutral state — most of all before it posts. Stories
        // are exempt: they have an author, not an artist.
        const noArtist = (!isStory && w.needs_artist)
            ? `<span class="book-noartist" title="No artist recorded — add one before posting">no artist</span>`
            : '';
        const initials = this.esc((w.title || w.name || '?').trim().charAt(0).toUpperCase());
        // data-rating drives the SFW/safe-mode blur (safe_mode.css). Lower-cased
        // so "General" matches; missing/unknown → blurred by default in safe mode.
        const rAttr = ` data-rating="${this.esc((w.rating || '').toLowerCase())}"`;
        // 4.18.0 (MEDIATYPES): a video / audio piece shows its kind + length on the cover (its poster).
        const mediaBadge = (window.MediaKinds && w.media_kind && w.media_kind !== 'image')
            ? `<span class="book-media-badge" title="${this.esc(w.media_kind)}">${this.esc(MediaKinds.badge(w.media, w.media_kind))}</span>` : '';
        const cover = w.thumb_url
            ? `<div class="book-cover"${rAttr} style="background-image:url('${this.esc(w.thumb_url)}')">${chip}${mediaBadge}</div>`
            : `<div class="book-cover book-cover--blank"${rAttr}><span class="book-initial">${initials}</span>${chip}${mediaBadge}</div>`;
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

        // The toggle on the cover — only when there is something to fan out.
        const n = this._partCount(w);
        const open = n ? this._isOpen(key) : false;
        const word = isStory ? `${n} ch` : `${n} version${n === 1 ? '' : 's'}`;
        const what = `${isStory ? `${n} chapters` : word} of ${w.title || w.name}`;
        const fan = n ? `
            <button type="button" class="book-fan${open ? ' is-open' : ''}" data-fan="${this.esc(key)}"
                data-what="${this.esc(what)}" aria-expanded="${open}" aria-label="${open ? 'Hide' : 'Show'} ${this.esc(what)}">
                <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4.5" y="1.5" width="9" height="9" rx="1.5"/><path d="M2.5 4.5v8a1.5 1.5 0 0 0 1.5 1.5h8"/></svg>${this.esc(word)}<span class="book-fan-chev" aria-hidden="true">›</span></button>` : '';

        // Select mode (batch posting) ticks art cards instead of opening them.
        const selMode = !isStory && window.Masterpieces && Masterpieces._selMode;
        const picked = selMode && Masterpieces._sel.has(w.name);
        const selAttrs = !isStory ? ` data-mp-name="${this.esc(w.name)}"` : '';
        const tick = selMode ? `<span class="mp-tick" aria-hidden="true">${picked ? '✓' : ''}</span>` : '';
        // In the junk view every art card carries a one-click Restore (was the
        // Masterpieces grid's; the Library's Junk filter is its home now).
        const restore = (!isStory && w.is_junk)
            ? `<button class="btn btn-sm book-restore" type="button" data-restore="${this.esc(w.name)}">♻ Restore</button>` : '';

        return `
            <div class="book${n ? ' book--stacked' : ''}${open ? ' is-open' : ''}${selMode ? ' is-selectable' : ''}${picked ? ' is-picked' : ''}"${selAttrs}${selMode ? ` role="checkbox" aria-checked="${picked}" tabindex="0"` : ''}>
                <a class="book-link" href="${this.esc(href)}"${selMode ? ' tabindex="-1"' : ''}>
                    ${cover}
                    <div class="book-spine">
                        <div class="book-title">${this.esc(w.title || w.name)}${warns}</div>
                        <div class="book-meta">${w.meta ? this.esc(w.meta) : (isStory ? 'Story' : 'Artwork')}${rating ? ' · ' : ''}${rating}${category ? ' ' : ''}${category}${noArtist ? ' ' + noArtist : ''}</div>
                        ${extra ? `<div class="book-chips">${extra}</div>` : ''}
                        ${postedLine}
                        ${series ? `<div class="book-series-line">${series}</div>` : ''}
                        ${blurb}
                        ${this._nums(w.stats, isStory)}
                        <div class="book-plats">${this._platIcons(w.platforms)}</div>
                        ${this._personaLine(w)}
                    </div>
                </a>
                ${tick}<div class="book-fanslot">${collect}${fan}</div>${restore}
            </div>${n ? this._partTiles(w, key, open) : ''}`;
    },

    /* The tiles a card fans out: every other version of a piece, or a story's first
       chapters + "+N more". Rendered hidden when closed, so opening is instant and
       keeps focus on the toggle. */
    _partTiles(w, key, open) {
        const hid = open ? '' : ' hidden';
        const of = ` data-part-of="${this.esc(key)}"`;
        const partChip = (p) => {
            if (p.status === 'live') return '';
            if (p.status === 'scheduled') {
                const when = this._when(p.scheduled_at);
                return `<span class="book-chip book-chip--sched">Scheduled${when ? ' · ' + this.esc(when) : ''}</span>`;
            }
            return '<span class="book-chip">Not posted</span>';
        };
        if (w.content_type === 'story') {
            const chs = w.chapters || [];
            const shown = chs.slice(0, this._MAX_CHAPTER_TILES);
            const top = Math.max(1, ...chs.map(c => (c.stats || {}).views || 0));
            const tiles = shown.map(c => {
                const reads = (c.stats || {}).views || 0;
                return `
            <a class="book book--part book--chapter" href="#/library/work/${this.esc(w.name)}"${of}${hid}
               title="Chapter ${c.index} of ${this.esc(w.title || w.name)}">
                <div class="book-chcover"><span class="book-chnum">${c.index}</span><span class="book-chword">chapter</span></div>
                <div class="book-spine">
                    <div class="book-title">${this.esc(c.title)}</div>
                    <div class="book-plats">${this._platIcons(c.platforms)}${partChip(c)}</div>
                    <div class="book-reach" role="img" aria-label="${reads} reads"><i style="width:${Math.round(reads / top * 100)}%"></i></div>
                    <div class="book-posted">${(window.Utils && Utils.formatNumber) ? Utils.formatNumber(reads) : reads} reads</div>
                </div>
            </a>`;
            }).join('');
            const more = chs.length - shown.length;
            return tiles + (more > 0 ? `
            <a class="book book--part book--more" href="#/library/work/${this.esc(w.name)}"${of}${hid}>
                <span class="book-more-n">+${more} more chapter${more === 1 ? '' : 's'}</span>
                <span class="book-more-go">Open the story →</span>
            </a>` : '');
        }
        return (w.variants || []).map(v => {
            const rAttr = ` data-rating="${this.esc((v.rating || w.rating || '').toLowerCase())}"`;
            const cover = v.thumb_url
                ? `<div class="book-cover"${rAttr} style="background-image:url('${this.esc(v.thumb_url)}')"><span class="book-vbadge">version</span></div>`
                : `<div class="book-cover book-cover--blank"${rAttr}><span class="book-vbadge">version</span></div>`;
            return `
            <a class="book book--part" href="${this.esc(v.detail_route || w.detail_route)}"${of}${hid}
               title="${this.esc(v.label || v.key)} — a version of ${this.esc(w.title || w.name)}">
                ${cover}
                <div class="book-spine">
                    <div class="book-title">${this.esc(v.label || v.key)}</div>
                    <div class="book-meta">${v.rating ? `<span class="book-rating">${this.esc(v.rating)}</span>` : ''}</div>
                    ${this._nums(v.stats)}
                    <div class="book-plats">${this._platIcons(v.platforms)}${partChip(v)}</div>
                </div>
            </a>`;
        }).join('');
    },

    /* The work detail page (renderWork / _paintWork / _wMedal) was deleted in
     * 4.5.0: #/library/work/ is the story board (story_board.js). The chapter
     * reach and achievements it drew live on there. */
};
