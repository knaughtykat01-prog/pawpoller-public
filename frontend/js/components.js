/* ── Reusable UI components ────────────────────────────────── */
/*
 * Reusable HTML template functions that return HTML strings.
 * All components use Utils for formatting/escaping and return
 * innerHTML-safe strings. Organized by platform (IB, FA, WS)
 * and feature (Groups, Analytics, Cross-Platform).
 */

const Components = {

    // Friendly labels for content types (raw → display tag).
    TW_TYPE_LABELS: { tweet: 'Tweet', reply: 'Reply', quote: 'Quote', retweet: 'Repost' },
    BSKY_TYPE_LABELS: { post: 'Post', reply: 'Reply', quote: 'Quote', repost: 'Repost' },
    MAST_TYPE_LABELS: { post: 'Post', reply: 'Reply', quote: 'Quote', repost: 'Repost' },
    TUM_TYPE_LABELS: { text: 'Text', photo: 'Photo', quote: 'Quote', link: 'Link', chat: 'Chat', audio: 'Audio', video: 'Video', answer: 'Answer' },
    PIX_TYPE_LABELS: { illust: 'Illust', manga: 'Manga', ugoira: 'Ugoira', novel: 'Novel' },
    THR_TYPE_LABELS: { text: 'Text', image: 'Image', video: 'Video', carousel: 'Album', audio: 'Audio', quote: 'Quote', repost: 'Repost' },
    IG_TYPE_LABELS: { text: 'Text', image: 'Image', video: 'Video', carousel: 'Album', audio: 'Audio', quote: 'Quote', repost: 'Repost', reel: 'Reel', story: 'Story' },
    E621_TYPE_LABELS: { image: 'Image', animation: 'GIF', video: 'Video', flash: 'Flash' },

    /**
     * Single metric card with optional 24h delta indicator.
     * Used in stats-grid sections on all dashboards (IB, FA, WS, Overview).
     * @param {string} label  - Display label (HTML-escaped internally)
     * @param {number} value  - Metric value (formatted via Utils.formatNumber)
     * @param {number|null} delta - Optional 24-hour change value; null hides the delta row
     * @returns {string} HTML string for one .stat-card element
     */
    statCard(label, value, delta = null, href = null) {
        let deltaHtml = '';
        if (delta !== null && delta !== undefined) {
            deltaHtml = Utils.formatDelta(delta);
        }
        const inner = `
                <div class="label">${Utils.escapeHtml(label)}</div>
                <div class="value">${Utils.formatNumber(value)}</div>
                ${deltaHtml ? `<div>${deltaHtml} <span style="font-size:11px;color:var(--text-muted)">24h</span></div>` : ''}
        `;
        // With an href the card becomes a link (e.g. "Total Tweets" → the tweets
        // list). a.stat-card already has hover styling in components.css.
        if (href) {
            return `<a class="stat-card" href="${href}" style="text-decoration:none;color:inherit;cursor:pointer">${inner}</a>`;
        }
        return `<div class="stat-card">${inner}</div>`;
    },

    /**
     * Clickable ranked list for IB submissions.
     * Each item navigates to the IB submission detail page via App.navigate().
     * Values are displayed in compact format (e.g. 1.2k) via Utils.formatCompact.
     * @param {Array} items    - Array of submission objects
     * @param {string} valueKey - Object key for the numeric display value (e.g. 'views')
     * @param {string} labelKey - Object key for the display title (default: 'title')
     * @param {string} idKey    - Object key for the submission ID used in navigation (default: 'submission_id')
     * @returns {string} HTML string for a <ul class="top-list"> element
     */
    topList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * Activity feed for IB faving users with timeAgo timestamps.
     * Each entry shows username, truncated submission title (clickable to detail page),
     * and relative time since the fave was first seen.
     * @param {Array} items - Array of fave objects with username, submission_id, submission_title, first_seen_at
     * @returns {string} HTML string of .fave-item elements
     */
    recentFaves(items) {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No faves recorded yet</p>';
        }
        return items.map(f => `
            <div class="fave-item">
                <span class="fave-user">${Utils.escapeHtml(f.username)}</span>
                <span class="fave-sub" style="cursor:pointer" data-nav="/submission/${f.submission_id}">faved ${Utils.escapeHtml(Utils.truncate(f.submission_title || '', 25))}</span>
                <span class="fave-time">${Utils.timeAgo(f.first_seen_at)}</span>
            </div>
        `).join('');
    },

    /**
     * Time preset buttons bar (24h / 7d / 30d / 90d / All Time).
     * Renders a row of buttons with the active preset highlighted.
     * Event binding for button clicks happens externally in app.js _bindDateRange(),
     * which attaches click listeners to buttons via the data-range attribute.
     * @param {string} activePreset - Currently selected preset key ('24h','7d','30d','90d','all')
     * @param {Function|null} onSelect - Unused; kept for API compatibility. Binding is external.
     * @returns {string} HTML string for the .date-range-bar container
     */
    dateRangeBar(activePreset = '7d', onSelect = null) {
        const presets = ['24h', '7d', '30d', '90d', 'all'];
        const buttons = presets.map(p => `
            <button class="range-btn ${p === activePreset ? 'active' : ''}"
                    data-range="${p}">${p === 'all' ? 'All Time' : p.toUpperCase()}</button>
        `).join('');
        return `<div class="date-range-bar" id="date-range-bar">${buttons}</div>`;
    },

    /**
     * Generic submission card grid. Renders submissions as glass cards in a CSS grid.
     * @param {Array} submissions - Array of submission objects
     * @param {Object} opts - Configuration:
     *   idKey: object key for ID (e.g. 'submission_id')
     *   titleKey: object key for title (e.g. 'title')
     *   thumbKey: object key for thumbnail URL (e.g. 'thumb_url'), null if none
     *   detailRoute: route prefix (e.g. '/submission') — full route = #${detailRoute}/${id}
     *   stats: array of {key, deltaKey, label} objects for stat display
     *   dateKey: object key for date (e.g. 'create_datetime')
     *   proxyThumb: whether to run thumb through Utils.thumbUrl (default true)
     */
    submissionCardGrid(submissions, opts) {
        if (!submissions || submissions.length === 0) {
            return '<div class="empty-state"><p>No submissions yet.</p></div>';
        }
        const cards = submissions.map(s => {
            const id = s[opts.idKey];
            const title = s[opts.titleKey] || '(untitled)';
            const thumbUrl = opts.thumbKey && s[opts.thumbKey]
                ? (opts.proxyThumb !== false ? Utils.thumbUrl(s[opts.thumbKey]) : s[opts.thumbKey])
                : null;
            const statsHtml = (opts.stats || []).map(st => {
                const val = Utils.formatCompact(s[st.key] || 0);
                const delta = st.deltaKey ? Utils.formatDelta(s[st.deltaKey]) : '';
                return `<span class="submission-card-stat">${val}${delta ? ' ' + delta : ''} <small>${st.label}</small></span>`;
            }).join('');
            const date = opts.dateKey && s[opts.dateKey] ? Utils.formatDate(s[opts.dateKey]) : '';
            const typeRaw = opts.typeKey ? (s[opts.typeKey] || '') : '';
            const typeBadge = typeRaw
                ? `<span class="card-type-badge type-${Utils.escapeHtml(typeRaw)}">${Utils.escapeHtml(
                    (opts.typeLabels && opts.typeLabels[typeRaw]) || (typeRaw.charAt(0).toUpperCase() + typeRaw.slice(1)))}</span>`
                : '';
            return `
                <a href="#${opts.detailRoute}/${id}" class="submission-card">
                    ${thumbUrl ? `<div class="submission-card-thumb"><img src="${Utils.escapeHtml(thumbUrl)}" loading="lazy" alt=""></div>` : ''}
                    <div class="submission-card-body">
                        ${typeBadge}
                        <div class="submission-card-title">${Utils.escapeHtml(title)}</div>
                        <div class="submission-card-stats">${statsHtml}</div>
                        ${date ? `<div class="submission-card-date">${date}</div>` : ''}
                    </div>
                </a>`;
        }).join('');
        return `<div class="submission-card-grid">${cards}</div>`;
    },

    /**
     * Full IB submissions table with sortable headers, thumbnails, deltas, and links
     * to detail pages. Thumbnails are proxied via Utils.thumbUrl() to avoid CORS issues.
     * Sortable column headers use data-sort attributes; sorting logic is handled in app.js.
     * Each row shows: thumbnail, title (links to #/ib/submission/:id), type, rating,
     * views + delta, faves + delta, comments + delta, and creation date.
     * @param {Array} submissions - Array of IB submission objects
     * @returns {string} HTML string for the full data-table with id="submissions-table"
     */
    submissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Run a poll to fetch data from Inkbunny.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumb_url ? `<img src="${Utils.thumbUrl(s.thumb_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/ib/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(s.type_name || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating_name || '--')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Faves">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Created">${Utils.formatDate(s.create_datetime)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th style="width:60px"></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="type_name">Type</th>
                        <th data-sort="rating_name">Rating</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Faves</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="create_datetime">Created</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * IB poll history table with color-coded status indicators.
     * Status colors: green (var(--success)) = success, red (var(--danger)) = error,
     * yellow (var(--warning)) = running/other. Shows time, status, submissions found,
     * snapshots inserted, new faves found, duration, and error message (truncated).
     * @param {Array} polls - Array of IB poll log objects
     * @returns {string} HTML string for the poll log data-table
     */
    pollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.new_faves_found || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Faves</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * Table of users who faved a specific IB submission.
     * Each username links externally to their Inkbunny profile (https://inkbunny.net/:username).
     * Shows first-seen timestamp for each faving user.
     * @param {Array} users - Array of objects with username and first_seen_at
     * @returns {string} HTML string for the faving users data-table
     */
    favingUsersTable(users) {
        if (!users || users.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No faving users tracked yet.</p>';
        }
        const rows = users.map(u => `
            <tr>
                <td><a href="https://inkbunny.net/${Utils.escapeHtml(u.username)}" target="_blank">${Utils.escapeHtml(u.username)}</a></td>
                <td>${Utils.formatDateTime(u.first_seen_at)}</td>
            </tr>
        `).join('');
        return `
            <table class="data-table">
                <thead><tr><th>Username</th><th>First Seen</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * Threaded comment display for IB submissions.
     * Replies are visually indented (margin-left + left border accent) and tagged with
     * a "reply" label. Each username links externally to the user's inkbunny.net profile.
     * Comments are scraped from the web during polling when comment count changes
     * (only available for IB platform).
     * @param {Array} comments - Array of comment objects with username, comment_text,
     *                           commented_at, is_reply, reply_to_comment_id
     * @returns {string} HTML string for the .comments-list container
     */
    commentsSection(comments) {
        if (!comments || comments.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No comments scraped yet. Comments are fetched during polling when comment count changes.</p>';
        }
        const items = comments.map(c => {
            const indent = c.is_reply ? 'margin-left:32px;border-left:3px solid var(--accent);' : '';
            const replyTag = c.reply_to_comment_id ? `<span style="font-size:11px;color:var(--text-muted)">reply</span> ` : '';
            return `
                <div class="comment-card" style="${indent}">
                    <div class="comment-header">
                        ${replyTag}<a href="https://inkbunny.net/${Utils.escapeHtml(c.username)}" target="_blank" class="comment-user">${Utils.escapeHtml(c.username)}</a>
                        <span class="comment-date">${Utils.escapeHtml(c.commented_at || '')}</span>
                    </div>
                    <div class="comment-body">${Utils.escapeHtml(c.comment_text)}</div>
                </div>
            `;
        }).join('');
        return `<div class="comments-list">${items}</div>`;
    },

    /**
     * Recent watchers feed for dashboards.
     * Shows username and relative timeAgo timestamp for each watcher.
     * Used on both IB and FA dashboards.
     * @param {Array} watchers - Array of watcher objects with username, first_seen_at
     * @returns {string} HTML string of .fave-activity elements
     */
    recentWatchers(watchers) {
        if (!watchers || watchers.length === 0) {
            return '<div class="empty-state"><p>No watchers recorded yet.</p></div>';
        }
        return watchers.map(w => `
            <div class="fave-activity">
                <span class="fave-user">${Utils.escapeHtml(w.username)}</span>
                <span class="fave-time">${Utils.timeAgo(w.first_seen_at)}</span>
            </div>
        `).join('');
    },

    /**
     * Compact recent comments feed for the IB dashboard.
     * Reuses the .fave-item layout: username, clickable submission title (navigates
     * to IB detail page), and relative timeAgo timestamp.
     * @param {Array} items - Array of comment objects with username, submission_id,
     *                        submission_title, first_seen_at
     * @returns {string} HTML string of .fave-item elements
     */
    recentComments(items) {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No comments recorded yet</p>';
        }
        return items.map(c => `
            <div class="fave-item">
                <span class="fave-user">${Utils.escapeHtml(c.username)}</span>
                <span class="fave-sub" style="cursor:pointer" data-nav="/submission/${c.submission_id}">on ${Utils.escapeHtml(Utils.truncate(c.submission_title || '', 25))}</span>
                <span class="fave-time">${Utils.timeAgo(c.first_seen_at)}</span>
            </div>
        `).join('');
    },

    /**
     * Three-period growth rate display (24h / 7d / 30d).
     * Each card shows views/day, faves/day, and comments/day with color-coded values:
     * views = accent, faves = danger, comments = success. Positive values prefixed
     * with '+'. Null/undefined values display as '--'.
     * @param {Object} rates - Object keyed by period ('24h','7d','30d'), each containing
     *                         views_per_day, faves_per_day, comments_per_day
     * @returns {string} HTML string for a .stats-grid.growth-grid container
     */
    growthRateCards(rates, metricLabels) {
        if (!rates) return '';
        const periods = ['24h', '7d', '30d'];
        const labels = { '24h': 'Last 24 Hours', '7d': 'Last 7 Days', '30d': 'Last 30 Days' };
        const ml = metricLabels || { views: 'views/day', faves: 'faves/day', comments: 'comments/day' };
        const fmt = (v) => v === null || v === undefined ? '--' : v >= 0 ? '+' + v.toFixed(1) : v.toFixed(1);
        const cls = (v) => v === null || v === undefined ? 'neutral' : v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral';

        const cards = periods.map(p => {
            const r = rates[p];
            if (!r) return '';
            return `
                <div class="stat-card growth-card">
                    <div class="label">${labels[p]}</div>
                    <div class="growth-metrics">
                        <div class="growth-metric">
                            <span class="growth-val" style="color:var(--accent)">${fmt(r.views_per_day)}</span>
                            <span class="growth-lbl">${ml.views}</span>
                        </div>
                        <div class="growth-metric">
                            <span class="growth-val" style="color:var(--danger)">${fmt(r.faves_per_day)}</span>
                            <span class="growth-lbl">${ml.faves}</span>
                        </div>
                        <div class="growth-metric">
                            <span class="growth-val" style="color:var(--success)">${fmt(r.comments_per_day)}</span>
                            <span class="growth-lbl">${ml.comments}</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        return `<div class="stats-grid growth-grid">${cards}</div>`;
    },

    /**
     * Parses a JSON keyword string and renders each keyword as a styled tag badge.
     * Handles invalid/empty JSON gracefully by returning empty string.
     * @param {string} jsonStr - JSON-encoded array of keyword strings, e.g. '["fox","wolf"]'
     * @returns {string} HTML string of <span class="tag"> elements, or empty string
     */
    keywords(jsonStr) {
        try {
            const kws = JSON.parse(jsonStr || '[]');
            if (!kws.length) return '';
            return kws.map(k => `<span class="tag">${Utils.escapeHtml(k)}</span>`).join('');
        } catch {
            return '';
        }
    },

    // ── Overview Components ──────────────────────────────────────

    /**
     * Cross-platform top list with platform badges (IB / FA / WS).
     * Determines the correct platform-specific detail route based on item._platform:
     *   'fa' -> /fa/submission/, 'ws' -> /ws/submission/, default -> /submission/ (IB).
     * Each item shows a colored platform badge, clickable title, and compact value.
     * @param {Array} items    - Array of submission objects with _platform field
     * @param {string} valueKey - Object key for numeric display value
     * @param {string} labelKey - Object key for display title (default: 'title')
     * @param {string} idKey    - Object key for submission ID (default: 'submission_id')
     * @returns {string} HTML string for a <ul class="top-list"> with platform badges
     */
    overviewTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => {
            const prefixes = { fa: '/fa/submission/', ws: '/ws/submission/', sf: '/sf/submission/', sqw: '/sqw/submission/', ao3: '/ao3/submission/', da: '/da/submission/', wp: '/wp/submission/', ik: '/ik/submission/', bsky: '/bsky/submission/', tw: '/tw/submission/', mast: '/mast/submission/', tum: '/tum/submission/', pix: '/pix/submission/', thr: '/thr/submission/', ig: '/ig/submission/', e621: '/e621/submission/', ib: '/submission/' };
            const prefix = prefixes[item._platform] || prefixes.ib;
            const badges = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', mast: '<span class="platform-badge mast">MAST</span>', tum: '<span class="platform-badge tum">TUM</span>', pix: '<span class="platform-badge pix">PIX</span>', thr: '<span class="platform-badge thr">THR</span>', ig: '<span class="platform-badge ig">IG</span>', ib: '<span class="platform-badge ib">IB</span>' };
            const badge = badges[item._platform] || badges.ib;
            return `
                <li>
                    ${badge}
                    <span class="top-title" data-nav="${prefix}${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 28))}</span>
                    <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
                </li>
            `;
        }).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * Merged activity feed from all platforms with platform badges and action type.
     * Combines fave and comment activity across IB, FA, and WS into a single feed.
     * Action text is 'faved' for faves and 'on' for comments (based on item._type).
     * Routes to the correct platform-specific detail page based on item._platform.
     * @param {Array} items - Array of activity objects with _platform, _type, username,
     *                        submission_id, submission_title, first_seen_at
     * @returns {string} HTML string of .fave-item elements with platform badges
     */
    overviewRecentActivity(items) {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No recent activity</p>';
        }
        return items.map(item => {
            const prefixes = { fa: '/fa/submission/', ws: '/ws/submission/', sf: '/sf/submission/', sqw: '/sqw/submission/', ao3: '/ao3/submission/', da: '/da/submission/', wp: '/wp/submission/', ik: '/ik/submission/', bsky: '/bsky/submission/', tw: '/tw/submission/', mast: '/mast/submission/', tum: '/tum/submission/', pix: '/pix/submission/', thr: '/thr/submission/', ig: '/ig/submission/', e621: '/e621/submission/', ib: '/submission/' };
            const prefix = prefixes[item._platform] || prefixes.ib;
            const badges = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', mast: '<span class="platform-badge mast">MAST</span>', tum: '<span class="platform-badge tum">TUM</span>', pix: '<span class="platform-badge pix">PIX</span>', thr: '<span class="platform-badge thr">THR</span>', ig: '<span class="platform-badge ig">IG</span>', ib: '<span class="platform-badge ib">IB</span>' };
            const badge = badges[item._platform] || badges.ib;
            const action = item._type === 'fave' ? 'faved' : 'on';
            return `
                <div class="fave-item">
                    ${badge}
                    <span class="fave-user">${Utils.escapeHtml(item.username)}</span>
                    <span class="fave-sub" style="cursor:pointer" data-nav="${prefix}${item.submission_id}">${action} ${Utils.escapeHtml(Utils.truncate(item.submission_title || '', 22))}</span>
                    <span class="fave-time">${Utils.timeAgo(item.first_seen_at)}</span>
                </div>
            `;
        }).join('');
    },

    // ── FurAffinity Components ─────────────────────────────────

    /**
     * FA-specific ranked list that links to /fa/ routes.
     * Identical in structure to topList() but navigates to /fa/submission/:id.
     * @param {Array} items    - Array of FA submission objects
     * @param {string} valueKey - Object key for numeric display value
     * @param {string} labelKey - Object key for display title (default: 'title')
     * @param {string} idKey    - Object key for submission ID (default: 'submission_id')
     * @returns {string} HTML string for a <ul class="top-list"> for FA submissions
     */
    faTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/fa/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * FA-specific recent comments feed.
     * Same layout as recentComments() but navigates to /fa/submission/:id routes.
     * @param {Array} items - Array of FA comment objects
     * @returns {string} HTML string of .fave-item elements for FA comments
     */
    faRecentComments(items) {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No comments recorded yet</p>';
        }
        return items.map(c => `
            <div class="fave-item">
                <span class="fave-user">${Utils.escapeHtml(c.username)}</span>
                <span class="fave-sub" style="cursor:pointer" data-nav="/fa/submission/${c.submission_id}">on ${Utils.escapeHtml(Utils.truncate(c.submission_title || '', 25))}</span>
                <span class="fave-time">${Utils.timeAgo(c.first_seen_at)}</span>
            </div>
        `).join('');
    },

    /**
     * FA-specific submissions table linking to /fa/ routes.
     * Uses FA-specific fields: category (instead of type_name), rating text (instead of
     * rating_name), and posted_at (instead of create_datetime). Thumbnails are proxied
     * via Utils.faThumbUrl(). Sortable headers use data-sort attributes.
     * @param {Array} submissions - Array of FA submission objects
     * @returns {string} HTML string for the data-table with id="fa-submissions-table"
     */
    faSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your FA account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumbnail_url ? `<img src="${Utils.faThumbUrl(s.thumbnail_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/fa/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Category">${Utils.escapeHtml(s.category || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Faves">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="fa-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th style="width:60px"></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="category">Category</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Faves</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * FA-specific poll history table with color-coded status.
     * Same color coding as pollLogTable(): green=success, red=error, yellow=running.
     * Shows new_comments_found instead of new_faves_found (FA tracks comment discovery).
     * @param {Array} polls - Array of FA poll log objects
     * @returns {string} HTML string for the FA poll log data-table
     */
    faPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No FA polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.new_comments_found || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Comments</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── Weasyl Components ──────────────────────────────────────

    /**
     * WS-specific ranked list linking to /ws/ routes.
     * Identical in structure to topList() but navigates to /ws/submission/:id.
     * @param {Array} items    - Array of WS submission objects
     * @param {string} valueKey - Object key for numeric display value
     * @param {string} labelKey - Object key for display title (default: 'title')
     * @param {string} idKey    - Object key for submission ID (default: 'submission_id')
     * @returns {string} HTML string for a <ul class="top-list"> for WS submissions
     */
    wsTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/ws/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * WS-specific submissions table linking to /ws/ routes.
     * Uses WS-specific fields: subtype (instead of type_name) for the Type column,
     * and posted_at for date. Thumbnails are rendered directly (no proxy needed for WS).
     * Sortable headers use data-sort attributes with 'subtype' for the Type column.
     * @param {Array} submissions - Array of WS submission objects
     * @returns {string} HTML string for the data-table with id="ws-submissions-table"
     */
    wsSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your Weasyl account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumbnail_url ? `<img src="${Utils.escapeHtml(s.thumbnail_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/ws/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(s.subtype || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Faves">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="ws-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th style="width:60px"></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="subtype">Type</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Faves</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * WS-specific poll history table with color-coded status.
     * Same color coding as pollLogTable(): green=success, red=error, yellow=running.
     * Notable difference: no Comments column because the WS API does not provide
     * comment discovery data. Only shows Time, Status, Subs, Snaps, Duration, Error.
     * @param {Array} polls - Array of WS poll log objects
     * @returns {string} HTML string for the WS poll log data-table
     */
    wsPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No Weasyl polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── SF (SoFurry) Components ──────────────────────────────────

    sfTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/sf/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    sfSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your SoFurry account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumbnail_url ? `<img src="${Utils.escapeHtml(s.thumbnail_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/sf/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(s.content_type || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Likes">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="sf-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th style="width:60px"></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Likes</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    sfPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No SoFurry polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── SQW (SquidgeWorld) Components ──────────────────────────────

    sqwTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/sqw/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    sqwSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your SquidgeWorld account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/sqw/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Fandom">${Utils.escapeHtml(s.fandom || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Hits">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Kudos">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Bookmarks">${Utils.formatNumber(s.bookmarks_count || 0)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="sqw-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="fandom">Fandom</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Hits</th>
                        <th data-sort="favorites_count">Kudos</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="bookmarks_count">Bookmarks</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    sqwPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No SquidgeWorld polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── AO3 (Archive of Our Own) Components ──────────────────────

    ao3TopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/ao3/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    ao3SubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your AO3 account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/ao3/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Fandom">${Utils.escapeHtml(s.fandom || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Hits">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Kudos">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Bookmarks">${Utils.formatNumber(s.bookmarks_count || 0)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="ao3-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="fandom">Fandom</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Hits</th>
                        <th data-sort="favorites_count">Kudos</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="bookmarks_count">Bookmarks</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    ao3PollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No AO3 polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── DeviantArt Components ──────────────────────────────────────

    /**
     * DA-specific ranked list linking to /da/ routes.
     * Identical in structure to faTopList() but navigates to /da/submission/:id.
     * @param {Array} items    - Array of DA submission objects
     * @param {string} valueKey - Object key for numeric display value
     * @param {string} labelKey - Object key for display title (default: 'title')
     * @param {string} idKey    - Object key for submission ID (default: 'submission_id')
     * @returns {string} HTML string for a .top-list element
     */
    daTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/da/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * DA-specific submissions table linking to /da/ routes.
     * Includes Views, Favourites, Comments, and Downloads columns (Downloads is unique to DA).
     * No thumbnail column or proxy. Sortable headers use data-sort attributes.
     * @param {Array} submissions - Array of DA submission objects
     * @returns {string} HTML string for the data-table with id="da-submissions-table"
     */
    daSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your DeviantArt account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumbnail_url ? `<img src="${Utils.escapeHtml(s.thumbnail_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/da/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Category">${Utils.escapeHtml(s.category || '--')}</td>
                <td data-label="Rating">${Utils.escapeHtml(s.rating || '--')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Favourites">${Utils.formatNumber(s.favorites_count)} ${Utils.formatDelta(s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Downloads">${Utils.formatNumber(s.downloads || 0)} ${Utils.formatDelta(s.downloads_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="da-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="category">Category</th>
                        <th data-sort="rating">Rating</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Favourites</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="downloads">Downloads</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * DA-specific poll history table with color-coded status.
     * Same color coding as faPollLogTable(): green=success, red=error, yellow=running.
     * @param {Array} polls - Array of DA poll log objects
     * @returns {string} HTML string for the DA poll log data-table
     */
    daPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No DA polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * Clickable ranked list for WP (Wattpad) submissions.
     * Each item navigates to the WP submission detail page via App.navigate().
     * @param {Array} items    - Array of submission objects
     * @param {string} valueKey - Object key for the numeric display value (e.g. 'reads')
     * @param {string} labelKey - Object key for the display label (default 'title')
     * @param {string} idKey   - Object key for the submission ID (default 'submission_id')
     * @returns {string} HTML string for a .top-list element
     */
    wpTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/wp/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * WP-specific submissions table linking to /wp/ routes.
     * Includes Reads, Votes, Comments, and Lists columns (Wattpad-specific metric names).
     * Sortable headers use data-sort attributes.
     * @param {Array} submissions - Array of WP submission objects
     * @returns {string} HTML string for the data-table with id="wp-submissions-table"
     */
    wpSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your Wattpad account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.cover_url ? `<img src="${Utils.escapeHtml(s.cover_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/wp/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Reads">${Utils.formatNumber(s.reads || s.views || 0)} ${Utils.formatDelta(s.reads_delta || s.views_delta)}</td>
                <td data-label="Votes">${Utils.formatNumber(s.votes || s.favorites_count || 0)} ${Utils.formatDelta(s.votes_delta || s.faves_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Lists">${Utils.formatNumber(s.num_lists || 0)} ${Utils.formatDelta(s.lists_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="wp-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="reads">Reads</th>
                        <th data-sort="votes">Votes</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="num_lists">Lists</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * WP-specific poll history table with color-coded status.
     * Same color coding as daPollLogTable(): green=success, red=error, yellow=running.
     * @param {Array} polls - Array of WP poll log objects
     * @returns {string} HTML string for the WP poll log data-table
     */
    wpPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No WP polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── IK (Itaku) Components ────────────────────────────────────

    /**
     * Clickable ranked list for IK submissions.
     * Each item navigates to the IK submission detail page via App.navigate().
     * @param {Array} items    - Array of submission objects
     * @param {string} valueKey - Object key for the numeric display value (e.g. 'likes')
     * @param {string} labelKey - Object key for the display label (default: 'title')
     * @param {string} idKey    - Object key for the submission ID (default: 'submission_id')
     * @returns {string} HTML string for a .top-list element
     */
    ikTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/ik/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * IK-specific submissions table linking to /ik/ routes.
     * Includes Type, Likes, Comments, and Reshares columns (Itaku-specific metrics — NO views).
     * Sortable headers use data-sort attributes.
     * @param {Array} submissions - Array of IK submission objects
     * @returns {string} HTML string for the data-table with id="ik-submissions-table"
     */
    ikSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No submissions</h3><p>Connect your Itaku account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td class="mobile-hide" data-label="">${s.thumbnail_url ? `<img src="${Utils.escapeHtml(s.thumbnail_url)}" class="thumb-cell" loading="eager">` : ''}</td>
                <td data-label="Title"><a href="#/ik/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(s.content_type || 'image')}</td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Reshares">${Utils.formatNumber(s.reshares || 0)} ${Utils.formatDelta(s.reshares_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="ik-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th></th>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="reshares">Reshares</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * IK-specific poll history table with color-coded status.
     * Green=success, red=error, yellow=running.
     * @param {Array} polls - Array of IK poll log objects
     * @returns {string} HTML string for the IK poll log data-table
     */
    ikPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No IK polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── BSKY Components ──────────────────────────────────────────

    /**
     * Clickable ranked list for BSKY submissions.
     * Each item navigates to the BSKY submission detail page via App.navigate().
     * Uses rkey (last segment of AT URI) for routing.
     */
    bskyTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => {
            const rkey = String(item[idKey]).split('/').pop();
            return `
            <li>
                <span class="top-title" data-nav="/bsky/submission/${rkey}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `;
        }).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * BSKY-specific submissions table.
     * Columns: Title, Likes, Reposts, Replies, Quotes, Posted (NO views).
     */
    bskySubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Bluesky account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => {
            const rkey = String(s.submission_id).split('/').pop();
            return `
            <tr>
                <td data-label="Title"><a href="#/bsky/submission/${rkey}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Reposts">${Utils.formatNumber(s.reposts || 0)} ${Utils.formatDelta(s.reposts_delta)}</td>
                <td data-label="Replies">${Utils.formatNumber(s.replies || 0)} ${Utils.formatDelta(s.replies_delta)}</td>
                <td data-label="Quotes">${Utils.formatNumber(s.quotes || 0)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `;
        }).join('');

        return `
            <table class="data-table" id="bsky-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="reposts">Reposts</th>
                        <th data-sort="replies">Replies</th>
                        <th data-sort="quotes">Quotes</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * BSKY-specific poll history table with color-coded status.
     */
    bskyPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No BSKY polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── MAST Components ──────────────────────────────────────────

    /**
     * Clickable ranked list for MAST submissions.
     * Each item navigates to the MAST submission detail page via App.navigate().
     * Uses rkey (last segment of the status URI) for routing.
     */
    mastTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => {
            const rkey = String(item[idKey]).split('/').pop();
            return `
            <li>
                <span class="top-title" data-nav="/mast/submission/${rkey}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `;
        }).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * MAST-specific submissions table.
     * Columns: Title, Type, Likes, Reposts, Replies, Posted (NO views/quotes).
     */
    mastSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Mastodon account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => {
            const rkey = String(s.submission_id).split('/').pop();
            return `
            <tr>
                <td data-label="Title"><a href="#/mast/submission/${rkey}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.MAST_TYPE_LABELS[s.content_type] || s.content_type || 'Post')}</td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Reposts">${Utils.formatNumber(s.reposts || 0)} ${Utils.formatDelta(s.reposts_delta)}</td>
                <td data-label="Replies">${Utils.formatNumber(s.replies || 0)} ${Utils.formatDelta(s.replies_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `;
        }).join('');

        return `
            <table class="data-table" id="mast-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="reposts">Reposts</th>
                        <th data-sort="replies">Replies</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * MAST-specific poll history table with color-coded status.
     */
    mastPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No MAST polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── TUM Components ───────────────────────────────────────────

    /**
     * Clickable ranked list for TUM submissions (by notes).
     */
    tumTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/tum/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * TUM-specific submissions table.
     * Columns: Title, Type, Notes, Posted (Tumblr's single engagement metric).
     */
    tumSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Tumblr blog and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/tum/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.TUM_TYPE_LABELS[s.content_type] || s.content_type || 'Text')}</td>
                <td data-label="Notes">${Utils.formatNumber(s.notes || 0)} ${Utils.formatDelta(s.notes_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="tum-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="notes">Notes</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * TUM-specific poll history table with color-coded status.
     */
    tumPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No TUM polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── PIX Components ───────────────────────────────────────────

    /**
     * Clickable ranked list for PIX submissions.
     */
    pixTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/pix/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * PIX-specific submissions table.
     * Columns: Title, Type, Views, Bookmarks, Comments, Posted.
     */
    pixSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No works</h3><p>Connect your Pixiv account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/pix/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.PIX_TYPE_LABELS[s.content_type] || s.content_type || 'Illust')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views || 0)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Bookmarks">${Utils.formatNumber(s.favorites_count || 0)} ${Utils.formatDelta(s.favorites_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="pix-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="favorites_count">Bookmarks</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * PIX-specific poll history table with color-coded status.
     */
    pixPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No PIX polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── E621 Components ──────────────────────────────────────────

    e621TopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/e621/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    fnTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/fn/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * e621-specific submissions table.
     * Columns: Title, Type, Score, Favorites, Comments, Posted. e621 has no
     * view count, so Score (score.total, may be negative) is the headline.
     */
    e621SubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your e621 account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/e621/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.E621_TYPE_LABELS[s.content_type] || s.content_type || 'Image')}</td>
                <td data-label="Score">${Utils.formatNumber(s.score || 0)} ${Utils.formatDelta(s.score_delta)}</td>
                <td data-label="Favorites">${Utils.formatNumber(s.favorites_count || 0)} ${Utils.formatDelta(s.favorites_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="e621-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="score">Score</th>
                        <th data-sort="favorites_count">Favorites</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * e621-specific poll history table with color-coded status.
     */
    e621PollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No e621 polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── Furbooru Components ──────────────────────────────────────
    // Furbooru is a Philomena booru — SCORE model, identical shape to e621.

    fbrTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/fbr/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * Furbooru submissions table.
     * Columns: Title, Type, Score, Favorites, Comments, Posted. Booru score
     * (upvotes − downvotes, may be negative) is the headline metric.
     */
    fbrSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Furbooru account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/fbr/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.E621_TYPE_LABELS[s.content_type] || s.content_type || 'Image')}</td>
                <td data-label="Score">${Utils.formatNumber(s.score || 0)} ${Utils.formatDelta(s.score_delta)}</td>
                <td data-label="Favorites">${Utils.formatNumber(s.favorites_count || 0)} ${Utils.formatDelta(s.favorites_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments_count || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="fbr-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="score">Score</th>
                        <th data-sort="favorites_count">Favorites</th>
                        <th data-sort="comments_count">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * Furbooru poll history table with color-coded status.
     */
    /* FurryNetwork's poll log. FN shipped as the 18th platform (2.200.0) with a
       `tableFn: 'fnPollLogTable'` entry in the polling tab's platform list, but
       this renderer was never written — so opening Polling threw
       "Components[p.tableFn] is not a function" and the ENTIRE tab rendered as
       "Failed to load polling data", not just FN's row. `fn_poll_log` carries
       the same columns as e621/fbr. (3.17.1) */
    fnPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No FurryNetwork polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /* ── Publish confirmation + results (4.1.0) ────────────────────
     *
     * The first reusable modal in the codebase. app.js:15838 says outright
     * "there's no shared confirm helper", and ~85 sites use native confirm()
     * instead — which cannot show a LIST, and the one thing a publish
     * confirmation most needs to show is a list: what, where, as whom.
     *
     * Every artwork and post publish trigger was unconfirmed at both ends until
     * 4.0.11 added the server guard; this is the client half. Quick Publish is
     * the sharpest case: it uploads AND publishes in one click to every
     * platform in a preset restored from localStorage, so the ticks may be from
     * last time and unread. The count in the confirm button's own label is the
     * last defence against that.
     *
     * Deliberately NO typed-phrase gate. One exists (publish_check.js) but
     * guards a local delete. Publishing is frequent and deliberate; a phrase
     * you type every time is a phrase you stop reading. The list is the safety
     * feature, not the friction. docs/specs/publish_flow.md §8.1.
     */

    /**
     * Show a publish confirmation. Resolves `{ ok: true, tgDescription }` on
     * confirm (truthy — callers may test it as a boolean), false on cancel,
     * Escape, or a click on the backdrop.
     *
     * @param {Object} o
     * @param {string} o.title       What is being published (piece / story title).
     * @param {string} [o.subtitle]  e.g. "Chapter 3", "3 chapters × 2 sites".
     * @param {string} [o.thumb]     Optional image URL.
     * @param {Array}  o.targets     [{ code, label, account?, disabled?, reason? }]
     * @param {string} [o.persona]   "Posting as <persona>", when one is chosen.
     * @param {string} [o.verb]      Button verb, default "Publish".
     * @param {string} [o.noun]      Plural noun for the count, default "sites".
     * @param {string} [o.warning]   Extra line, e.g. drip schedules a month of posts.
     * @param {Object} [o.tgDesc]    When given, a "Telegram text for this post" box
     *                               (4.3.0); its value comes back as tgDescription.
     */
    confirmPublish(o) {
        const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
        const targets = (o.targets || []);
        const live = targets.filter(t => !t.disabled);
        const verb = o.verb || 'Publish';
        const noun = o.noun || 'sites';
        const n = live.length;
        const rows = targets.map(t => `
            <li class="pub-confirm-row${t.disabled ? ' is-disabled' : ''}">
                <span class="pub-confirm-plat">${esc(t.emoji || '')} ${esc(t.label || t.code)}</span>
                <span class="pub-confirm-acct muted">${t.disabled
                    ? esc(t.reason || 'skipped')
                    : (t.account ? 'as ' + esc(t.account) : '')}</span>
            </li>`).join('');
        return new Promise(resolve => {
            const ov = document.createElement('div');
            ov.className = 'modal-overlay open';
            ov.setAttribute('role', 'dialog');
            ov.setAttribute('aria-modal', 'true');
            ov.setAttribute('aria-labelledby', 'pub-confirm-title');
            ov.innerHTML = `
                <div class="modal-panel pub-confirm">
                    <h3 id="pub-confirm-title">${esc(verb)} to ${n} ${n === 1 ? noun.replace(/s$/, '') : noun}?</h3>
                    <div class="pub-confirm-what">
                        ${o.thumb ? `<img class="pub-confirm-thumb" src="${esc(o.thumb)}" alt="">` : ''}
                        <div>
                            <div class="pub-confirm-title">${esc(o.title || '')}</div>
                            ${o.subtitle ? `<div class="muted" style="font-size:12.5px">${esc(o.subtitle)}</div>` : ''}
                            ${o.persona ? `<div class="pub-confirm-persona">Posting as <strong>${esc(o.persona)}</strong></div>` : ''}
                        </div>
                    </div>
                    <ul class="pub-confirm-list">${rows}</ul>
                    ${o.warning ? `<p class="pub-confirm-warn">${esc(o.warning)}</p>` : ''}
                    ${o.tgDesc ? `<label class="pub-confirm-tgdesc">
                        <span>Telegram text for this post <span class="muted">— optional, this post only. Blank uses the piece's saved Telegram text, then its description.</span></span>
                        <textarea data-pub-tgdesc rows="2" maxlength="900">${esc(o.tgDesc.value || '')}</textarea>
                    </label>` : ''}
                    <p class="pub-confirm-note muted">This goes out live. Taking it down afterwards means doing it on each site by hand.</p>
                    <div class="pub-confirm-actions">
                        <button type="button" class="btn btn-secondary" data-pub-cancel>Cancel</button>
                        <button type="button" class="btn btn-primary" data-pub-ok ${n ? '' : 'disabled'}>${esc(verb)} to ${n} ${n === 1 ? noun.replace(/s$/, '') : noun}</button>
                    </div>
                </div>`;
            const done = (v) => {
                document.removeEventListener('keydown', onKey);
                ov.remove();
                resolve(v);
            };
            const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); done(false); } };
            ov.addEventListener('click', (e) => { if (e.target === ov) done(false); });
            ov.querySelector('[data-pub-cancel]').addEventListener('click', () => done(false));
            ov.querySelector('[data-pub-ok]').addEventListener('click', () => done({
                ok: true,
                tgDescription: ((ov.querySelector('[data-pub-tgdesc]') || {}).value || '').trim(),
            }));
            document.addEventListener('keydown', onKey);
            document.body.appendChild(ov);
            // Focus lands on Cancel: Enter from a stale keypress must not publish.
            ov.querySelector('[data-pub-cancel]').focus();
        });
    },

    /**
     * Per-platform publish outcomes, one row each: ✓ with a link to the new
     * post, or ✗ with the platform's own error. Replaces the transient
     * "2 ok, 3 failed" toast that discarded results[] — which three, and why,
     * was never shown (artwork.js:1243, masterpieces.js:1192 before 4.1.0).
     * posts.js:524 had half of this; publish_check.js:1541 had all of it.
     */
    publishResults(results, opts) {
        const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
        const okText = (opts && opts.okText) || 'Posted';
        const plat = (c) => (window.platformByCode && window.platformByCode(c)) || { label: c, emoji: '' };
        const rows = (results || []).map(r => {
            const p = plat(r.platform);
            const ok = !!r.success;
            const skipped = !ok && !!r.skipped;   // sync: post-only sites (4.2.0)
            const url = r.external_url || r.url || '';
            const what = ok
                ? (r.queued_desktop ? 'Queued for desktop'
                    : url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(okText)} ↗</a>` : esc(okText))
                : skipped ? esc(r.reason || 'skipped')
                : esc(r.error || 'Failed');
            return `<li class="pub-result ${ok ? 'is-ok' : skipped ? 'is-skip' : 'is-fail'}">
                <span class="pub-result-mark">${ok ? '✓' : skipped ? '–' : '✗'}</span>
                <span class="pub-result-plat">${esc(p.emoji || '')} ${esc(p.label)}</span>
                <span class="pub-result-what">${what}</span>
            </li>`;
        }).join('');
        return `<ul class="pub-results">${rows}</ul>`;
    },

    /**
     * Render publishResults just after `anchor` (usually the surface's message
     * element), replacing any earlier panel. Returns the failure count so the
     * caller can decide whether to navigate away — on partial failure it must
     * NOT, or the panel is wiped before it can be read (the §7.4 re-render bug).
     */
    showPublishResults(anchor, results, opts) {
        const fails = (results || []).filter(r => !r.success && !r.skipped).length;
        if (!anchor) return fails;
        const prev = anchor.parentElement && anchor.parentElement.querySelector('.pub-results');
        if (prev) prev.remove();
        anchor.insertAdjacentHTML('afterend', this.publishResults(results, opts));
        return fails;
    },

    /* ── Persona picker (4.2.0) ──────────────────────────────────────
     *
     * Persona-first account selection, shared by the artwork form, the
     * artwork and masterpiece detail pages, the post composer and Publish
     * Check. Promoted from Quick Publish's _qpBuildMap, which was already the
     * right idea on one page, with one bug: two accounts on one platform
     * collapsed silently to whichever was default or first.
     *
     * Three rules (docs/specs/publish_flow.md §3, §8.2, §10 Q1/Q2):
     *  - a persona with ONE account on a platform shows a label, not a
     *    dropdown — but the id still travels, as a hidden input, because the
     *    platform default may belong to a different persona;
     *  - TWO or more accounts on a platform is a real choice: a dropdown with
     *    a visible marker, never a silent pick;
     *  - a platform the persona has NO account on is shown disabled with the
     *    reason, and its checkbox is cleared — the server refuses it anyway.
     * "All accounts" keeps the pre-4.2.0 behaviour exactly: a dropdown only
     * where there is a real choice, else the platform default.
     */

    /**
     * @param {Object}   o
     * @param {Element}  o.host         Where the chips render; gets data-persona-id / -label.
     * @param {string[]} o.platforms    Codes this surface offers.
     * @param {Function} o.slot         code → element for the account control (or null).
     * @param {Function} [o.row]        code → the platform row (toggles .is-unavailable).
     * @param {string}   o.selectClass  The class the surface's collector reads.
     * @param {string}   o.storageKey   localStorage key remembering the last persona.
     * @param {Function} [o.onChange]
     * @returns {Promise<{personaId: number|null, personaLabel: string}>}
     */
    async personaPicker(o) {
        const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
        let personas = [], accounts = [];
        try {
            const [pRes, aRes] = await Promise.all([API.getPersonas(), API.getAccounts()]);
            personas = (pRes && pRes.personas) || [];
            accounts = (aRes && aRes.accounts) || [];
        } catch (e) { /* no chips; rows fall back to the per-platform picker */ }

        const offered = new Set(o.platforms || []);
        const enabled = accounts.filter(a => a.enabled && offered.has(a.platform));
        const byPersona = {};   // persona_id → {code: [accounts]}
        const all = {};         // code → [accounts]
        for (const a of enabled) {
            (all[a.platform] = all[a.platform] || []).push(a);
            if (a.persona_id) {
                const b = byPersona[a.persona_id] = byPersona[a.persona_id] || {};
                (b[a.platform] = b[a.platform] || []).push(a);
            }
        }
        const options = personas.filter(p => byPersona[p.persona_id]).map(p => ({
            id: p.persona_id, label: p.name, color: p.color || '#6c8cff', map: byPersona[p.persona_id],
        }));
        const state = { personaId: null, personaLabel: '' };
        const name = (a) => a.label || a.handle || ('account ' + a.account_id);
        const opts = (accts) => accts.map(a =>
            `<option value="${a.account_id}"${a.is_default ? ' selected' : ''}>${esc(name(a))}</option>`).join('');

        const control = (code, accts, persona) => {
            if (!persona) {
                // All accounts: unchanged behaviour — no control unless there is a real choice.
                if (accts.length < 2) return '';
                return `<label class="acct-as">as <select class="${o.selectClass}" data-platform="${code}">${opts(accts)}</select></label>`;
            }
            if (accts.length === 1) {
                const a = accts[0];
                return `<span class="acct-as">as <strong>${esc(name(a))}</strong></span>` +
                    `<input type="hidden" class="${o.selectClass}" data-platform="${code}" value="${a.account_id}" data-account-label="${esc(name(a))}">`;
            }
            return `<label class="acct-as acct-as--choice" title="${esc(persona.label)} has ${accts.length} accounts here — pick one">` +
                `as <select class="${o.selectClass}" data-platform="${code}">${opts(accts)}</select>` +
                `<span class="acct-choice-mark">${accts.length} accounts</span></label>`;
        };

        const apply = (pid) => {
            const persona = options.find(p => p.id === pid) || null;
            state.personaId = persona ? persona.id : null;
            state.personaLabel = persona ? persona.label : '';
            o.host.dataset.personaId = persona ? String(persona.id) : '';
            o.host.dataset.personaLabel = state.personaLabel;
            const key = persona ? String(persona.id) : 'all';
            o.host.querySelectorAll('[data-persona]').forEach(c => c.classList.toggle('is-on', c.dataset.persona === key));
            for (const code of (o.platforms || [])) {
                const slot = o.slot(code);
                const row = o.row ? o.row(code) : null;
                const accts = persona ? (persona.map[code] || []) : (all[code] || []);
                const missing = !!persona && !accts.length;
                if (slot) {
                    slot.innerHTML = missing
                        ? `<span class="acct-as acct-missing" title="A persona publish never falls back to the platform default">no ${esc(persona.label)} account</span>`
                        : control(code, accts, persona);
                }
                if (row) {
                    row.classList.toggle('is-unavailable', missing);
                    const cb = row.querySelector('input[type=checkbox]');
                    if (cb) {
                        if (missing) { cb.checked = false; cb.disabled = true; cb.dataset.personaDisabled = '1'; }
                        else if (cb.dataset.personaDisabled) { cb.disabled = false; delete cb.dataset.personaDisabled; }
                    }
                }
            }
            try { localStorage.setItem(o.storageKey, key); } catch (e) { /* ignore */ }
            if (o.onChange) o.onChange({ ...state });
        };

        if (!options.length) {
            // No persona holds an account on these platforms — nothing to pick
            // between. The pre-4.2.0 per-platform pickers, unchanged.
            o.host.innerHTML = '';
            o.host.hidden = true;
            apply(null);
            return state;
        }
        o.host.hidden = false;
        o.host.innerHTML = `<span class="persona-picker-label muted">Post as</span>` +
            options.map(p => `<button type="button" class="persona-chip" data-persona="${p.id}">` +
                `<span class="persona-dot" style="background:${esc(p.color)}"></span>${esc(p.label)}</button>`).join('') +
            `<button type="button" class="persona-chip persona-chip--all" data-persona="all">All accounts</button>`;
        o.host.querySelectorAll('[data-persona]').forEach(btn =>
            btn.addEventListener('click', () => apply(btn.dataset.persona === 'all' ? null : parseInt(btn.dataset.persona, 10))));

        let last = null;
        try { last = localStorage.getItem(o.storageKey); } catch (e) { /* ignore */ }
        const start = last === 'all' ? null : (options.find(p => String(p.id) === last) || options[0]).id;
        apply(start);
        return state;
    },

    /* ── Telegram ─────────────────────────────────────────────
     *
     * One metric, and one thing these renderers must never do: show a bare 0
     * for a post whose reactions were never observed. Reactions arrive only as
     * pushed updates and cannot be backfilled, so anything published before
     * tracking was switched on has no count and never will. `reactions_counted`
     * (from the API) separates the two, and every surface below renders the
     * unobserved case as "not counted" rather than a number.
     */

    tgReactions(sub) {
        // The single most misleading thing this platform's UI could do is let
        // "we were not listening" look like "nobody reacted".
        if (sub && sub.reactions_counted === false) {
            return '<span class="muted" title="Published before reaction tracking was switched on — Telegram cannot backfill it">not counted</span>';
        }
        return `${Utils.formatNumber((sub && sub.reactions_count) || 0)} ${Utils.formatDelta(sub && sub.reactions_delta)}`;
    },

    tgTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/tg/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey] || '(no title)', 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * Telegram posts table. Columns: Title, Type, Reactions, Posted, Link.
     * No views or comments columns — a channel exposes neither to a bot, so
     * an empty column would imply a measurement that does not exist.
     */
    tgSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Posts PawPoller sends to your channel appear here.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/tg/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title || '(no title)', 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(s.content_type || 'artwork')}</td>
                <td data-label="Reactions">${Components.tgReactions(s)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
                <td data-label="Link">${s.link ? `<a href="${Utils.escapeHtml(s.link)}" target="_blank" rel="noopener">open ↗</a>` : '<span class="muted" title="A private channel has no public permalink">private</span>'}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="tg-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="reactions_count">Reactions</th>
                        <th data-sort="posted_at">Posted</th>
                        <th>Link</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    tgPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No Telegram polls recorded yet.</p>';
        }
        // "Subs" is omitted deliberately: a Telegram poll fetches a subscriber
        // count, not a submission list, so the column would read 0 forever.
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');
        return `
            <table class="data-table">
                <thead><tr><th>Time</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    fbrPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No Furbooru polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── THR Components ───────────────────────────────────────────

    /**
     * Clickable ranked list for THR submissions.
     */
    thrTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/thr/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * THR-specific submissions table.
     * Columns: Title, Type, Views, Likes, Reposts, Replies, Posted.
     */
    thrSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Threads account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/thr/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.THR_TYPE_LABELS[s.content_type] || s.content_type || 'Text')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views || 0)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Reposts">${Utils.formatNumber(s.reposts || 0)} ${Utils.formatDelta(s.reposts_delta)}</td>
                <td data-label="Replies">${Utils.formatNumber(s.replies || 0)} ${Utils.formatDelta(s.replies_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="thr-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="reposts">Reposts</th>
                        <th data-sort="replies">Replies</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * THR-specific poll history table with color-coded status.
     */
    thrPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No THR polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── IG Components ────────────────────────────────────────────

    /**
     * Clickable ranked list for IG submissions.
     */
    igTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/ig/submission/${encodeURIComponent(item[idKey])}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * IG-specific submissions table.
     * Columns: Title, Type, Views, Likes, Reach, Comments, Posted.
     */
    igSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No posts</h3><p>Connect your Instagram account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/ig/submission/${encodeURIComponent(s.submission_id)}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.IG_TYPE_LABELS[s.content_type] || s.content_type || 'Text')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views || 0)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Reach">${Utils.formatNumber(s.reach || 0)} ${Utils.formatDelta(s.reach_delta)}</td>
                <td data-label="Comments">${Utils.formatNumber(s.comments || 0)} ${Utils.formatDelta(s.comments_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="ig-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="reach">Reach</th>
                        <th data-sort="comments">Comments</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * IG-specific poll history table with color-coded status.
     */
    igPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No IG polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── TW Components ────────────────────────────────────────────

    /**
     * Clickable ranked list for TW submissions.
     * Each item navigates to the TW submission detail page via App.navigate().
     */
    twTopList(items, valueKey, labelKey = 'title', idKey = 'submission_id') {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No data yet</p>';
        }
        const lis = items.map(item => `
            <li>
                <span class="top-title" data-nav="/tw/submission/${item[idKey]}">${Utils.escapeHtml(Utils.truncate(item[labelKey], 30))}</span>
                <span class="top-value">${Utils.formatCompact(item[valueKey])}</span>
            </li>
        `).join('');
        return `<ul class="top-list">${lis}</ul>`;
    },

    /**
     * TW-specific submissions table.
     * Columns: Title, Type, Views, Likes, Retweets, Replies, Posted.
     */
    twSubmissionsTable(submissions) {
        if (!submissions || submissions.length === 0) {
            return `<div class="empty-state"><h3>No tweets</h3><p>Connect your X/Twitter account and run a poll to fetch data.</p></div>`;
        }
        const rows = submissions.map(s => `
            <tr>
                <td data-label="Title"><a href="#/tw/submission/${s.submission_id}">${Utils.escapeHtml(Utils.truncate(s.title, 45))}</a></td>
                <td data-label="Type">${Utils.escapeHtml(Components.TW_TYPE_LABELS[s.content_type] || s.content_type || 'Tweet')}</td>
                <td data-label="Views">${Utils.formatNumber(s.views || 0)} ${Utils.formatDelta(s.views_delta)}</td>
                <td data-label="Likes">${Utils.formatNumber(s.likes || 0)} ${Utils.formatDelta(s.likes_delta)}</td>
                <td data-label="Retweets">${Utils.formatNumber(s.retweets || 0)} ${Utils.formatDelta(s.retweets_delta)}</td>
                <td data-label="Replies">${Utils.formatNumber(s.replies || 0)} ${Utils.formatDelta(s.replies_delta)}</td>
                <td data-label="Posted">${Utils.formatDate(s.posted_at)}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" id="tw-submissions-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th data-sort="title">Title</th>
                        <th data-sort="content_type">Type</th>
                        <th data-sort="views">Views</th>
                        <th data-sort="likes">Likes</th>
                        <th data-sort="retweets">Retweets</th>
                        <th data-sort="replies">Replies</th>
                        <th data-sort="posted_at">Posted</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * TW-specific poll history table with color-coded status.
     */
    twPollLogTable(polls) {
        if (!polls || polls.length === 0) {
            return '<p style="color:var(--text-muted)">No TW polls recorded yet.</p>';
        }
        const rows = polls.map(p => `
            <tr>
                <td>${Utils.formatDateTime(p.started_at)}</td>
                <td><span style="color:${p.status === 'success' ? 'var(--success)' : p.status === 'error' ? 'var(--danger)' : 'var(--warning)'}">${p.status}</span></td>
                <td>${p.submissions_found || 0}</td>
                <td>${p.snapshots_inserted || 0}</td>
                <td>${p.duration_seconds ? p.duration_seconds.toFixed(1) + 's' : '--'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${Utils.escapeHtml(p.error_message || '')}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th><th>Status</th><th>Subs</th><th>Snaps</th><th>Duration</th><th>Error</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    // ── Groups Components ────────────────────────────────────────

    /**
     * Grid of group cards that navigate to the group detail page on click.
     * Each card shows group name, description (if present), and member count
     * (number of submissions in the group). Reuses .stat-card styling.
     * @param {Array} groups - Array of group objects with group_id, name, description, member_count
     * @returns {string} HTML string of clickable .stat-card elements
     */
    groupsList(groups) {
        if (!groups || groups.length === 0) {
            return '<div class="empty-state"><h3>No groups yet</h3><p>Create a group to track related submissions across platforms.</p></div>';
        }
        return groups.map(g => `
            <div class="stat-card" style="cursor:pointer" data-nav="/group/${g.group_id}">
                <div class="label">${Utils.escapeHtml(g.name)}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${Utils.escapeHtml(g.description || '')}</div>
                <div style="font-size:12px;color:var(--text-secondary);margin-top:8px">${g.member_count || 0} submissions</div>
            </div>
        `).join('');
    },

    // ── Analytics Components ─────────────────────────────────────

    /**
     * Ranked leaderboard table of top fans.
     * Columns: rank (#), username, fave count, comment count, weighted score.
     * Score formula: (faves * 2) + comments -- weighted to emphasize fave engagement.
     * Rank numbers are 1-indexed from the array position.
     * @param {Array} fans - Array of fan objects with username, fave_count, comment_count, score
     * @returns {string} HTML string for the top fans data-table
     */
    topFansTable(fans) {
        if (!fans || fans.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No fan data available yet. Run polls to build up data.</p>';
        }
        const rows = fans.map((f, i) => `
            <tr>
                <td data-label="#" style="font-weight:600;color:var(--text-muted)">#${i + 1}</td>
                <td data-label="Username">${Utils.escapeHtml(f.username)}</td>
                <td data-label="Faves">${f.fave_count || 0}</td>
                <td data-label="Comments">${f.comment_count || 0}</td>
                <td data-label="Score" style="font-weight:600;color:var(--accent)">${f.score || 0}</td>
            </tr>
        `).join('');

        return `
            <table class="data-table" data-mobile-cards>
                <thead>
                    <tr>
                        <th style="width:40px">#</th><th>Username</th><th>Faves</th><th>Comments</th><th>Score</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /**
     * Spike detection results displayed as clickable cards with platform badges.
     * Each card shows the submission title with platform badge (IB/FA/WS), delta values
     * for views/faves/comments that triggered the spike, and the z-score indicating
     * how far above normal the activity is. Navigates to the correct platform-specific
     * detail page on click.
     * @param {Array} items - Array of trending objects with platform, submission_id, title,
     *                        views_delta, faves_delta, comments_delta, max_z
     * @returns {string} HTML string of clickable .stat-card elements
     */
    trendingCards(items) {
        if (!items || items.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No trending submissions detected. Need at least a few polls to calculate trends.</p>';
        }
        return items.map(item => {
            const badgeMap = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', mast: '<span class="platform-badge mast">MAST</span>', tum: '<span class="platform-badge tum">TUM</span>', pix: '<span class="platform-badge pix">PIX</span>', thr: '<span class="platform-badge thr">THR</span>', ig: '<span class="platform-badge ig">IG</span>', ib: '<span class="platform-badge ib">IB</span>' };
            const platformBadge = badgeMap[item.platform] || badgeMap.ib;
            const prefixMap = { fa: '/fa/submission/', ws: '/ws/submission/', sf: '/sf/submission/', sqw: '/sqw/submission/', ao3: '/ao3/submission/', da: '/da/submission/', wp: '/wp/submission/', ik: '/ik/submission/', bsky: '/bsky/submission/', tw: '/tw/submission/', mast: '/mast/submission/', tum: '/tum/submission/', pix: '/pix/submission/', thr: '/thr/submission/', ig: '/ig/submission/', e621: '/e621/submission/', ib: '/submission/' };
            const prefix = prefixMap[item.platform] || prefixMap.ib;
            const metrics = [];
            if (item.views_delta) metrics.push(`Views +${item.views_delta}`);
            if (item.faves_delta) metrics.push(`Faves +${item.faves_delta}`);
            if (item.comments_delta) metrics.push(`Comments +${item.comments_delta}`);
            return `
                <div class="stat-card" style="cursor:pointer" data-nav="${prefix}${item.submission_id}">
                    <div class="label">${platformBadge} ${Utils.escapeHtml(Utils.truncate(item.title, 35))}</div>
                    <div style="font-size:13px;color:var(--success);margin-top:6px">${metrics.join(' &middot; ')}</div>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:4px">z-score: ${(item.max_z || 0).toFixed(1)}</div>
                </div>
            `;
        }).join('');
    },

    // ── Cross-Platform Link Components ───────────────────────────

    /**
     * Linked submission cards showing members from different platforms.
     * Each card lists all linked submissions with platform badges (IB/FA/WS) and
     * provides Stats and Remove action buttons. Stats button calls App.viewLinkStats()
     * and Remove button calls App.deleteLink() with the link_id.
     * @param {Array} links - Array of link objects with link_id and members array
     *                        (each member has platform, title, submission_id)
     * @returns {string} HTML string of .stat-card elements with action buttons
     */
    linkCards(links) {
        if (!links || links.length === 0) {
            return '<div class="empty-state"><h3>No linked submissions</h3><p>Link the same work across platforms to see combined stats.</p></div>';
        }
        return links.map(link => {
            const members = (link.members || []).map(m => {
                const badgeMap = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', mast: '<span class="platform-badge mast">MAST</span>', tum: '<span class="platform-badge tum">TUM</span>', pix: '<span class="platform-badge pix">PIX</span>', thr: '<span class="platform-badge thr">THR</span>', ib: '<span class="platform-badge ib">IB</span>' };
                const badge = badgeMap[m.platform] || badgeMap.ib;
                return `${badge} ${Utils.escapeHtml(Utils.truncate(m.title || '#' + m.submission_id, 25))}`;
            }).join('<br>');
            return `
                <div class="stat-card">
                    <div style="font-size:13px;margin-bottom:8px">${members}</div>
                    <div style="display:flex;gap:8px;margin-top:8px">
                        <button class="btn btn-secondary" style="font-size:11px" data-link-stats="${link.link_id}">Stats</button>
                        <button class="btn btn-danger" style="font-size:11px" data-link-delete="${link.link_id}">Remove</button>
                    </div>
                </div>
            `;
        }).join('');
    },

    /**
     * Auto-detected similar titles across platforms with similarity percentage
     * and one-click Link button. Titles are compared across IB/FA/WS and shown
     * with bidirectional arrow (&harr;) between platform-badged entries.
     * Similarity score is displayed as a percentage. The Link button calls
     * App.createLinkFromSuggestion() with the full items array to create the link.
     * @param {Array} suggestions - Array of suggestion objects, each with items array
     *                              (platform, title, submission_id) and similarity float
     * @returns {string} HTML string of .fave-item elements with Link buttons
     */
    linkSuggestions(suggestions) {
        if (!suggestions || suggestions.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No suggestions found. Submissions need similar titles across platforms.</p>';
        }
        return suggestions.map(s => {
            // Backend (analytics_queries.auto_suggest_links) returns the pair under
            // `submissions`, NOT `items` — reading s.items threw "undefined.map" and
            // broke the whole Cross-Platform screen. Guard it too.
            const items = (s.submissions || []).map(i => {
                const badgeMap = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', mast: '<span class="platform-badge mast">MAST</span>', tum: '<span class="platform-badge tum">TUM</span>', pix: '<span class="platform-badge pix">PIX</span>', thr: '<span class="platform-badge thr">THR</span>', ib: '<span class="platform-badge ib">IB</span>' };
                const badge = badgeMap[i.platform] || badgeMap.ib;
                return `${badge} ${Utils.escapeHtml(Utils.truncate(i.title, 30))}`;
            }).join(' &harr; ');
            return `
                <div class="fave-item" style="flex-wrap:wrap">
                    <span style="flex:1">${items}</span>
                    <span style="font-size:11px;color:var(--text-muted)">${(s.similarity * 100).toFixed(0)}% match</span>
                    <button class="btn btn-primary" style="font-size:11px;padding:4px 10px" data-link-suggest data-items='${Utils.escapeHtml(JSON.stringify(s.submissions || []))}'>Link</button>
                </div>
            `;
        }).join('');
    },

    /**
     * FA comment display with external furaffinity.net profile links.
     * Supports reply threading via reply_level and reply_to fields: replies with
     * reply_level > 0 or a reply_to value are indented with a left accent border.
     * Each username links externally to https://www.furaffinity.net/user/:username/.
     * @param {Array} comments - Array of FA comment objects with username, comment_text,
     *                           commented_at, reply_level, reply_to
     * @returns {string} HTML string for the .comments-list container
     */
    faCommentsSection(comments) {
        if (!comments || comments.length === 0) {
            return '<p style="color:var(--text-muted);font-size:13px">No comments fetched yet. Comments are fetched during polling when comment count changes.</p>';
        }
        const items = comments.map(c => {
            const indent = (c.reply_level > 0 || c.reply_to) ? 'margin-left:32px;border-left:3px solid var(--accent);' : '';
            const replyTag = c.reply_to ? `<span style="font-size:11px;color:var(--text-muted)">reply</span> ` : '';
            return `
                <div class="comment-card" style="${indent}">
                    <div class="comment-header">
                        ${replyTag}<a href="https://www.furaffinity.net/user/${Utils.escapeHtml(c.username)}/" target="_blank" class="comment-user">${Utils.escapeHtml(c.username)}</a>
                        <span class="comment-date">${Utils.escapeHtml(c.commented_at || '')}</span>
                    </div>
                    <div class="comment-body">${Utils.escapeHtml(c.comment_text)}</div>
                </div>
            `;
        }).join('');
        return `<div class="comments-list">${items}</div>`;
    },

    /* ── Pinned Submissions ──────────────────────────────────── */
    pinnedSubmissions(items, platform) {
        if (!items || items.length === 0) return '';
        /* Platform-aware metric labels: WP uses reads/votes, IK has likes (no views) */
        const metricLabels = { ib: { v: 'views', f: 'faves' }, fa: { v: 'views', f: 'faves' }, ws: { v: 'views', f: 'faves' }, sf: { v: 'views', f: 'faves' }, sqw: { v: 'views', f: 'faves' }, ao3: { v: 'views', f: 'faves' }, da: { v: 'views', f: 'faves' }, wp: { v: 'reads', f: 'votes' }, ik: { v: null, f: 'likes' } };
        const labels = metricLabels[platform] || metricLabels.ib;
        const cards = items.map(sub => `
            <div class="pinned-card" data-nav="${platform === 'ib' ? '' : platform + '/'}submission/${sub.submission_id}">
                <div class="pinned-title">${Utils.escapeHtml(sub.title)}</div>
                <div class="pinned-stats">
                    ${labels.v ? `<div><span>${Utils.formatCompact(sub.views || sub.reads || 0)}</span> ${labels.v}</div>` : ''}
                    <div><span>${Utils.formatCompact(sub.favorites_count || sub.votes || sub.likes || 0)}</span> ${labels.f}</div>
                    <div><span>${Utils.formatCompact(sub.comments_count)}</span> cmts</div>
                </div>
                <button class="btn-unpin" data-platform="${platform}" data-id="${sub.submission_id}">Unpin</button>
            </div>
        `).join('');
        return `<div class="pinned-section"><h3>Pinned</h3><div class="pinned-row">${cards}</div></div>`;
    },

    /* ── Goal Progress Cards ─────────────────────────────────── */
    goalProgressCards(goals) {
        if (!goals || goals.length === 0) return '';
        const metricLabels = { views: 'Views', favorites_count: 'Faves', comments_count: 'Comments', watchers: 'Watchers' };
        const cards = goals.map(g => {
            const pct = g.target_value > 0 ? Math.min(100, Math.round((g.current_value / g.target_value) * 100)) : 0;
            const complete = pct >= 100;
            const title = g.submission_title ? Utils.truncate(g.submission_title, 25) : 'Account Total';
            return `
                <div class="goal-card">
                    <div class="goal-header">
                        <div>
                            <div class="goal-title">${Utils.escapeHtml(title)}</div>
                            <div class="goal-metric">${metricLabels[g.metric] || g.metric}</div>
                        </div>
                        <button class="btn-goal-delete" data-goal-id="${g.goal_id}" title="Delete goal">&#x2715;</button>
                    </div>
                    <div class="goal-progress-bar">
                        <div class="goal-progress-fill ${complete ? 'complete' : ''}" style="width:${pct}%"></div>
                    </div>
                    <div class="goal-numbers">
                        <span class="goal-current">${Utils.formatNumber(g.current_value)}</span>
                        <span>${Utils.formatNumber(g.target_value)} (${pct}%)</span>
                    </div>
                </div>
            `;
        }).join('');
        return `<div class="goal-grid">${cards}</div>`;
    },

    /* ── Tag Badge ───────────────────────────────────────────── */
    tagBadge(tag) {
        return `<span class="tag-badge" data-tag-id="${tag.tag_id}" style="background:${Utils.escapeHtml(tag.color)}">${Utils.escapeHtml(tag.name)}</span>`;
    },

    /* ── Highlight Card (Analytics) ──────────────────────────── */
    highlightCard(label, value, subtitle) {
        return `
            <div class="highlight-card">
                <div class="label">${Utils.escapeHtml(label)}</div>
                <div class="value">${value}</div>
                ${subtitle ? `<div class="subtitle">${Utils.escapeHtml(subtitle)}</div>` : ''}
            </div>
        `;
    },

    /* ── Platform empty state (unconfigured) ─────────────────
     * Drop-in replacement for the stat cards / charts when a
     * platform has no configured credentials. Friendlier than
     * empty cells; the connect CTA points at Settings → Platforms
     * so the user has a one-click path to fix it. */
    platformEmptyState(code, opts = {}) {
        const labels = (window.PlatformHealth && window.PlatformHealth.LABELS) || {};
        const label = labels[code] || code.toUpperCase();
        const emojis = {
            ib: '\u{1F43E}', fa: '\u{1F98A}', ws: '\u{1F98E}', sf: '\u{1F43A}',
            sqw: '\u{1F991}', ao3: '\u{1F4D6}', da: '\u{1F3A8}', wp: '\u{1F4D9}',
            ik: '\u{1F3AF}', bsky: '\u{1F98B}', tw: '\u{1F426}', mast: '\u{1F418}', tum: '\u{1F4D8}', pix: '\u{1F58C}', thr: '\u{1F9F5}', ig: '\u{1F4F8}', e621: '\u{1F43E}',
        };
        const emoji = emojis[code] || '\u{1F517}';
        // Two distinct states:
        //  • not connected  → no opts; CTA is "set up".
        //  • connected but empty → caller passes opts.reason (or .configured).
        //    The account IS connected, it just has no polled data yet, so the
        //    right action is to poll/retry — not "set up". (Without this, an
        //    account with zero items, e.g. an X handle with no tweets, showed
        //    a misleading "not connected" screen and no poll button.)
        const configured = !!(opts.configured || opts.reason);
        if (configured) {
            const reason = opts.reason || `${label} is connected but nothing has been polled yet.`;
            return `
                <div class="platform-empty-state">
                    <div class="empty-state-emoji">${emoji}</div>
                    <h3>No ${Utils.escapeHtml(label)} data yet</h3>
                    <p>${Utils.escapeHtml(reason)}</p>
                    <div class="empty-state-actions">
                        <button class="btn btn-primary" data-poll="${code}">Poll now</button>
                        <a href="#/settings" class="btn btn-secondary">${Utils.escapeHtml(label)} settings</a>
                    </div>
                </div>
            `;
        }
        const reason = opts.reason || `Connect ${label} to start polling.`;
        return `
            <div class="platform-empty-state">
                <div class="empty-state-emoji">${emoji}</div>
                <h3>${Utils.escapeHtml(label)} not connected</h3>
                <p>${Utils.escapeHtml(reason)}</p>
                <a href="#/settings" class="btn btn-primary">Set up ${Utils.escapeHtml(label)}</a>
                <div class="empty-state-secondary">
                    <a href="https://github.com/knaughtykat01-prog/PawPoller#readme" target="_blank" rel="noopener">Setup guide</a>
                </div>
            </div>
        `;
    },

    /* ── System Events Feed (Overview) ───────────────────────
     * Renders the Recent System Events panel — backed by
     * /api/activity/recent. Each event is a poll completion or
     * post action with status colour and platform badge. Showing
     * "the system is working" reduces the ambient anxiety the
     * user reported with the silent-button problem in Batch 1. */
    systemEventsFeed(events) {
        const labels = (window.PlatformHealth && window.PlatformHealth.LABELS) || {};
        if (!events || events.length === 0) {
            return `
                <div class="chart-container">
                    <h3>Recent System Events</h3>
                    <div class="empty-state-mini">No recent activity yet — events will appear here as polls run.</div>
                </div>
            `;
        }
        const rows = events.map((e) => {
            const platLabel = labels[e.platform] || (e.platform || '').toUpperCase();
            const when = (window.PlatformHealth && window.PlatformHealth.relativePast)
                ? window.PlatformHealth.relativePast(e.timestamp)
                : (e.timestamp || '');
            const statusClass = `sys-evt-status-${e.status || 'unknown'}`;
            const tooltip = e.detail
                ? ` data-tooltip="${Utils.escapeHtml(e.detail)}"`
                : '';
            return `
                <li class="sys-evt-row ${statusClass}"${tooltip}>
                    <span class="sys-evt-dot"></span>
                    <span class="sys-evt-platform">${Utils.escapeHtml(platLabel)}</span>
                    <span class="sys-evt-kind">${Utils.escapeHtml(e.kind || 'event')}</span>
                    <span class="sys-evt-summary">${Utils.escapeHtml(e.summary || '')}</span>
                    <span class="sys-evt-when">${Utils.escapeHtml(when)}</span>
                </li>
            `;
        }).join('');
        return `
            <div class="chart-container">
                <h3>Recent System Events</h3>
                <ul class="sys-evt-list">${rows}</ul>
            </div>
        `;
    },
};
