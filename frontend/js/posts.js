/* ── Posts hub (microblog / "tweet-like" publishing) ─────────────
 *
 * Three pages, dispatched from the SPA router:
 *   #/posts          the feed — every post with its per-site numbers (spec 018)
 *   #/posts/new      the composer — each site's own limit, reason and preview
 *   #/posts/contacts tag contacts — one @alias, each site's real handle
 * The post item page (#/posts/<id>) lives in post_board.js.
 *
 * Every number on the feed comes from ONE request (GET /api/posts resolves all
 * publications in a batched pass). "Not measured" is never shown as 0. The composer
 * asks the server how each site will treat the draft (POST /api/posts/preview runs
 * the publisher's own renderer), so the browser keeps no second copy of the rules.
 */
window.Posts = {

    /* Microblog platforms the module can post to, in the composer's order. */
    _PLATFORMS: ['bsky', 'tw', 'mast', 'thr', 'tum', 'ig', 'tg', 'fb'],
    /* Ticked by default — the rest need their posting creds set up first. */
    _DEFAULT_CHECKED: ['bsky', 'mast'],
    /* Post-only broadcast targets that aren't in the pollable window.PLATFORMS
     * registry — give _plat() their label/emoji so compose renders them nicely. */
    _POST_ONLY_META: {
        tg: { code: 'tg', label: 'Telegram', emoji: '\u{1F4E3}', color: '#2AABEE' },
    },
    /* Used until GET /api/posts/rules answers (and if it never does). The server's
     * table in posting/post_publisher.py is the real one. */
    _RULES_FALLBACK: {
        labels: { bsky: 'Bluesky', tw: 'X', mast: 'Mastodon', thr: 'Threads', tum: 'Tumblr', ig: 'Instagram', tg: 'Telegram', fb: 'Facebook' },
        limits: { bsky: 300, tw: 280, mast: 500, thr: 500, ig: 2200, tg: 4096, tum: null, fb: 63206 },
        tg_caption_limit: 1024, max_images: 4, text_only: [], image_required: ['ig'],
        thread_platforms: ['bsky', 'mast'], handle_platforms: ['bsky', 'tw', 'mast', 'thr', 'tum'],
    },
    _rules: null,
    _MAX_IMAGES: 4,       // X / Bluesky / Mastodon all cap a post at 4 images

    _pendingFiles: [],    // Files awaiting upload (ordered)
    _previewUrls: [],     // object URLs for the compose previews (index-aligned)
    _altTexts: [],        // ALT text per image (index-aligned)

    _contacts: [],           // handle-book (loaded once per render)
    _mentionBindings: {},    // { token: contactId } — @aliases bound in this draft
    _addForToken: null,      // the @token the open "add contact" form will bind

    /* Per-platform handle fields shown in the contact forms. */
    _MENTION_FIELDS: [
        { code: 'bsky', label: 'Bluesky', key: 'handle_bsky', ph: 'name.bsky.social' },
        { code: 'tw', label: 'X', key: 'handle_tw', ph: 'xhandle' },
        { code: 'mast', label: 'Mastodon', key: 'handle_mast', ph: 'user@instance.social' },
        { code: 'thr', label: 'Threads', key: 'handle_thr', ph: 'threadshandle' },
        { code: 'tum', label: 'Tumblr', key: 'handle_tum', ph: 'blogname' },
    ],

    _DRAFT_KEY: 'pp-post-draft-v1',
    _VIEW_KEY: 'pp-posts-view',
    _FILTER_KEY: 'pp-posts-filters',

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || this._POST_ONLY_META[code]
            || { code, label: code, emoji: '', color: '#888' };
    },

    _label(code) {
        const r = this._rules || this._RULES_FALLBACK;
        return (r.labels && r.labels[code]) || this._plat(code).label;
    },

    /* A site's logo (bundled under /img/platforms/), else its emoji. Decorative: the
     * site's name always sits next to it in text. */
    _logo(code) {
        const p = this._plat(code);
        const src = p.logo || (code === 'tg' ? '/img/platforms/tg.svg' : '');
        return src ? `<img class="pp-ico" src="${src}" alt="" aria-hidden="true">`
            : `<span class="pp-ico pp-ico--emoji" aria-hidden="true">${p.emoji || '•'}</span>`;
    },

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
    },

    /* "API 409: {"detail":"…"}" → "…" */
    _errText(err) {
        const m = /^API \d+: ([\s\S]*)$/.exec((err && err.message) || String(err));
        if (m) { try { return JSON.parse(m[1]).detail || m[1]; } catch (e) { return m[1]; } }
        return (err && err.message) || String(err);
    },

    _ls(key, val) {
        try {
            if (val === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
            if (val === null) localStorage.removeItem(key);
            else localStorage.setItem(key, JSON.stringify(val));
        } catch (e) { /* private window / blocked storage: no memory, still works */ }
        return null;
    },

    _num(n) {
        if (n == null) return '—';
        if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'm';
        if (n >= 1e4) return Math.round(n / 1e3) + 'k';
        if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
        return String(n);
    },

    /* "now", "12 m", "2 h", then "3 Sep" / "25 Oct 2022" (Utils.time: the saved zone). */
    _when(v) {
        const T = Utils.time, ms = T.ms(v);
        if (isNaN(ms)) return '';
        const mins = Math.round((Date.now() - ms) / 60000);
        if (mins >= 0 && mins < 1) return 'now';
        if (mins >= 0 && mins < 60) return `${mins} m`;
        if (mins >= 0 && mins < 24 * 60) return `${Math.round(mins / 60)} h`;
        return T.fmt.date(v);
    },

    _avatar(persona, extra = '') {
        const name = (persona && persona.name) || '?';
        const color = (persona && persona.color) || '';
        const style = color ? ` style="--av:${this.esc(color)}"` : '';
        return `<span class="pp-av ${extra}"${style} aria-hidden="true">${this.esc(name.trim().charAt(0).toUpperCase() || '?')}</span>`;
    },

    /* Post text, escaped, with @tags picked out. */
    _bodyHtml(text) {
        return this.esc(text).replace(/(^|[\s(])(@[\w][\w.@-]*)/g, '$1<span class="pp-m">$2</span>');
    },

    _settingsHref(code) {
        return code === 'tg' ? '#/settings/telegram' : `#/settings/platforms/${code}`;
    },

    async _loadRules() {
        if (this._rules) return this._rules;
        try { this._rules = await API.getPostRules(); } catch (e) { this._rules = null; }
        return this._rules || this._RULES_FALLBACK;
    },

    /* ══ Page: feed ════════════════════════════════════════════════
     * A view of what you've published; composing lives under Create → New post
     * (#/posts/new). Spec 018: posts read like posts, each with its numbers. */

    _filters: { status: '', persona: '', q: '' },
    _seq: 0,

    async render() {
        const _rt = App._routeToken();   // route race guard (App._stale)
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
        const saved = this._ls(this._FILTER_KEY) || {};
        this._filters = { status: saved.status || '', persona: saved.persona || '', q: '', kind: saved.kind || '' };
        this._view = this._ls(this._VIEW_KEY) === 'table' ? 'table' : 'feed';
        app.innerHTML = `
            <div class="pp-page">
                <div class="page-header pp-head">
                    <div>
                        <h1>Posts</h1>
                        <p class="muted">Short posts across your microblog accounts.</p>
                    </div>
                    <div class="pp-head-actions">
                        <a class="btn" href="#/posts/contacts">@ Tag contacts</a>
                        <a class="btn" href="#/posts/journal">＋ New journal</a>
                        <a class="btn btn-primary pp-new-btn" href="#/posts/new">＋ New post</a>
                    </div>
                </div>
                <div id="pp-stats" class="pp-stats" hidden></div>
                <div class="pp-bar">
                    <div class="pp-seg" id="pp-status" role="group" aria-label="Show"></div>
                    <div class="pp-seg" id="pp-persona" role="group" aria-label="Persona" hidden></div>
                    <label class="sr-only" for="pp-q">Search posts</label>
                    <input type="search" id="pp-q" class="pp-search" placeholder="Search posts…" autocomplete="off">
                    <span class="pp-sp"></span>
                    <div class="pp-seg" id="pp-view" role="group" aria-label="Layout">
                        <button type="button" data-view="feed" aria-pressed="${this._view === 'feed'}">▤ Feed</button>
                        <button type="button" data-view="table" aria-pressed="${this._view === 'table'}">☰ Table</button>
                    </div>
                </div>
                <div id="pp-banner"></div>
                <div class="pp-layout">
                    <div id="post-feed" class="pp-feed">${Utils.skeleton('rows', 6, 'your posts')}</div>
                    <aside id="pp-side" class="pp-side" aria-label="Posts at a glance"></aside>
                </div>
                <a class="pp-fab" href="#/posts/new" aria-label="New post">＋</a>
            </div>`;
        this._paintStatus({});
        this._wireFeed();
        this._loadRules();
        await Promise.all([this._loadFeed(), this._loadSummary(), this._loadBanner()]);
    },

    _wireFeed() {
        const q = document.getElementById('pp-q');
        let t = null;
        q.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => { this._filters.q = q.value.trim(); this._loadFeed(); }, 250);
        });
        document.getElementById('pp-status').addEventListener('click', e => {
            const b = e.target.closest('[data-status]');
            if (!b) return;
            // Spec 027: "Journals" is a kind, not a status; the other buttons show every kind.
            this._filters.kind = b.dataset.status === 'journal' ? 'journal' : '';
            this._filters.status = b.dataset.status === 'journal' ? '' : b.dataset.status;
            this._saveFilters();
            this._loadFeed();
        });
        document.getElementById('pp-persona').addEventListener('click', e => {
            const b = e.target.closest('[data-persona]');
            if (!b) return;
            this._filters.persona = b.dataset.persona;
            this._saveFilters();
            this._loadFeed();
        });
        document.getElementById('pp-view').addEventListener('click', e => {
            const b = e.target.closest('[data-view]');
            if (!b || b.dataset.view === this._view) return;
            this._view = b.dataset.view;
            this._ls(this._VIEW_KEY, this._view);
            document.querySelectorAll('#pp-view [data-view]').forEach(x =>
                x.setAttribute('aria-pressed', String(x.dataset.view === this._view)));
            this._paintFeed();
        });
        const feed = document.getElementById('post-feed');
        feed.addEventListener('click', e => this._onFeedClick(e));
        feed.addEventListener('keydown', e => this._onMenuKey(e));
        this._wireMenuDismiss();
    },

    _saveFilters() {
        this._ls(this._FILTER_KEY, { status: this._filters.status, persona: this._filters.persona,
                                     kind: this._filters.kind || '' });
    },

    _paintStatus(counts) {
        const el = document.getElementById('pp-status');
        if (!el) return;
        const cur = this._filters.kind === 'journal' ? 'journal' : this._filters.status;
        const b = (key, label, n, cls) => `<button type="button" data-status="${key}" aria-pressed="${cur === key}">`
            + `${label}${n ? ` <b class="${cls}">${n}</b>` : ''}</button>`;
        el.innerHTML = b('', 'All', 0, '') + b('scheduled', 'Scheduled', counts.scheduled, 'pp-n-warn')
            + b('failed', 'Failed', counts.failed, 'pp-n-bad') + b('journal', 'Journals', 0, '');
    },

    _paintPersonas(personas) {
        const el = document.getElementById('pp-persona');
        if (!el) return;
        if (!personas || personas.length < 2) {
            el.hidden = true;
            if (this._filters.persona) { this._filters.persona = ''; this._saveFilters(); }
            return;
        }
        const cur = String(this._filters.persona || '');
        el.hidden = false;
        el.innerHTML = `<button type="button" data-persona="" aria-pressed="${cur === ''}">All personas</button>`
            + personas.map(p => `<button type="button" data-persona="${p.persona_id}" aria-pressed="${cur === String(p.persona_id)}">`
                + `${this.esc(p.name)}</button>`).join('');
    },

    async _loadFeed() {
        const feed = document.getElementById('post-feed');
        if (!feed) return;
        const seq = ++this._seq;
        let data;
        try {
            data = await API.getPosts({ status: this._filters.status, persona_id: this._filters.persona,
                                        q: this._filters.q, kind: this._filters.kind || '' });
        } catch (err) {
            if (seq !== this._seq) return;
            feed.innerHTML = `<div class="card error">Failed to load posts: ${this.esc(err.message)}</div>`;
            return;
        }
        if (seq !== this._seq || !document.getElementById('post-feed')) return;
        // Feed order, kept so the post item page can step prev/next through the
        // list you were actually looking at (4.34.4).
        this._feed = (data && data.posts) || [];
        this._paintStatus((data && data.counts) || {});
        this._paintPersonas((data && data.personas) || []);
        this._paintFeed();
    },

    _paintFeed() {
        const feed = document.getElementById('post-feed');
        if (!feed) return;
        const posts = this._feed || [];
        if (!posts.length) {
            const filtered = this._filters.status || this._filters.persona || this._filters.q || this._filters.kind;
            feed.innerHTML = filtered
                ? `<div class="empty-state"><p class="muted">No posts match.</p>
                     <button type="button" class="btn btn-sm" data-act="clear-filters">Show all posts</button></div>`
                : `<div class="empty-state"><p class="muted">No posts yet.</p>
                     <a class="btn btn-primary" href="#/posts/new">Write your first one</a></div>`;
            return;
        }
        feed.innerHTML = this._view === 'table' ? this._table(posts) : posts.map(p => this._postCard(p)).join('');
        if (window.Comments) Comments.wireRetry(feed, () => this._loadFeed());   // spec 021
    },

    /* The number a site's chip shows: likes where the site counts them, else views. */
    _headline(stats) {
        if (!stats) return '';
        if (stats.favorites != null) return `<span class="pp-n">♥ ${this._num(stats.favorites)}</span>`;
        if (stats.views != null) return `<span class="pp-n">👁 ${this._num(stats.views)}</span>`;
        return '';
    },

    _chips(p) {
        const pubs = p.publications || [];
        const out = pubs.map(pub => {
            const label = this.esc(this._label(pub.platform));
            if (pub.status === 'posted') {
                const inner = `${this._logo(pub.platform)}${label} ${this._headline(pub.stats)}`;
                const url = Utils.safeUrl ? Utils.safeUrl(pub.external_url || '') : (pub.external_url || '');
                return url
                    ? `<a class="pp-chip" href="${this.esc(url)}" target="_blank" rel="noopener" title="Open on ${label}">${inner}</a>`
                    : `<span class="pp-chip">${inner}</span>`;
            }
            if (pub.status === 'failed') {
                return `<span class="pp-chip pp-chip--bad" title="${this.esc(pub.error)}">${this._logo(pub.platform)}${label} <span aria-hidden="true">✕</span><span class="sr-only">failed</span></span>`;
            }
            return `<span class="pp-chip">${this._logo(pub.platform)}${label} <span class="muted">${this.esc(pub.status || '')}</span></span>`;
        });
        const seen = new Set(pubs.map(x => x.platform));
        (p.scheduled || []).forEach(s => {
            if (seen.has(s.platform)) return;
            seen.add(s.platform);
            out.push(`<span class="pp-chip pp-chip--queued">${this._logo(s.platform)}${this.esc(this._label(s.platform))}</span>`);
        });
        return out.join('');
    },

    _engagement(p) {
        const pubs = (p.publications || []).filter(x => x.status === 'posted');
        if (!pubs.length) return '';
        const t = p.totals || {}, tr = t.tracked || {};
        const bits = [];
        if (tr.favorites) bits.push(`<span title="Likes">♥ <b>${this._num(t.favorites)}</b><span class="sr-only"> likes</span></span>`);
        if (tr.reposts) bits.push(`<span title="Reposts">⟳ <b>${this._num(t.reposts)}</b><span class="sr-only"> reposts</span></span>`);
        if (tr.comments) bits.push(`<span title="Replies">💬 <b>${this._num(t.comments)}</b><span class="sr-only"> replies</span></span>`);
        if (tr.views) bits.push(`<span title="Views">👁 <b>${this._num(t.views)}</b><span class="sr-only"> views</span></span>`);
        if (!bits.length) return `<div class="pp-eng muted">Not measured yet: the next poll fills this in.</div>`;
        if (!tr.views) {
            const sites = [...new Set(pubs.map(x => this._label(x.platform)))].join(', ');
            bits.push(`<span class="muted">views not tracked on ${this.esc(sites)}</span>`);
        }
        return `<div class="pp-eng">${bits.join('')}</div>`;
    },

    _images(p) {
        const media = p.media || [];
        if (!media.length) return '';
        const n = Math.min(media.length, 4);
        const imgs = media.slice(0, 4).map((m, i) =>
            `<img class="post-card-img" data-rating="${this.esc(p.rating || 'general')}" loading="lazy"
                src="${API.postImageUrl(p.post_id)}&idx=${i}" alt="${this.esc(m.alt || '')}">`).join('');
        return `<div class="pp-imgs pp-imgs--${n}">${imgs}</div>`;
    },

    _menu(p) {
        const id = p.post_id;
        return `
            <button type="button" class="pp-more" data-menu-btn="${id}" aria-haspopup="menu" aria-expanded="false"
                aria-controls="pp-menu-${id}" title="More">⋯<span class="sr-only"> More for this post</span></button>
            <div class="pp-menu" id="pp-menu-${id}" role="menu" hidden>
                <a role="menuitem" href="#/posts/${id}">Open post page</a>
                ${p.kind === 'journal'
                    ? `<a role="menuitem" href="#/posts/journal/${id}">Edit journal</a>`
                    : `<button type="button" role="menuitem" data-act="again" data-id="${id}">Post again…</button>`}
                <button type="button" role="menuitem" data-act="copy" data-id="${id}">Copy text</button>
                <button type="button" role="menuitem" data-act="collect" data-id="${id}">Add to a collection</button>
                <button type="button" role="menuitem" class="pp-menu-del" data-act="delete" data-id="${id}">Delete from PawPoller…</button>
            </div>`;
    },

    _postCard(p) {
        const scheduled = p.scheduled || [];
        const failed = p.failed_sites || [];
        const posted = (p.publications || []).some(x => x.status === 'posted');
        const name = p.persona ? p.persona.name : 'No persona';
        const tags = [];
        if (scheduled.length) {
            tags.push(`<span class="pp-tag pp-tag--sched">Scheduled · ${this.esc(Utils.time.fmt.dateTime(scheduled[0].scheduled_at))}</span>`);
        }
        if (p.rating && p.rating !== 'general') {
            tags.push(`<span class="pp-tag pp-tag--${this.esc(p.rating)}">${this.esc(p.rating.charAt(0).toUpperCase() + p.rating.slice(1))}</span>`);
        }
        if (p.thread_count) tags.push(`<span class="pp-tag pp-tag--thread">🧵 ${p.thread_count + 1} parts</span>`);
        if (p.kind === 'journal') tags.push('<span class="pp-tag pp-tag--journal">📓 Journal</span>');
        if (!posted && !scheduled.length && !failed.length) tags.push('<span class="pp-tag">Not posted</span>');
        const time = scheduled.length && !posted ? '' :
            `<time class="muted" datetime="${this.esc(p.created_at)}" title="${this.esc(Utils.time.fmt.dateTime(p.created_at))}">· ${this.esc(this._when(p.created_at))}</time>`;
        const fails = failed.map(code => {
            const pub = (p.publications || []).find(x => x.platform === code && x.status === 'failed') || {};
            const label = this.esc(this._label(code));
            return `<div class="pp-failbox" data-fail="${this.esc(code)}">
                <span aria-hidden="true">✕</span>
                <span><b>${label} didn't post:</b> ${this.esc(pub.error || 'no reason was given')}</span>
                <span class="pp-sp"></span>
                <button type="button" class="btn btn-sm btn-primary" data-retry="${this.esc(code)}" data-id="${p.post_id}">Retry ${label}</button>
                <a class="btn btn-sm" href="${this.esc(this._settingsHref(code))}">Open account</a>
            </div>`;
        }).join('');
        const schedActions = scheduled.length ? `
            <div class="pp-row pp-sched-actions">
                <button type="button" class="btn btn-sm" data-sched="time" data-id="${p.post_id}">Change time</button>
                <button type="button" class="btn btn-sm" data-sched="now" data-id="${p.post_id}">Post now</button>
                <button type="button" class="btn btn-sm btn-ghost" data-sched="cancel" data-id="${p.post_id}">Unschedule</button>
            </div><div class="pp-sched-form" id="pp-sched-form-${p.post_id}" hidden></div>` : '';
        const text = (p.kind === 'journal' && p.title ? `<b class="pp-jtitle">${this.esc(p.title)}</b><br>` : '')
            + (p.body ? this._bodyHtml(p.body) : '<span class="muted">(image only)</span>');
        return `
            <article class="pp-card${scheduled.length ? ' pp-card--sched' : ''}${failed.length ? ' pp-card--fail' : ''}" data-post="${p.post_id}">
                ${this._avatar(p.persona)}
                <div class="pp-card-main">
                    <div class="pp-who"><b>${this.esc(name)}</b>${time}${tags.join('')}${this._menu(p)}</div>
                    <a class="pp-txt pp-open" href="#/posts/${p.post_id}">${text}</a>
                    ${this._images(p)}
                    ${fails}
                    <div class="pp-where">${this._chips(p) || '<span class="muted">Not published anywhere yet.</span>'}</div>
                    ${this._commentsLine(p)}
                    ${this._engagement(p)}
                    ${schedActions}
                </div>
            </article>`;
    },

    /* Each site's paired comment (spec 021): only the ones worth a look — failed, waiting,
     * skipped — plus a quiet "💬 on N sites" when they all went up. */
    _commentsLine(p) {
        const cs = p.comments || {};
        const codes = Object.keys(cs);
        if (!codes.length || !window.Comments) return '';
        const odd = codes.filter(c => cs[c].status !== 'posted');
        if (!odd.length) return `<div class="pp-cm-line muted">💬 Comment under it on ${codes.map(c => this.esc(this._label(c))).join(', ')}</div>`;
        return `<div class="pp-cm-line">${odd.map(c => `<div><b>${this.esc(this._label(c))}:</b> ${Comments.stateHtml(cs[c])}</div>`).join('')}</div>`;
    },

    _table(posts) {
        const num = (v, on) => `<td class="pp-num">${on ? this._num(v) : '<span class="muted" title="Not tracked">—</span>'}</td>`;
        const rows = posts.map(p => {
            const t = p.totals || {}, tr = t.tracked || {};
            const thumb = (p.media || []).length
                ? `<img class="post-card-img pp-thumb" data-rating="${this.esc(p.rating || 'general')}" loading="lazy" src="${API.postImageUrl(p.post_id)}" alt="">`
                : '';
            const sites = (p.publications || []).map(pub => pub.status === 'failed'
                ? `<span class="pp-ico-bad" title="${this.esc(this._label(pub.platform))} failed">${this._logo(pub.platform)}<span class="sr-only">${this.esc(this._label(pub.platform))} failed</span></span>`
                : `<span title="${this.esc(this._label(pub.platform))}">${this._logo(pub.platform)}<span class="sr-only">${this.esc(this._label(pub.platform))}</span></span>`).join('');
            const when = (p.scheduled || []).length
                ? `<span class="pp-n-warn">${this.esc(Utils.time.fmt.dateTime(p.scheduled[0].scheduled_at))}</span>`
                : this.esc(this._when(p.created_at));
            return `<tr data-post="${p.post_id}">
                <td>${thumb}</td>
                <td class="pp-td-txt"><a href="#/posts/${p.post_id}">${p.kind === 'journal' && p.title ? `📓 <b>${this.esc(p.title)}</b> ` : ''}${this.esc(p.body) || '<span class="muted">(image only)</span>'}</a></td>
                <td><span class="pp-icos">${sites}</span></td>
                ${num(t.favorites, tr.favorites)}${num(t.comments, tr.comments)}${num(t.views, tr.views)}
                <td class="pp-nowrap">${when}</td>
                <td class="pp-td-menu">${this._menu(p)}</td></tr>`;
        }).join('');
        return `<div class="pp-table-wrap"><table class="pp-table">
            <thead><tr><th><span class="sr-only">Image</span></th><th>Post</th><th>Sites</th>
                <th class="pp-num">Likes</th><th class="pp-num">Replies</th><th class="pp-num">Views</th>
                <th>When</th><th><span class="sr-only">More</span></th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    },

    _post(id) { return (this._feed || []).find(p => String(p.post_id) === String(id)); },

    /* ── The ⋯ menu: one open at a time; keyboard per the ARIA menu pattern ── */

    _openMenu(btn) {
        this._closeMenus();
        const menu = document.getElementById(btn.getAttribute('aria-controls'));
        if (!menu) return;
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        const first = menu.querySelector('[role="menuitem"]');
        if (first) first.focus();
    },

    _closeMenus(returnFocus) {
        document.querySelectorAll('.pp-menu:not([hidden])').forEach(m => {
            m.hidden = true;
            const btn = document.querySelector(`[aria-controls="${m.id}"]`);
            if (btn) {
                btn.setAttribute('aria-expanded', 'false');
                if (returnFocus) btn.focus();
            }
        });
    },

    _wireMenuDismiss() {
        if (this._menuDismissWired) return;
        this._menuDismissWired = true;
        document.addEventListener('click', e => {
            if (!e.target.closest('.pp-menu, [data-menu-btn]')) this._closeMenus();
        });
    },

    _onMenuKey(e) {
        const menu = e.target.closest('.pp-menu');
        if (!menu) return;
        const items = [...menu.querySelectorAll('[role="menuitem"]')];
        const i = items.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.preventDefault(); this._closeMenus(true); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
        else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
        else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
        else if (e.key === 'Tab') this._closeMenus();
    },

    async _onFeedClick(e) {
        const t = e.target;
        const mb = t.closest('[data-menu-btn]');
        if (mb) {
            e.preventDefault();
            if (mb.getAttribute('aria-expanded') === 'true') this._closeMenus(true);
            else this._openMenu(mb);
            return;
        }
        const act = t.closest('[data-act]');
        if (act) {
            const p = this._post(act.dataset.id);
            const a = act.dataset.act;
            if (a === 'clear-filters') {
                this._filters = { status: '', persona: '', q: '', kind: '' };
                const q = document.getElementById('pp-q');
                if (q) q.value = '';
                this._saveFilters();
                this._loadFeed();
            } else if (a === 'again' && p) {
                this._prefill = { body: p.body || '', rating: p.rating || 'general' };
                window.location.hash = '#/posts/new';
            } else if (a === 'copy' && p) {
                this._closeMenus(true);
                try { await navigator.clipboard.writeText(p.body || ''); this._toast('success', 'Copied'); }
                catch (err) { this._toast('error', 'Copy failed: your browser blocked the clipboard.'); }
            } else if (a === 'collect' && p) {
                this._collectionPicker(act.closest('.pp-menu'), p);
            } else if (a === 'add-to' && p) {
                await this._addToCollection(p, act.dataset.cid, act.textContent.trim());
            } else if (a === 'delete' && p) {
                this._closeMenus(true);
                this._delete(p.post_id);
            }
            return;
        }
        const retry = t.closest('[data-retry]');
        if (retry) { this._retry(this._post(retry.dataset.id), retry.dataset.retry, retry); return; }
        const sched = t.closest('[data-sched]');
        if (sched) {
            const p = this._post(sched.dataset.id);
            if (!p) return;
            if (sched.dataset.sched === 'time') this._changeTimeForm(p);
            else if (sched.dataset.sched === 'now') this._postNow(p, sched);
            else if (sched.dataset.sched === 'cancel') this._unschedule(p);
        }
    },

    async _collectionPicker(menu, p) {
        if (!menu) return;
        menu.innerHTML = '<span class="pp-menu-note">Loading collections…</span>';
        let cols = [];
        try { cols = ((await API.getCollections()) || {}).collections || []; } catch (e) { cols = []; }
        menu.innerHTML = cols.length
            ? `<span class="pp-menu-note">Add to…</span>` + cols.map(c =>
                `<button type="button" role="menuitem" data-act="add-to" data-id="${p.post_id}" data-cid="${c.id}">${this.esc(c.name)}</button>`).join('')
            : `<span class="pp-menu-note">No collections yet.</span><a role="menuitem" href="#/collections">Make one in Collections</a>`;
        const first = menu.querySelector('[role="menuitem"]');
        if (first) first.focus();
    },

    async _addToCollection(p, cid, name) {
        this._closeMenus(true);
        try {
            await API.addCollectionMember(cid, { member_type: 'post', member_ref: String(p.post_id) });
            this._toast('success', `Added to ${name}`);
        } catch (err) {
            this._toast('error', 'Could not add it: ' + this._errText(err));
        }
        this._paintFeed();   // restore the menus' normal items
    },

    /* The one place this module publishes. `body` carries platforms / account_ids /
     * persona_id; spec 017's background job + live grid draws below `host`. Resolves
     * null when the grid was minimised or the page left (the result toast reports). */
    async _send(postId, body, host) {
        const send = (extra) => API.publishPost(postId, { ...body, confirm_live: true, ...extra });
        return window.Activity ? Activity.run(send, host) : send({});
    },

    async _retry(p, site, btn) {
        if (!p) return;
        const pub = (p.publications || []).find(x => x.platform === site && x.status === 'failed') || {};
        btn.disabled = true;
        try {
            const ids = pub.account_id ? { [site]: pub.account_id } : {};
            const res = await this._send(p.post_id, { platforms: [site], account_ids: ids, persona_id: null },
                btn.closest('.pp-failbox'));
            if (!res) return;
            const ok = (res.results || []).some(r => r.success);
            this._toast(ok ? 'success' : 'error', ok ? `Posted to ${this._label(site)}`
                : `${this._label(site)}: ${((res.results || [])[0] || {}).error || 'failed again'}`);
            await this._loadFeed();
        } catch (err) {
            btn.disabled = false;
            this._toast('error', 'Retry failed: ' + this._errText(err));
        }
    },

    _changeTimeForm(p) {
        const box = document.getElementById(`pp-sched-form-${p.post_id}`);
        if (!box) return;
        if (!box.hidden) { box.hidden = true; return; }
        const id = `pp-when-${p.post_id}`;
        box.hidden = false;
        box.innerHTML = `<label for="${id}">New time (${this.esc(Utils.time.abbr())})</label>
            <input type="datetime-local" id="${id}" value="${this.esc(Utils.time.toPicker(p.scheduled[0].scheduled_at))}">
            <button type="button" class="btn btn-sm btn-primary" data-save-time>Save</button>
            <span class="muted" role="status"></span>`;
        box.querySelector('input').focus();
        box.querySelector('[data-save-time]').addEventListener('click', async () => {
            const msg = box.querySelector('[role="status"]');
            const when = Utils.time.toUtc(box.querySelector('input').value);
            if (!when || when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }
            try {
                for (const s of p.scheduled) {
                    await API.reschedulePostingQueue(s.queue_id, { scheduled_at: when.toISOString() });
                }
                this._toast('success', `Moved to ${Utils.time.fmt.dateTime(when)}`);
                await Promise.all([this._loadFeed(), this._loadSummary()]);
            } catch (err) {
                msg.textContent = 'Could not move it: ' + this._errText(err);
            }
        });
    },

    /* Cancel the queued rows, then post those sites now. Only rows the cancel actually
     * stopped are posted, so a row the scheduler already picked up never goes twice. */
    async _postNow(p, btn) {
        const rows = p.scheduled || [];
        if (!rows.length) return;
        const ok = await Components.confirmPublish({
            title: (p.body || '').slice(0, 90) || '(image only)',
            subtitle: 'Post · now instead of at the scheduled time',
            persona: p.persona ? p.persona.name : '',
            targets: rows.map(r => ({ code: r.platform, label: this._label(r.platform), emoji: this._plat(r.platform).emoji, account: '' })),
        });
        if (!ok) return;
        btn.disabled = true;
        const stopped = [];
        for (const r of rows) {
            try { await API.cancelPostingQueue(r.queue_id); stopped.push(r); } catch (e) { /* already firing */ }
        }
        if (!stopped.length) { this._toast('info', 'It is already posting.'); await this._loadFeed(); return; }
        const account_ids = {};
        stopped.forEach(r => { if (r.account_id) account_ids[r.platform] = r.account_id; });
        try {
            const res = await this._send(p.post_id, { platforms: stopped.map(r => r.platform), account_ids,
                persona_id: stopped[0].persona_id || null }, btn.closest('.pp-sched-actions'));
            if (!res) return;
            this._toast(res.failures ? 'error' : 'success', `Posted: ${res.successes || 0} ok, ${res.failures || 0} failed`);
        } catch (err) {
            this._toast('error', 'Post failed: ' + this._errText(err));
        }
        await Promise.all([this._loadFeed(), this._loadSummary()]);
    },

    async _unschedule(p) {
        let n = 0;
        for (const r of (p.scheduled || [])) {
            try { await API.cancelPostingQueue(r.queue_id); n++; } catch (e) { /* already firing */ }
        }
        this._toast(n ? 'success' : 'info', n ? 'Unscheduled. The post stays here as a draft.' : 'It is already posting.');
        await Promise.all([this._loadFeed(), this._loadSummary()]);
    },

    async _delete(id) {
        if (!confirm('Delete this post from PawPoller? Anything already posted stays live on each site.')) return;
        try {
            await API.deletePost(id);
            this._toast('success', 'Deleted');
            await Promise.all([this._loadFeed(), this._loadSummary()]);
        } catch (err) {
            this._toast('error', 'Delete failed: ' + (err.message || err));
        }
    },

    /* ── Header strip + side column (stored data only) ── */

    async _loadSummary() {
        let s;
        try { s = await API.getPostsSummary(); } catch (e) { return; }
        const stats = document.getElementById('pp-stats');
        const side = document.getElementById('pp-side');
        if (!stats || !side) return;
        const delta = (now, last, pct) => {
            if (!last && !now) return '';
            if (!last) return '<span class="pp-d pp-d--up">new this month</span>';
            const d = now - last;
            if (!d) return '<span class="pp-d">same as last month</span>';
            const txt = pct ? `${Math.abs(Math.round(100 * d / last))}%` : `${Math.abs(d)} on last month`;
            return `<span class="pp-d pp-d--${d > 0 ? 'up' : 'down'}">${d > 0 ? '▲' : '▼'} ${txt}</span>`;
        };
        const stat = (v, l, d) => `<div class="pp-stat"><div class="pp-stat-v">${v}</div><div class="pp-stat-l">${l}</div>${d || ''}</div>`;
        stats.hidden = false;
        stats.innerHTML = stat(this._num(s.posts_this_month), s.posts_this_month === 1 ? 'post this month' : 'posts this month', delta(s.posts_this_month, s.posts_last_month))
            + stat(this._num(s.likes_this_month), "likes on this month's posts", delta(s.likes_this_month, s.likes_last_month, true))
            + stat(this._num(s.reposts_this_month), 'reposts')
            + stat(s.best_site ? this.esc(this._label(s.best_site)) : '—', 'best site this month');

        const up = (s.coming_up || []).map(c => `
            <a class="pp-up" href="#/posts/${c.post_id}">
                <span class="pp-up-t">${this.esc(Utils.time.fmt.dateTime(c.scheduled_at))}</span>
                <span>${c.thread_count ? `🧵 ${c.thread_count + 1} parts · ` : ''}${this.esc(c.body || '(image only)')}</span>
            </a>`).join('');
        const max = Math.max(1, ...(s.by_site_30d || []).map(x => x.favorites));
        const bars = (s.by_site_30d || []).map(x => `
            <div class="pp-bar-row">${this._logo(x.platform)}<span class="sr-only">${this.esc(this._label(x.platform))}</span>
                <i style="width:${Math.max(2, Math.round(100 * x.favorites / max))}%" aria-hidden="true"></i>
                <span>${this._num(x.favorites)}</span></div>`).join('');
        const best = s.best_post ? `
            <a class="pp-best" href="#/posts/${s.best_post.post_id}">“${this.esc(s.best_post.body || '(image only)')}”
                <span class="muted">${this._num(s.best_post.favorites)} likes${s.best_post.reposts ? ` · ${this._num(s.best_post.reposts)} reposts` : ''}</span></a>` : '';
        side.innerHTML = `
            <section class="pp-box"><h2>Coming up</h2>${up || '<p class="muted">Nothing scheduled.</p>'}
                <a class="pp-box-link" href="#/posting/queue">Open the queue →</a></section>
            ${bars ? `<section class="pp-box"><h2>Likes by site · 30 days</h2><div class="pp-bars">${bars}</div></section>` : ''}
            ${best ? `<section class="pp-box"><h2>Best this month</h2>${best}</section>` : ''}`;
    },

    async _loadBanner() {
        const el = document.getElementById('pp-banner');
        if (!el) return;
        let n = 0;
        try { n = ((await API.getImportablePostCount()) || {}).count || 0; } catch (e) { n = 0; }
        if (!n) { el.innerHTML = ''; return; }
        el.innerHTML = `<div class="pp-banner" role="status">
            <span aria-hidden="true">📥</span>
            <span><b>${n} post${n === 1 ? '' : 's'}</b> on your accounts ${n === 1 ? "isn't" : "aren't"} in PawPoller yet. Bring them in so their numbers count.</span>
            <span class="pp-sp"></span>
            <a class="btn btn-sm" href="#/library/discovered">Review</a>
            <button type="button" class="btn btn-sm btn-primary" id="pp-import-all">Import all</button></div>`;
        document.getElementById('pp-import-all').addEventListener('click', async (e) => {
            e.target.disabled = true;
            e.target.textContent = 'Importing…';
            try {
                const r = await API.importDiscoveredPosts();
                this._toast(r.failed ? 'warn' : 'success',
                    `Imported ${r.imported}${r.failed ? `, ${r.failed} failed` : ''}`);
            } catch (err) {
                this._toast('error', 'Import failed: ' + this._errText(err));
            }
            await Promise.all([this._loadFeed(), this._loadSummary(), this._loadBanner()]);
        });
    },

    /* ══ Page: compose (Create → New post) ═════════════════════════
     * Left: you write. Right: one row per site (switch, account, its own character
     * ring, the reason it would refuse or change the post), the preview each site
     * will get, and Post now / Schedule. A clean success goes to the feed. */
    async renderCompose() {
        const _rt = App._routeToken();   // route race guard (App._stale)
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
        app.innerHTML = `
            <div class="pp-page pp-compose-page">
                <div class="page-header pp-head">
                    <div>
                        <h1>New post</h1>
                        <p class="muted"><a href="#/posts">← Posts</a></p>
                    </div>
                    <span class="pp-draft-state muted" id="pp-draft-state" aria-live="polite"></span>
                </div>
                <div id="post-compose"></div>
            </div>`;

        this._mentionBindings = {};
        this._addForToken = null;
        this._activeTab = null;
        const [cd] = await Promise.all([API.getContacts().catch(() => null), this._loadRules()]);
        this._contacts = (cd && cd.contacts) || [];   // tagging is additive — degrade to none
        if (App._stale(_rt)) return;

        this._renderCompose(document.getElementById('post-compose'));
        this._restoreDraft();
        // A time handed over from Analytics → When to post ("Schedule for …"): open the
        // picker pre-filled. It only pre-fills; nothing is scheduled until Confirm.
        if (this._scheduleAt) {
            document.getElementById('post-schedule-form').style.display = '';
            document.getElementById('post-schedule-datetime').value = this._scheduleAt;
            this._scheduleAt = null;
        }
        // An image handed over from the Promo Maker ("💬 Send to Posts"), else
        // re-sync previews for anything still pending from an earlier visit.
        if (this._handoffFiles && this._handoffFiles.length) {
            const files = this._handoffFiles;
            this._handoffFiles = null;
            this._addFiles(files);
        } else {
            this._renderPreviews();
        }
        this._suggestTime();
    },

    _renderCompose(el) {
        const rules = this._rules || this._RULES_FALLBACK;
        el.innerHTML = `
            <div class="pp-comp">
                <div class="pp-editor">
                    <div class="pp-edhead"><div class="persona-picker" data-persona-picker hidden></div></div>
                    <div id="pp-parts">
                        <div class="pp-part">
                            <div class="pp-rail"><span id="pp-av-slot">${this._avatar(null)}</span><span class="pp-line"></span></div>
                            <div class="pp-part-main">
                                <label class="sr-only" for="post-body">Post text</label>
                                <textarea id="post-body" class="post-body pp-ta" rows="4"
                                    placeholder="What's happening? Type @ to tag someone."
                                    aria-autocomplete="list" aria-controls="pp-pop" aria-expanded="false"></textarea>
                                <div id="pp-pop" class="pp-pop" role="listbox" aria-label="Tag contacts" hidden></div>
                                <div id="post-mentions" class="post-mentions" hidden></div>
                                <div id="post-contact-form" class="post-contact-form" hidden></div>
                                <div id="post-image-preview" class="pp-drop"></div>
                                <div id="pp-alt-edit" class="pp-alt-edit" hidden></div>
                            </div>
                        </div>
                        <div id="post-parts"></div>
                    </div>
                    <div class="pp-comment" id="pp-comment"></div>
                    <div class="pp-edfoot">
                        <label class="pp-tool">🖼 Images
                            <input type="file" id="post-image" accept="image/png,image/jpeg,image/gif,image/webp" hidden multiple>
                        </label>
                        <button type="button" class="pp-tool" id="post-addpart"
                            title="Each part posts as a reply to the one before (${rules.thread_platforms.map(c => this._label(c)).join(' and ')}; other sites get part 1)">🧵 Add part</button>
                        <button type="button" class="pp-tool" id="pp-tag">@ Tag</button>
                        <label class="pp-tool-label">Rating
                            <select id="post-rating">
                                <option value="general" selected>General</option>
                                <option value="mature">Mature</option>
                                <option value="adult">Adult</option>
                            </select>
                        </label>
                    </div>
                </div>
                <div class="pp-comp-side">
                    <div class="pp-dest" id="post-platforms" role="group" aria-label="Where it goes"></div>
                    <section class="pp-prev" id="pp-preview" aria-label="Preview"></section>
                    <div class="pp-actions">
                        <button type="button" class="btn btn-primary" id="post-submit">Post now</button>
                        <button type="button" class="btn" id="post-schedule-toggle" aria-expanded="false" aria-controls="post-schedule-form">🕘 Schedule…</button>
                    </div>
                    <div class="schedule-form" id="post-schedule-form" style="display:none">
                        <div class="schedule-form-inner">
                            <label class="schedule-label" for="post-schedule-datetime">Post the switched-on sites at:</label>
                            <input type="datetime-local" class="schedule-datetime" id="post-schedule-datetime">
                            <span class="pp-hint" id="pp-best-time" hidden></span>
                            <div class="schedule-form-actions">
                                <button class="btn btn-sm btn-primary" id="post-schedule-confirm">Confirm schedule</button>
                                <button class="btn btn-sm btn-outline" id="post-schedule-cancel">Cancel</button>
                            </div>
                        </div>
                    </div>
                    <p id="post-msg" class="pp-msg" role="status"></p>
                </div>
            </div>`;

        this._renderPlatformRows(document.getElementById('post-platforms'));
        this._comment = { open: false, shared: '', sites: {}, touched: {}, linked: null };
        this._paintComment();
        this._wireCompose();
        this._populateAccountSelectors();
        if (window.Comments) Comments.load().then(() => { this._applyCommentDefaults(); this._paintComment(); });
    },

    /* ── Paired comment (4.56.0, spec 021) ─────────────────────────
     * A comment under the post, from the same account, on every switched-on site that
     * can take a reply. One shared text; a site can have its own (its default template
     * fills it when the site is switched on, until you change it). An own text left empty
     * means "no comment on that site". `{link}`-style fill-ins come from a linked piece. */

    _replySites() {
        const reply = window.Comments ? Comments.REPLY : [];
        return this._selectedPlatforms().filter(c => reply.includes(c));
    },

    _applyCommentDefaults() {
        if (!window.Comments || !this._comment) return;
        const c = this._comment;
        this._replySites().forEach(code => {
            if (c.touched[code] || code in c.sites) return;
            const d = Comments.defaultFor(code);
            if (d.text) { c.sites[code] = d.text; c.open = true; }
        });
    },

    /* {site: text} for the switched-on reply sites ('' = none there); undefined when no comment at all. */
    _commentsPayload() {
        const c = this._comment;
        if (!c || !c.open) return undefined;
        const out = {};
        this._replySites().forEach(code => {
            out[code] = (code in c.sites ? c.sites[code] : c.shared).trim();
        });
        return Object.values(out).some(Boolean) ? out : undefined;
    },

    _paintComment() {
        const host = document.getElementById('pp-comment');
        if (!host || !this._comment) return;
        const c = this._comment;
        if (!c.open) {
            host.innerHTML = `<button type="button" class="pp-tool" data-cm="open">💬 Add a comment under it</button>`;
            return;
        }
        const sites = this._replySites();
        const tpl = window.Comments ? Comments.templateOptions('') : '';
        const rows = sites.map(code => {
            const own = code in c.sites;
            return `<div class="pp-cm-site" data-cm-site="${code}">
                <span class="pp-cm-name">${this._logo(code)}${this.esc(this._label(code))}</span>
                ${own ? `<textarea class="pp-ta pp-ta--part" rows="2" maxlength="2000" data-cm-own="${code}"
                            aria-label="${this.esc(this._label(code))} comment" placeholder="No comment on ${this.esc(this._label(code))}">${this.esc(c.sites[code])}</textarea>
                        <button type="button" class="pp-link" data-cm="shared" data-code="${code}">Use the shared text</button>`
                     : `<span class="muted">Shared text</span>
                        <button type="button" class="pp-link" data-cm="own" data-code="${code}">Different text here</button>`}
                <span class="pp-cm-why" id="pp-cm-why-${code}"></span>
            </div>`;
        }).join('');
        const skipped = this._selectedPlatforms().filter(code => !sites.includes(code))
            .map(code => this.esc(this._label(code)));
        const linked = c.linked
            ? `Fill-ins from <b>${this.esc(c.linked.title || c.linked.ref)}</b> <button type="button" class="pp-link" data-cm="unlink">Remove</button>`
            : `<button type="button" class="pp-link" data-cm="link">Link a piece</button> <span class="muted">for {link}, {title}, {artist}</span>`;
        host.innerHTML = `<div class="pp-cm-box">
            <div class="pp-cm-head"><label for="pp-cm-text">💬 Comment under the post</label>
                <span class="muted">Posted from the same account once the post is up.</span>
                <button type="button" class="pp-link" data-cm="close">Remove comment</button></div>
            <textarea id="pp-cm-text" class="pp-ta pp-ta--part" rows="2" maxlength="2000"
                placeholder="Links, credits, “full story here”…">${this.esc(c.shared)}</textarea>
            <div class="pp-cm-tools">
                <select id="pp-cm-tpl" aria-label="Fill the shared text from a template">${tpl}</select>
                <span class="pp-cm-linked">${linked}</span>
            </div>
            ${rows ? `<div class="pp-cm-sites">${rows}</div>` : '<p class="muted">Switch on a site that takes comments.</p>'}
            ${skipped.length ? `<p class="muted">No comment on ${skipped.join(', ')} (no replies there).</p>` : ''}
        </div>`;
    },

    _wireComment() {
        const host = document.getElementById('pp-comment');
        if (!host) return;
        host.addEventListener('click', e => {
            const b = e.target.closest('[data-cm]');
            if (!b) return;
            const c = this._comment, code = b.dataset.code;
            if (b.dataset.cm === 'open') { c.open = true; this._applyCommentDefaults(); }
            if (b.dataset.cm === 'close') { c.open = false; c.shared = ''; c.sites = {}; c.touched = {}; }
            if (b.dataset.cm === 'own') { c.sites[code] = c.shared; c.touched[code] = true; }
            if (b.dataset.cm === 'shared') { delete c.sites[code]; c.touched[code] = true; }
            if (b.dataset.cm === 'unlink') c.linked = null;
            if (b.dataset.cm === 'link' && window.WorkPicker) {
                WorkPicker.open({
                    title: 'Link a piece', confirmLabel: 'Link', multi: false,
                    onConfirm: async (items) => {
                        const it = (items || [])[0];
                        const m = it && /^(artwork|story):(.+)$/.exec(it.member_ref || '');
                        if (!m) { this._toast('error', 'Pick one of your artworks or stories'); return; }
                        c.linked = { kind: m[1], ref: m[2], title: it.title || m[2] };
                        this._paintComment(); this._changed();
                    },
                });
                return;
            }
            this._paintComment();
            this._changed();
            const focus = b.dataset.cm === 'open' ? document.getElementById('pp-cm-text')
                : b.dataset.cm === 'own' ? host.querySelector(`[data-cm-own="${code}"]`) : null;
            if (focus) focus.focus();
        });
        host.addEventListener('input', e => {
            const c = this._comment;
            if (e.target.id === 'pp-cm-text') c.shared = e.target.value;
            else if (e.target.dataset.cmOwn) { c.sites[e.target.dataset.cmOwn] = e.target.value; c.touched[e.target.dataset.cmOwn] = true; }
            else return;
            this._changed();
        });
        host.addEventListener('change', e => {
            if (e.target.id !== 'pp-cm-tpl' || !e.target.value) return;
            this._comment.shared = Comments.templateText(e.target.value);
            this._paintComment(); this._changed();
        });
    },

    _renderPlatformRows(el) {
        el.innerHTML = this._PLATFORMS.map(code => {
            const label = this.esc(this._label(code));
            const on = this._DEFAULT_CHECKED.includes(code) ? ' checked' : '';
            return `
            <div class="pp-drow post-plat" data-platform="${code}">
                <span class="pp-drow-logo">${this._logo(code)}</span>
                <div class="pp-drow-main">
                    <div class="pp-drow-name">${label}</div>
                    <span class="post-acct-slot" data-platform="${code}"></span>
                    <div class="pp-drow-why" id="pp-why-${code}"></div>
                </div>
                <span class="pp-ring" id="pp-ring-${code}"></span>
                <label class="pp-switch">
                    <input type="checkbox" class="post-plat-check" value="${code}" role="switch"${on}>
                    <span class="pp-switch-ui" aria-hidden="true"></span>
                    <span class="sr-only">Post to ${label}</span>
                </label>
            </div>`;
        }).join('');
    },

    async _populateAccountSelectors() {
        // 4.2.0: persona-first, via the shared picker (see artwork.js).
        const host = document.querySelector('[data-persona-picker]');
        const scope = document.getElementById('post-platforms') || document;
        await Components.personaPicker({
            host: host || document.createElement('div'),
            platforms: this._PLATFORMS,
            slot: code => scope.querySelector(`.post-acct-slot[data-platform="${code}"]`),
            row: code => scope.querySelector(`.post-plat[data-platform="${code}"]`),
            selectClass: 'post-acct-select',
            storageKey: 'pp-persona-posts',
            onChange: () => { this._paintAvatar(); this._schedulePreview(); },
        });
        this._paintAvatar();
        const d = this._pendingDraftPlatforms;
        if (d) {   // re-apply the draft's switches after the picker set its own
            document.querySelectorAll('.post-plat-check').forEach(c => {
                if (!c.disabled) c.checked = d.includes(c.value);
            });
            this._pendingDraftPlatforms = null;
        }
        this._schedulePreview();
    },

    _paintAvatar() {
        const slot = document.getElementById('pp-av-slot');
        if (!slot) return;
        const label = this._personaLabel();
        slot.innerHTML = this._avatar(label ? { name: label } : null);
    },

    _personaId() {
        const h = document.querySelector('[data-persona-picker]');
        return h && h.dataset.personaId ? parseInt(h.dataset.personaId, 10) : null;
    },
    _personaLabel() {
        const h = document.querySelector('[data-persona-picker]');
        return h ? (h.dataset.personaLabel || '') : '';
    },

    _wireCompose() {
        const body = document.getElementById('post-body');
        // Thread parts (gap-wave-3 §4): text-only parts 2+, stacked on a rail.
        document.getElementById('post-addpart').addEventListener('click', () => this._addPart(''));
        body.addEventListener('input', () => {
            this._syncMentions();
            this._popUpdate();
            this._changed();
        });
        body.addEventListener('keydown', e => this._popKey(e));
        body.addEventListener('click', () => this._popUpdate());
        body.addEventListener('blur', () => setTimeout(() => this._popClose(), 150));
        document.getElementById('pp-tag').addEventListener('click', () => {
            const at = body.selectionStart || body.value.length;
            const pre = body.value.slice(0, at);
            const lead = pre && !/\s$/.test(pre) ? ' @' : '@';
            body.value = pre + lead + body.value.slice(at);
            body.focus();
            body.selectionStart = body.selectionEnd = at + lead.length;
            this._popUpdate();
        });
        document.getElementById('post-rating').addEventListener('change', () => this._changed());
        document.getElementById('post-platforms').addEventListener('change', e => {
            if (e.target.closest('.post-plat-check')) { this._applyCommentDefaults(); this._paintComment(); }
            if (e.target.closest('.post-plat-check, .post-acct-select')) this._changed();
        });
        this._wireComment();

        // Mention panel: a <select> per @alias binds it to a handle-book contact.
        document.getElementById('post-mentions').addEventListener('change', e => {
            const sel = e.target.closest('.post-mention-select');
            if (sel) this._onMentionSelect(sel.dataset.token, sel.value);
        });
        document.getElementById('pp-pop').addEventListener('mousedown', e => {
            const it = e.target.closest('[data-pick]');
            if (!it) return;
            e.preventDefault();   // keep the textarea focused
            this._popPick(it.dataset.pick);
        });

        const fileInput = document.getElementById('post-image');
        fileInput.addEventListener('change', () => {
            if (fileInput.files && fileInput.files.length) this._addFiles(fileInput.files);
            fileInput.value = '';   // let the same file be re-picked / more added
        });
        document.getElementById('post-image-preview').addEventListener('click', (e) => {
            const rm = e.target.closest('.post-thumb-remove');
            if (rm) { this._removeFileAt(parseInt(rm.dataset.idx, 10)); return; }
            const alt = e.target.closest('[data-alt]');
            if (alt) this._editAlt(parseInt(alt.dataset.alt, 10));
        });
        const editor = document.querySelector('.pp-editor');
        editor.addEventListener('dragover', e => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) e.preventDefault(); });
        editor.addEventListener('drop', e => {
            if (!e.dataTransfer || !e.dataTransfer.files.length) return;
            e.preventDefault();
            this._addFiles(e.dataTransfer.files);
        });
        document.getElementById('pp-preview').addEventListener('click', e => {
            const tab = e.target.closest('[data-tab]');
            if (tab) { this._activeTab = tab.dataset.tab; this._paintPreview(); }
        });
        document.getElementById('post-submit').addEventListener('click', () => this._submit());

        // Scheduling: toggle the picker, confirm (create + queue), cancel.
        const schedForm = document.getElementById('post-schedule-form');
        const schedInput = document.getElementById('post-schedule-datetime');
        const toggle = document.getElementById('post-schedule-toggle');
        toggle.addEventListener('click', () => {
            const showing = schedForm.style.display !== 'none';
            schedForm.style.display = showing ? 'none' : '';
            toggle.setAttribute('aria-expanded', String(!showing));
            if (!showing && !schedInput.value) schedInput.value = this._defaultScheduleLocal();
            if (!showing) schedInput.focus();
        });
        document.getElementById('post-schedule-cancel').addEventListener('click', () => {
            schedForm.style.display = 'none';
            toggle.setAttribute('aria-expanded', 'false');
        });
        document.getElementById('post-schedule-confirm').addEventListener('click', () => this._submit(schedInput.value));

        this._syncMentions();
    },

    _addPart(text) {
        const box = document.getElementById('post-parts');
        if (!box) return;
        const n = box.children.length + 2;
        const wrap = document.createElement('div');
        wrap.className = 'pp-part pp-part--more';
        wrap.innerHTML = `<div class="pp-rail">${this._avatar(this._personaLabel() ? { name: this._personaLabel() } : null, 'pp-av--dim')}</div>
            <div class="pp-part-main">
                <label class="sr-only" for="pp-part-${n}">Part ${n}</label>
                <textarea id="pp-part-${n}" class="post-part-text pp-ta pp-ta--part" rows="2"
                    placeholder="Part ${n}: keep going…"></textarea>
                <button type="button" class="pp-part-del" title="Remove this part">✕<span class="sr-only"> Remove part ${n}</span></button>
            </div>`;
        const ta = wrap.querySelector('textarea');
        ta.value = text || '';
        ta.addEventListener('input', () => this._changed());
        wrap.querySelector('.pp-part-del').addEventListener('click', () => {
            wrap.remove();
            this._renumberParts();
            this._changed();
        });
        box.appendChild(wrap);
        if (!text) ta.focus();
        this._changed();
    },

    _renumberParts() {
        document.querySelectorAll('#post-parts .pp-part').forEach((w, i) => {
            const ta = w.querySelector('textarea');
            ta.placeholder = `Part ${i + 2}: keep going…`;
            const lbl = w.querySelector('label');
            if (lbl) lbl.textContent = `Part ${i + 2}`;
        });
    },

    /* datetime-local wants 'YYYY-MM-DDTHH:MM' in LOCAL time; default one hour out. */
    _defaultScheduleLocal() {
        return Utils.time.pickerIn(60);   // in the saved zone
    },

    /* The best window from Analytics → When to post (spec 015), as a one-tap time. */
    async _suggestTime() {
        const el = document.getElementById('pp-best-time');
        if (!el) return;
        let d = null;
        try {
            const r = await fetch('/api/analytics/when-to-post?kind=post&tz=' + encodeURIComponent(Utils.time.zoneName()));
            d = r.ok ? await r.json() : null;
        } catch (e) { d = null; }
        const w = d && (d.windows || [])[0];
        if (!w || !w.next_at || !document.getElementById('pp-best-time')) return;
        el.hidden = false;
        el.innerHTML = `★ Your best time for posts: <button type="button" class="pp-link" id="pp-use-best">${this.esc(Utils.time.fmt.dateTime(w.next_at))}</button>`;
        document.getElementById('pp-use-best').addEventListener('click', () => {
            document.getElementById('post-schedule-datetime').value = Utils.time.toPicker(w.next_at);
        });
    },

    /* ── Draft kept on this device (spec 018: device-local only) ── */

    _changed() {
        this._schedulePreview();
        clearTimeout(this._draftTimer);
        this._draftTimer = setTimeout(() => this._saveDraft(), 400);
    },

    _draftState() {
        const body = document.getElementById('post-body');
        if (!body) return null;
        return {
            body: body.value,
            parts: [...document.querySelectorAll('.post-part-text')].map(t => t.value),
            rating: document.getElementById('post-rating').value,
            platforms: this._selectedPlatforms(),
            bindings: this._mentionBindings,
            comment: this._comment,
        };
    },

    _saveDraft() {
        const d = this._draftState();
        if (!d) return;
        const st = document.getElementById('pp-draft-state');
        if (!d.body.trim() && !d.parts.some(x => x.trim())) {
            this._ls(this._DRAFT_KEY, null);
            if (st) st.textContent = '';
            return;
        }
        this._ls(this._DRAFT_KEY, d);
        if (st) st.textContent = 'Draft saved on this device';
    },

    _restoreDraft() {
        const body = document.getElementById('post-body');
        const pre = this._prefill;
        this._prefill = null;
        const d = pre ? { body: pre.body, rating: pre.rating, parts: [] } : this._ls(this._DRAFT_KEY);
        if (!d || (!d.body && !(d.parts || []).length)) return;
        body.value = d.body || '';
        if (d.rating) document.getElementById('post-rating').value = d.rating;
        (d.parts || []).forEach(t => this._addPart(t));
        if (d.bindings && typeof d.bindings === 'object') this._mentionBindings = { ...d.bindings };
        if (Array.isArray(d.platforms)) this._pendingDraftPlatforms = d.platforms;
        if (d.comment && typeof d.comment === 'object' && d.comment.sites) {
            this._comment = { touched: {}, linked: null, ...d.comment };
            this._paintComment();
        }
        this._syncMentions();
        const st = document.getElementById('pp-draft-state');
        if (st && !pre) {
            st.innerHTML = 'Draft restored · <button type="button" class="pp-link" id="pp-discard">Discard</button>';
            document.getElementById('pp-discard').addEventListener('click', () => {
                this._ls(this._DRAFT_KEY, null);
                this.renderCompose();
            });
        }
        this._schedulePreview();
    },

    /* ── Per-site preview: rings, reasons and the preview pane ── */

    _schedulePreview() {
        clearTimeout(this._previewTimer);
        this._previewTimer = setTimeout(() => this._refreshPreview(), 300);
    },

    async _refreshPreview() {
        const body = document.getElementById('post-body');
        if (!body) return null;
        const seq = (this._previewSeq = (this._previewSeq || 0) + 1);
        const req = {
            body: body.value,
            platforms: this._PLATFORMS,
            mentions: this._collectMentions(),
            image_count: this._pendingFiles.length,
            rating: document.getElementById('post-rating')?.value || 'general',   // the under-18 lock (4.58.0)
            parts: [...document.querySelectorAll('.post-part-text')].map(t => t.value.trim()).filter(Boolean),
            account_ids: this._accountIds(this._PLATFORMS),
            comments: this._commentsPayload(),
            linked: this._comment && this._comment.linked
                ? { kind: this._comment.linked.kind, ref: this._comment.linked.ref } : undefined,
        };
        let r;
        try { r = await API.previewPost(req); } catch (e) { return null; }
        if (seq !== this._previewSeq || !document.getElementById('post-body')) return null;
        this._preview = (r && r.sites) || {};
        this._paintRows();
        this._paintPreview();
        return this._preview;
    },

    _ring(site) {
        if (!site || !site.limit) return '';
        const C = 75.4, frac = Math.min(site.length / site.limit, 1);
        const left = site.limit - site.length;
        const cls = site.over ? 'pp-ring--over' : (left <= Math.max(20, site.limit * 0.1) ? 'pp-ring--near' : '');
        return `<span class="pp-ring-in ${cls}" role="img" aria-label="${site.length} of ${site.limit} characters${site.over ? `, ${-left} over` : ''}">
            <svg width="30" height="30" viewBox="0 0 30 30" aria-hidden="true">
                <circle cx="15" cy="15" r="12" class="pp-ring-track"/>
                <circle cx="15" cy="15" r="12" class="pp-ring-bar" stroke-dasharray="${C}" stroke-dashoffset="${(C * (1 - frac)).toFixed(1)}"/>
            </svg><span aria-hidden="true">${left <= 99 ? left : ''}</span></span>`;
    },

    _paintRows() {
        const sites = this._preview || {};
        let n = 0;
        this._PLATFORMS.forEach(code => {
            const site = sites[code];
            const row = document.querySelector(`.pp-drow[data-platform="${code}"]`);
            const why = document.getElementById(`pp-why-${code}`);
            const ring = document.getElementById(`pp-ring-${code}`);
            const on = !!document.querySelector(`.post-plat-check[value="${code}"]:checked`);
            if (on) n++;
            if (!row || !site) return;
            row.classList.toggle('is-off', !on);
            ring.innerHTML = on ? this._ring(site) : '';
            const w = site.warnings || [];
            const block = w.find(x => x.level === 'block');
            const first = block || w[0];
            if (!site.connected) {
                why.innerHTML = `<span class="pp-bad">Not connected · <a href="${this._settingsHref(code)}">Set up</a></span>`;
            } else if (first) {
                why.innerHTML = `<span class="${first.level === 'block' ? 'pp-bad' : 'pp-warn'}">${this.esc(first.text)}</span>`
                    + (w.length > 1 ? ` <span class="muted">+${w.length - 1} more</span>` : '');
            } else {
                why.innerHTML = '';
            }
            const cwhy = document.getElementById(`pp-cm-why-${code}`);
            if (cwhy) {
                const cm = site.comment;
                const cw = cm && (cm.warnings || []).find(x => x.level === 'block') || (cm && (cm.warnings || [])[0]);
                cwhy.innerHTML = cm ? `${cm.limit ? `<span class="${cm.over ? 'pp-bad' : 'muted'}">${cm.length}/${cm.limit}</span> ` : ''}`
                    + (cw ? `<span class="${cw.level === 'block' ? 'pp-bad' : 'pp-warn'}">${this.esc(cw.text)}</span>` : '') : '';
            }
        });
        const btn = document.getElementById('post-submit');
        if (btn) btn.textContent = n ? `Post now to ${n} site${n === 1 ? '' : 's'}` : 'Post now';
    },

    _paintPreview() {
        const box = document.getElementById('pp-preview');
        if (!box) return;
        const sites = this._preview || {};
        const on = this._selectedPlatforms().filter(c => sites[c]);
        if (!on.length) { box.innerHTML = '<p class="muted">Switch on a site to see how the post will look there.</p>'; return; }
        if (!on.includes(this._activeTab)) this._activeTab = on[0];
        const code = this._activeTab, site = sites[code];
        const tabs = on.map(c => `<button type="button" role="tab" data-tab="${c}" aria-selected="${c === code}">${this._logo(c)}${this.esc(this._label(c))}</button>`).join('');
        const imgs = this._previewUrls.length && !(this._rules || this._RULES_FALLBACK).text_only.includes(code)
            ? `<div class="pp-pv-imgs">${this._previewUrls.map(u => `<img src="${u}" alt="">`).join('')}</div>` : '';
        const notes = (site.warnings || []).map(w =>
            `<li class="${w.level === 'block' ? 'pp-bad' : 'pp-warn'}">${this.esc(w.text)}</li>`).join('');
        const persona = this._personaLabel() || 'You';
        box.innerHTML = `<div class="pp-tabs" role="tablist" aria-label="Preview for">${tabs}</div>
            <div class="pp-pv" role="tabpanel">
                <div class="pp-pv-h">${this._avatar({ name: persona })}<b>${this.esc(persona)}</b><small>now</small></div>
                <div class="pp-pv-txt">${site.text ? this._bodyHtml(site.text) : '<span class="muted">Your text shows here.</span>'}</div>
                ${imgs}
            </div>
            ${site.comment && site.comment.text ? `<div class="pp-pv pp-pv--reply" aria-label="Comment under the post">
                <div class="pp-pv-h">${this._avatar({ name: persona })}<b>${this.esc(persona)}</b><small>replying</small></div>
                <div class="pp-pv-txt">${this._bodyHtml(site.comment.text)}</div></div>` : ''}
            ${notes ? `<ul class="pp-notes">${notes}</ul>` : ''}`;
    },

    /* ── @mentions (handle-book) ─────────────────────────────────
     * You type one alias (@luna); each platform needs that person's OWN handle.
     * Typing @ offers your contacts; a picked contact binds the alias, and the
     * backend expands it per platform at publish (and builds Bluesky's mention
     * facet). Unbound aliases stay plain text. */

    _mentionTokens(text) {
        const out = [], seen = new Set();
        const re = /@(\w+)/g;
        let m;
        while ((m = re.exec(text || ''))) {
            if (!seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
        }
        return out;
    },

    _tagOf(c) { return ((c.alias || c.name || '').replace(/[^\w]/g, '')) || ''; },

    _contactFor(token) {
        const t = String(token || '').toLowerCase();
        return this._contacts.find(x => (x.alias || '').toLowerCase() === t)
            || this._contacts.find(x => (x.name || '').toLowerCase() === t);
    },

    _syncMentions() {
        const body = document.getElementById('post-body');
        const panel = document.getElementById('post-mentions');
        if (!body || !panel) return;
        const tokens = this._mentionTokens(body.value);
        Object.keys(this._mentionBindings).forEach(t => {
            if (!tokens.includes(t)) delete this._mentionBindings[t];
        });
        // The word being typed at the caret belongs to the @ popover, not this panel yet.
        const typing = this._popQuery();
        const shown = tokens.filter(t => t !== typing);
        if (!shown.length) { panel.hidden = true; panel.innerHTML = ''; return; }
        // First time we see an alias, auto-bind it to the contact that answers to it.
        tokens.forEach(t => {
            if (this._mentionBindings[t] === undefined) {
                const c = this._contactFor(t);
                if (c) this._mentionBindings[t] = c.id;
            }
        });
        panel.hidden = false;
        panel.innerHTML = `
            <div class="post-mentions-head">Tagged <span class="muted">— who each @ is, so every site gets their right handle.</span></div>
            ${shown.map(t => this._mentionRow(t)).join('')}`;
    },

    _mentionRow(token) {
        const bound = this._mentionBindings[token];
        const opts = [`<option value="">— don't tag —</option>`]
            .concat(this._contacts.map(c =>
                `<option value="${c.id}"${String(bound) === String(c.id) ? ' selected' : ''}>${this.esc(c.name)}</option>`))
            .concat(`<option value="__new">＋ Add new contact…</option>`)
            .join('');
        const c = this._contacts.find(x => String(x.id) === String(bound));
        return `
            <div class="post-mention-row">
                <span class="post-mention-alias">@${this.esc(token)}</span>
                <select class="post-mention-select" data-token="${this.esc(token)}" aria-label="Who is @${this.esc(token)}?">${opts}</select>
                <span class="post-mention-hint muted">${c ? this._contactHint(c) : ''}</span>
            </div>`;
    },

    _contactHint(c) {
        return this._MENTION_FIELDS
            .filter(f => (c[f.key] || '').trim())
            .map(f => `${this.esc(f.label)} @${this.esc(c[f.key])}`)
            .join(' · ');
    },

    _contactSites(c) {
        const have = this._MENTION_FIELDS.filter(f => (c[f.key] || '').trim()).map(f => f.label);
        return have.length ? have.join(' · ') : 'no handles yet';
    },

    _onMentionSelect(token, value) {
        if (value === '__new') { this._openContactForm(token); return; }
        if (value) this._mentionBindings[token] = parseInt(value, 10);
        else delete this._mentionBindings[token];
        this._syncMentions();
        this._changed();
    },

    /* The @ popover: the word being typed at the caret, matched against contacts. */
    _popQuery() {
        const body = document.getElementById('post-body');
        if (!body || body.selectionStart !== body.selectionEnd) return null;
        const pre = body.value.slice(0, body.selectionStart);
        const m = /(?:^|[^\w@.])@(\w*)$/.exec(pre);
        return m ? m[1] : null;
    },

    _popUpdate() {
        const pop = document.getElementById('pp-pop');
        const body = document.getElementById('post-body');
        if (!pop || !body) return;
        const q = this._popQuery();
        if (q === null) { this._popClose(); return; }
        const ql = q.toLowerCase();
        const hits = this._contacts.filter(c => !ql
            || (c.name || '').toLowerCase().includes(ql) || (c.alias || '').toLowerCase().startsWith(ql)).slice(0, 6);
        const exact = hits.some(c => this._tagOf(c).toLowerCase() === ql);
        this._popItems = hits.map(c => ({ kind: 'contact', id: c.id }));
        if (q && !exact) this._popItems.push({ kind: 'new', token: q });
        if (!this._popItems.length) {
            pop.hidden = false;
            pop.innerHTML = '<div class="pp-pop-note muted">No tag contacts yet. Type a name to add one.</div>';
            body.setAttribute('aria-expanded', 'true');
            return;
        }
        this._popActive = Math.min(this._popActive || 0, this._popItems.length - 1);
        pop.innerHTML = this._popItems.map((it, i) => {
            const id = `pp-opt-${i}`;
            if (it.kind === 'new') {
                return `<div class="pp-pop-it pp-pop-new" role="option" id="${id}" data-pick="${i}" aria-selected="${i === this._popActive}">＋ New contact “${this.esc(it.token)}”…</div>`;
            }
            const c = this._contacts.find(x => x.id === it.id);
            return `<div class="pp-pop-it" role="option" id="${id}" data-pick="${i}" aria-selected="${i === this._popActive}">
                ${this._avatar({ name: c.name })}<div>${this.esc(c.name)}${c.alias ? ` <span class="muted">@${this.esc(c.alias)}</span>` : ''}
                <small>${this.esc(this._contactSites(c))}</small></div></div>`;
        }).join('');
        pop.hidden = false;
        body.setAttribute('aria-expanded', 'true');
        body.setAttribute('aria-activedescendant', `pp-opt-${this._popActive}`);
    },

    _popClose() {
        const pop = document.getElementById('pp-pop');
        const body = document.getElementById('post-body');
        if (pop) { pop.hidden = true; pop.innerHTML = ''; }
        if (body) { body.setAttribute('aria-expanded', 'false'); body.removeAttribute('aria-activedescendant'); }
        this._popItems = [];
        this._popActive = 0;
    },

    _popKey(e) {
        const pop = document.getElementById('pp-pop');
        if (!pop || pop.hidden || !(this._popItems || []).length) return;
        const n = this._popItems.length;
        if (e.key === 'ArrowDown') { e.preventDefault(); this._popActive = (this._popActive + 1) % n; this._popUpdate(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); this._popActive = (this._popActive - 1 + n) % n; this._popUpdate(); }
        else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); this._popPick(this._popActive); }
        else if (e.key === 'Escape') { e.preventDefault(); this._popClose(); }
    },

    _popPick(i) {
        const it = (this._popItems || [])[Number(i)];
        const body = document.getElementById('post-body');
        if (!it || !body) return;
        const q = this._popQuery() || '';
        if (it.kind === 'new') { this._popClose(); this._openContactForm(it.token); return; }
        const c = this._contacts.find(x => x.id === it.id);
        const tag = this._tagOf(c);
        const at = body.selectionStart;
        const start = at - q.length;
        body.value = body.value.slice(0, start) + tag + ' ' + body.value.slice(at);
        body.selectionStart = body.selectionEnd = start + tag.length + 1;
        this._mentionBindings[tag] = c.id;
        this._popClose();
        this._syncMentions();
        this._changed();
        body.focus();
    },

    _openContactForm(token) {
        this._addForToken = token;
        const form = document.getElementById('post-contact-form');
        if (!form) return;
        const rows = this._MENTION_FIELDS.map(f =>
            `<label class="post-cf-field">${this.esc(f.label)}
                <input type="text" class="post-cf-input" data-key="${f.key}" placeholder="${this.esc(f.ph)}">
            </label>`).join('');
        form.hidden = false;
        form.innerHTML = `
            <div class="post-cf-head">New contact for <strong>@${this.esc(token || '')}</strong>
                <span class="muted">— paste each site's handle (leave blank to skip that one).</span></div>
            <label class="post-cf-field">Name
                <input type="text" class="post-cf-input" data-key="name" value="${this.esc(token || '')}" placeholder="who is this?">
            </label>
            <label class="post-cf-field">Tag as @
                <input type="text" class="post-cf-input" data-key="alias" value="${this.esc(token || '')}" placeholder="short tag">
            </label>
            ${rows}
            <div class="post-cf-actions">
                <button type="button" class="btn btn-sm btn-primary" id="post-cf-save">Save contact</button>
                <button type="button" class="btn btn-sm" id="post-cf-cancel">Cancel</button>
                <span class="muted post-cf-msg" id="post-cf-msg" role="status"></span>
            </div>`;
        form.querySelector('#post-cf-save').addEventListener('click', () => this._saveContact());
        form.querySelector('#post-cf-cancel').addEventListener('click', () => this._closeContactForm());
        const nameInput = form.querySelector('.post-cf-input[data-key="name"]');
        if (nameInput) { nameInput.focus(); nameInput.select(); }
    },

    async _saveContact() {
        const form = document.getElementById('post-contact-form');
        const msg = form.querySelector('#post-cf-msg');
        const payload = {};
        form.querySelectorAll('.post-cf-input').forEach(inp => { payload[inp.dataset.key] = inp.value.trim(); });
        if (!payload.name) { msg.textContent = 'Give the contact a name.'; return; }
        const save = form.querySelector('#post-cf-save');
        save.disabled = true; msg.textContent = 'Saving…';
        try {
            const r = await API.createContact(payload);
            const contact = r && r.contact;
            if (contact) {
                this._contacts.push(contact);
                if (this._addForToken) this._mentionBindings[this._addForToken] = contact.id;
            }
            this._closeContactForm();
            this._toast('success', 'Contact saved');
            this._changed();
        } catch (err) {
            save.disabled = false;
            msg.textContent = 'Not saved: ' + this._errText(err);
        }
    },

    _closeContactForm() {
        const form = document.getElementById('post-contact-form');
        if (form) { form.hidden = true; form.innerHTML = ''; }
        this._addForToken = null;
        this._syncMentions();
    },

    _collectMentions() {
        const body = document.getElementById('post-body');
        return this._mentionTokens(body ? body.value : '')
            .filter(t => this._mentionBindings[t])
            .map(t => ({ token: t, contact_id: this._mentionBindings[t] }));
    },

    /* ── Images ── */

    _addFiles(fileList) {
        for (const file of Array.from(fileList)) {
            if (this._pendingFiles.length >= this._MAX_IMAGES) {
                this._toast('error', `Up to ${this._MAX_IMAGES} images per post.`);
                break;
            }
            if (!/\.(png|jpe?g|gif|webp)$/i.test(file.name)) {
                this._toast('error', 'Please choose PNG, JPG, GIF or WebP images.');
                continue;
            }
            this._pendingFiles.push(file);
            this._previewUrls.push(URL.createObjectURL(file));
            this._altTexts.push('');
        }
        this._renderPreviews();
        this._changed();
    },

    _renderPreviews() {
        const box = document.getElementById('post-image-preview');
        if (!box) return;
        const n = this._pendingFiles.length;
        const thumbs = this._previewUrls.map((url, i) => {
            const has = !!(this._altTexts[i] || '').trim();
            return `<figure class="post-thumb pp-thumb-big">
                <img src="${url}" alt="${this.esc(this._altTexts[i] || `Image ${i + 1}`)}">
                <button type="button" class="post-thumb-remove" data-idx="${i}" title="Remove image" aria-label="Remove image ${i + 1}">✕</button>
                <button type="button" class="pp-alt-btn${has ? ' is-set' : ''}" data-alt="${i}"
                    aria-label="${has ? 'Edit' : 'Add'} the description of image ${i + 1}">${has ? 'ALT ✓' : '+ ALT'}</button>
            </figure>`;
        }).join('');
        const add = n < this._MAX_IMAGES
            ? `<label class="pp-add-img" for="post-image">＋ Image<br><span class="muted">${n} of ${this._MAX_IMAGES}</span></label>` : '';
        box.innerHTML = n ? thumbs + add : '';
        box.hidden = !n;
    },

    _editAlt(i) {
        const box = document.getElementById('pp-alt-edit');
        if (!box || i < 0 || i >= this._pendingFiles.length) return;
        box.hidden = false;
        box.innerHTML = `<label for="pp-alt-input">Describe image ${i + 1} for people who can't see it</label>
            <textarea id="pp-alt-input" rows="2" maxlength="1500"></textarea>
            <div class="pp-row"><button type="button" class="btn btn-sm btn-primary" id="pp-alt-done">Done</button></div>`;
        const ta = box.querySelector('textarea');
        ta.value = this._altTexts[i] || '';
        ta.focus();
        box.querySelector('#pp-alt-done').addEventListener('click', () => {
            this._altTexts[i] = ta.value.trim();
            box.hidden = true;
            box.innerHTML = '';
            this._renderPreviews();
            const btn = document.querySelector(`[data-alt="${i}"]`);
            if (btn) btn.focus();
        });
    },

    _removeFileAt(i) {
        if (i < 0 || i >= this._pendingFiles.length) return;
        URL.revokeObjectURL(this._previewUrls[i]);
        this._pendingFiles.splice(i, 1);
        this._previewUrls.splice(i, 1);
        this._altTexts.splice(i, 1);
        this._renderPreviews();
        this._changed();
    },

    _clearFiles() {
        this._previewUrls.forEach(u => URL.revokeObjectURL(u));
        this._pendingFiles = [];
        this._previewUrls = [];
        this._altTexts = [];
        const fi = document.getElementById('post-image');
        if (fi) fi.value = '';
        this._renderPreviews();
    },

    _selectedPlatforms() {
        return Array.from(document.querySelectorAll('.post-plat-check:checked')).map(c => c.value);
    },

    _accountIds(platforms) {
        const ids = {};
        document.querySelectorAll('.post-acct-select').forEach(sel => {
            if (platforms.includes(sel.dataset.platform) && sel.value) ids[sel.dataset.platform] = parseInt(sel.value, 10);
        });
        return ids;
    },

    /* Compose + publish. Pass a datetime-local string (from the Schedule picker)
     * to queue it for later instead of posting now. */
    async _submit(scheduledLocal) {
        const msg = document.getElementById('post-msg');
        const body = document.getElementById('post-body').value.trim();
        const rating = document.getElementById('post-rating').value;
        const platforms = this._selectedPlatforms();

        if (!body && !this._pendingFiles.length) { msg.textContent = 'Write something or attach an image.'; return; }
        if (!platforms.length) { msg.textContent = 'Switch on at least one site.'; return; }

        // When scheduling, validate the time before we create anything.
        let scheduledIso = null;
        if (scheduledLocal) {
            const when = Utils.time.toUtc(scheduledLocal);   // the picker is in the saved zone
            if (!when) { msg.textContent = 'Invalid date/time.'; return; }
            if (when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }
            scheduledIso = when.toISOString();
        }

        // Every reason a site would refuse the post, checked on the current text (SC-002).
        const sites = (await this._refreshPreview()) || this._preview || {};
        const blocked = platforms.map(c => {
            const b = ((sites[c] || {}).warnings || []).find(w => w.level === 'block');
            const cb = (((sites[c] || {}).comment || {}).warnings || []).find(w => w.level === 'block');
            return b ? `${this._label(c)}: ${b.text}` : cb ? `${this._label(c)} comment: ${cb.text}` : '';
        }).filter(Boolean);
        if (blocked.length) {
            msg.textContent = `Fix these or switch the site off first. ${blocked.join(' · ')}`;
            return;
        }

        const btn = document.getElementById('post-submit');
        btn.disabled = true;
        msg.textContent = scheduledIso ? 'Scheduling…' : 'Posting…';

        try {
            const fd = new FormData();
            fd.append('body', body);
            fd.append('rating', rating);
            const mentions = this._collectMentions();
            if (mentions.length) fd.append('mentions', JSON.stringify(mentions));
            const partTexts = Array.from(document.querySelectorAll('.post-part-text'))
                .map(t => t.value.trim()).filter(Boolean);
            if (partTexts.length) fd.append('parts', JSON.stringify(partTexts));
            if (this._altTexts.some(a => (a || '').trim())) fd.append('alts', JSON.stringify(this._altTexts));
            this._pendingFiles.forEach(f => fd.append('files', f));
            const comments = this._commentsPayload();
            if (comments) fd.append('comments', JSON.stringify(comments));
            if (comments && this._comment.linked) {
                fd.append('linked', JSON.stringify({ kind: this._comment.linked.kind, ref: this._comment.linked.ref }));
            }
            // Before createPost: that writes a post row, and a cancel after it
            // would leave a stray draft in the feed. Schedules skip the dialog.
            if (!scheduledIso && !(await Components.confirmPublish({
                title: body.slice(0, 90) || '(image only)',
                subtitle: this._pendingFiles.length ? `Post · ${this._pendingFiles.length} image${this._pendingFiles.length === 1 ? '' : 's'}` : 'Post',
                persona: this._personaLabel(),
                targets: platforms.map(code => {
                    const p = this._plat(code);
                    const sel = document.querySelector(`.post-acct-select[data-platform="${code}"]`);
                    return { code, label: this._label(code), emoji: p.emoji,
                             account: sel ? (sel.dataset.accountLabel || ((sel.options && sel.options[sel.selectedIndex]) || {}).text || '') : '' };
                }),
            }))) { msg.textContent = 'Not posted.'; return; }   // finally{} re-enables the button
            const { post_id } = await API.createPost(fd);

            let fail = 0;
            if (scheduledIso) {
                await API.schedulePost(post_id, {
                    platforms, account_ids: this._accountIds(platforms), scheduled_at: scheduledIso,
                    persona_id: this._personaId(),
                });
                this._toast('success', `Scheduled for ${Utils.time.fmt.dateTime(scheduledIso)}`);
                msg.textContent = '';
                const sf = document.getElementById('post-schedule-form');
                if (sf) sf.style.display = 'none';
            } else {
                // spec 017: background job + live grid (null = minimised / left; the toast reports).
                const res = await this._send(post_id, {
                    platforms, account_ids: this._accountIds(platforms), persona_id: this._personaId() }, msg);
                this._ls(this._DRAFT_KEY, null);
                if (!res) return;
                const ok = res.successes || 0; fail = res.failures || 0;
                this._toast(fail ? 'error' : 'success', `Posted: ${ok} ok, ${fail} failed`);
                if (fail) {
                    const errs = (res.results || []).filter(r => !r.success)
                        .map(r => `${this._label(r.platform)}: ${r.error}`).join(' · ');
                    msg.textContent = errs;
                } else {
                    msg.textContent = '';
                }
            }
            // Reset the composer, keep the site switches.
            this._ls(this._DRAFT_KEY, null);
            document.getElementById('post-body').value = '';
            document.getElementById('post-parts').innerHTML = '';
            const st = document.getElementById('pp-draft-state');
            if (st) st.textContent = '';
            this._mentionBindings = {};
            this._comment = { open: false, shared: '', sites: {}, touched: {}, linked: null };
            this._applyCommentDefaults();
            this._paintComment();
            this._closeContactForm();
            this._syncMentions();
            this._clearFiles();
            this._schedulePreview();
            // A clean success (or any schedule) goes to the feed so the post is visible;
            // a partial failure stays put so the failed sites can be retried from here.
            if (!fail) window.location.hash = '#/posts';
        } catch (err) {
            msg.textContent = 'Failed: ' + this._errText(err);
        } finally {
            btn.disabled = false;
        }
    },

    /* ══ Journals (spec 027) — #/posts/journal (new) and #/posts/journal/<id> (edit) ══════
     * A journal is a post with a title, for FurAffinity, Weasyl and DeviantArt. It shares the
     * Posts feed, scheduling, results and retry; this page is its composer. */

    _JOURNAL_SITES: ['ws', 'da'],          // posted for you; FurAffinity is copied (its journal form needs a CAPTCHA)
    _JOURNAL_LIMITS: { fa: 60, ws: 100, da: 50 },

    async renderJournal(editId) {
        const _rt = App._routeToken();
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
        let post = null;
        if (editId) {
            try { post = await API.get(`/api/posts/${encodeURIComponent(editId)}`); } catch (err) { post = null; }
            if (App._stale(_rt)) return;
            if (!post || post.kind !== 'journal') {
                app.innerHTML = '<div class="card error">That journal wasn\'t found.</div>';
                return;
            }
        }
        const esc = (s) => this.esc(s);
        const posted = post ? (post.publications || []).filter(p => p.status === 'posted') : [];
        const rows = this._JOURNAL_SITES.map(code => `
            <div class="pp-drow post-plat" data-platform="${code}">
                <span class="pp-drow-logo">${this._logo(code)}</span>
                <div class="pp-drow-main">
                    <div class="pp-drow-name">${esc(this._label(code))} <span class="muted">· title up to ${this._JOURNAL_LIMITS[code]} characters</span></div>
                    <span class="post-acct-slot" data-platform="${code}"></span>
                    <div class="pp-drow-why" id="pj-why-${code}"></div>
                </div>
                <label class="pp-switch">
                    <input type="checkbox" class="post-plat-check" value="${code}" role="switch" checked>
                    <span class="pp-switch-ui" aria-hidden="true"></span>
                    <span class="sr-only">Post to ${esc(this._label(code))}</span>
                </label>
            </div>`).join('');
        const where = posted.length
            ? `<p class="muted">Posted on ${posted.map(p => esc(this._label(p.platform))).join(', ')}. Saving updates it there.</p>`
            : '';
        app.innerHTML = `
            <div class="pp-page pj-page">
                <div class="page-header pp-head">
                    <div>
                        <h1>${post ? 'Edit journal' : 'New journal'}</h1>
                        <p class="muted">A titled journal written once: posted to Weasyl and DeviantArt, and ready to paste on FurAffinity.</p>
                    </div>
                    <div class="pp-head-actions"><a class="btn" href="#/posts">← Posts</a></div>
                </div>
                <div class="card pj-form">
                    ${post ? where : '<div class="persona-picker" data-persona-picker hidden></div>'}
                    <label class="pj-field">Title
                        <input type="text" id="pj-title" class="search-input" maxlength="200" value="${esc(post ? post.title : '')}">
                    </label>
                    <div class="muted pj-count" id="pj-count" aria-live="polite"></div>
                    <label class="pj-field">Text
                        <textarea id="pj-body" class="pp-ta" rows="12">${esc(post ? post.body : '')}</textarea>
                    </label>
                    <p class="muted pj-hint">**bold**, *italic*, [link text](https://…), a line starting "- " for a list, "# " for a heading. Each site gets its own formatting.</p>
                    <label class="pj-field">Tags <span class="muted">(Weasyl and DeviantArt; FurAffinity journals have none)</span>
                        <input type="text" id="pj-tags" class="search-input" value="${esc(post ? post.tags : '')}" placeholder="commissions news">
                    </label>
                    <label class="pj-field">Rating
                        <select id="pj-rating">${['general', 'mature', 'adult'].map(r =>
                            `<option value="${r}"${(post ? post.rating : 'general') === r ? ' selected' : ''}>${r[0].toUpperCase() + r.slice(1)}</option>`).join('')}</select>
                    </label>
                    <div class="pj-copy">
                        <button type="button" class="btn" id="pj-copy-fa">${this._hasFaWindow() ? '🦊 Open FurAffinity, filled in' : '📋 Copy for FurAffinity'}</button>
                        <span class="muted">${this._hasFaWindow()
                            ? 'FurAffinity asks for a CAPTCHA on journals, so PawPoller can\'t post them. This opens FA\'s journal page with everything filled in: tick the CAPTCHA and press Post.'
                            : 'FurAffinity asks for a CAPTCHA on journals, so PawPoller can\'t post them. This copies the FA version and opens FA\'s journal page: paste it there. (In the desktop app it fills the page in for you.)'}</span>
                    </div>
                    ${post ? '' : `<div class="pj-sites" id="post-platforms">${rows}</div>`}
                    <div class="pj-actions">
                        ${post ? `<button type="button" class="btn btn-primary" id="pj-save">Save and update the sites</button>
                                  <button type="button" class="btn btn-ghost" id="pj-remove">Remove from sites…</button>`
                               : `<button type="button" class="btn btn-primary" id="pj-post">Post now</button>
                                  <input type="datetime-local" id="pj-when" aria-label="Schedule for">
                                  <button type="button" class="btn" id="pj-schedule">Schedule</button>`}
                    </div>
                    <div id="pj-msg" class="pj-msg" aria-live="polite"></div>
                </div>
            </div>`;
        const count = () => {
            const n = document.getElementById('pj-title').value.length;
            const over = Object.keys(this._JOURNAL_LIMITS).filter(c => n > this._JOURNAL_LIMITS[c]).map(c => this._label(c));
            document.getElementById('pj-count').textContent = over.length ? `${n} characters — too long for ${over.join(', ')}` : `${n} characters`;
        };
        document.getElementById('pj-title').addEventListener('input', count);
        count();
        this._journalPostId = post ? post.post_id : null;
        document.getElementById('pj-copy-fa').addEventListener('click', () => this._copyForFa());
        if (post) {
            document.getElementById('pj-save').addEventListener('click', () => this._saveJournal(post));
            document.getElementById('pj-remove').addEventListener('click', () => this._removeJournal(post, posted));
            return;
        }
        await Components.personaPicker({
            host: document.querySelector('[data-persona-picker]') || document.createElement('div'),
            platforms: this._JOURNAL_SITES,
            slot: code => document.querySelector(`.post-acct-slot[data-platform="${code}"]`),
            row: code => document.querySelector(`.post-plat[data-platform="${code}"]`),
            selectClass: 'post-acct-select',
            storageKey: 'pp-persona-journals',
            onChange: () => {},
        });
        document.getElementById('pj-post').addEventListener('click', () => this._submitJournal(null));
        document.getElementById('pj-schedule').addEventListener('click', () => {
            const v = document.getElementById('pj-when').value;
            if (!v) { document.getElementById('pj-msg').textContent = 'Pick a date and time first.'; return; }
            this._submitJournal(v);
        });
    },

    _journalFields() {
        return {
            title: document.getElementById('pj-title').value.trim(),
            body: document.getElementById('pj-body').value.trim(),
            tags: document.getElementById('pj-tags').value.trim(),
            rating: document.getElementById('pj-rating').value,
        };
    },

    async _submitJournal(scheduledLocal) {
        const msg = document.getElementById('pj-msg');
        const f = this._journalFields();
        const platforms = this._selectedPlatforms();
        if (!f.title || !f.body) { msg.textContent = 'A journal needs a title and some text.'; return; }
        if (!platforms.length) { msg.textContent = 'Switch on at least one site.'; return; }
        let scheduledIso = null;
        if (scheduledLocal) {
            const when = Utils.time.toUtc(scheduledLocal);
            if (!when || when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }
            scheduledIso = when.toISOString();
        }
        const account_ids = this._accountIds(platforms);
        let sites = {};
        try {
            sites = (await API.previewPost({ kind: 'journal', title: f.title, platforms, account_ids })).sites || {};
        } catch (err) { sites = {}; }
        this._JOURNAL_SITES.forEach(c => {
            const el = document.getElementById(`pj-why-${c}`);
            if (el) el.textContent = ((sites[c] || {}).warnings || []).map(w => w.text).join(' · ');
        });
        const blocked = platforms.map(c => {
            const b = ((sites[c] || {}).warnings || []).find(w => w.level === 'block');
            return b ? `${this._label(c)}: ${b.text}` : '';
        }).filter(Boolean);
        if (blocked.length) { msg.textContent = `Fix these or switch the site off first. ${blocked.join(' · ')}`; return; }
        if (!scheduledIso && !(await Components.confirmPublish({
            title: f.title, subtitle: 'Journal', noun: 'sites',
            targets: platforms.map(code => ({ code, label: this._label(code), emoji: this._plat(code).emoji })),
        }))) { msg.textContent = 'Not posted.'; return; }
        const fd = new FormData();
        fd.append('kind', 'journal');
        fd.append('title', f.title);
        fd.append('body', f.body);
        fd.append('tags', f.tags);
        fd.append('rating', f.rating);
        try {
            if (this._journalPostId) {         // saved earlier by the FA button: bring it up to date first
                await API.patch(`/api/posts/${this._journalPostId}`, { title: f.title, body: f.body, tags: f.tags, rating: f.rating });
            }
            const post_id = this._journalPostId || (await API.createPost(fd)).post_id;
            this._journalPostId = post_id;      // the FA window (desktop) records onto the same journal
            if (scheduledIso) {
                await API.schedulePost(post_id, { platforms, account_ids, scheduled_at: scheduledIso });
                this._toast('success', `Scheduled for ${Utils.time.fmt.dateTime(scheduledIso)}`);
                window.location.hash = '#/posts';
                return;
            }
            msg.textContent = 'Posting…';
            const res = await this._send(post_id, { platforms, account_ids }, msg);
            if (!res) return;
            Components.showPublishResults(msg, res.results);
            if (!res.failures) this._toast('success', 'Journal posted');
        } catch (err) {
            msg.textContent = 'Failed: ' + this._errText(err);
        }
    },

    _hasFaWindow() {
        return !!(window.pywebview && window.pywebview.api && window.pywebview.api.fa_journal);
    },

    async _copyForFa() {
        const msg = document.getElementById('pj-msg');
        const f = this._journalFields();
        if (!f.body) { msg.textContent = 'Write the journal first.'; return; }
        try {
            const r = await API.post('/api/posts/journal-copy', { site: 'fa', title: f.title, body: f.body, rating: f.rating });
            if (this._hasFaWindow()) {
                // Spec 027: the desktop fills FA's page in; the person does the CAPTCHA. A saved journal row lets
                // PawPoller record the FA link when FA lands on it.
                if (!this._journalPostId && f.title) {
                    const fd = new FormData();
                    fd.append('kind', 'journal'); fd.append('title', f.title); fd.append('body', f.body);
                    fd.append('tags', f.tags); fd.append('rating', f.rating);
                    this._journalPostId = (await API.createPost(fd)).post_id;
                }
                const out = await window.pywebview.api.fa_journal(this._journalPostId, r.title, r.text, r.rating);
                msg.textContent = out && out.ok
                    ? 'FurAffinity is open with your journal filled in. Tick the CAPTCHA and press Post there; PawPoller notes the link when it\'s up.'
                    : 'Couldn\'t open it: ' + ((out && out.message) || 'unknown reason');
                return;
            }
            await navigator.clipboard.writeText(r.text);
            window.open(r.open_url, '_blank', 'noopener');
            msg.textContent = `Copied the FurAffinity text. On FA: paste it into the message box, use the title "${r.title}"`
                + `${r.title_cut ? ' (cut to FA\'s 60 characters)' : ''}, pick the rating and pass the CAPTCHA.`;
        } catch (err) {
            msg.textContent = 'Copy failed: ' + this._errText(err);
        }
    },

    async _saveJournal(post) {
        const msg = document.getElementById('pj-msg');
        const f = this._journalFields();
        if (!f.title || !f.body) { msg.textContent = 'A journal needs a title and some text.'; return; }
        msg.textContent = 'Saving and updating the sites…';
        try {
            const res = await API.patch(`/api/posts/${post.post_id}`, f);
            if ((res.results || []).length) Components.showPublishResults(msg, res.results);
            else msg.textContent = 'Saved. It isn\'t posted anywhere yet.';
        } catch (err) {
            msg.textContent = 'Failed: ' + this._errText(err);
        }
    },

    async _removeJournal(post, posted) {
        const msg = document.getElementById('pj-msg');
        if (!posted.length) { msg.textContent = 'It isn\'t posted anywhere.'; return; }
        const names = posted.map(p => this._label(p.platform)).join(', ');
        if (!window.confirm(`Remove "${post.title}" from ${names}? PawPoller removes it from Weasyl; on FurAffinity and DeviantArt it tells you to remove it there.`)) return;
        try {
            const res = await API.post(`/api/posts/${post.post_id}/remove-from-sites`, { confirm: true });
            const lines = (res.results || []).map(r => `${this._label(r.platform)}: ${r.success ? 'removed' : r.error}`);
            msg.textContent = lines.join(' · ');
        } catch (err) {
            msg.textContent = 'Failed: ' + this._errText(err);
        }
    },

    /* ══ Tag contacts (handle-book manager) — #/posts/contacts ══════ */

    _pcFilter: { q: '', missing: false },

    async renderContacts() {
        const _rt = App._routeToken();   // route race guard (App._stale)
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
        this._pcFilter = { q: '', missing: false };
        app.innerHTML = `
            <div class="pp-page">
                <div class="page-header pp-head">
                    <div>
                        <h1>Tag contacts</h1>
                        <p class="muted"><a href="#/posts">← Posts</a></p>
                    </div>
                    <div class="pp-head-actions"><button type="button" class="btn btn-primary" id="pc-add">＋ New contact</button></div>
                </div>
                <div id="pc-body">${Utils.skeleton('cards', 3, 'your contacts')}</div>
            </div>`;
        document.getElementById('pc-add').addEventListener('click', () => this._openManagerForm(null));
        document.getElementById('pc-body').addEventListener('click', e => this._onContactsClick(e));
        await this._loadContactList();
    },

    async _loadContactList() {
        const host = document.getElementById('pc-body');
        if (!host) return;
        let contacts = [], suggestions = [];
        try {
            const [d, s] = await Promise.all([API.getContacts(), API.suggestContacts().catch(() => null)]);
            contacts = (d && d.contacts) || [];
            suggestions = (s && s.suggestions) || [];
        } catch (err) {
            host.innerHTML = `<div class="card error">Failed to load contacts: ${this.esc(err.message)}</div>`;
            return;
        }
        this._contacts = contacts;
        this._suggestions = suggestions;
        this._paintContacts();
    },

    _missing(c) { return this._MENTION_FIELDS.filter(f => !(c[f.key] || '').trim()).length; },

    _suggestChips() {
        if (!(this._suggestions || []).length) return '';
        return `<div class="pc-suggest"><h2 class="pc-suggest-h">Found in your posts · pick one to start a contact</h2>
            <div class="pp-row">${this._suggestions.map(s =>
                `<button type="button" class="pp-chip" data-suggest="${this.esc(s.name)}">@${this.esc(s.name)} <span class="pp-n">· ${s.count} post${s.count === 1 ? '' : 's'}</span></button>`).join('')}</div></div>`;
    },

    _paintContacts() {
        const host = document.getElementById('pc-body');
        if (!host) return;
        const contacts = this._contacts || [];
        if (!contacts.length) {
            host.innerHTML = `
                <div class="pc-empty">
                    <div>
                        <h2 class="pc-empty-h">Tag a person once, right on every site</h2>
                        <p class="muted">People have different handles on Bluesky, X and Mastodon. Save them here, then type
                        one short @name when you post. Each site gets that person's real handle.</p>
                        <ol class="pc-steps">
                            <li>Add a contact: a short name plus each site's handle.</li>
                            <li>Type <b class="pp-m">@name</b> in a new post.</li>
                            <li>PawPoller swaps in the right handle for each site.</li>
                        </ol>
                        <button type="button" class="btn btn-primary" data-new-contact>＋ Add your first contact</button>
                        ${this._suggestChips()}
                    </div>
                    <div class="pc-how" aria-label="Example">
                        <div class="pc-how-l">You type</div>
                        <div class="pc-how-src">Lines by <span class="pp-m">@inkwolf</span> 🧡</div>
                        <div class="pc-how-l">Each site gets</div>
                        <div class="pc-how-out">${this._logo('bsky')}<span>Bluesky</span><code>@inkwolf.bsky.social</code></div>
                        <div class="pc-how-out">${this._logo('tw')}<span>X</span><code>@InkwolfArt</code></div>
                        <div class="pc-how-out">${this._logo('mast')}<span>Mastodon</span><code>@inkwolf@example.social</code></div>
                        <div class="pc-how-out">${this._logo('tum')}<span>Tumblr</span><span class="muted">no handle saved: plain “@inkwolf”</span></div>
                    </div>
                </div>
                <div id="pc-form-slot"></div>`;
            return;
        }
        const missing = contacts.filter(c => this._missing(c)).length;
        host.innerHTML = `
            <div class="pp-bar">
                <label class="sr-only" for="pc-q">Find a contact</label>
                <input type="search" id="pc-q" class="pp-search" placeholder="Find a contact…" value="${this.esc(this._pcFilter.q)}" autocomplete="off">
                <span class="pp-sp"></span>
                <div class="pp-seg" role="group" aria-label="Show">
                    <button type="button" data-pc-missing="0" aria-pressed="${!this._pcFilter.missing}">All ${contacts.length}</button>
                    <button type="button" data-pc-missing="1" aria-pressed="${this._pcFilter.missing}">Missing a handle${missing ? ` <b class="pp-n-warn">${missing}</b>` : ''}</button>
                </div>
            </div>
            ${this._suggestChips()}
            <div class="pc-layout">
                <div class="pc-grid" id="pc-grid"></div>
                <div id="pc-form-slot"></div>
            </div>`;
        document.getElementById('pc-q').addEventListener('input', e => {
            this._pcFilter.q = e.target.value.trim().toLowerCase();
            this._paintContactGrid();
        });
        this._paintContactGrid();
    },

    _paintContactGrid() {
        const grid = document.getElementById('pc-grid');
        if (!grid) return;
        const f = this._pcFilter;
        const list = (this._contacts || []).filter(c => (!f.missing || this._missing(c))
            && (!f.q || (c.name || '').toLowerCase().includes(f.q) || (c.alias || '').toLowerCase().includes(f.q)
                || this._MENTION_FIELDS.some(x => (c[x.key] || '').toLowerCase().includes(f.q))));
        grid.innerHTML = list.length ? list.map(c => this._contactCard(c)).join('')
            : '<p class="muted">No contacts match.</p>';
    },

    _contactCard(c) {
        const lines = this._MENTION_FIELDS.map(f => {
            const v = (c[f.key] || '').trim();
            const chk = (c.checks || {})[f.code];
            const found = chk && chk.handle === v && chk.found === true ? ' <span class="pc-ok" title="Found on the site">✓</span>' : '';
            return `${this._logo(f.code)}<span class="sr-only">${this.esc(f.label)}: </span>`
                + (v ? `<span>${this.esc(v)}${found}</span>` : `<span class="pc-gone">no ${this.esc(f.label)} handle</span>`);
        }).join('');
        const miss = this._missing(c);
        const used = c.used_count ? `tagged in ${c.used_count} post${c.used_count === 1 ? '' : 's'}` : 'not used yet';
        return `
            <article class="pc-card" data-id="${c.id}">
                <div class="pc-top">${this._avatar({ name: c.name })}
                    <div><b>${this.esc(c.name)}</b><small>@${this.esc(this._tagOf(c))} · ${used}</small></div></div>
                <div class="pc-lines">${lines}</div>
                <div class="pc-foot">${miss ? `<span class="pp-warn">⚠ ${miss} site${miss === 1 ? '' : 's'} missing</span>`
                    : '<span class="pc-ok">✓ every site</span>'}
                    <button type="button" class="btn btn-sm pc-edit" data-id="${c.id}" aria-label="Edit ${this.esc(c.name)}">Edit</button></div>
            </article>`;
    },

    _onContactsClick(e) {
        const t = e.target;
        if (t.closest('[data-new-contact]')) { this._openManagerForm(null); return; }
        const sug = t.closest('[data-suggest]');
        if (sug) { this._openManagerForm({ name: sug.dataset.suggest, alias: sug.dataset.suggest }); return; }
        const miss = t.closest('[data-pc-missing]');
        if (miss) {
            this._pcFilter.missing = miss.dataset.pcMissing === '1';
            document.querySelectorAll('[data-pc-missing]').forEach(b =>
                b.setAttribute('aria-pressed', String(b.dataset.pcMissing === (this._pcFilter.missing ? '1' : '0'))));
            this._paintContactGrid();
            return;
        }
        const ed = t.closest('.pc-edit');
        if (ed) { this._openManagerForm(this._contacts.find(x => String(x.id) === ed.dataset.id)); }
    },

    _openManagerForm(contact) {
        const slot = document.getElementById('pc-form-slot');
        if (!slot) return;
        const editing = !!(contact && contact.id);
        const val = (k) => this.esc((contact && contact[k]) || '');
        const checks = (contact && contact.checks) || {};
        const status = (f) => {
            const c = checks[f.code];
            if (!c || c.handle !== ((contact && contact[f.key]) || '').trim()) return '';
            if (c.found === true) return '<span class="pc-ok">✓ found</span>';
            if (c.found === false) return '<span class="pp-bad">not found</span>';
            return '<span class="muted">couldn\'t check</span>';
        };
        const rows = this._MENTION_FIELDS.map(f =>
            `<div class="pc-fld">${this._logo(f.code)}
                <label for="pc-f-${f.key}">${this.esc(f.label)}</label>
                <input type="text" id="pc-f-${f.key}" class="post-cf-input pc-input" data-key="${f.key}" value="${val(f.key)}" placeholder="${this.esc(f.ph)}" autocomplete="off">
                <span class="pc-check" data-check="${f.code}">${status(f)}</span>
            </div>`).join('');
        slot.innerHTML = `
            <form class="pc-drawer" aria-labelledby="pc-drawer-h" novalidate>
                <h2 id="pc-drawer-h">${editing ? `Edit ${val('name')}` : 'New contact'}</h2>
                <div class="pc-fld"><span></span><label for="pc-f-name">Name</label>
                    <input type="text" id="pc-f-name" class="post-cf-input pc-input" data-key="name" value="${val('name')}" placeholder="who is this?" autocomplete="off"><span></span></div>
                <div class="pc-fld"><span></span><label for="pc-f-alias">Tag as @</label>
                    <input type="text" id="pc-f-alias" class="post-cf-input pc-input" data-key="alias" value="${val('alias')}" placeholder="short tag (optional)" autocomplete="off"><span></span></div>
                ${rows}
                <p class="muted pc-note">“✓ found” means PawPoller asked the site and the handle exists. Only Bluesky and Mastodon can be asked; the rest stay blank.</p>
                <div class="pp-row">
                    <button type="submit" class="btn btn-primary" id="pc-save">${editing ? 'Save' : 'Save contact'}</button>
                    <button type="button" class="btn" id="pc-cancel">Cancel</button>
                    ${editing ? '<button type="button" class="btn btn-ghost" id="pc-check">Check handles</button>' : ''}
                    <span class="pp-sp"></span>
                    ${editing ? `<button type="button" class="btn btn-ghost pc-del" id="pc-delete">Delete contact</button>` : ''}
                </div>
                <p class="pp-msg" id="pc-msg" role="status"></p>
            </form>`;
        slot.dataset.contactId = editing ? String(contact.id) : '';
        this._wireContactForm();
        const nameInput = slot.querySelector('#pc-f-name');
        if (nameInput) { nameInput.focus(); if (!editing) nameInput.select(); }
    },

    _wireContactForm() {
        const slot = document.getElementById('pc-form-slot');
        const form = slot && slot.querySelector('.pc-drawer');
        if (!form || form.dataset.wired) return;
        form.dataset.wired = '1';
        const id = slot.dataset.contactId ? parseInt(slot.dataset.contactId, 10) : null;
        form.addEventListener('submit', e => { e.preventDefault(); this._saveManagerContact(id); });
        form.querySelector('#pc-cancel').addEventListener('click', () => { slot.innerHTML = ''; });
        const chk = form.querySelector('#pc-check');
        if (chk) chk.addEventListener('click', () => this._checkHandles(id));
        const del = form.querySelector('#pc-delete');
        if (del) del.addEventListener('click', () => this._deleteContact(id));
    },

    async _saveManagerContact(id) {
        const slot = document.getElementById('pc-form-slot');
        const msg = slot.querySelector('#pc-msg');
        const payload = {};
        slot.querySelectorAll('.pc-input').forEach(inp => { payload[inp.dataset.key] = inp.value.trim(); });
        if (!payload.name) { msg.textContent = 'Give the contact a name.'; return; }
        const before = id ? (this._contacts.find(x => x.id === id) || {}) : {};
        const save = slot.querySelector('#pc-save');
        save.disabled = true; msg.textContent = 'Saving…';
        try {
            const r = id ? await API.updateContact(id, payload) : await API.createContact(payload);
            const saved = r && r.contact;
            slot.innerHTML = '';
            this._toast('success', 'Saved');
            await this._loadContactList();
            // A new or changed Bluesky / Mastodon handle is looked up straight away.
            if (saved && ['handle_bsky', 'handle_mast'].some(k => payload[k] && payload[k] !== (before[k] || ''))) {
                this._checkHandles(saved.id, true);
            }
        } catch (err) {
            save.disabled = false;
            msg.textContent = 'Not saved: ' + this._errText(err);
        }
    },

    async _checkHandles(id, quiet) {
        if (!id) return;
        const btn = document.getElementById('pc-check');
        if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
        try {
            await API.checkContact(id);
            await this._loadContactList();
            const slot = document.getElementById('pc-form-slot');
            if (slot && slot.dataset.contactId === String(id) && slot.innerHTML.trim()) {
                this._openManagerForm(this._contacts.find(x => x.id === id));
            }
        } catch (err) {
            if (!quiet) this._toast('error', 'Could not check: ' + this._errText(err));
            if (btn) { btn.disabled = false; btn.textContent = 'Check handles'; }
        }
    },

    async _deleteContact(id) {
        if (!confirm('Delete this contact? Posts that tagged them keep their text, but the tag stops linking.')) return;
        try {
            await API.deleteContact(id);
            const slot = document.getElementById('pc-form-slot');
            if (slot) slot.innerHTML = '';
            this._toast('success', 'Deleted');
            await this._loadContactList();
        } catch (err) {
            this._toast('error', 'Delete failed: ' + (err.message || err));
        }
    },
};
