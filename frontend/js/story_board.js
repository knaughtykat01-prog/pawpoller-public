/* ── The Story board — #/library/work/<name> (4.5.0, C2 spec phase 2) ─────
 *
 * The story page, on the same board as the Masterpiece page: a compact hero
 * and three columns of cards — the record (editable), where it goes, what
 * happened. It replaces two read-only surfaces that showed the same data in
 * two shapes (Posting.renderStoryDetail with tabs, Bookshelf._paintWork with
 * a margin rail) and adds the one thing neither had: editing the five
 * canonical fields in place, on the same PUT + mtime check the metadata
 * drawer uses, so a stale page conflicts instead of overwriting.
 *
 * Reads:  GET /api/posting/stories/{name}          (everything the old pages read)
 *         GET /api/editor/stories/{name}/metadata  (raw story.json + last_modified)
 *         GET /api/editor/stories/{name}/tag-preview (what each site gets)
 * Writes: PUT /api/editor/stories/{name}/metadata  (merge, never replace)
 *         POST /api/posting/stories/{name}/link-url (preview, then confirm)
 *
 * ⚠ Publish Check is NOT mounted here (spec §9 Q3): publish_check.js builds a
 * modal with global singleton ids, so a second copy would collide with it.
 * The card lists the unpublished sites and a button opens the modal.
 *
 * ⚠ The per-chapter Update buttons keep data-post-action="update-single";
 * app.js dispatches that to Posting._updateSingle, whose success path now
 * re-renders this board. Nothing here ever emits the upload action.
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = (n) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString();
    const plat = (code) => (window.platformByCode && window.platformByCode(code))
        || (window.PLATFORMS || []).find(p => p.code === code)
        || { code, label: code, emoji: '' };
    const pick = (s, keys) => { for (const k of keys) { if (s && s[k] != null) return Number(s[k]) || 0; } return 0; };
    const views = (s) => pick(s, ['views', 'hits', 'reads']);
    const faves = (s) => pick(s, ['favorites_count', 'kudos', 'votes', 'favorites']);
    const comments = (s) => pick(s, ['comments_count', 'comments']);

    // The five canonical AO3-style ratings the metadata PUT accepts (editor_api
    // _VALID_RATINGS_CANONICAL). A free-text box here would produce a save
    // that fails on a typo.
    const RATINGS = ['Not Rated', 'General Audiences', 'Teen And Up Audiences', 'Mature', 'Explicit'];

    const StoryBoard = {
        _name: null,
        _data: null,        // GET /api/posting/stories/{name}
        _meta: null,        // GET /api/editor/stories/{name}/metadata  ({metadata, last_modified})
        _budget: null,      // GET …/tag-preview
        _linkPreview: null,
        _wired: false,

        async render(name) {
            this._name = name;
            this._wire();
            const app = document.getElementById('app');
            app.innerHTML = `
                <div class="work-back"><a href="#/library">&larr; Library</a></div>
                <div id="sb-detail"><div class="loading-spinner">Opening the story…</div></div>`;
            let d, meta;
            try {
                [d, meta] = await Promise.all([
                    API.getPostingStory(name),
                    // The editable record and its mtime. Missing story.json is not
                    // fatal to the page — the record card says so instead.
                    API.getStoryMetadata(name).catch(() => null),
                ]);
            } catch (err) {
                const status = (err && /404/.test(err.message)) ? 'This story no longer exists.' : esc(err.message);
                document.getElementById('sb-detail').innerHTML =
                    `<div class="card error">Couldn't open this story: ${status}</div>`;
                return;
            }
            this._data = d;
            this._meta = meta;
            this._budget = null;
            this._linkPreview = null;
            this._paint(name, d, meta);
            this._loadTagPreview();
            this._loadChart(d);
        },

        /* ── Composition (spec §5.7) ─────────────────────────────────────── */
        _paint(name, d, meta) {
            const root = document.getElementById('sb-detail');
            if (!root) return;
            const v = this._view(name, d);
            root.innerHTML = `
                <div class="board-wrap">
                    ${this._heroHtml(name, d, v)}
                    <div class="board">
                        <div class="board-col">${this._canonicalHtml(name, d, meta)}${this._tagsHtml(d, meta)}${this._budgetHtml()}</div>
                        <div class="board-col">${this._chaptersHtml(name, d, v)}${this._publishHtml(name, d)}${this._linkHtml()}</div>
                        <div class="board-col board-col--3">${this._locationsHtml(name, d, v)}${this._growthHtml()}${this._attentionHtml(d)}${this._laurelsHtml(d, v)}${this._moreHtml(name, d)}</div>
                    </div>
                </div>`;
            this._tagChips();
        },

        /* Per-platform aggregates the hero, chapters and Published-to share. */
        _view(name, d) {
            const pubs = d.publications || [];
            const byPlat = {};
            pubs.forEach(p => {
                const b = byPlat[p.platform] || (byPlat[p.platform] = { views: 0, faves: 0, comments: 0, url: '', chapters: new Set(), rows: [] });
                b.views += views(p.stats);
                b.faves = Math.max(b.faves, faves(p.stats));
                b.comments = Math.max(b.comments, comments(p.stats));
                if (p.external_url && !b.url) b.url = p.external_url;
                b.chapters.add(p.chapter_index == null ? 0 : p.chapter_index);
                b.rows.push(p);
            });
            const published = (d.published_platforms && d.published_platforms.length)
                ? d.published_platforms : Object.keys(byPlat);
            const totals = {
                views: Object.values(byPlat).reduce((s, b) => s + b.views, 0),
                faves: Object.values(byPlat).reduce((s, b) => s + b.faves, 0),
                comments: Object.values(byPlat).reduce((s, b) => s + b.comments, 0),
            };
            const coverFile = d.images && d.images.cover;
            const coverUrl = coverFile
                ? `/api/posting/image?story=${encodeURIComponent(name)}&file=${encodeURIComponent(coverFile)}` : '';
            return { pubs, byPlat, published, totals, coverUrl };
        },

        _heroHtml(name, d, v) {
            const initial = esc((d.title || name || '?').trim().charAt(0).toUpperCase());
            const tile = v.coverUrl
                ? `<div class="board-hero-tile sb-cover" style="background-image:url('${esc(v.coverUrl)}')" role="img" aria-label="Cover"></div>`
                : `<div class="board-hero-tile sb-cover sb-cover--blank" aria-hidden="true"><span class="book-initial">${initial}</span></div>`;
            const meta = [d.rating, d.category, d.fandom, d.series ? `📚 ${d.series}${d.series_index ? ' #' + d.series_index : ''}` : '']
                .filter(Boolean).map(x => `<span class="pill">${esc(x)}</span>`).join('');
            const warns = (d.warnings || []).map(w => `<span class="pill pill--warn" title="Archive warning">⚠ ${esc(w)}</span>`).join('');
            const chars = (d.characters || []).map(c => `<span class="chip chip-character">${esc(c)}</span>`).join('')
                + (d.relationships || []).map(r => `<span class="chip chip-relationship">${esc(r)}</span>`).join('');
            const live = v.published.length ? `<span class="pill pill--live">live on ${v.published.length}</span>` : '';
            const posted = d.first_posted
                ? `<div class="mp-firstposted muted" title="${d.first_posted_source === 'title' ? 'Matched to a site upload by its title' : 'The date the Library sorts this story by'}">First posted ${d.first_posted_source === 'title' ? '≈ ' : ''}${Utils.formatDate(d.first_posted)}</div>`
                : `<div class="mp-firstposted muted">Not posted anywhere PawPoller knows of</div>`;
            return `
                <div class="board-hero">
                    ${tile}
                    <div class="board-hero-mid">
                        <h1 class="mp-title">${esc(d.title || name)}</h1>
                        <div class="mp-artist">${esc(d.author ? 'by ' + d.author : 'A work')}</div>
                        ${posted}
                        <div class="chip-rows">
                            <div class="chip-row">${meta}${live}${warns}</div>
                            ${chars ? `<div class="chip-row">${chars}</div>` : ''}
                        </div>
                        <div class="board-stats">
                            <div class="n">${num(d.total_words)}<small>Words</small></div>
                            <div class="n">${num(d.total_chapters)}<small>Chapters</small></div>
                            <div class="n">${num(v.totals.views)}<small>Reads</small></div>
                            <div class="n">${num(v.totals.faves)}<small>Faves</small></div>
                            <div class="n">${num(v.totals.comments)}<small>Comments</small></div>
                            <div class="n">${v.published.length}<small>Sites</small></div>
                        </div>
                    </div>
                    <div class="board-hero-actions">
                        <a class="btn btn-sm btn-primary" href="#/editor/${encodeURIComponent(name)}">✎ Open in editor</a>
                        <button class="btn btn-sm" type="button" data-sb-pubcheck title="Every chapter against every platform — opens the Publish Check">✓ Publish check…</button>
                        <button class="btn btn-sm" type="button" data-post-action="update-all" data-post-story="${esc(name)}" title="Push the local files to every site this story is on">↑ Update all</button>
                    </div>
                </div>`;
        },

        /* ── Column 1 — the record ─────────────────────────────────────── */
        _canonicalHtml(name, d, meta) {
            if (!meta || !meta.metadata) {
                return `
                <section class="card" aria-labelledby="sb-sec-canon">
                    <div class="sec-title"><h2 id="sb-sec-canon">Canonical record</h2></div>
                    <p class="board-readonly">This story has no story.json to edit — open it in the editor to create one.</p>
                </section>`;
            }
            const m = meta.metadata;
            const cur = String(m.rating || '');
            const opts = RATINGS.slice();
            // A stored value outside the five (old lowercase short forms) is
            // kept as an option, so an untouched select never rewrites it.
            if (cur && !opts.some(r => r.toLowerCase() === cur.toLowerCase())) opts.unshift(cur);
            const ratingOpts = `<option value=""${cur ? '' : ' selected'}>— none —</option>` + opts.map(r =>
                `<option value="${esc(r)}"${r.toLowerCase() === cur.toLowerCase() ? ' selected' : ''}>${esc(r)}</option>`).join('');
            const chars = Array.isArray(m.characters) ? m.characters.join(', ') : (m.characters || '');
            return `
                <section class="card" aria-labelledby="sb-sec-canon">
                    <div class="sec-title"><h2 id="sb-sec-canon">Canonical record</h2>
                        <button class="btn btn-primary btn-sm" type="button" data-sb-save>Save</button></div>
                    <p class="sec-note">The five fields every site reads. Everything else is in <a href="#/editor/${encodeURIComponent(name)}">the editor</a>.</p>
                    <div class="mp-edit">
                        <label class="mp-field"><span>Title</span>
                            <input class="mp-input" id="sb-e-title" value="${esc(m.title || '')}"></label>
                        <label class="mp-field"><span>Description <span class="muted">(the blurb each site shows)</span></span>
                            <textarea class="mp-input" id="sb-e-desc" rows="4">${esc(m.description || '')}</textarea></label>
                        <div class="mp-field-row">
                            <label class="mp-field"><span>Rating</span>
                                <select class="mp-input" id="sb-e-rating">${ratingOpts}</select></label>
                            <label class="mp-field"><span>Characters <span class="muted">(comma-separated)</span></span>
                                <input class="mp-input" id="sb-e-chars" value="${esc(chars)}"></label>
                        </div>
                        <div class="mp-edit-actions"><span class="mp-edit-msg muted" id="sb-edit-msg"></span></div>
                    </div>
                </section>`;
        },

        _tagsHtml(d, meta) {
            const raw = (meta && meta.metadata && meta.metadata.tags && typeof meta.metadata.tags === 'object') ? meta.metadata.tags : null;
            const list = raw ? (raw.default || []) : ((d.tags_by_platform || {}).default || []);
            return `
                <section class="card" aria-labelledby="sb-sec-tags">
                    <div class="sec-title"><h2 id="sb-sec-tags">Tags</h2>
                        <button class="btn btn-sm btn-browse" type="button" data-sb-tagbrowse title="Pick from the tag library">🏷️ Browse library</button></div>
                    <p class="sec-note">The default list every site starts from — per-site lists are in the editor.</p>
                    <div class="tagblock">
                        <div class="tagbar"><span class="tagcount"><b id="sb-tagcount">${list.length}</b> tags</span></div>
                        <textarea id="sb-e-tags" class="mp-input" hidden aria-hidden="true">${esc(list.join(', '))}</textarea>
                        <ul class="tagchips" id="sb-tagchips" role="list" aria-label="Default tags"></ul>
                        <div class="tag-legend">
                            <span><span class="tagchip tagchip--cut"><b>trimmed</b></span> dropped on at least one site</span>
                        </div>
                    </div>
                </section>`;
        },

        _budgetHtml() {
            return `
                <section class="card" aria-labelledby="sb-sec-budget">
                    <div class="sec-title"><h2 id="sb-sec-budget">What each site gets</h2></div>
                    <p class="sec-note">Trimming drops from the tail. AO3 and Wattpad are the two that bite.</p>
                    <div id="sb-tagbudget" class="mp-tagbudget"><div class="card-skel"></div></div>
                </section>`;
        },

        /* ── Column 2 — where it goes ──────────────────────────────────── */
        /* Chapter × platform reach (from the old Library work page). Multi-
         * chapter platforms carry per-chapter rows (chapter_index > 0); single-
         * post platforms publish the whole story (chapter_index 0). */
        _chaptersHtml(name, d, v) {
            const chapters = d.chapters || [];
            if (!chapters.length) return '';
            const multi = v.published.filter(c => v.byPlat[c] && [...v.byPlat[c].chapters].some(i => i > 0));
            const reach = {};
            v.pubs.forEach(p => {
                const idx = p.chapter_index == null ? 0 : p.chapter_index;
                (reach[idx] || (reach[idx] = new Set())).add(p.platform);
            });
            const rows = chapters.map(ch => {
                const idx = ch.index;
                const lit = v.published.map(code => {
                    const on = (reach[idx] && reach[idx].has(code)) || (reach[0] && reach[0].has(code) && !multi.includes(code));
                    const p = plat(code);
                    return `<span class="ch-dot ${on ? 'is-on' : 'is-off'}" title="${esc(p.label)}${on ? '' : ' — not here'}">${p.emoji || '•'}</span>`;
                }).join('');
                const gaps = multi.filter(code => !(reach[idx] && reach[idx].has(code)));
                const gap = gaps.length ? `<span class="ch-gap" title="Missing from ${esc(gaps.map(c => plat(c).label).join(', '))}">incomplete</span>` : '';
                return `
                    <div class="chapter-row">
                        <span class="chapter-idx">${idx}</span>
                        <span class="chapter-name">${esc(ch.title || 'Chapter ' + idx)}${ch.word_count ? ` <em>${num(ch.word_count)}w</em>` : ''}${ch.description ? `<div class="muted" style="font-size:11.5px">${esc(ch.description)}</div>` : ''}</span>
                        <span class="chapter-reach">${lit}</span>
                        ${gap}
                    </div>`;
            }).join('');
            return `
                <section class="card" aria-labelledby="sb-sec-chap">
                    <div class="sec-title"><h2 id="sb-sec-chap">Chapters</h2></div>
                    <p class="sec-note">Where each one is live.</p>
                    <div class="chapter-list">${rows}</div>
                </section>`;
        },

        _publishHtml(name, d) {
            const un = d.unpublished_platforms || [];
            const rows = un.length
                ? un.map(c => { const p = plat(c); return `<div class="plat-line">${p.emoji || ''} ${esc(p.label)}</div>`; }).join('')
                : `<div class="muted" style="font-size:12.5px">On every site it is configured for.</div>`;
            return `
                <section class="card" aria-labelledby="sb-sec-pub">
                    <div class="sec-title"><h2 id="sb-sec-pub">Publish</h2>
                        <button class="btn btn-sm btn-primary" type="button" data-sb-pubcheck>✓ Publish check…</button></div>
                    <p class="sec-note">Sites this story isn't on yet. Publishing runs from the check, chapter by chapter.</p>
                    <div class="plat-lines">${rows}</div>
                </section>`;
        },

        _linkHtml() {
            return `
                <section class="card" aria-labelledby="sb-sec-link">
                    <div class="sec-title"><h2 id="sb-sec-link">Link this story elsewhere</h2></div>
                    <p class="sec-note">Posted it by hand? Paste the page's link to record it here.</p>
                    <div class="sb-link">
                        <input type="url" class="mp-input" id="sb-link-url" placeholder="https://…" aria-label="Link to a posted copy">
                        <button class="btn btn-sm" type="button" data-sb-link-preview>Check</button>
                    </div>
                    <div id="sb-link-body"></div>
                </section>`;
        },

        /* ── Column 3 — what happened ──────────────────────────────────── */
        _healthDot(code) {
            const PH = window.PlatformHealth;
            if (!PH || !PH.classify) return '';
            const state = PH.get(code) ? PH.classify(code) : 'unknown';
            const labels = { healthy: 'Polling normally', stale: 'Last poll is overdue', throttled: 'Rate-limited by the site',
                             error: 'Last poll failed — see Settings', running: 'Polling now', unknown: 'Not checked yet',
                             unconfigured: 'No credentials for this site' };
            const text = labels[state] || state;
            return `<span class="health-dot health-dot--${esc(state)}" title="${esc(text)}" aria-label="${esc(text)}"></span>`;
        },

        _locationsHtml(name, d, v) {
            if (!v.published.length) {
                return `
                <section class="card" aria-labelledby="sb-sec-loc">
                    <div class="sec-title"><h2 id="sb-sec-loc">Published to</h2></div>
                    <div class="mp-empty">Not posted anywhere yet — the Publish check posts it, and <strong>Link this story elsewhere</strong> records a copy you posted by hand.</div>
                </section>`;
            }
            const groups = v.published.map(code => {
                const b = v.byPlat[code] || { views: 0, faves: 0, comments: 0, url: '', rows: [], chapters: new Set() };
                const p = plat(code);
                const safe = b.url && window.Utils && Utils.safeUrl ? Utils.safeUrl(b.url) : '';
                const open = safe ? `<a class="btn btn-sm" href="${esc(safe)}" target="_blank" rel="noopener">open&nbsp;&#8599;</a>` : '<span></span>';
                const nCh = b.rows.length;
                const sub = b.rows.map(r => {
                    const ch = r.chapter_index > 0 ? `Ch ${r.chapter_index}` : 'Whole story';
                    const rsafe = r.external_url && window.Utils && Utils.safeUrl ? Utils.safeUrl(r.external_url) : '';
                    const when = r.updated_at || r.posted_at || r.created_at;
                    const drift = r.change_detected ? `<span class="change-badge change-drift" title="The local file changed since this was posted">drifted</span>` : '';
                    return `<div class="pub-sub">
                        <span>${esc(ch)}${r.chapter_title ? ` <span class="muted">· ${esc(r.chapter_title)}</span>` : ''}</span>
                        <span class="loc-stats" title="views · favourites · comments">${num(views(r.stats))} · ${num(faves(r.stats))} · ${num(comments(r.stats))}</span>
                        <span class="muted" title="${esc(when || '')}">${when && Utils.timeAgo ? Utils.timeAgo(when) : ''}</span>
                        ${drift}
                        ${rsafe ? `<a href="${esc(rsafe)}" target="_blank" rel="noopener" class="muted">link</a>` : ''}
                        <button class="btn btn-sm" type="button" data-post-action="update-single" data-post-story="${esc(name)}" data-post-platform="${esc(r.platform)}" data-post-chapter="${r.chapter_index == null ? 0 : r.chapter_index}" title="Push the local file to this site">Update</button>
                    </div>`;
                }).join('');
                return `
                    <div class="pub-group">
                        <div class="loc-row">
                            <span class="thumb-sq thumb-sq--emoji" aria-hidden="true">${p.emoji || ''}</span>
                            <div class="loc-site"><span class="name">${this._healthDot(code)}${esc(p.label)}</span>
                                <span class="sub">${nCh} ${nCh === 1 ? 'post' : 'posts'}</span></div>
                            <span class="loc-stats" title="views · favourites · comments">${num(b.views)} · ${num(b.faves)} · ${num(b.comments)}</span>
                            ${open}
                            <span></span>
                        </div>
                        <div class="pub-subs">${sub}</div>
                    </div>`;
            }).join('');
            return `
                <section class="card" aria-labelledby="sb-sec-loc">
                    <div class="sec-title"><h2 id="sb-sec-loc">Published to</h2></div>
                    <p class="sec-note">Where this story already lives.</p>
                    <div class="loc-list">${groups}</div>
                </section>`;
        },

        _growthHtml() {
            return `
                <section class="card" id="sb-chart-card" style="display:none" aria-labelledby="sb-sec-growth">
                    <div class="sec-title"><h2 id="sb-sec-growth">Growth</h2></div>
                    <p class="sec-note">One line per posting, last 30 days.</p>
                    <div class="mp-chart-wrap"><canvas id="story-comparison-chart"></canvas></div>
                </section>`;
        },

        /* From data the page already has: drifted publications, the queue,
         * failed log rows (spec §5.5). Empty state: "Nothing needs you." */
        _attentionHtml(d) {
            const items = [];
            (d.publications || []).filter(p => p.change_detected).forEach(p => {
                const pl = plat(p.platform);
                items.push(`<div class="needs-row"><span class="needs-k">drifted</span> ${pl.emoji || ''} ${esc(pl.label)} · ${p.chapter_index > 0 ? 'Ch ' + p.chapter_index : 'whole story'} <span class="muted">— the local file changed since it was posted; Update pushes it</span></div>`);
            });
            (d.pending_queue || []).forEach(q => {
                const pl = plat(q.platform);
                const when = q.scheduled_at && window.Posting && Posting._schedInstant ? Posting._schedInstant(q.scheduled_at).toLocaleString() : 'next scheduler tick';
                items.push(`<div class="needs-row"><span class="needs-k">queued</span> ${esc(q.action || '')} ${q.chapter_index > 0 ? 'Ch ' + q.chapter_index : 'whole story'} → ${pl.emoji || ''} ${esc(pl.label)} <span class="muted">(${esc(q.status || 'pending')}, ${esc(when)})</span></div>`);
            });
            (d.recent_log || []).filter(e => e.status !== 'success').forEach(e => {
                const pl = plat(e.platform);
                items.push(`<div class="needs-row"><span class="needs-k needs-k--bad">failed</span> ${esc(e.action || '')} ${e.chapter_index > 0 ? 'Ch ' + e.chapter_index : ''} → ${pl.emoji || ''} ${esc(pl.label)}${e.error_message ? ` <span class="muted" title="${esc(e.error_message)}">— ${esc(String(e.error_message).slice(0, 90))}${String(e.error_message).length > 90 ? '…' : ''}</span>` : ''}</div>`);
            });
            return `
                <section class="card" aria-labelledby="sb-sec-needs">
                    <div class="sec-title"><h2 id="sb-sec-needs">Needs attention</h2></div>
                    ${items.length ? items.join('') : '<p class="muted" style="margin:0;font-size:12.5px">Nothing needs you.</p>'}
                </section>`;
        },

        /* Per-work achievements (Laurels) — carried over from the old page. */
        _laurelsHtml(d, v) {
            if (!(window.Laurels && window.Laurels.workMedals)) return '';
            const chapters = d.chapters || [];
            const multi = v.published.filter(c => v.byPlat[c] && [...v.byPlat[c].chapters].some(i => i > 0));
            const reach = {};
            v.pubs.forEach(p => { const i = p.chapter_index == null ? 0 : p.chapter_index; (reach[i] || (reach[i] = new Set())).add(p.platform); });
            const incomplete = chapters.reduce((n, ch) => n + (multi.some(c => !(reach[ch.index] && reach[ch.index].has(c))) ? 1 : 0), 0);
            const medals = Laurels.workMedals({
                views: v.totals.views, faves: v.totals.faves, comments: v.totals.comments,
                platforms: v.published, chapters: chapters.length, words: d.total_words, incompleteChapters: incomplete,
            }) || [];
            if (!medals.length) return '';
            const earned = medals.filter(x => x.earned).length;
            const chip = (x) => `<div class="wm ${x.earned ? 'is-earned' : 'is-locked'}" title="${esc(x.desc || '')}"><span class="wm-ico" aria-hidden="true">${x.icon}</span><span class="wm-name">${esc(x.name)}</span>${x.sub ? `<span class="wm-sub">${esc(x.sub)}</span>` : ''}</div>`;
            return `
                <section class="card" aria-labelledby="sb-sec-laurels">
                    <div class="sec-title"><h2 id="sb-sec-laurels">Achievements</h2><span class="muted" style="font-size:12px">${earned} of ${medals.length}</span></div>
                    <div class="wm-grid">${medals.map(chip).join('')}</div>
                </section>`;
        },

        /* History and Formats, collapsed (spec §5.6). The Timeline tab is gone:
         * it was derived from the dates Published to already shows. */
        _moreHtml(name, d) {
            const log = (d.recent_log || []).map(e => {
                const pl = plat(e.platform);
                const ch = e.chapter_index > 0 ? `Ch${e.chapter_index}` : 'Full';
                const link = e.external_url && window.Utils && Utils.safeUrl && Utils.safeUrl(e.external_url)
                    ? ` · <a href="${esc(Utils.safeUrl(e.external_url))}" target="_blank" rel="noopener">link</a>` : '';
                return `<div class="log-row ${e.status === 'success' ? 'log-success' : 'log-failed'}">
                    <span class="log-when" title="${esc(e.created_at || '')}">${e.created_at && Utils.timeAgo ? Utils.timeAgo(e.created_at) : ''}</span>
                    <span class="log-action">${pl.emoji || ''} ${esc(e.action || '')} ${ch}</span>
                    <span class="log-status">${esc(e.status || '')}${e.duration_seconds ? ` · ${Number(e.duration_seconds).toFixed(1)}s` : ''}${link}</span>
                    ${e.error_message ? `<div class="log-error" title="${esc(e.error_message)}">${esc(String(e.error_message).slice(0, 80))}</div>` : ''}
                </div>`;
            }).join('');
            const formats = d.formats || {};
            const badges = Object.keys(formats).map(k => {
                const meta = formats[k] || {};
                const label = k.replace(/_/g, ' ');
                if (!meta.available || !meta.files || !meta.files.length) return `<span class="format-badge format-empty" title="No files found on disk">${esc(label)}</span>`;
                const first = meta.files[0];
                const url = `/api/posting/file?story=${encodeURIComponent(name)}&file=${encodeURIComponent(first.path)}`;
                const sizeText = meta.files.length === 1 ? this._size(first.size) : `${meta.files.length} files`;
                return `<a class="format-badge format-link" href="${esc(url)}" download title="${esc(first.path)}">${esc(label)} <span class="format-meta">${esc(sizeText)}</span></a>`;
            }).join('');
            return `
                <details class="card more-card">
                    <summary>More <span class="muted">— history and files</span></summary>
                    <div class="body">
                        <h3 class="sb-h3">Recent activity</h3>
                        ${log || '<p class="muted" style="font-size:12.5px">No posting activity recorded.</p>'}
                        <h3 class="sb-h3">Files</h3>
                        <div class="format-badges">${badges || '<span class="muted" style="font-size:12.5px">No formats declared.</span>'}</div>
                        <div class="format-actions" style="margin-top:.75em">
                            <a class="btn btn-sm btn-outline" href="/api/posting/archive?story=${encodeURIComponent(name)}" download
                               title="Download the entire story folder as a zip (every format + cover + Markdown source)">Download all (zip)</a>
                        </div>
                    </div>
                </details>`;
        },

        _size(bytes) {
            const b = Number(bytes) || 0;
            if (b < 1024) return `${b} B`;
            if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
            return `${(b / (1024 * 1024)).toFixed(1)} MB`;
        },

        /* ── Fills ─────────────────────────────────────────────────────── */
        async _loadTagPreview() {
            const box = document.getElementById('sb-tagbudget');
            if (!box || !this._name) return;
            let d;
            try {
                d = await API.getStoryTagPreview(this._name);
            } catch (err) {
                box.innerHTML = `<div class="muted">Couldn't load the per-site view.
                    <button class="btn btn-sm" type="button" data-sb-budget-retry>Retry</button></div>`;
                return;
            }
            this._budget = d;
            const rows = (d.platforms || []).map(p => {
                const pl = plat(p.platform);
                const trimmed = (p.dropped || []).length > 0;
                const detail = p.override
                    ? `<span class="mp-tb-tag">curated</span> ${p.sent} tags`
                    : trimmed ? `<strong>${p.sent}</strong> of ${p.total} — <span class="mp-tb-cut">${p.dropped.length} cut</span>` : `all ${p.sent}`;
                return `<div class="mp-tb-row ${p.override ? 'is-override' : (trimmed ? 'is-trimmed' : '')}">
                    <span class="mp-tb-site">${pl.emoji || ''} ${esc(pl.label)}</span>
                    <span class="mp-tb-lim">${esc(p.limit || '')}</span>
                    <span class="mp-tb-got">${detail}</span>
                    <span></span>
                </div>`;
            }).join('');
            box.innerHTML = rows || `<div class="muted">No site budgets to show.</div>`;
            this._tagChips();
        },

        _loadChart(d) {
            const pubs = (d.publications || []).filter(p => (p.snapshots || []).length >= 2);
            const card = document.getElementById('sb-chart-card');
            if (!card || pubs.length < 2 || !(window.Posting && Posting._renderComparisonChart)) return;
            card.style.display = '';
            // The old page's chart renderer, with its hand-rolled Chart.js
            // lifecycle intact — it exists for a reason (posting.js).
            try { Posting._renderComparisonChart(pubs); } catch (e) { card.style.display = 'none'; }
        },

        /* ── Tag chips — a view over #sb-e-tags, as the Masterpiece page ─── */
        _tagsList() {
            const ta = document.getElementById('sb-e-tags');
            return ta ? ta.value.split(',').map(x => x.trim()).filter(Boolean) : [];
        },
        _setTags(list) {
            const ta = document.getElementById('sb-e-tags');
            if (!ta) return;
            ta.value = list.join(', ');
            this._tagChips();
        },
        _tagChips() {
            const host = document.getElementById('sb-tagchips');
            if (!host) return;
            const tags = this._tagsList();
            const d = this._budget || null;
            const cutBy = {};
            ((d && d.platforms) || []).forEach(p => (p.dropped || []).forEach(t => {
                const k = String(t).toLowerCase();
                (cutBy[k] = cutBy[k] || []).push(plat(p.platform).label);
            }));
            const chips = tags.map(t => {
                const k = t.toLowerCase();
                const cls = 'tagchip' + (cutBy[k] ? ' tagchip--cut' : '');
                const title = cutBy[k] ? ` title="Cut on ${esc(cutBy[k].join(', '))}"` : '';
                return `<li class="${cls}"${title}><b>${esc(t)}</b><button type="button" class="x" data-sb-chip-x="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button></li>`;
            }).join('');
            host.innerHTML = chips
                + (tags.length ? '' : `<li class="tag-empty">No tags yet — add some, or browse the library.</li>`)
                + `<li class="tagchip-slot"><button type="button" class="tagchip-add" data-sb-chip-add>+ add tag</button>
                   <input type="text" class="tagchip-input" id="sb-tag-add" placeholder="tag, another tag" hidden aria-label="Add tags"></li>`;
            const n = document.getElementById('sb-tagcount'); if (n) n.textContent = String(tags.length);
        },
        _addTagsFromInput(input) {
            const add = input.value.split(',').map(x => x.trim()).filter(Boolean);
            input.value = ''; input.hidden = true;
            if (add.length) {
                const cur = this._tagsList();
                const seen = new Set(cur.map(x => x.toLowerCase()));
                add.forEach(t => { if (!seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); cur.push(t); } });
                this._setTags(cur);
            }
            const btn = document.querySelector('[data-sb-chip-add]');
            if (btn) btn.focus();
        },
        _removeTag(tag) {
            const cur = this._tagsList();
            const i = cur.findIndex(x => x.toLowerCase() === String(tag).toLowerCase());
            if (i === -1) return;
            cur.splice(i, 1);
            this._setTags(cur);
            const xs = document.querySelectorAll('#sb-tagchips [data-sb-chip-x]');
            const next = xs[Math.min(i, xs.length - 1)] || document.querySelector('[data-sb-chip-add]');
            if (next) next.focus();
        },

        /* ── Canonical save: merge into the loaded record, mtime-checked ─── */
        /* "API 409: {"detail":"…"}" → the detail. The envelope is for logs. */
        _errText(err) {
            const raw = String((err && err.message) || err || '').replace(/^API \d+:\s*/, '');
            try {
                const j = JSON.parse(raw);
                if (j && typeof j.detail === 'string') return j.detail;
            } catch (_) { /* not JSON — already plain text */ }
            return raw;
        },

        async _save() {
            const meta = this._meta;
            const msg = document.getElementById('sb-edit-msg');
            if (!meta || !meta.metadata || !this._name) return;
            const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
            const title = val('sb-e-title').trim();
            if (!title) { if (msg) { msg.textContent = 'Enter a title.'; msg.classList.add('mp-err'); } return; }
            // ⚠ Merge, never replace: the PUT writes the whole story.json, so a
            // five-key object would delete every field the drawer owns (§5.7).
            const next = { ...meta.metadata };
            next.title = title;
            next.description = val('sb-e-desc');
            const rating = val('sb-e-rating');
            if (rating) next.rating = rating; else delete next.rating;
            next.characters = val('sb-e-chars').split(',').map(x => x.trim()).filter(Boolean);
            next.tags = { ...((meta.metadata.tags && typeof meta.metadata.tags === 'object') ? meta.metadata.tags : {}), default: this._tagsList() };
            if (msg) { msg.textContent = 'Saving…'; msg.classList.remove('mp-err'); }
            try {
                const r = await API.saveStoryMetadata(this._name, { metadata: next, expected_mtime: meta.last_modified });
                this._meta = { metadata: next, last_modified: (r && r.last_modified) || meta.last_modified };
                if (msg) msg.textContent = 'Saved.';
                // The hero shows the record: repaint it (and only it) so the
                // title, rating and characters match what was just saved.
                if (this._data) {
                    Object.assign(this._data, { title: next.title, description: next.description || '',
                        rating: next.rating || '', characters: next.characters || [] });
                    const hero = document.querySelector('#sb-detail .board-hero');
                    if (hero) hero.outerHTML = this._heroHtml(this._name, this._data, this._view(this._name, this._data));
                }
                if (window.App && App.toast) App.toast('success', 'Saved');
            } catch (err) {
                if (/409/.test(err.message || '')) {
                    // Changed elsewhere since the page opened. Never auto-merge.
                    if (msg) {
                        msg.classList.add('mp-err');
                        msg.innerHTML = `Changed elsewhere since you opened it — <button class="btn btn-sm" type="button" data-sb-reload>Reload</button> to see the newer version.`;
                    }
                    return;
                }
                if (msg) { msg.classList.add('mp-err'); msg.textContent = 'Could not save: ' + this._errText(err); }
            }
        },

        /* ── Link by URL: preview, then confirm with a chapter ─────────── */
        async _linkPreviewRun() {
            const input = document.getElementById('sb-link-url');
            const body = document.getElementById('sb-link-body');
            if (!input || !body) return;
            const url = input.value.trim();
            if (!url) { body.innerHTML = '<div class="muted">Paste a link first.</div>'; return; }
            body.innerHTML = '<div class="card-skel"></div>';
            let r;
            try {
                r = await API.linkStoryByUrl(this._name, { url });
            } catch (err) {
                body.innerHTML = `<div class="mp-err" style="font-size:12.5px">${esc(this._errText(err))}</div>`;
                return;
            }
            this._linkPreview = r;
            const cands = r.candidates || [];
            if (!cands.length) { body.innerHTML = '<div class="muted">Nothing recognised in that link.</div>'; return; }
            const total = Number(r.total_chapters) || 0;
            // chapter_index has no safe default (spec §6.6.3): a single-chapter
            // story is 0; a multi-chapter one makes the user pick.
            const chapterSel = total > 1
                ? `<label class="mp-field"><span>Which chapter is this?</span><select class="mp-input" id="sb-link-chapter">
                        <option value="">— pick —</option><option value="0">The whole story</option>
                        ${Array.from({ length: total }, (_, i) => `<option value="${i + 1}">Chapter ${i + 1}</option>`).join('')}
                   </select></label>`
                : `<input type="hidden" id="sb-link-chapter" value="0">`;
            body.innerHTML = cands.map((c, i) => {
                const p = plat(c.platform);
                const state = c.publication_of
                    ? `<span class="mp-err">already recorded for “${esc(c.publication_of)}”${c.publication_chapter != null ? ` (ch ${c.publication_chapter})` : ''}</span>`
                    : c.linked_to ? `<span class="mp-err">attached to the artwork “${esc(c.linked_to)}”</span>`
                    : c.known ? `<span class="muted">seen by the poller${c.title ? ` — ${esc(c.title)}` : ''}</span>`
                    : `<span class="muted">not seen by the poller yet — it will show stats once polled</span>`;
                return `<div class="link-cand"><strong>${p.emoji || ''} ${esc(p.label)}</strong> #${esc(c.submission_id)} — ${state}</div>`;
            }).join('') + chapterSel + `
                <div class="mp-edit-actions">
                    <button class="btn btn-sm btn-primary" type="button" data-sb-link-confirm ${cands.length === 1 ? '' : 'disabled title="Ambiguous link — several sites matched"'}>Record it</button>
                    <span class="mp-edit-msg muted" id="sb-link-msg"></span>
                </div>`;
        },

        async _linkConfirm() {
            const r = this._linkPreview;
            const msg = document.getElementById('sb-link-msg');
            const sel = document.getElementById('sb-link-chapter');
            if (!r || !(r.candidates || []).length) return;
            const ch = sel ? sel.value : '';
            if (ch === '') { if (msg) { msg.textContent = 'Pick the chapter first.'; msg.classList.add('mp-err'); } return; }
            const c = r.candidates[0];
            if (msg) { msg.textContent = 'Recording…'; msg.classList.remove('mp-err'); }
            try {
                await API.linkStoryByUrl(this._name, { url: (document.getElementById('sb-link-url') || {}).value || '',
                    platform: c.platform, submission_id: c.submission_id, chapter_index: Number(ch), confirm: true });
                if (window.App && App.toast) App.toast('success', 'Recorded');
                this.render(this._name);
            } catch (err) {
                if (msg) { msg.classList.add('mp-err'); msg.textContent = this._errText(err); }
            }
        },

        /* ── Delegation, once ───────────────────────────────────────────── */
        _wire() {
            if (this._wired) return;
            this._wired = true;
            document.addEventListener('click', (e) => {
                const t = e.target;
                if (!t || !t.closest) return;
                let el;
                if ((el = t.closest('[data-sb-save]')))         { e.preventDefault(); this._save(); return; }
                if ((el = t.closest('[data-sb-reload]')))       { e.preventDefault(); this.render(this._name); return; }
                if ((el = t.closest('[data-sb-pubcheck]')))     { e.preventDefault(); if (window.PublishCheck) PublishCheck.open(this._name); return; }
                if ((el = t.closest('[data-sb-tagbrowse]')))    { e.preventDefault(); this._browse(); return; }
                if ((el = t.closest('[data-sb-chip-x]')))       { e.preventDefault(); this._removeTag(el.dataset.sbChipX); return; }
                if ((el = t.closest('[data-sb-chip-add]')))     { e.preventDefault(); const inp = document.getElementById('sb-tag-add'); if (inp) { inp.hidden = false; inp.focus(); } return; }
                if ((el = t.closest('[data-sb-budget-retry]'))) { e.preventDefault(); this._loadTagPreview(); return; }
                if ((el = t.closest('[data-sb-link-preview]'))) { e.preventDefault(); this._linkPreviewRun(); return; }
                if ((el = t.closest('[data-sb-link-confirm]'))) { e.preventDefault(); this._linkConfirm(); return; }
            });
            document.addEventListener('keydown', (e) => {
                const inp = e.target && e.target.id === 'sb-tag-add' ? e.target : null;
                if (inp) {
                    if (e.key === 'Enter') { e.preventDefault(); this._addTagsFromInput(inp); }
                    else if (e.key === 'Escape') { e.preventDefault(); inp.value = ''; inp.hidden = true; const b = document.querySelector('[data-sb-chip-add]'); if (b) b.focus(); }
                    else if (e.key === 'Backspace' && !inp.value) {
                        e.preventDefault();
                        const xs = document.querySelectorAll('#sb-tagchips [data-sb-chip-x]');
                        const last = xs[xs.length - 1];
                        if (last) this._removeTag(last.dataset.sbChipX);
                        const again = document.getElementById('sb-tag-add');
                        if (again) { again.hidden = false; again.focus(); }
                    }
                    return;
                }
                if (e.target && e.target.id === 'sb-link-url' && e.key === 'Enter') { e.preventDefault(); this._linkPreviewRun(); }
            });
            document.addEventListener('focusout', (e) => {
                if (e.target && e.target.id === 'sb-tag-add' && e.target.value.trim()) this._addTagsFromInput(e.target);
            });
        },

        _browse() {
            if (!window.TagPicker) return;
            TagPicker.open({
                title: 'Default tags',
                selected: this._tagsList(),
                onConfirm: (names) => { this._setTags(names || []); },
            });
        },
    };

    window.StoryBoard = StoryBoard;
})();
