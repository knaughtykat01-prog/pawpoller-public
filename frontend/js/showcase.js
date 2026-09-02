/* ── Showcase — the Library's XMB landing (2.158.0) ───────────────────────────
 *
 * the owner's PS3-XMB browse, placement #2 — AN OPTION, NEVER FORCED: the Library
 * opens in whichever view was last chosen ("▤ Shelf view" here / "✕ Classic
 * view" back, remembered in localStorage 'pp_library_view'; default classic).
 * TWO animated shelves — Stories on top, Artwork (Masterpieces) underneath. ←→
 * glides along the active shelf (focused cover centred + scaled, neighbours
 * recede), ↑↓ / click switches shelves, Enter (or clicking the focused cover)
 * opens the piece. A giant blurred copy of the focused art floats behind
 * everything (the ambient backdrop, same treatment as the piece detail).
 *
 * Each shelf's right edge has "⛶ Open shelf" → the CLASSIC grid filtered to
 * that type (#/library/type/…); "✕ Classic view" (and Esc) closes the showcase
 * into the classic full shelf (#/library/browse). The classic view links back
 * with "▤ Shelf view". Deep-links (type/sort/work/discovered) stay classic, so
 * every existing bookmark and stat-card landing keeps working.
 *
 * Template strings + addEventListener wiring (CSP-safe, no inline handlers);
 * key/wheel listeners are bound per-render and torn down when the view leaves
 * the DOM (guarded by an epoch check). Respects prefers-reduced-motion via CSS.
 */
