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
    async renderStoryDetail(storyName) {
        App._setContent('<div class="loading">Loading story...</div>');

        try {
            const data = await API.getPostingStory(storyName);
            const title = Utils.escapeHtml(data.title || storyName.replace(/_/g, ' '));

            // ── Cover image ────────────────────────────────────
            // Same /api/posting/image route + encodeURIComponent dance as the
            // listing card. Auto-detect fallback now lives on the backend so
            // stories without images.cover in story.json still render here.
            const coverSrc = data.images && data.images.cover
                ? `/api/posting/image?story=${encodeURIComponent(storyName)}&file=${encodeURIComponent(data.images.cover)}`
                : '';
            const coverHtml = coverSrc
                ? `<div class="story-detail-cover" style="background-image:url('${coverSrc}')"></div>`
                : '';

            // ── Characters & relationships chips ───────────────
            const chipChars = (data.characters || []).map(c =>
                `<span class="chip chip-character">${Utils.escapeHtml(c)}</span>`
            ).join('');
            const chipRels = (data.relationships || []).map(r =>
                `<span class="chip chip-relationship">${Utils.escapeHtml(r)}</span>`
            ).join('');
            const chipsHtml = (chipChars || chipRels)
                ? `<div class="story-detail-chips">${chipChars}${chipRels}</div>`
                : '';

            // ── Summary (longer OTW-style blurb) ───────────────
            // Only render if it's actually different from the description —
            // story.json sometimes mirrors them and we don't want a duplicate.
            const summary = (data.summary || '').trim();
            const description = (data.description || '').trim();
            const summaryHtml = (summary && summary !== description)
                ? `<div class="story-detail-summary"><strong>Summary:</strong> ${Utils.escapeHtml(summary)}</div>`
                : '';

            // ── Info card ──────────────────────────────────────
            const infoHtml = `
                <div class="story-detail-info card${coverHtml ? ' story-detail-info--hascover' : ''}">
                    ${coverHtml}
                    <div class="story-detail-info-body">
                        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
                            <h2 style="margin:0">${title}</h2>
                            <a href="#/editor/${Utils.escapeHtml(storyName)}" class="btn btn-sm btn-outline">Edit in Editor</a>
                        </div>
                        <p class="page-subtitle">by ${Utils.escapeHtml(data.author || 'Unknown')}</p>
                        <div class="story-detail-meta">
                            <span>${(data.total_words || 0).toLocaleString()} words</span>
                            <span>${data.total_chapters || 0} chapters</span>
                            ${data.rating ? `<span class="story-rating rating-${data.rating}">${data.rating}</span>` : ''}
                            ${data.category ? `<span>${Utils.escapeHtml(data.category)}</span>` : ''}
                            ${data.fandom ? `<span>${Utils.escapeHtml(data.fandom)}</span>` : ''}
                        </div>
                        ${data.warnings && data.warnings.length ? `<p class="story-warnings">⚠ ${Utils.escapeHtml(data.warnings.join(', '))}</p>` : ''}
                        ${chipsHtml}
                        <p class="story-detail-desc">${Utils.escapeHtml(description)}</p>
                        ${summaryHtml}
                    </div>
                </div>`;

            // ── Pending queue callout ──────────────────────────
            // Top-of-page banner for any in-flight or scheduled work, since
            // this is the most actionable thing on the page when present.
            const pendingQueue = data.pending_queue || [];
            let pendingHtml = '';
            if (pendingQueue.length > 0) {
                const items = pendingQueue.map(q => {
                    const emoji = PLATFORM_EMOJI[q.platform] || '📦';
                    const ch = q.chapter_index > 0 ? `Ch${q.chapter_index}` : 'Full';
                    const sched = q.scheduled_at
                        ? `scheduled for ${Utils.escapeHtml(this._schedInstant(q.scheduled_at).toLocaleString())}`
                        : 'next scheduler tick';
                    const status = Utils.escapeHtml(q.status || 'pending');
                    return `<li>${emoji} <strong>${Utils.escapeHtml(q.action)}</strong> ${ch} → ${Utils.escapeHtml((PLATFORM_LABELS[q.platform] || q.platform).replace(/^.+\s/, ''))} <span class="muted">(${status}, ${sched})</span></li>`;
                }).join('');
                pendingHtml = `
                    <div class="card pending-queue-card">
                        <h3>🕐 Pending (${pendingQueue.length})</h3>
                        <ul class="pending-queue-list">${items}</ul>
                    </div>`;
            }

            // ── Cross-platform totals strip ────────────────────
            // Sum across all publications. Each platform names stats
            // differently (views/hits/reads, favorites_count/kudos/votes), so
            // we resolve per-row before summing — same convention used by
            // the per-pub stats line below.
            const pubs = data.publications || [];
            let totalViews = 0, totalFaves = 0, totalComments = 0;
            for (const p of pubs) {
                if (!p.stats) continue;
                totalViews    += p.stats.views || p.stats.hits || p.stats.reads || 0;
                totalFaves    += p.stats.favorites_count || p.stats.kudos || p.stats.votes || 0;
                totalComments += p.stats.comments_count || 0;
            }
            const totalsHtml = pubs.length > 0 ? `
                <div class="card totals-strip">
                    <div class="totals-stat"><span class="totals-value">${totalViews.toLocaleString()}</span><span class="totals-label">total views</span></div>
                    <div class="totals-stat"><span class="totals-value">${totalFaves.toLocaleString()}</span><span class="totals-label">total faves</span></div>
                    <div class="totals-stat"><span class="totals-value">${totalComments.toLocaleString()}</span><span class="totals-label">total comments</span></div>
                    <div class="totals-stat"><span class="totals-value">${pubs.length}</span><span class="totals-label">platform${pubs.length === 1 ? '' : 's'}</span></div>
                </div>` : '';

            // ── Platforms section ──────────────────────────────
            const unpublished = data.unpublished_platforms || [];

            // Find the best-performing pub by views (or views-equivalent).
            // Used to add a 👑 badge to that row. Single-pub stories don't
            // get a badge — best-of-one is meaningless.
            let bestPubKey = null;
            let bestPubViews = -1;
            if (pubs.length > 1) {
                for (const p of pubs) {
                    if (!p.stats) continue;
                    const v = p.stats.views || p.stats.hits || p.stats.reads || 0;
                    if (v > bestPubViews) {
                        bestPubViews = v;
                        bestPubKey = `${p.platform}-${p.chapter_index}`;
                    }
                }
            }

            let pubRows = '';
            if (pubs.length > 0) {
                pubRows = pubs.map(p => {
                    const emoji = PLATFORM_EMOJI[p.platform] || '📦';
                    const ch = p.chapter_index > 0 ? `Ch${p.chapter_index}` : 'Full';
                    const link = p.external_url
                        ? `<a href="${Utils.escapeHtml(p.external_url)}" target="_blank">View</a>` : '';
                    let statsHtml = '';
                    if (p.stats) {
                        const v = p.stats.views || p.stats.hits || p.stats.reads || 0;
                        const f = p.stats.favorites_count || p.stats.kudos || p.stats.votes || 0;
                        const c = p.stats.comments_count || 0;
                        statsHtml = `<span class="pub-stats">${v.toLocaleString()}v / ${f.toLocaleString()}f / ${c.toLocaleString()}c</span>`;
                    }
                    // Sparkline from per-pub snapshots fetched server-side.
                    const sparklineHtml = buildSparkline(p.snapshots || []);
                    // 👑 best-performer badge (only renders on the row with
                    // the highest views and only when there are 2+ pubs).
                    const isBest = `${p.platform}-${p.chapter_index}` === bestPubKey;
                    const bestBadge = isBest
                        ? `<span class="best-badge" title="Best performing of ${pubs.length} platforms">👑</span>`
                        : '';
                    // Days-since timestamp with raw value on hover. Falls back
                    // to first_posted_at if no edit has happened yet.
                    const updated = p.last_updated_at || p.first_posted_at || '';
                    const updatedAgo = updated ? Utils.timeAgo(updated) : '';
                    const updatedTitle = updated ? Utils.escapeHtml(updated) : '';
                    // Update count badge — only render when there's been at
                    // least one edit since the original post.
                    const updateCount = p.update_count || 0;
                    const updateBadge = updateCount > 0
                        ? `<span class="update-count-badge" title="${updateCount} update${updateCount === 1 ? '' : 's'} since first post">↻ ${updateCount}</span>`
                        : '';
                    // Change-detection badge. Backend hashes the current local
                    // file and compares against publications.file_hash. Four
                    // statuses; we only render the visible badge for changed /
                    // file_missing / no_hash since "unchanged" is the silent
                    // default.
                    let changeBadge = '';
                    if (p.change_status === 'changed') {
                        changeBadge = `<span class="change-badge change-drift" title="Local file has drifted from the last upload — push an update">⚠ drifted</span>`;
                    } else if (p.change_status === 'file_missing') {
                        changeBadge = `<span class="change-badge change-missing" title="Local format file is missing — can't compare">? missing</span>`;
                    } else if (p.change_status === 'no_hash') {
                        changeBadge = `<span class="change-badge change-unknown" title="No hash recorded — likely claimed retroactively from a manual upload">? no hash</span>`;
                    }
                    // Top fans inline (IB-only). Limited to first 5 names from
                    // faving_users; full list still available via the
                    // submission detail page.
                    const topFans = p.top_fans || [];
                    const fansHtml = topFans.length > 0
                        ? `<div class="pub-fans">Top fans: ${topFans.map(f => `<span class="fan-chip">${Utils.escapeHtml(f.username)}</span>`).join(' ')}</div>`
                        : '';
                    return `
                        <div class="pub-row-wrapper">
                            <div class="pub-row">
                                <span class="pub-platform">${bestBadge}${emoji} ${(PLATFORM_LABELS[p.platform] || p.platform).replace(/^.+\s/, '')}</span>
                                <span class="pub-chapter">${ch}</span>
                                ${statsHtml}
                                <span class="pub-spark">${sparklineHtml}</span>
                                <span class="pub-date" title="${updatedTitle}">${updatedAgo}</span>
                                ${updateBadge}
                                ${changeBadge}
                                <span class="pub-actions">${link}
                                    <button class="btn btn-sm btn-secondary" data-post-action="update-single" data-post-story="${Utils.escapeHtml(storyName)}" data-post-platform="${p.platform}" data-post-chapter="${p.chapter_index}">Update</button>
                                </span>
                            </div>
                            ${fansHtml}
                        </div>`;
                }).join('');
            }

            // Sites the story is not on yet: named, but with no publish buttons
            // here (4.2.0, spec §7.6 / §10 Q5). Those buttons posted as the
            // platform DEFAULT account with no picker and no confirmation —
            // on a multi-persona install, possibly the wrong persona. Publishing
            // lives in the editor's Publish Check, which has the persona picker,
            // the dialog and per-site results.
            let uploadHtml = '';
            if (unpublished.length > 0) {
                const names = unpublished.map(p =>
                    `${PLATFORM_EMOJI[p] || '📦'} ${Utils.escapeHtml((PLATFORM_LABELS[p] || p).replace(/^.+\s/, ''))}`).join(' · ');
                uploadHtml = `<div class="upload-actions muted" style="font-size:.85rem">Not yet on ${names} —
                    <a href="#/editor/${Utils.escapeHtml(storyName)}">publish from the editor ↗</a></div>`;
            }

            // Smarter "Update All" label: when change detection knows some
            // publications have drifted (local file changed since upload),
            // surface the count so the button communicates intent
            // ("Update Drifted (3)") rather than hiding it behind a generic label.
            const driftedCount = pubs.filter(p => p.change_detected).length;
            const updateAllLabel = driftedCount > 0
                ? `Update Drifted (${driftedCount})`
                : 'Update All';
            const updateAllClass = driftedCount > 0
                ? 'btn btn-sm btn-primary'
                : 'btn btn-sm btn-secondary';

            const platformsHtml = `
                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.75rem">
                        <h3 style="margin:0">Platforms</h3>
                        ${pubs.length > 0 ? `<button class="${updateAllClass}" data-post-action="update-all" data-post-story="${Utils.escapeHtml(storyName)}">${updateAllLabel}</button>` : ''}
                    </div>
                    ${pubRows || '<p class="page-subtitle">Not published anywhere yet.</p>'}
                    ${uploadHtml}
                </div>`;

            // ── Chapters section with descriptions ─────────────
            let chaptersHtml = '';
            if (data.chapters && data.chapters.length > 0) {
                const chRows = data.chapters.map(ch => {
                    const desc = (ch.description || '').trim();
                    const descHtml = desc
                        ? `<div class="chapter-desc">${Utils.escapeHtml(desc)}</div>`
                        : '';
                    return `
                    <div class="chapter-entry">
                        <div class="chapter-row">
                            <span class="chapter-num">Ch${ch.index}</span>
                            <span class="chapter-title">${Utils.escapeHtml(ch.title)}</span>
                            <span class="chapter-words">${(ch.word_count || 0).toLocaleString()}w</span>
                        </div>
                        ${descHtml}
                    </div>`;
                }).join('');
                chaptersHtml = `<div class="card"><h3>Chapters</h3>${chRows}</div>`;
            }

            // ── Per-platform tags accordion ────────────────────
            // Native <details> elements — collapsed by default since some
            // platforms (IB, FA) carry 100+ tags and would otherwise dominate
            // the page. Sorted by tag count desc so the richest list opens first.
            const tagsByPlatform = data.tags_by_platform || {};
            const tagPlatformEntries = Object.entries(tagsByPlatform)
                .filter(([_, tags]) => Array.isArray(tags) && tags.length > 0)
                .sort((a, b) => b[1].length - a[1].length);
            let tagsHtml = '';
            if (tagPlatformEntries.length > 0) {
                const tagBlocks = tagPlatformEntries.map(([plat, tags]) => {
                    const emoji = PLATFORM_EMOJI[plat] || '📦';
                    const label = (PLATFORM_LABELS[plat] || plat).replace(/^.+\s/, '');
                    const tagSpans = tags.map(t =>
                        `<span class="tag-pill">${Utils.escapeHtml(t)}</span>`
                    ).join('');
                    return `
                        <details class="tags-platform">
                            <summary>${emoji} ${Utils.escapeHtml(label)} <span class="tag-count">(${tags.length})</span></summary>
                            <div class="tag-list">${tagSpans}</div>
                        </details>`;
                }).join('');
                tagsHtml = `<div class="card"><h3>Tags by Platform</h3>${tagBlocks}</div>`;
            }

            // ── Comparison overlay chart ───────────────────────
            // One Chart.js line chart with all pubs overlaid. Only renders
            // when there are 2+ pubs AND at least one of them has snapshot
            // data — single-pub stories already have their growth shown by
            // the inline sparkline, no point repeating it bigger.
            const pubsWithData = pubs.filter(p => (p.snapshots || []).length >= 2);
            let comparisonHtml = '';
            if (pubsWithData.length >= 2) {
                comparisonHtml = `
                    <div class="card">
                        <h3>Growth Comparison</h3>
                        <p class="page-subtitle">${pubsWithData.length} platforms over the last 30 days</p>
                        <div class="chart-wrap" style="height:220px">
                            <canvas id="story-comparison-chart"></canvas>
                        </div>
                    </div>`;
            }

            // ── Publication timeline ───────────────────────────
            // Chronological list of post + update events derived from the
            // existing first_posted_at / last_updated_at columns. No new
            // backend data needed. Sorted newest-first because that's what
            // the user generally cares about ("when was the last thing").
            const timelineEvents = [];
            for (const p of pubs) {
                const platLabel = (PLATFORM_LABELS[p.platform] || p.platform).replace(/^.+\s/, '');
                const emoji = PLATFORM_EMOJI[p.platform] || '📦';
                const ch = p.chapter_index > 0 ? `Ch${p.chapter_index}` : 'Full story';
                if (p.first_posted_at) {
                    timelineEvents.push({
                        when: p.first_posted_at,
                        kind: 'post',
                        label: `Posted ${ch} to ${emoji} ${platLabel}`,
                    });
                }
                if (p.last_updated_at && p.last_updated_at !== p.first_posted_at) {
                    timelineEvents.push({
                        when: p.last_updated_at,
                        kind: 'update',
                        label: `Updated ${ch} on ${emoji} ${platLabel} (#${p.update_count || '?'})`,
                    });
                }
            }
            timelineEvents.sort((a, b) => (b.when || '').localeCompare(a.when || ''));
            let timelineHtml = '';
            if (timelineEvents.length > 0) {
                const eventLines = timelineEvents.map(e => {
                    const ago = e.when ? Utils.timeAgo(e.when) : '';
                    const title = e.when ? Utils.escapeHtml(e.when) : '';
                    return `
                        <div class="timeline-event timeline-${e.kind}">
                            <span class="timeline-dot"></span>
                            <span class="timeline-when" title="${title}">${ago}</span>
                            <span class="timeline-label">${e.label}</span>
                        </div>`;
                }).join('');
                timelineHtml = `<div class="card"><h3>Publication Timeline</h3><div class="timeline-list">${eventLines}</div></div>`;
            }

            // ── Recent posting log card ────────────────────────
            // Last 5 posting actions for this story (server-side filtered).
            // Shows the human-readable history of uploads/updates with their
            // success/failure state and any error message.
            const recentLog = data.recent_log || [];
            let recentLogHtml = '';
            if (recentLog.length > 0) {
                const logRows = recentLog.map(entry => {
                    const emoji = PLATFORM_EMOJI[entry.platform] || '📦';
                    const ch = entry.chapter_index > 0 ? `Ch${entry.chapter_index}` : 'Full';
                    const statusClass = entry.status === 'success' ? 'log-success' : 'log-failed';
                    const when = entry.created_at ? Utils.timeAgo(entry.created_at) : '';
                    const whenTitle = entry.created_at ? Utils.escapeHtml(entry.created_at) : '';
                    const errHint = entry.error_message
                        ? `<div class="log-error" title="${Utils.escapeHtml(entry.error_message)}">${Utils.escapeHtml(entry.error_message.substring(0, 80))}${entry.error_message.length > 80 ? '…' : ''}</div>`
                        : '';
                    const link = entry.external_url
                        ? ` · <a href="${Utils.escapeHtml(entry.external_url)}" target="_blank">link</a>`
                        : '';
                    const dur = entry.duration_seconds
                        ? ` · ${entry.duration_seconds.toFixed(1)}s`
                        : '';
                    return `
                        <div class="log-row ${statusClass}">
                            <span class="log-when" title="${whenTitle}">${when}</span>
                            <span class="log-action">${emoji} ${Utils.escapeHtml(entry.action)} ${ch}</span>
                            <span class="log-status">${Utils.escapeHtml(entry.status)}${dur}${link}</span>
                            ${errHint}
                        </div>`;
                }).join('');
                recentLogHtml = `<div class="card"><h3>Recent Activity</h3>${logRows}</div>`;
            }

            // ── Formats card with file metadata + downloads ────
            // The backend now returns formats as an enriched dict:
            //   {key: {available, files: [{path, size, modified}]}}
            // (rather than the old {key: bool} flag dict). For each format
            // we show the file count + total size, and link the badge to
            // /api/posting/file for direct download. Multi-file formats
            // (chapter_bbcode, squidgeworld) link the FIRST file's download
            // and show "(N files)" instead of a single size.
            const formats = data.formats || {};
            const formatBadges = Object.keys(formats).map(fmtKey => {
                const meta = formats[fmtKey] || {};
                const label = fmtKey.replace(/_/g, ' ');
                if (!meta.available || !meta.files || meta.files.length === 0) {
                    // Declared in story.json but no file resolved on disk —
                    // render as a muted, non-clickable badge.
                    return `<span class="format-badge format-empty" title="No files found on disk">${label}</span>`;
                }
                const files = meta.files;
                const first = files[0];
                const sizeText = files.length === 1
                    ? formatFileSize(first.size)
                    : `${files.length} files`;
                const totalSize = files.reduce((sum, f) => sum + (f.size || 0), 0);
                const tooltip = files.length === 1
                    ? `${first.path} · ${formatFileSize(first.size)} · modified ${first.modified}`
                    : `${files.length} files, ${formatFileSize(totalSize)} total. First: ${first.path}`;
                const url = `/api/posting/file?story=${encodeURIComponent(storyName)}&file=${encodeURIComponent(first.path)}`;
                return `<a class="format-badge format-link" href="${url}" title="${Utils.escapeHtml(tooltip)}" download>${label} <span class="format-meta">${sizeText}</span></a>`;
            }).join('');
            // "Download all" button streams the entire story folder
            // (excluding Backups/) as a single zip — handy when you're
            // on a phone and want every format with one tap. The card
            // always renders so this footer button is reachable even
            // when no individual format badges have anything to point
            // at (a brand-new story with only MASTER.md still has
            // something worth zipping).
            const archiveUrl = `/api/posting/archive?story=${encodeURIComponent(storyName)}`;
            const downloadAllHtml = `<a class="btn btn-sm btn-outline" href="${archiveUrl}" download title="Download the entire story folder as a zip (every format + cover + Markdown source)">Download all (zip)</a>`;
            const badgeListHtml = formatBadges
                || '<div style="color:#888;font-style:italic">No format files found yet — try Regenerate from the editor.</div>';
            const formatsHtml = `<div class="card"><h3>Available Formats</h3>
                <div class="format-list">${badgeListHtml}</div>
                <div class="format-actions" style="margin-top:0.75em">${downloadAllHtml}</div>
              </div>`;

            // ── Tabbed secondary sections (2.150.0) ────────────
            // The page used to stack TEN sections vertically — the "endless
            // scroll" complaint. The hero + anything actionable (pending queue)
            // + the headline totals stay always-visible; everything else moves
            // behind tabs, so the page is one screen deep instead of ten.
            // Empty sections are dropped so a story never shows a dead tab.
            // Platforms is first (and therefore the default-open panel) —
            // important, because the comparison chart lives in it and Chart.js
            // needs a VISIBLE canvas to size itself correctly on first render.
            const detTabs = [
                { id: 'platforms', label: 'Platforms', html: platformsHtml + comparisonHtml },
                { id: 'chapters', label: 'Chapters', html: chaptersHtml },
                { id: 'tags', label: 'Tags', html: tagsHtml },
                { id: 'timeline', label: 'Timeline', html: timelineHtml },
                { id: 'history', label: 'History', html: recentLogHtml },
                { id: 'formats', label: 'Formats', html: formatsHtml },
            ].filter(t => (t.html || '').trim());

            const tabBar = `<div class="det-tabs" role="tablist">${detTabs.map((t, i) =>
                `<button type="button" class="det-tab${i === 0 ? ' is-active' : ''}" role="tab"
                    aria-selected="${i === 0}" data-tab="${t.id}">${t.label}</button>`).join('')}</div>`;
            const tabPanels = detTabs.map((t, i) =>
                `<div class="det-panel${i === 0 ? ' is-active' : ''}" role="tabpanel"
                    data-panel="${t.id}">${t.html}</div>`).join('');

            App._setContent(`
                <a href="#/library/type/story" class="back-link">&larr; All Stories</a>
                ${infoHtml}
                ${pendingHtml}
                ${totalsHtml}
                ${tabBar}
                ${tabPanels}`);

            document.querySelectorAll('.det-tab').forEach(btn =>
                btn.addEventListener('click', () => {
                    document.querySelectorAll('.det-tab').forEach(x => {
                        const on = x === btn;
                        x.classList.toggle('is-active', on);
                        x.setAttribute('aria-selected', String(on));
                    });
                    document.querySelectorAll('.det-panel').forEach(p =>
                        p.classList.toggle('is-active', p.dataset.panel === btn.dataset.tab));
                }));

            // Comparison chart needs to be initialised AFTER the canvas is
            // in the DOM. Single deferred call — Chart.js handles the rest.
            if (pubsWithData.length >= 2) {
                this._renderComparisonChart(pubsWithData);
            }

        } catch (err) {
            App._setContent(`<div class="error-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

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
            this.renderStoryDetail(storyName);
        } catch (err) {
            alert('Update failed: ' + err.message);
        }
    },

    async _updateAll(storyName) {
        if (!confirm(`Push updates for ALL ${storyName.replace(/_/g, ' ')} publications?`)) return;
        try {
            await API.updateStory({ story_name: storyName, confirm_live: true });
            alert('Updates sent!');
            this.renderStoryDetail(storyName);
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
        const schedNote = scheduled.length
            ? `<p class="page-subtitle">${scheduled.length} scheduled &middot; ${queue.length} total. Scheduled items publish automatically at their time (server runs them; desktop-only platforms fire next time the app is open).</p>`
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
                    <td data-label="Status"><span class="status-badge status-${item.status}">${item.status}</span></td>
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
