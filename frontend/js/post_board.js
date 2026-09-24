/* ── The Post board — #/posts/<id> (4.34.4, UNIFORMITEM phase 4) ──────────
 *
 * The one phase of that spec that is a FEATURE rather than a consolidation.
 * Posts were the only item type with nowhere to click into: a post goes to up to
 * six platforms and every one of them writes a `post_publications` row, but the
 * feed was the whole surface — no per-post numbers, no history, no way to send it
 * somewhere it hasn't been. "How did that post do?" had no answer.
 *
 * Built on the shared frame rather than beside it: `ItemFrame` for the back-bar,
 * hero and headline row, `ItemNav` for prev/next, and the contract's section
 * vocabulary from `ItemFrame.SECTIONS` so this page cannot invent a synonym for
 * "Published to" the way Collections had ("Locations") before phase 3.
 *
 * Reads:  GET /api/posts/{id}            (the post + publications with live stats)
 *         GET /api/posts/{id}/snapshots  (combined growth)
 * Writes: POST /api/posts/{id}/publish   (send it to a platform it missed)
 *
 * ⚠ Stats can be null, and null is not zero. A platform that has been posted to
 * but not yet polled shows "—", never 0 — "we have not measured this" and "nobody
 * looked at it" are different statements and the whole point of this spec is
 * surfaces that do not confuse the two.
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = (n) => (n == null || n === '') ? '—' : Number(n).toLocaleString();

    const PostBoard = {
        _id: null,
        _data: null,
        _wired: false,

        async render(id) {
            this._id = id;
            this._wire();
            const app = document.getElementById('app');
            app.innerHTML = `
                ${ItemFrame.backBar({ href: '#/posts', label: 'Posts' })}
                <div id="pb-detail"><div class="loading-spinner">Opening the post…</div></div>`;
            let d;
            try {
                d = await API.getPost(id);
            } catch (err) {
                const status = (err && /404/.test(err.message)) ? 'This post no longer exists.' : esc(err.message);
                document.getElementById('pb-detail').innerHTML =
                    `<div class="card error">Couldn't open this post: ${status}</div>`;
                return;
            }
            this._data = d;
            this._paint(d);
            this._renderDetailNav(id);
            this._loadChart(id);
        },

        _paint(d) {
            const root = document.getElementById('pb-detail');
            if (!root) return;
            root.innerHTML = `
                <div class="board-wrap">
                    ${this._heroHtml(d)}
                    <div class="board">
                        <div class="board-col">${this._recordHtml(d)}</div>
                        <div class="board-col">${this._publishedToHtml(d)}${this._publishToHtml(d)}</div>
                        <div class="board-col board-col--3">${this._growthHtml()}${this._historyHtml(d)}</div>
                    </div>
                </div>`;
        },

        /* ── Hero ────────────────────────────────────────────────────────── */
        _heroHtml(d) {
            const t = d.totals || {};
            const hasImg = !!d.image_path;
            const tile = hasImg
                ? `<div class="board-hero-tile"><img class="mp-hero-img" src="${esc(API.postImageUrl(d.post_id))}" alt="${esc(d.image_alt || '')}"></div>`
                : `<div class="board-hero-tile board-hero-tile--blank" aria-hidden="true"><span class="book-initial">✎</span></div>`;
            const posted = (d.publications || []).filter(p => p.status === 'posted').length;
            const pills = `<div class="chip-rows"><div class="chip-row">
                ${d.rating && d.rating !== 'general' ? `<span class="pill">${esc(d.rating)}</span>` : ''}
                ${posted ? `<span class="pill pill--live">live on ${posted}</span>` : '<span class="pill">not published</span>'}
                ${d.thread_count ? `<span class="pill">🧵 ${d.thread_count + 1} parts</span>` : ''}
            </div></div>`;
            // A post has no title, so the body IS the headline -- trimmed, because a
            // 300-character hero heading is not a heading.
            const head = (d.body || '').trim();
            const title = head ? esc(head.length > 90 ? head.slice(0, 90) + '…' : head)
                               : '<span class="muted">(image only)</span>';
            return ItemFrame.hero({
                tile,
                eyebrow: esc(d.created_at || ''),
                title,
                pills,
                stats: ItemFrame.statRow([
                    { value: t.views, label: 'Views' },
                    { value: t.favorites, label: 'Favourites' },
                    { value: t.comments, label: 'Comments' },
                    { value: t.sites, label: 'Sites' },
                ]),
            });
        },

        /* ── 1. Record ───────────────────────────────────────────────────── */
        _recordHtml(d) {
            const parts = (d.thread_parts || []).map((p, i) => `
                <div class="pub-group">
                    <div class="muted" style="font-size:11px">Part ${i + 2}</div>
                    <p class="post-card-text">${esc(p.body)}</p>
                </div>`).join('');
            return ItemFrame.section({
                id: 'pb-sec-record', title: ItemFrame.SECTIONS.record,
                body: `
                    <p class="post-card-text">${esc(d.body) || '<span class="muted">(image only)</span>'}</p>
                    ${d.image_alt ? `<p class="muted" style="font-size:12px">Alt text: ${esc(d.image_alt)}</p>` : ''}
                    <div class="muted" style="font-size:12px">Rating: ${esc(d.rating || 'general')}</div>
                    ${parts}
                    <p class="muted" style="font-size:12px;margin-top:.6rem">
                        A post's text is fixed once it is out — editing here would not change
                        what each site already shows.</p>`,
            });
        },

        /* ── 2. Published to ─────────────────────────────────────────────── */
        _publishedToHtml(d) {
            const rows = (d.publications || []).map(p => {
                const plat = this._plat(p.platform);
                const s = p.stats || {};
                if (p.status !== 'posted') {
                    return `<div class="loc-row">
                        <span class="thumb-sq thumb-sq--emoji" aria-hidden="true">${plat.emoji || ''}</span>
                        <div class="loc-site"><span class="name">${esc(plat.label)}</span>
                        <span class="muted" style="font-size:12px">${esc(p.error || p.status || 'not posted')}</span></div>
                    </div>`;
                }
                const link = p.external_url
                    ? `<a href="${esc(Utils.safeUrl(p.external_url) || '#')}" target="_blank" rel="noopener" class="muted">link ↗</a>` : '';
                return `<div class="loc-row">
                    <span class="thumb-sq thumb-sq--emoji" aria-hidden="true">${plat.emoji || ''}</span>
                    <div class="loc-site"><span class="name">${esc(plat.label)}</span>
                        <span class="muted" style="font-size:12px">
                            👁 ${num(s.views)} · ❤ ${num(s.favorites)} · 💬 ${num(s.comments)}
                        </span>
                    </div>
                    ${link}
                </div>`;
            }).join('');
            return ItemFrame.section({
                id: 'pb-sec-pubto', title: ItemFrame.SECTIONS.publishedTo,
                body: rows || '<p class="muted">Not published anywhere yet.</p>',
            });
        },

        /* ── 3. Publish to more ──────────────────────────────────────────── */
        _publishToHtml(d) {
            const done = new Set((d.publications || [])
                .filter(p => p.status === 'posted').map(p => p.platform));
            const all = (window.Posts && Posts._PLATFORMS) || [];
            const left = all.filter(c => !done.has(c));
            if (!left.length) {
                return ItemFrame.section({
                    id: 'pb-sec-more', title: ItemFrame.SECTIONS.publishTo,
                    body: '<p class="muted">Everywhere PawPoller can post this, it has.</p>',
                });
            }
            const boxes = left.map(c => {
                const plat = this._plat(c);
                return `<label class="pill" style="cursor:pointer">
                    <input type="checkbox" data-pb-plat="${esc(c)}"> ${plat.emoji || ''} ${esc(plat.label)}
                </label>`;
            }).join(' ');
            return ItemFrame.section({
                id: 'pb-sec-more', title: ItemFrame.SECTIONS.publishTo,
                body: `<div class="chip-row" style="gap:.4rem">${boxes}</div>
                    <div style="margin-top:.6rem;display:flex;gap:.5rem;align-items:center">
                        <button class="btn btn-sm btn-primary" type="button" data-pb-publish>Publish to the ticked sites</button>
                        <span class="muted" id="pb-publish-msg"></span>
                    </div>`,
            });
        },

        /* ── 5. Growth ───────────────────────────────────────────────────── */
        _growthHtml() {
            return `<div class="card" id="pb-chart-card" style="display:none;">
                ${ItemFrame.sectionHead({ id: 'pb-sec-growth', title: ItemFrame.SECTIONS.growth })}
                <p class="muted" style="margin:.1rem 0 .6rem;">Summed views, favourites and comments across every site over time.</p>
                <div class="chart-wrap"><canvas id="pb-combined-chart"></canvas></div>
            </div>`;
        },

        async _loadChart(id) {
            // A nicety: a post with one data point has no trend worth drawing, and a
            // failure here must never take the page with it.
            try {
                const snap = await API.getPostSnapshots(id);
                const rows = (snap && snap.snapshots) || [];
                if (rows.length > 1 && window.Charts) {
                    const card = document.getElementById('pb-chart-card');
                    if (card) {
                        card.style.display = '';
                        Charts.aggregateLine('pb-combined-chart', rows,
                            ['views', 'favorites_count', 'comments_count']);
                    }
                }
            } catch (e) { /* optional */ }
        },

        /* ── 7. History ──────────────────────────────────────────────────── */
        _historyHtml(d) {
            const events = [];
            if (d.created_at) events.push({ when: d.created_at, what: 'Composed' });
            (d.publications || []).forEach(p => {
                const plat = this._plat(p.platform);
                if (p.status === 'posted') {
                    events.push({ when: p.created_at, what: `Published to ${plat.label}` });
                } else if (p.error) {
                    events.push({ when: p.created_at, what: `Failed on ${plat.label}: ${p.error}` });
                }
            });
            events.sort((a, b) => String(a.when || '').localeCompare(String(b.when || '')));
            const rows = events.map(e => `
                <div class="loc-row">
                    <div class="loc-site"><span class="name">${esc(e.what)}</span>
                    <span class="muted" style="font-size:12px">${esc(e.when || '')}</span></div>
                </div>`).join('');
            return ItemFrame.section({
                id: 'pb-sec-history', title: ItemFrame.SECTIONS.history,
                body: rows,
            });
        },

        /* ── Prev/next across the feed ───────────────────────────────────── */
        async _renderDetailNav(id) {
            if (!window.ItemNav) return;
            let posts = (window.Posts && Posts._feed) || null;
            if (!posts || !posts.length) {
                try { posts = ((await API.getPosts()) || {}).posts || []; }
                catch (e) { return; }
            }
            ItemNav.mount({
                names: posts.map(p => String(p.post_id)), current: String(id),
                href: n => `#/posts/${encodeURIComponent(n)}`,
                // "new" and "contacts" are pages, not posts.
                routeTest: h => /^#\/posts\/[^/]/.test(h)
                    && !/^#\/posts\/(new|contacts)/.test(h),
            });
        },

        _plat(code) {
            if (window.Posts && Posts._plat) return Posts._plat(code);
            return (window.platformByCode && window.platformByCode(code))
                || { code, label: code, emoji: '' };
        },

        _wire() {
            if (this._wired) return;
            this._wired = true;
            document.addEventListener('click', async (e) => {
                if (!e.target.closest('[data-pb-publish]')) return;
                const msg = document.getElementById('pb-publish-msg');
                const picked = [...document.querySelectorAll('[data-pb-plat]:checked')]
                    .map(i => i.dataset.pbPlat);
                if (!picked.length) { if (msg) msg.textContent = 'Pick at least one site.'; return; }
                if (msg) msg.textContent = 'Publishing…';
                try {
                    await API.publishPost(this._id, { platforms: picked });
                    if (msg) msg.textContent = 'Done.';
                    await this.render(this._id);
                } catch (err) {
                    if (msg) msg.textContent = 'Failed: ' + (err.message || err);
                }
            });
        },
    };

    window.PostBoard = PostBoard;
})();