window.Showcase = {
    _shelves: [],       // [{kind, label, items:[{title, img, href, sub}]}]
    _active: 0,         // which shelf has focus
    _focus: [0, 0],     // focused index per shelf
    _epoch: 0,          // bumps every render; stale listeners self-remove

    esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _num(n) { return (window.Utils && Utils.formatNumber) ? Utils.formatNumber(n || 0) : String(n || 0); },

    async renderLibrary() {
        // Desktop-only (2.159.1): the showcase is keyboard/wheel-driven and its
        // shelf layout has no phone answer, so mobile mode always gets the
        // classic grid instead. Deliberately does NOT touch the stored
        // preference — a desktop that chose shelves stays on shelves; the same
        // account on a phone just lands classic for the visit.
        if (typeof App !== 'undefined' && App.isMobileLayoutActive && App.isMobileLayoutActive()) {
            if (window.Bookshelf) { window.Bookshelf._type = 'all'; window.Bookshelf.render(); }
            return;
        }
        const app = document.getElementById('app');
        const epoch = ++this._epoch;
        app.innerHTML = `
            <div class="sc-root" id="sc-root">
                <img class="sc-bg" id="sc-bg" alt="" aria-hidden="true">
                <div class="sc-top">
                    <div>
                        <div class="shelf-eyebrow">Your works</div>
                        <h1 class="sc-title">Library</h1>
                    </div>
                    <div class="sc-top-actions">
                        <span class="sc-hint">←→ browse · ↑↓ switch shelf · Enter opens · Esc closes</span>
                        <a class="btn btn-secondary btn-sm" id="sc-classic-btn" href="#/library/browse"
                           title="Back to the classic grid — the Library will open there until you switch again">✕ Classic view</a>
                    </div>
                </div>
                <div id="sc-shelves"><div class="loading-spinner">Setting up the shelves…</div></div>
            </div>`;

        // Leaving via ✕ remembers classic as the opening view (last choice wins).
        const classicBtn = document.getElementById('sc-classic-btn');
        if (classicBtn) classicBtn.addEventListener('click', () => {
            try { localStorage.setItem('pp_library_view', 'classic'); } catch { /* still navigates */ }
        });

        let works = [], mps = [];
        try {
            const [w, m] = await Promise.all([API.getWorks(), API.getMasterpieces()]);
            works = (w && w.works) || [];
            mps = ((m && m.masterpieces) || []).filter(x => x.status !== 'junk');
        } catch (err) {
            const el = document.getElementById('sc-shelves');
            if (el) el.innerHTML = `<div class="card error">Couldn't open the library: ${this.esc(err.message)}</div>`;
            return;
        }
        if (epoch !== this._epoch) return;   // user already navigated away

        const stories = works.filter(x => x.content_type === 'story').map(x => ({
            title: x.title || x.name,
            img: x.thumb_url || '',
            href: x.detail_route || `#/library/work/${x.name}`,
            sub: `👁 ${this._num((x.stats || {}).views)} · ❤ ${this._num((x.stats || {}).favorites)}`,
        }));
        const art = mps.map(x => ({
            title: x.title || x.name,
            img: x.image ? `/api/artwork/image?name=${encodeURIComponent(x.name)}&file=${encodeURIComponent(x.image)}`
                : ((x.summary || {}).cover_thumb || ''),
            href: `#/masterpieces/${x.name}`,
            sub: `👁 ${this._num(((x.summary || {}).totals || {}).views)} · ${((x.summary || {}).member_count || 0)} sites`,
        }));
        this._shelves = [
            { kind: 'story', label: 'Stories', open: '#/library/type/story', items: stories },
            { kind: 'art', label: 'Artwork', open: '#/library/type/masterpiece', items: art },
        ];
        this._active = 0;
        this._focus = [0, 0];
        this._paint();
        this._bind(epoch);
    },

    _paint() {
        const wrap = document.getElementById('sc-shelves');
        if (!wrap) return;
        wrap.innerHTML = this._shelves.map((sh, si) => `
            <div class="sc-shelf${si === this._active ? ' is-active' : ''}" data-shelf="${si}">
                <div class="sc-shelf-head">
                    <span class="sc-shelf-label">${this.esc(sh.label)}
                        <span class="muted">· ${sh.items.length}</span></span>
                    <a class="btn btn-secondary btn-sm sc-open" href="${sh.open}"
                       title="Open the full ${this.esc(sh.label)} grid">⛶ Open shelf</a>
                </div>
                <div class="sc-strip" data-strip="${si}">
                    ${sh.items.length ? sh.items.map((it, i) => `
                        <div class="sc-item" data-shelf="${si}" data-i="${i}">
                            ${it.img ? `<img src="${this.esc(it.img)}" alt="" loading="lazy">`
                                     : `<div class="sc-ph">🖼️</div>`}
                        </div>`).join('')
                    : `<div class="muted" style="padding:2rem">Nothing here yet.</div>`}
                </div>
                <div class="sc-caption" data-caption="${si}"></div>
            </div>`).join('');
        wrap.querySelectorAll('.sc-item').forEach(el => el.addEventListener('click', () => {
            const si = +el.dataset.shelf, i = +el.dataset.i;
            if (si === this._active && i === this._focus[si]) { this._open(); return; }
            this._active = si;
            this._focus[si] = i;
            this._layout();
        }));
        wrap.querySelectorAll('.sc-shelf').forEach(el => el.addEventListener('click', (e) => {
            if (e.target.closest('.sc-item') || e.target.closest('a')) return;
            this._active = +el.dataset.shelf;
            this._layout();
        }));
        this._layout();
    },

    /* Transform-based XMB layout: focused cover centred + scaled, neighbours
       recede each side. Pure CSS transitions do the gliding. */
    _layout() {
        const SPACE = 168, FOCUS_SCALE = 1.32, SIDE_SCALE = .82;
        this._shelves.forEach((sh, si) => {
            const strip = document.querySelector(`[data-strip="${si}"]`);
            const shelfEl = document.querySelector(`.sc-shelf[data-shelf="${si}"]`);
            if (!strip || !shelfEl) return;
            shelfEl.classList.toggle('is-active', si === this._active);
            const f = this._focus[si];
            strip.querySelectorAll('.sc-item').forEach(el => {
                const i = +el.dataset.i, off = i - f;
                const x = off === 0 ? 0 : off * SPACE + Math.sign(off) * 46;
                el.style.transform = `translateX(${x}px) translateX(-50%) scale(${off === 0 ? FOCUS_SCALE : SIDE_SCALE})`;
                el.style.opacity = Math.abs(off) > 6 ? 0 : (off === 0 ? 1 : .45);
                el.style.zIndex = 100 - Math.abs(off);
                el.classList.toggle('is-focus', off === 0 && si === this._active);
            });
            const cap = document.querySelector(`[data-caption="${si}"]`);
            const it = sh.items[f];
            if (cap) cap.innerHTML = it
                ? `<span class="sc-cap-title">${this.esc(it.title)}</span><span class="sc-cap-sub">${this.esc(it.sub || '')}</span>`
                : '';
        });
        const act = this._shelves[this._active];
        const it = act && act.items[this._focus[this._active]];
        const bg = document.getElementById('sc-bg');
        if (bg && it && it.img) { bg.src = it.img; bg.style.opacity = ''; }
        else if (bg) bg.style.opacity = '0';
    },

    _move(d) {
        const sh = this._shelves[this._active];
        if (!sh || !sh.items.length) return;
        this._focus[this._active] = Math.max(0, Math.min(sh.items.length - 1, this._focus[this._active] + d));
        this._layout();
    },

    _open() {
        const sh = this._shelves[this._active];
        const it = sh && sh.items[this._focus[this._active]];
        if (it) window.location.hash = it.href.replace(/^#/, '');
    },

    _bind(epoch) {
        const alive = () => epoch === this._epoch && document.getElementById('sc-root');
        const onKey = (e) => {
            if (!alive()) { removeEventListener('keydown', onKey); removeEventListener('wheel', onWheel); return; }
            if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
            if (e.key === 'ArrowRight') this._move(1);
            else if (e.key === 'ArrowLeft') this._move(-1);
            else if (e.key === 'ArrowDown') this._active = Math.min(this._shelves.length - 1, this._active + 1), this._layout();
            else if (e.key === 'ArrowUp') this._active = Math.max(0, this._active - 1), this._layout();
            else if (e.key === 'Enter') this._open();
            else if (e.key === 'Escape') {
                try { localStorage.setItem('pp_library_view', 'classic'); } catch { /* still navigates */ }
                window.location.hash = 'library/browse';
                return;
            }
            else return;
            e.preventDefault();
        };
        let last = 0;
        const onWheel = (e) => {
            if (!alive()) { removeEventListener('wheel', onWheel); removeEventListener('keydown', onKey); return; }
            const now = Date.now();
            if (now - last < 150) return;
            last = now;
            this._move(e.deltaY > 0 ? 1 : -1);
        };
        addEventListener('keydown', onKey);
        addEventListener('wheel', onWheel, { passive: true });
    },
};
