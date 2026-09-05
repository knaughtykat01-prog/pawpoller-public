/* ── Posting Module — Frontend Pages ─────────────────────────── */
/*
 *   1. Detail     (#/posting/story/{name}) — Single story detail with platform controls
 *   2. Queue      (#/posting/queue)        — Pending/scheduled items
 *   3. Published  (#/posting/published)    — Registry of what's posted where (legacy, redirects to stories)
 *   4. History    (#/posting/log)          — Audit log of all posting actions
 *
 * The Stories HUB (#/posting) is RETIRED (2.155.0, backlog L): it was /api/works
 * filtered to stories with no search/sort — a strict subset of the Library's
 * Stories segment, linking to the same detail page below. The route redirects to
 * #/library/type/story, so `renderUpload()` here is unreachable; it's kept only
 * as a port source and is tracked for removal (backlog L2).
 */

/* ── File-size formatter ─────────────────────────────────────
 * Bytes → human-readable string. Used by the format-download badges.
 */
function formatFileSize(bytes) {
    if (!bytes || bytes < 0) return '0 B';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/* Comparison chart palette — distinct categorical hues that read on both the
 * light (Quill) and dark themes. The PRIMARY series (index 0) is drawn from the
 * live `--accent` token at render time (see below) so it always matches the
 * active theme; these are the secondary hues, cycled via modulo. */
const PUB_CHART_COLORS = [
    '#c9822f', '#5ae0a0', '#f0a050', '#70a0ff', '#f07070',
    '#fbc050', '#a880ff', '#5ac0e0', '#f580a0', '#80e070', '#e0a0ff',
];

/* ── Sparkline helper ────────────────────────────────────────
 * Builds a tiny SVG line chart for a publication's snapshots. We use
 * inline SVG (not Chart.js) so each pub row stays light — Chart.js per
 * row would mean N canvases per page, each with its own resize observer
 * and animation loop. SVG is one DOM tree per chart, no JS lifecycle.
 *
 * snapshots: [{t: "2026-04-01 00:00:00", v: 123}, ...] in chronological
 *            order. Empty/single-point series render as an empty span.
 * width / height: pixel dimensions of the sparkline.
 * Returns an HTML string ready for innerHTML insertion.
 */
function buildSparkline(snapshots, width = 100, height = 24) {
    if (!Array.isArray(snapshots) || snapshots.length < 2) return '';
    const values = snapshots.map(s => s.v || 0);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;     // avoid div-by-zero on flat series
    const step = width / (snapshots.length - 1);
    const points = snapshots.map((s, i) => {
        const x = (i * step).toFixed(1);
        // SVG y axis grows downward, so invert: high values render high.
        const y = (height - ((s.v - min) / range) * height).toFixed(1);
        return `${x},${y}`;
    }).join(' ');
    // Last point gets a small dot so even a flat series has a visual cue.
    const lastX = ((snapshots.length - 1) * step).toFixed(1);
    const lastY = (height - ((values[values.length - 1] - min) / range) * height).toFixed(1);
    return `
        <svg class="sparkline" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
            <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
            <circle cx="${lastX}" cy="${lastY}" r="1.8" fill="currentColor" />
        </svg>`;
}

const PLATFORM_LABELS = {
    ib: '🐾 Inkbunny', fa: '🦊 FurAffinity', ws: '🦎 Weasyl',
    sf: '🐺 SoFurry', sqw: '🦑 SquidgeWorld', ao3: '📖 AO3', da: '🎨 DeviantArt', ik: '🎯 Itaku', bsky: '🦋 Bluesky', wp: '📙 Wattpad',
    inkbunny: '🐾 Inkbunny', furaffinity: '🦊 FurAffinity', weasyl: '🦎 Weasyl',
    sofurry: '🐺 SoFurry', squidgeworld: '🦑 SquidgeWorld', ao3: '📖 AO3', deviantart: '🎨 DeviantArt', itaku: '🎯 Itaku', bluesky: '🦋 Bluesky', wattpad: '📙 Wattpad',
};
const PLATFORM_EMOJI = {
    ib: '🐾', fa: '🦊', ws: '🦎', sf: '🐺', sqw: '🦑', ao3: '📖', da: '🎨', ik: '🎯', bsky: '🦋', wp: '📙',
    inkbunny: '🐾', furaffinity: '🦊', weasyl: '🦎', sofurry: '🐺', squidgeworld: '🦑', ao3: '📖', deviantart: '🎨', itaku: '🎯', bluesky: '🦋', wattpad: '📙',
};
const PLAT_ID = { inkbunny: 'ib', furaffinity: 'fa', weasyl: 'ws', sofurry: 'sf', squidgeworld: 'sqw', ao3: 'ao3', deviantart: 'da', itaku: 'ik', bluesky: 'bsky', wattpad: 'wp' };

const Posting = {

    /* ── 1. Stories Hub (Card Grid) ──────────────────────────── */
    async renderUpload() {
        App._setContent('<div class="page-header"><h2>Stories</h2></div><div class="loading">Loading stories...</div>');

        try {
            const { stories } = await API.getPostingStories();

            if (!stories.length) {
                App._setContent(`
                    <div class="page-header"><h2>Stories</h2></div>
                    <div class="empty-state"><h3>No stories found</h3><p>Sync your archive with <code>pawsync.bat</code></p></div>`);
                return;
            }

            const cards = stories.map(s => {
                const title = Utils.escapeHtml(s.title || s.name.replace(/_/g, ' '));
                const words = (s.word_count || 0).toLocaleString();
                const chs = s.chapters || 0;
                const rating = s.rating ? `<span class="story-rating rating-${s.rating}">${s.rating}</span>` : '';
                const category = s.category ? `<span class="story-category">${Utils.escapeHtml(s.category)}</span>` : '';

                // Platform badges
                const published = s.published_platforms || [];
                const available = (s.platforms || []).map(p => PLAT_ID[p] || p);
                const platformBadges = available.map(p => {
                    const emoji = PLATFORM_EMOJI[p] || '📦';
                    const isPublished = published.includes(p);
                    return `<span class="plat-badge ${isPublished ? 'plat-published' : 'plat-available'}" title="${isPublished ? 'Published' : 'Not uploaded'}">${emoji}</span>`;
                }).join('');

                // Cover image. Sub-story names contain a slash
                // (e.g. My_Story/Nice_Version) and image paths can be
                // nested (Images/cover.png), so both go through encodeURIComponent
                // and ride as query params on /api/posting/image rather than path
                // segments — keeps the round-trip unambiguous.
                const coverSrc = s.images && s.images.cover
                    ? `/api/posting/image?story=${encodeURIComponent(s.name)}&file=${encodeURIComponent(s.images.cover)}`
                    : '';
                const coverHtml = coverSrc ? `<div class="story-card-cover" style="background-image:url('${coverSrc}')"></div>` : '';

                // Description
                const desc = s.description ? Utils.escapeHtml(s.description.substring(0, 120)) + (s.description.length > 120 ? '...' : '') : '';

                // Warnings
                const warnings = (s.warnings || []).length > 0
                    ? `<span class="story-warning" title="${Utils.escapeHtml(s.warnings.join(', '))}">⚠</span>` : '';

                return `
                    <a href="#/posting/story/${Utils.escapeHtml(s.name)}" class="story-card">
                        ${coverHtml}
                        <div class="story-card-body">
                            <div class="story-card-header">
                                <h3 class="story-card-title">${title} ${warnings}</h3>
                                <div class="story-card-meta">${rating} ${category}</div>
                            </div>
                            <p class="story-card-desc">${desc}</p>
                            <div class="story-card-footer">
                                <span class="story-card-stats">${words} words${chs > 0 ? ` · ${chs} ch` : ''}</span>
                                <div class="story-card-platforms">${platformBadges}</div>
                            </div>
                        </div>
                    </a>`;
            }).join('');

            App._setContent(`
                <div class="page-header"><h2>Stories</h2>
                    <p class="page-subtitle">${stories.length} stories in archive</p>
                </div>
                <div class="story-card-grid">${cards}</div>`);
        } catch (err) {
            App._setContent(`<div class="error-state"><h3>Error loading stories</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* ── 2. Story Detail Page ────────────────────────────────── */
    /* renderStoryDetail (the tabbed story page) was deleted in 4.5.0 — the
     * story board (story_board.js) is the one story page. The chart renderer
     * and the two Update pushers below are what it still calls. */

    /* ── Comparison chart renderer ───────────────────────────
     * Builds a Chart.js line chart with one dataset per publication.
     * Uses the per-pub snapshots that get_story_detail now returns
     * (last 30d). Reuses the global Chart.js instance loaded by
     * index.html — no need for the existing Charts module wrapper
     * since this is a one-off and we want full control over the
     * legend / tooltip shape.
     */
    _renderComparisonChart(pubsWithData) {
        const canvas = document.getElementById('story-comparison-chart');
        if (!canvas || typeof Chart === 'undefined') return;

        // Destroy any existing chart on this canvas (route() doesn't
        // clean up posting.js charts the way it does for the rest of
        // the app, so we manage our own lifecycle).
        if (canvas._ppChart) {
            try { canvas._ppChart.destroy(); } catch (e) {}
        }

        const _pubAccent = (getComputedStyle(document.documentElement)
            .getPropertyValue('--accent') || '').trim() || '#9a5b34';
        const datasets = pubsWithData.map((p, i) => {
            const color = i === 0 ? _pubAccent : PUB_CHART_COLORS[i % PUB_CHART_COLORS.length];
            const platLabel = (PLATFORM_LABELS[p.platform] || p.platform).replace(/^.+\s/, '');
            return {
                label: platLabel,
                data: (p.snapshots || []).map(s => ({ x: s.t, y: s.v })),
                borderColor: color,
                backgroundColor: color + '33',     // ~20% alpha
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                tension: 0.25,
                fill: false,
            };
        });

        // Read CSS custom properties so the chart matches dark/light theme.
        const styles = getComputedStyle(document.documentElement);
        const textMuted = styles.getPropertyValue('--text-muted').trim() || '#888';
        const border = styles.getPropertyValue('--border').trim() || '#333';

        canvas._ppChart = new Chart(canvas, {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'nearest', intersect: false },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: { color: textMuted, boxWidth: 12, font: { size: 11 } },
                    },
                    tooltip: {
                        callbacks: {
                            title: (items) => items[0]?.parsed?.x
                                ? new Date(items[0].parsed.x).toLocaleString('en-AU')
                                : '',
                            label: (item) => `${item.dataset.label}: ${item.parsed.y.toLocaleString()}`,
                        },
                    },
                },
                scales: {
                    x: {
                        type: 'time',
                        time: { tooltipFormat: 'dd MMM yyyy HH:mm' },
                        grid: { color: border, drawBorder: false },
                        ticks: { color: textMuted, font: { size: 10 }, maxRotation: 0 },
                    },
                    y: {
                        beginAtZero: false,
                        grid: { color: border, drawBorder: false },
                        ticks: { color: textMuted, font: { size: 10 } },
                    },
                },
            },
        });
    },

    async _updateSingle(storyName, platform, chapterIndex) {
        if (!confirm(`Push update for ${storyName.replace(/_/g, ' ')} on ${PLATFORM_LABELS[platform] || platform}?`)) return;
        try {
            await API.updateStory({ story_name: storyName, platforms: [platform], chapters: [chapterIndex], confirm_live: true });
            alert('Update sent!');
            if (window.StoryBoard) StoryBoard.render(storyName);
        } catch (err) {
            alert('Update failed: ' + err.message);
        }
    },

    async _updateAll(storyName) {
        if (!confirm(`Push updates for ALL ${storyName.replace(/_/g, ' ')} publications?`)) return;
        try {
            await API.updateStory({ story_name: storyName, confirm_live: true });
            alert('Updates sent!');
            if (window.StoryBoard) StoryBoard.render(storyName);
        } catch (err) {
            alert('Update failed: ' + err.message);
        }
    },

    /* ── 3. Queue Page ───────────────────────────────────────── */
    /* A stored scheduled_at is UTC 'YYYY-MM-DD HH:MM:SS'. Turn it into a real
     * instant for display / for a datetime-local input's LOCAL value. */
    _schedInstant(utcStr) {
        return new Date((utcStr || '').replace(' ', 'T') + 'Z');
    },
    _toLocalInput(utcStr) {
        const d = this._schedInstant(utcStr);
        if (isNaN(d.getTime())) return '';
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
            `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },

    async renderQueue() {
        App._setContent('<div class="page-header"><h2>Queue &amp; Schedule</h2></div><div class="loading">Loading queue...</div>');
        try {
            // content_type omitted → the endpoint returns stories, artwork AND posts.
            const { queue } = await API.getPostingQueue({ include_completed: true });
            this._queueData = queue;
            // Counted server-side rather than from `queue`, so the number in the
            // button is the number the DELETE will actually match.
            this._clearable = await API.getClearableQueue().catch(() => null);
            if (!this._queueView) this._queueView = 'list';
            this._paintQueue();
        } catch (err) {
            App._setContent(`<div class="error-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* Paint the Queue & Schedule page in the chosen view (list | calendar) from
     * the cached queue data, so toggling views doesn't refetch. */
    /* Does this install send scheduled items on time? A server always does; a desktop
     * only while it is open. Runtime mode is cached on App by the sidebar version check;
     * until it is known we assume desktop, the honest default. (4.11.0) */
    _sendsOnTime() {
        return App._runtimeMode === 'server';
    },

    _isOverdue(item) {
        if (!item || item.status !== 'pending' || !item.scheduled_at) return false;
        const t = this._schedInstant(item.scheduled_at);
        return t instanceof Date && !isNaN(t) && t.getTime() < Date.now();
    },

    _paintQueue() {
        const queue = this._queueData || [];
        if (!queue.length) {
            App._setContent(`
                <div class="page-header"><h2>Queue &amp; Schedule</h2></div>
                <div class="empty-state"><h3>Nothing queued</h3><p>Schedule a story, artwork or post, or upload one, and it shows up here.</p></div>`);
            return;
        }
        const view = this._queueView;
        const scheduled = queue.filter(q => q.status === 'pending' && q.scheduled_at);
        // Honest wording (HOSTFREE rung 1, 4.11.0): a server sends on time; a desktop only
        // sends while it is open, and catches up within a minute of the next launch.
        const overdue = scheduled.filter(q => this._isOverdue(q)).length;
        const rule = this._sendsOnTime()
            ? 'The server sends them on time.'
            : 'They go out at their time while PawPoller is open &mdash; otherwise the next time you open it.';
        const overdueNote = overdue ? ` <strong>${overdue} overdue</strong> &mdash; sending at the next check.` : '';
        const schedNote = scheduled.length
            ? `<p class="page-subtitle">${scheduled.length} scheduled &middot; ${queue.length} total. ${rule}${overdueNote}</p>`
            : `<p class="page-subtitle">${queue.length} items</p>`;
        const toggle = `<div class="q-viewtoggle" style="display:inline-flex;gap:.3rem;flex-shrink:0;">
            <button class="btn btn-sm${view === 'list' ? ' btn-primary' : ' btn-outline'}" data-q-view="list">&#9776; List</button>
            <button class="btn btn-sm${view === 'calendar' ? ' btn-primary' : ' btn-outline'}" data-q-view="calendar">&#128197; Calendar</button>
        </div>`;
        const body = view === 'calendar' ? this._renderQueueCalendar(queue) : this._renderQueueList(queue);
        const clearBtn = this._clearFinishedButton();

        App._setContent(`
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div><h2>Queue &amp; Schedule</h2>${schedNote}</div>
                <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;">${clearBtn}${toggle}</div>
            </div>
            ${body}`);

        document.querySelectorAll('[data-q-view]').forEach(b =>
            b.addEventListener('click', () => { this._queueView = b.dataset.qView; this._paintQueue(); }));
        this._wireClearFinished();
        if (view === 'calendar') this._wireQueueCalendar();
        else this._wireQueueActions();
    },

    /* "Clear finished" — deletes rows the queue has already given up on.
     *
     * Exists because one dead DeviantArt token accumulated ~4,400 of them
     * through the retry bug fixed in 3.21.0. They are inert, but the Queue page
     * loads every row, so real work sits under thousands of corpses. 3.21.0
     * stopped them being created; this clears the ones already there — as a
     * button rather than a migration, because deleting someone's rows is their
     * call to make and to repeat.
     *
     * Hidden when there is nothing to clear, so it never invites a click that
     * does nothing. */
    _clearFinishedButton() {
        const c = this._clearable;
        if (!c || !c.total) return '';
        const n = c.total.toLocaleString();
        return `<button class="btn btn-sm btn-outline" data-q-clear title="Delete finished queue rows (failed, cancelled, completed). Scheduled and in-progress work is never touched.">&#128465; Clear ${n} finished</button>`;
    },

    _wireClearFinished() {
        const btn = document.querySelector('[data-q-clear]');
        if (!btn) return;
        btn.addEventListener('click', async () => {
            const c = this._clearable || {};
            const byStatus = Object.entries(c.by_status || {})
                .map(([s, n]) => `  ${n.toLocaleString()} ${s}`).join('\n');
            const byPlatform = Object.entries(c.by_platform || {}).slice(0, 6)
                .map(([p, n]) => `  ${p}: ${n.toLocaleString()}`).join('\n');
            const msg = `Delete ${(c.total || 0).toLocaleString()} finished queue rows?\n\n`
                + `${byStatus}\n\n`
                + (byPlatform ? `Failed rows by platform:\n${byPlatform}\n\n` : '')
                + `Scheduled and in-progress items are NOT touched.\n`
                + `This cannot be undone.`;
            if (!confirm(msg)) return;
            btn.disabled = true;
            btn.textContent = 'Clearing...';
            try {
                const r = await API.clearPostingQueue(['failed', 'cancelled', 'completed']);
                // posting.js has no toast helper — this file reports success by
                // re-rendering and failure with alert(). Keep to that; inventing
                // App.showToast() here is how `this._toast` shipped undefined.
                await this.renderQueue();
                alert(`Cleared ${(r.cleared || 0).toLocaleString()} finished queue rows.`);
            } catch (err) {
                btn.disabled = false;
                btn.textContent = 'Clear finished';
                alert(`Could not clear the queue: ${err.message}`);
            }
        });
    },

    _renderQueueList(queue) {
        // Scheduled-for-the-future items first (soonest first), then the rest.
        const isSched = q => q.status === 'pending' && q.scheduled_at;
        const scheduled = queue.filter(isSched)
            .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
        const rest = queue.filter(q => !isSched(q));
        const ordered = scheduled.concat(rest);

        const rowHtml = (item) => {
                const ct = item.content_type || 'story';
                const isArt = ct === 'artwork';
                const isPost = ct === 'post';
                let href, name, typeIcon, chap;
                if (isPost) {
                    // story_name is a bare post_id; the readable label rides in title_override.
                    href = '#/posts';
                    name = Utils.escapeHtml(item.title_override || `Post #${item.story_name}`);
                    typeIcon = '&#128172;';            // 💬
                    chap = '&mdash;';
                } else if (isArt) {
                    href = `#/artwork/image/${encodeURIComponent(item.story_name)}`;
                    name = Utils.escapeHtml((item.story_name || '').replace(/_/g, ' '));
                    typeIcon = '&#128444;&#65039;';    // 🖼️
                    chap = '&mdash;';
                } else {
                    href = `#/posting/story/${encodeURIComponent(item.story_name)}`;
                    name = Utils.escapeHtml((item.story_name || '').replace(/_/g, ' '));
                    // Drip rows (gap G1) carry their "💧 drip i/N" label in
                    // title_override — display-only (the scheduler never passes
                    // story-row overrides into the actual post).
                    if (item.drip_group && item.title_override) {
                        name += ` <span class="muted" style="font-size:.85em">${Utils.escapeHtml(item.title_override)}</span>`;
                    }
                    typeIcon = '&#128214;';            // 📖
                    chap = (item.chapter_index || 'Full');
                }
                const whenLabel = item.scheduled_at
                    ? Utils.escapeHtml(this._schedInstant(item.scheduled_at).toLocaleString())
                    : '<span class="muted">Immediate</span>';
                const pending = item.status === 'pending';
                const isOverdue = this._isOverdue(item);
                const statusBadge = isOverdue
                    ? `<span class="status-badge status-overdue" title="${this._sendsOnTime() ? 'Sending at the next check' : 'Its time passed while PawPoller was closed; it sends at the next check'}">overdue</span>`
                    : `<span class="status-badge status-${item.status}">${item.status}</span>`;
                let actions = '';
                if (pending && item.scheduled_at) {
                    actions += `<button class="btn btn-sm btn-outline" data-q-resched="${item.queue_id}">Reschedule</button> `;
                }
                if (pending) {
                    actions += `<button class="btn btn-sm btn-danger" data-q-cancel="${item.queue_id}">Cancel</button>`;
                }
                // Drip rows (gap G1): one extra action that cancels the whole
                // campaign — every row sharing this drip_group.
                if (pending && item.drip_group) {
                    actions += ` <button class="btn btn-sm btn-outline" data-q-dripcancel="${Utils.escapeHtml(item.drip_group)}"
                        title="Cancel every item in this drip">💧 Cancel drip</button>`;
                }
                return `
                <tr data-q-row="${item.queue_id}">
                    <td data-label="Type">${typeIcon}</td>
                    <td data-label="Item"><a href="${href}">${name}</a></td>
                    <td data-label="Ch">${chap}</td>
                    <td data-label="Platform">${PLATFORM_LABELS[item.platform] || item.platform}</td>
                    <td data-label="Action">${item.action}</td>
                    <td data-label="When" class="q-when">
                        <span class="q-when-label">${whenLabel}</span>
                        <span class="q-when-edit" style="display:none">
                            <input type="datetime-local" class="q-when-input" value="${this._toLocalInput(item.scheduled_at)}">
                            <button class="btn btn-xs btn-primary" data-q-save="${item.queue_id}">Save</button>
                            <button class="btn btn-xs btn-outline" data-q-editcancel="${item.queue_id}">&times;</button>
                        </span>
                    </td>
                    <td data-label="Status">${statusBadge}</td>
                    <td data-label="Actions">${actions}</td>
                </tr>`;
            };

        return `
            <div class="card">
                <table class="data-table" data-mobile-cards>
                    <thead><tr>
                        <th>Type</th><th>Item</th><th>Ch</th><th>Platform</th><th>Action</th>
                        <th>When</th><th>Status</th><th>Actions</th>
                    </tr></thead>
                    <tbody>${ordered.map(rowHtml).join('')}</tbody>
                </table>
            </div>`;
    },

    /* Read-only month calendar of what's scheduled (backlog Z completion). Lays
     * the pending scheduled items onto their local-time days; ‹ › page months,
     * click an item to jump to its detail. Drag-to-reschedule is deliberately
     * out of scope — reschedule lives in the List view's inline editor. */
    _renderQueueCalendar(queue) {
        const items = queue
            .filter(q => q.status === 'pending' && q.scheduled_at)
            .map(q => ({ ...q, _dt: this._schedInstant(q.scheduled_at) }))
            .filter(q => !isNaN(q._dt.getTime()))
            .sort((a, b) => a._dt - b._dt);

        if (!this._calMonth) {
            const anchor = items.length ? items[0]._dt : new Date();
            this._calMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
        }
        const y = this._calMonth.getFullYear(), m = this._calMonth.getMonth();
        const key = d => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
        const byDay = {};
        for (const it of items) { const k = key(it._dt); (byDay[k] = byDay[k] || []).push(it); }

        const firstDow = new Date(y, m, 1).getDay();          // 0 = Sunday
        const daysInMonth = new Date(y, m + 1, 0).getDate();
        const todayKey = key(new Date());
        const cells = [];
        for (let i = 0; i < firstDow; i++) cells.push('<div class="qcal-cell qcal-empty"></div>');
        for (let d = 1; d <= daysInMonth; d++) {
            const dayItems = byDay[`${y}-${m}-${d}`] || [];
            const chips = dayItems.map(it => {
                const ct = it.content_type || 'story';
                const icon = ct === 'post' ? '&#128172;' : (ct === 'artwork' ? '&#128444;&#65039;' : '&#128214;');
                const time = it._dt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
                const label = ct === 'post'
                    ? (it.title_override || ('Post #' + it.story_name))
                    : (it.story_name || '').replace(/_/g, ' ');
                const plat = PLATFORM_LABELS[it.platform] || it.platform;
                const tip = `${label} → ${plat} at ${it._dt.toLocaleString()}`;
                return `<div class="qcal-item" data-q-goto="${it.queue_id}" title="${Utils.escapeHtml(tip)}">`
                    + `${icon} ${Utils.escapeHtml(time)} ${Utils.escapeHtml(plat)}</div>`;
            }).join('');
            cells.push(`<div class="qcal-cell${(`${y}-${m}-${d}`) === todayKey ? ' qcal-today' : ''}">`
                + `<div class="qcal-daynum">${d}</div>${chips}</div>`);
        }

        const monthName = this._calMonth.toLocaleString([], { month: 'long', year: 'numeric' });
        const dow = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
            .map(x => `<div class="qcal-dow">${x}</div>`).join('');
        return `
            <div class="card qcal-card">
                <div class="qcal-head">
                    <button class="btn btn-sm btn-outline" data-cal-nav="-1" aria-label="Previous month">&lsaquo;</button>
                    <strong>${Utils.escapeHtml(monthName)}</strong>
                    <button class="btn btn-sm btn-outline" data-cal-nav="1" aria-label="Next month">&rsaquo;</button>
                </div>
                <div class="qcal-grid">${dow}${cells.join('')}</div>
                ${items.length ? '' : '<p class="muted" style="margin-top:.6rem;">Nothing scheduled yet — schedule a story, artwork or post and it lands here.</p>'}
            </div>`;
    },

    _wireQueueCalendar() {
        document.querySelectorAll('[data-cal-nav]').forEach(b =>
            b.addEventListener('click', () => {
                const delta = parseInt(b.dataset.calNav, 10) || 0;
                this._calMonth = new Date(this._calMonth.getFullYear(), this._calMonth.getMonth() + delta, 1);
                this._paintQueue();
            }));
        document.querySelectorAll('[data-q-goto]').forEach(el =>
            el.addEventListener('click', () => {
                const it = (this._queueData || []).find(q => q.queue_id === Number(el.dataset.qGoto));
                if (!it) return;
                const ct = it.content_type || 'story';
                if (ct === 'post') window.location.hash = '#/posts';
                else if (ct === 'artwork') window.location.hash = `#/artwork/image/${encodeURIComponent(it.story_name)}`;
                else window.location.hash = `#/posting/story/${encodeURIComponent(it.story_name)}`;
            }));
    },

    /* Wire the queue table's cancel / reschedule controls. Local listeners
     * (not the global data-post-action delegation) so the inline reschedule
     * editor can toggle within its own row. */
    _wireQueueActions() {
        document.querySelectorAll('[data-q-cancel]').forEach(btn =>
            btn.addEventListener('click', () => this._cancelQueue(Number(btn.dataset.qCancel))));
        // Drip group cancel (gap G1) — one click cancels the whole campaign.
        document.querySelectorAll('[data-q-dripcancel]').forEach(btn =>
            btn.addEventListener('click', async () => {
                if (!confirm('Cancel EVERY item in this drip?')) return;
                try {
                    const resp = await fetch(`/api/posting/drip/${encodeURIComponent(btn.dataset.qDripcancel)}`,
                        { method: 'DELETE' });
                    const data = await resp.json();
                    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
                    if (window.toast) window.toast.success(`Drip cancelled (${data.cancelled} items)`);
                    this.renderQueue();
                } catch (err) {
                    alert('Cancel drip failed: ' + (err.message || err));
                }
            }));

        const rowOf = id => document.querySelector(`[data-q-row="${id}"]`);
        const toggleEdit = (id, on) => {
            const row = rowOf(id);
            if (!row) return;
            const label = row.querySelector('.q-when-label');
            const edit = row.querySelector('.q-when-edit');
            if (label) label.style.display = on ? 'none' : '';
            if (edit) edit.style.display = on ? '' : 'none';
        };
        document.querySelectorAll('[data-q-resched]').forEach(btn =>
            btn.addEventListener('click', () => toggleEdit(btn.dataset.qResched, true)));
        document.querySelectorAll('[data-q-editcancel]').forEach(btn =>
            btn.addEventListener('click', () => toggleEdit(btn.dataset.qEditcancel, false)));
        document.querySelectorAll('[data-q-save]').forEach(btn =>
            btn.addEventListener('click', () => this._rescheduleQueue(Number(btn.dataset.qSave))));
    },

    async _cancelQueue(queueId) {
        if (!confirm('Cancel this queue item?')) return;
        try {
            await API.cancelPostingQueue(queueId);
            this.renderQueue();
        } catch (err) {
            alert('Cancel failed: ' + err.message);
        }
    },

    async _rescheduleQueue(queueId) {
        const row = document.querySelector(`[data-q-row="${queueId}"]`);
        const input = row && row.querySelector('.q-when-input');
        const val = input && input.value;
        if (!val) { alert('Pick a date and time.'); return; }
        const when = new Date(val);
        if (isNaN(when.getTime())) { alert('Invalid date/time.'); return; }
        if (when.getTime() < Date.now()) { alert('Pick a time in the future.'); return; }
        try {
            // toISOString() converts the LOCAL picker value to a UTC instant.
            await API.reschedulePostingQueue(queueId, { scheduled_at: when.toISOString() });
            if (window.toast) window.toast.success(`Rescheduled for ${when.toLocaleString()}`);
            this.renderQueue();
        } catch (err) {
            alert('Reschedule failed: ' + err.message);
        }
    },

    /* ── 4. Published (redirects to Stories hub) ─────────────── */
    async renderPublished() {
        // Redirect to the stories hub — publications are now shown per-story
        window.location.hash = '#/library/type/story';
    },

    /* ── 5. History / Log Page ───────────────────────────────── */
    async renderLog() {
        App._setContent('<div class="page-header"><h2>Posting History</h2></div><div class="loading">Loading log...</div>');

        try {
            const { log } = await API.getPostingLog({ limit: 100 });
            if (!log.length) {
                App._setContent(`
                    <div class="page-header"><h2>Posting History</h2></div>
                    <div class="empty-state"><h3>No posting activity yet</h3></div>`);
                return;
            }

            const rows = log.map(entry => {
                const ch = entry.chapter_index > 0 ? `Ch${entry.chapter_index}` : 'Full';
                const statusClass = entry.status === 'success' ? 'status-posted' : 'status-failed';
                const link = entry.external_url
                    ? `<a href="${Utils.escapeHtml(entry.external_url)}" target="_blank">Link</a>` : '';
                const error = entry.error_message
                    ? `<span class="error-text" title="${Utils.escapeHtml(entry.error_message)}">&#9888;</span>` : '';
                const dur = entry.duration_seconds ? `${entry.duration_seconds.toFixed(1)}s` : '';
                return `<tr>
                    <td data-label="Time">${Utils.escapeHtml(entry.created_at || '')}</td>
                    <td data-label="Story"><a href="#/posting/story/${Utils.escapeHtml(entry.story_name)}">${Utils.escapeHtml((entry.story_name || '').replace(/_/g, ' '))}</a></td>
                    <td data-label="Platform">${PLATFORM_LABELS[entry.platform] || entry.platform}</td>
                    <td data-label="Action">${entry.action}</td>
                    <td data-label="Status"><span class="status-badge ${statusClass}">${entry.status}</span></td>
                    <td data-label="">${link} ${error}</td>
                    <td data-label="Duration">${dur}</td>
                </tr>`;
            }).join('');

            App._setContent(`
                <div class="page-header"><h2>Posting History</h2>
                    <p class="page-subtitle">${log.length} entries</p>
                </div>
                <div class="card">
                    <table class="data-table" data-mobile-cards>
                        <thead><tr>
                            <th>Time</th><th>Story</th><th>Platform</th>
                            <th>Action</th><th>Status</th><th>Details</th><th>Duration</th>
                        </tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>`);
        } catch (err) {
            App._setContent(`<div class="error-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },
};
