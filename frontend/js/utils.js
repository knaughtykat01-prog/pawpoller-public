/* ── Utility functions ─────────────────────────────────────── */
/*
 * Utils — singleton object of stateless helper functions used across
 * every frontend module (dashboard, detail views, tables, charts).
 *
 * All methods are pure (no side-effects, no DOM mutation) except where
 * they return HTML strings for innerHTML injection — those are clearly
 * marked.  Dates go through Utils.time: en-AU wording, the operator's time zone.
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

    /* ── time (4.49.0, spec 016) ─────────────────────────────────
     * The ONE place a date string becomes a Date and a Date becomes text. Every time on
     * screen is shown in the operator's zone (Settings → Preferences → display_timezone,
     * set at boot by App._refreshPrefsFromServer), not whatever zone the browser is in.
     * Storage stays naive UTC; anything without a zone marker is read as UTC.
     *   parse(v)            → Date | null. Date-only values ("2026-09-26", AO3/SquidgeWorld)
     *                         come back flagged `dateOnly` and always show as that calendar day.
     *   fmt.date/time/dateTime(v), dayKey(v)  → text / 'YYYY-MM-DD' in the zone.
     *   toPicker(v) / toUtc('YYYY-MM-DDTHH:MM') ↔ a datetime-local value in the zone.
     * Locale stays en-AU (the app's wording so far); the zone is what moved. */
    time: {
        zone: null,          // IANA name; null = the browser's own zone
        locale: 'en-AU',
        _cache: {},
        _ISO: /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$/,

        setZone(z) {
            let ok = null;
            if (z) {
                try { new Intl.DateTimeFormat('en-AU', { timeZone: z }); ok = z; } catch (e) { ok = null; }
            }
            if (ok !== this.zone) { this.zone = ok; this._cache = {}; }
            return ok;
        },

        /* The zone in use, as a name (the browser's when none is set). */
        zoneName() {
            try { return this.zone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
            catch (e) { return 'UTC'; }
        },

        parse(v) {
            if (v === null || v === undefined || v === '') return null;
            if (v instanceof Date) return isNaN(v) ? null : v;
            if (typeof v === 'number') return new Date(v);
            const s = String(v).trim();
            const m = this._ISO.exec(s);
            if (m) {
                const [, y, mo, d, hh, mi, ss, frac, tz] = m;
                const ms = frac ? Math.round(Number('0.' + frac) * 1000) : 0;
                let t = Date.UTC(+y, +mo - 1, +d, +(hh || 0), +(mi || 0), +(ss || 0), ms);
                if (tz && tz !== 'Z' && tz !== 'z') {
                    const digits = tz.slice(1).replace(':', '');
                    const off = (+digits.slice(0, 2)) * 60 + (+(digits.slice(2, 4) || 0));
                    t -= (tz[0] === '+' ? 1 : -1) * off * 60000;
                }
                const out = new Date(t);
                if (isNaN(out)) return null;
                if (hh === undefined) out.dateOnly = true;
                return out;
            }
            // An HTTP date ("Thu, 02 Oct 2026 01:00:00 GMT") says its zone; prose (FurAffinity
            // "August 7, 2019 11:57:56 PM"; Newgrounds) doesn't and is stored as UTC.
            const p = / (GMT|UTC)$/.test(s) ? new Date(s) : new Date(s.replace(/,(\s*\d{1,2}:)/, '$1') + ' UTC');
            return isNaN(p) ? null : p;
        },

        _f(opts, dateOnly, zone) {
            const key = JSON.stringify(opts) + (dateOnly ? '|d' : '') + '|' + (zone || '');
            if (!this._cache[key]) {
                const tz = dateOnly ? 'UTC' : (zone || this.zone);
                this._cache[key] = new Intl.DateTimeFormat(opts.locale || this.locale,
                    Object.assign({}, opts, { locale: undefined }, tz ? { timeZone: tz } : {}));
            }
            return this._cache[key];
        },

        /* The zone's short name at that instant: "AEST", "AEDT", or "GMT+10" where Intl has none. */
        abbr(v) {
            const d = this.parse(v) || new Date();
            const part = this._f({ hour: 'numeric', timeZoneName: 'short' }).formatToParts(d)
                .find(x => x.type === 'timeZoneName');
            return part ? part.value : '';
        },

        /* Epoch ms of a server time, NaN when unreadable (for comparisons). */
        ms(v) { const d = this.parse(v); return d ? d.getTime() : NaN; },

        dayKey(v) {
            const d = this.parse(v);
            if (!d) return '';
            return this._f({ locale: 'en-CA', year: 'numeric', month: '2-digit', day: '2-digit' }, d.dateOnly).format(d);
        },

        /* Any Intl options, in the zone ("Fri 8:00 pm", "October 2026"). */
        format(v, opts) {
            const d = this.parse(v);
            return d ? this._f(opts, d.dateOnly).format(d) : '--';
        },

        fmt: {
            date(v) {
                const T = Utils.time, d = T.parse(v);
                if (!d) return '--';
                const sameYear = T.dayKey(d).slice(0, 4) === T.dayKey(new Date()).slice(0, 4);
                return T._f(sameYear ? { day: 'numeric', month: 'short' }
                    : { day: 'numeric', month: 'short', year: 'numeric' }, d.dateOnly).format(d);
            },
            day(v) {   // "Thu, 1 Oct 2026" — a day heading
                const T = Utils.time, d = T.parse(v);
                return d ? T._f({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }, d.dateOnly).format(d) : '--';
            },
            time(v) {
                const T = Utils.time, d = T.parse(v);
                return d ? T._f({ hour: 'numeric', minute: '2-digit' }).format(d) : '--';
            },
            dateTime(v) {
                const T = Utils.time, d = T.parse(v);
                if (!d) return '--';
                if (d.dateOnly) return T.fmt.date(d);
                const key = T.dayKey(d), now = new Date();
                const yest = new Date(Date.parse(T.dayKey(now) + 'T00:00:00Z') - 86400000).toISOString().slice(0, 10);
                if (key === T.dayKey(now)) return 'Today ' + T.fmt.time(d);
                if (key === yest) return 'Yesterday ' + T.fmt.time(d);
                return T.fmt.date(d) + ', ' + T.fmt.time(d);
            },
        },

        /* Minutes the zone (default: the saved one) is ahead of UTC at instant `ms`. */
        _offset(ms, zone) {
            const p = {};
            this._f({ locale: 'en-CA', hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit' }, false, zone).formatToParts(new Date(ms))
                .forEach(x => { p[x.type] = x.value; });
            const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
            return Math.round((wall - Math.floor(ms / 1000) * 1000) / 60000);
        },

        /* A Date → 'YYYY-MM-DDTHH:MM' wall time in the zone, for <input type="datetime-local">. */
        toPicker(v, zone) {
            const d = this.parse(v);
            if (!d) return '';
            const w = new Date(d.getTime() + this._offset(d.getTime(), zone) * 60000);
            return w.toISOString().slice(0, 16);
        },

        /* 'YYYY-MM-DDTHH:MM' wall time in the zone → Date (UTC instant), or null.
         * A time that doesn't exist (the hour skipped when clocks go forward) comes back
         * moved forward by the gap, flagged `shifted`, so callers can say so. */
        toUtc(local, zone) {
            const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(local || ''));
            if (!m) return null;
            const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
            let t = guess - this._offset(guess, zone) * 60000;
            const t2 = guess - this._offset(t, zone) * 60000;
            const want = String(local).slice(0, 16);
            const fits = x => this.toPicker(new Date(x), zone) === want;
            if (!fits(t)) t = fits(t2) ? t2 : Math.max(t, t2);   // in the gap: the later reading
            const out = new Date(t);
            if (!fits(t)) out.shifted = true;
            return out;
        },

        /* A picker value moved by whole days in WALL time ("same time, N days on") — what a
         * drip does on the server, so the preview agrees across a daylight-saving change. */
        addDays(local, days) {
            const d = new Date(Date.parse(String(local).slice(0, 10) + 'T00:00:00Z') + days * 86400000);
            return d.toISOString().slice(0, 10) + String(local).slice(10, 16);
        },

        /* 'YYYY-MM-DDTHH:MM' for `minutes` from now, in the zone (a picker's default). */
        pickerIn(minutes) { return this.toPicker(new Date(Date.now() + minutes * 60000)); },

        /* Tomorrow (in the zone) at 'HH:MM' -> a picker value. */
        pickerTomorrowAt(hhmm) {
            const day = new Date(Date.parse(this.dayKey(new Date()) + 'T00:00:00Z') + 86400000).toISOString().slice(0, 10);
            return `${day}T${hhmm}`;
        },

        /* An ISO string with Z for the API ('2026-10-08T07:00:00.000Z'). */
        toUtcIso(local) {
            const d = this.toUtc(local);
            return d ? d.toISOString() : '';
        },
    },

    /* ── skeleton (4.51.0, spec 017) ─────────────────────────────
     * A page's first paint while its data loads: grey shapes in the page's real layout (so
     * nothing jumps when content lands) instead of a bare "Loading...". While a skeleton is on
     * screen a thin line sweeps the top (CSS: body:has(#app .skel)). After 8 s a line says what
     * it's waiting for — a CSS delay, no timer to clean up.
     *   kind: 'cards' (Library), 'rows' (lists — the default), 'chart' (Analytics),
     *         'overview' (tiles + chart). */
    skeleton(kind = 'rows', n = 8, page = '') {
        const card = '<div class="skel-card"><div class="skel-img"></div><div class="skel-l"></div><div class="skel-l s"></div></div>';
        const row = '<div class="skel-row"><div class="skel-l"></div><div class="skel-l s"></div></div>';
        const tiles = '<div class="skel-tiles">' + '<div class="skel-tile"></div>'.repeat(4) + '</div>';
        const body = kind === 'cards' ? `<div class="skel-grid">${card.repeat(n)}</div>`
            : kind === 'chart' ? `${tiles.replace(/(<div class="skel-tile"><\/div>){4}/, '<div class="skel-tile"></div>'.repeat(3))}<div class="skel-chart"></div>${row.repeat(3)}`
            : kind === 'overview' ? `<div class="skel-l s" style="max-width:40%"></div>${tiles}<div class="skel-chart"></div>`
            : row.repeat(n);
        const what = page ? `Still loading ${Utils.escapeHtml(page)}…` : 'Still loading…';
        return `<div class="skel" aria-busy="true" aria-label="Loading">${body}
            <p class="skel-slow" role="status">${what} The server is taking longer than usual.</p></div>`;
    },

    /* Old names, kept for the ~80 callers: thin wrappers over Utils.time (4.49.0). */
    _parseDate(dateStr) { return Utils.time.parse(dateStr); },
    formatDate(dateStr) { return Utils.time.fmt.date(dateStr); },
    formatDateTime(dateStr) { return Utils.time.fmt.dateTime(dateStr); },

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

// First paint in the saved zone: App._refreshPrefsFromServer caches it here.
try { Utils.time.setZone(localStorage.getItem('pp-display-tz')); } catch (e) { /* no storage */ }

// A top-level const is not a window property; 28 call sites guard on window.Utils (4.43.1).
if (typeof window !== 'undefined') window.Utils = Utils;   // node-run tests have no window
