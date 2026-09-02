/* ── Utility functions ─────────────────────────────────────── */
/*
 * Utils — singleton object of stateless helper functions used across
 * every frontend module (dashboard, detail views, tables, charts).
 *
 * All methods are pure (no side-effects, no DOM mutation) except where
 * they return HTML strings for innerHTML injection — those are clearly
 * marked.  Date handling uses Australian (en-AU) locale throughout.
 */

const Utils = {

    /* ── formatNumber ────────────────────────────────────────────
     * Locale-aware number formatting (e.g. 1234 -> "1,234").
     * Returns "0" for null/undefined so callers never see "NaN".
     */
    /* Bytes as a human size. Added for the Sync panel (3.18.0), whose whole
       job is making a transfer size legible — "0.2 MB" vs "158.6 MB" is the
       difference the per-file fetch bought. */
    formatBytes(n) {
        const b = Number(n) || 0;
        if (b < 1024) return b + ' B';
        if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
        if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
        return (b / 1073741824).toFixed(2) + ' GB';
    },

    formatNumber(n) {
        if (n == null) return '0';
        return Number(n).toLocaleString();
    },

    /* ── formatCompact ──────────────────────────────────────────
     * Human-readable abbreviated numbers for dashboard stat cards.
     * 1500 -> "1.5K", 2300000 -> "2.3M".  Values under 1000 are
     * returned as-is without a suffix.
     */
    formatCompact(n) {
        if (n == null) return '0';
        n = Number(n);
        if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
        if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
        return n.toString();
    },

    /* ── formatDelta ───────────────────────────────────────────
     * Returns an HTML <span> showing a signed, colour-coded change
     * indicator for 24-hour deltas.  Positive values get a green
     * "+" prefix, negative values get red, and zero/null shows a
     * neutral "--" placeholder.  Used in stat cards and tables.
     */
    formatDelta(n) {
        if (!n || n === 0) return '<span class="delta neutral">--</span>';
        const sign = n > 0 ? '+' : '';
        const cls = n > 0 ? 'positive' : 'negative';
        return `<span class="delta ${cls}">${sign}${Utils.formatNumber(n)}</span>`;
    },

    /* ── _parseDate (private) ────────────────────────────────────
     * Normalises the variety of date-string formats returned by the
     * backend into valid Date objects:
     *   - Inkbunny's "+00" TZ suffix  -> already parseable, passed through
     *   - Bare "YYYY-MM-DD HH:MM:SS"  -> gets "T" separator and "Z" suffix
     *   - ISO with "T" but no "Z"     -> passed through (browser assumes local)
     *   - Full ISO "...T...Z"         -> already valid, passed through
     * The leading underscore signals this is an internal helper; all
     * public date methods delegate to it first.
     */
    _parseDate(dateStr) {
        if (!dateStr) return null;
        // Handle Inkbunny's "+00" timezone format and bare datetimes
        let s = dateStr.trim();
        if (!s.includes('Z') && !s.includes('+') && !s.includes('T')) {
            s = s.replace(' ', 'T') + 'Z';
        } else if (!s.includes('T')) {
            s = s.replace(' ', 'T');
        }
        return new Date(s);
    },

    /* ── formatDate ───────────────────────────────────────────
     * Smart short-date formatting in Australian locale (en-AU).
     * Omits the year when the date falls in the current calendar year
     * (e.g. "4 Mar") and includes it otherwise (e.g. "4 Mar 2024").
     * Returns "--" for unparseable or missing input.
     */
    formatDate(dateStr) {
        const d = this._parseDate(dateStr);
        if (!d || isNaN(d)) return '--';
        const now = new Date();
        const sameYear = d.getFullYear() === now.getFullYear();
        if (sameYear) {
            return d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' });
        }
        return d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' });
    },

    /* ── formatDateTime ────────────────────────────────────────
     * Relative-friendly date + time string in Australian locale:
     *   - Same calendar day    -> "Today 2:30 PM"
     *   - Previous calendar day -> "Yesterday 2:30 PM"
     *   - Same year            -> "4 Mar, 2:30 PM"
     *   - Older                -> "4 Mar 2024, 2:30 PM"
     * Used in snapshot tables, poll logs, and detail views.
     */
    formatDateTime(dateStr) {
        const d = this._parseDate(dateStr);
        if (!d || isNaN(d)) return '--';
        const now = new Date();
        const diffMs = now - d;
        const diffHrs = diffMs / 3600000;

        // Today: "2:30 PM"
        if (diffHrs < 24 && d.getDate() === now.getDate()) {
            return 'Today ' + d.toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' });
        }
        // Yesterday
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        if (d.getDate() === yesterday.getDate() && d.getMonth() === yesterday.getMonth()) {
            return 'Yesterday ' + d.toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' });
        }
        // This year: "4 Mar, 2:30 PM"
        if (d.getFullYear() === now.getFullYear()) {
            return d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' }) + ', ' +
                   d.toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' });
        }
        // Older
        return d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' }) + ', ' +
               d.toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' });
    },

    /* ── timeAgo ──────────────────────────────────────────────
     * Compact relative-time string using escalating units:
     *   <1 min  -> "just now"
     *   <1 hr   -> "Xm ago"
     *   <1 day  -> "Xh ago"
     *   <1 week -> "Xd ago"
     *   <5 weeks-> "Xw ago"
     *   else    -> "Xmo ago"
     * Used beside poll-log timestamps and "last updated" badges.
     */
    timeAgo(dateStr) {
        const d = this._parseDate(dateStr);
        if (!d || isNaN(d)) return '--';
        const diff = Date.now() - d.getTime();
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return 'just now';
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        const days = Math.floor(hrs / 24);
        if (days < 7) return `${days}d ago`;
        const weeks = Math.floor(days / 7);
        if (weeks < 5) return `${weeks}w ago`;
        const months = Math.floor(days / 30);
        return `${months}mo ago`;
    },

    /* ── escapeHtml ───────────────────────────────────────────
     * Prevents XSS when user-supplied strings (submission titles,
     * usernames, descriptions) are inserted via innerHTML.  Encodes
     * the four dangerous HTML characters: & < > "
     */
    escapeHtml(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    /* ── safeUrl ──────────────────────────────────────────────
     * Neutralize dangerous URL schemes before a value from an EXTERNAL
     * source (scraped submission permalinks, discovered-art URLs, poll
     * data) is placed into an href/src. escapeHtml does NOT stop
     * `javascript:` — an HTML-escaped `javascript:alert(1)` still executes
     * on click. This allowlists safe forms and collapses everything else
     * (javascript:, vbscript:, data:text/html, ...) to ''.
     *   - relative / same-origin / anchors → always safe
     *   - http(s):// and blob: → safe
     *   - data:image/ → safe (inline images only; not data:text/html)
     * Returns '' for anything else; callers typically `|| '#'`.
     */
    safeUrl(url) {
        const s = String(url == null ? '' : url).trim();
        if (s === '') return '';
        if (/^(?:\/(?!\/)|#|\?|\.)/.test(s)) return s;      // relative / anchor / query
        if (/^https?:\/\//i.test(s)) return s;
        if (/^blob:/i.test(s)) return s;
        if (/^data:image\//i.test(s)) return s;
        return '';
    },

    /* ── cssUrl ───────────────────────────────────────────────
     * A URL safe to drop inside a CSS url('...') in an inline style.
     * Scheme-checks via safeUrl(), then percent-encodes the characters
     * that could break out of the url() string or the surrounding HTML
     * style attribute (quotes, parens, angle brackets, whitespace,
     * backslash). Returns '' when the scheme is unsafe. Wrap the result
     * as url('<here>').
     */
    cssUrl(url) {
        const s = this.safeUrl(url);
        if (!s) return '';
        // NB: encodeURIComponent leaves ' ( ) * ! untouched, so percent-encode
        // the breakout set explicitly by char code instead.
        return s.replace(/["'()\\\s<>]/g,
            c => '%' + c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0'));
    },

    /* ── truncate ─────────────────────────────────────────────
     * Clips long strings to `len` characters and appends "..." for
     * table cells, top-performer lists, and anywhere space is tight.
     * Returns an empty string for null/undefined input.
     */
    truncate(str, len = 60) {
        if (!str) return '';
        return str.length > len ? str.substring(0, len) + '...' : str;
    },

    /* ── thumbUrl / faThumbUrl ────────────────────────────────
     * Generate proxy URLs for Inkbunny and FurAffinity thumbnails.
     * The frontend cannot load these images directly due to CORS
     * restrictions and mixed-content (HTTP/HTTPS) issues, so they
     * are routed through the backend's /api/thumb and /api/fa/thumb
     * endpoints which fetch and relay the image bytes.
     */
    thumbUrl(url) {
        if (!url) return '';
        return '/api/thumb?url=' + encodeURIComponent(url);
    },

    faThumbUrl(url) {
        if (!url) return '';
        return '/api/fa/thumb?url=' + encodeURIComponent(url);
    },

    pixThumbUrl(url) {
        if (!url) return '';
        return '/api/pix/thumb?url=' + encodeURIComponent(url);
    },

    /* ── getDateRange ───────────────────────────────────────────
     * Converts a UI preset string ("24h", "7d", "30d", "90d", "all")
     * into { start, end } ISO datetime strings (without the "T" and
     * "Z" — formatted as "YYYY-MM-DD HH:MM:SS") suitable for passing
     * directly to API query parameters.  "all" returns both as null so
     * the backend omits the time filter entirely.
     */
    getDateRange(preset) {
        const now = new Date();
        let start = null;
        switch (preset) {
            case '24h':
                start = new Date(now.getTime() - 24 * 60 * 60 * 1000);
                break;
            case '7d':
                start = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
                break;
            case '30d':
                start = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
                break;
            case '90d':
                start = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
                break;
            case 'all':
            default:
                return { start: null, end: null };
        }
        return {
            start: start.toISOString().replace('T', ' ').substring(0, 19),
            end: now.toISOString().replace('T', ' ').substring(0, 19),
        };
    },

    /**
     * Build a CSV blob from headers + rows and trigger a browser
     * download. Cells starting with `=`/`+`/`-`/`@`/`\t`/`\r` get a
     * leading apostrophe (OWASP CSV-injection mitigation), matching
     * the same rule the backend uses on its own CSV exports.
     */
    downloadCSV(headers, rows, filename) {
        const sanitiseCell = (val) => {
            const s = String(val ?? '');
            const first = s.charAt(0);
            const safe = (first === '=' || first === '+' || first === '-'
                          || first === '@' || first === '\t' || first === '\r')
                          ? "'" + s : s;
            // Quote if the cell contains comma / quote / newline.
            if (/[",\n]/.test(safe)) {
                return '"' + safe.replace(/"/g, '""') + '"';
            }
            return safe;
        };
        const lines = [headers.map(sanitiseCell).join(',')];
        for (const row of rows) {
            lines.push(row.map(sanitiseCell).join(','));
        }
        // Excel-compatible: BOM + CRLF.
        const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'pawpoller-export.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Release the object URL after the download dialog has had a
        // chance to grab the blob — Chrome holds the reference until
        // navigation, but we clear it for tidiness.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    /** YYYY-MM-DD stamp suitable for embedding in a download filename. */
    dateStamp() {
        const d = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    },
};
