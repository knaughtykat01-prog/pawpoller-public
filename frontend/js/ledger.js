/* ── Ledger — dated timelines (concept-layer Slice D · "Almanac") ─────────────
 *
 * A dated spine of typed events. Two scopes, one renderer:
 *   1. WORK timeline  — a work's own history (posted / updated per platform),
 *      shown as a "Timeline" tab on the Bookshelf work-detail. Built from the
 *      publications already fetched for that page (no extra request).
 *   2. ACTIVITY timeline — the system/account history (polls, posts, issues)
 *      at #/ledger, from the ready-made /api/activity/recent event feed, with
 *      platform + kind filters (a per-platform filter == an account's history).
 *
 * Deliberately NOT the home — time-order buries "is everything OK right now",
 * so this lives as a tab / destination, never the landing page.
 * Template-string rendering, CSP-safe (delegated listeners, no inline handlers). */
window.Ledger = {

    esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _num(n) { return (window.Utils && Utils.formatNumber) ? Utils.formatNumber(n || 0) : String(n || 0); },
    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || { code, label: code, emoji: '', color: '#888' };
    },

    // Node character per event type: icon + a semantic status→colour class.
    _TYPE: {
        post:      { icon: '\u{1F4E4}', label: 'Posted' },
        update:    { icon: '\u{270E}', label: 'Updated' },
        edit:      { icon: '\u{270E}', label: 'Edited' },
        poll:      { icon: '\u{1F504}', label: 'Polled' },
        milestone: { icon: '\u{1F3C5}', label: 'Milestone' },
        created:   { icon: '\u{1F4DD}', label: 'Created' },
        session:   { icon: '\u{1F50C}', label: 'Session' },
    },
    _node(type) { return this._TYPE[type] || { icon: '•', label: type || 'Event' }; },

    _date(iso) {
        if (!iso) return null;
        const d = new Date(iso);
        return isNaN(d) ? null : d;
    },
    _dayKey(d) { return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; },
    _dayLabel(d) {
        try {
            return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
        } catch (e) { return d.toISOString().slice(0, 10); }
    },
    _timeLabel(d) {
        try { return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); }
        catch (e) { return ''; }
    },

    /* ── Core renderer: a dated spine grouped by day ─────────────── */
    renderTimeline(container, events, opts) {
        if (!container) return;
        opts = opts || {};
        const parsed = (events || [])
            .map(e => ({ ...e, _d: this._date(e.date) }))
            .filter(e => e._d)
            .sort((a, b) => b._d - a._d);

        if (!parsed.length) {
            container.innerHTML = `<div class="led-empty">${this.esc(opts.empty || 'Nothing on the timeline yet.')}</div>`;
            return;
        }

        // Group into day buckets (already sorted newest-first).
        const groups = [];
        let cur = null;
        parsed.forEach(e => {
            const k = this._dayKey(e._d);
            if (!cur || cur.key !== k) { cur = { key: k, day: e._d, items: [] }; groups.push(cur); }
            cur.items.push(e);
        });

        container.innerHTML = `<div class="led-spine">${groups.map(g => `
            <div class="led-day">
                <div class="led-day-label">${this.esc(this._dayLabel(g.day))}</div>
                <div class="led-day-nodes">
                    ${g.items.map(e => this._nodeHtml(e)).join('')}
                </div>
            </div>`).join('')}</div>`;
    },

    _nodeHtml(e) {
        const n = this._node(e.type);
        const status = (e.status || '').toLowerCase();
        const statusCls = /err|fail/.test(status) ? 'is-error'
            : /partial|warn/.test(status) ? 'is-warn'
            : /run/.test(status) ? 'is-running' : 'is-ok';
        const plat = e.platform && e.platform !== 'posting' ? this._plat(e.platform) : null;
        const platChip = plat
            ? `<span class="led-plat" title="${this.esc(plat.label)}">${plat.emoji || ''} ${this.esc(plat.label)}</span>` : '';
        const time = this._timeLabel(e._d);
        const link = e.url
            ? `<a class="led-open" href="${this.esc(e.url)}" target="_blank" rel="noopener">open ↗</a>` : '';
        const detail = e.detail ? `<div class="led-detail">${this.esc(e.detail)}</div>` : '';
        return `
            <div class="led-node ${statusCls}">
                <span class="led-dot" aria-hidden="true">${n.icon}</span>
                <div class="led-body">
                    <div class="led-line">
                        <span class="led-title">${this.esc(e.title)}</span>
                        ${platChip}
                        ${time ? `<span class="led-time">${this.esc(time)}</span>` : ''}
                        ${link}
                    </div>
                    ${detail}
                </div>
            </div>`;
    },

    /* ── Work timeline (from an already-fetched posting-story `d`) ── */
    workEvents(name, d) {
        const evs = [];
        if (d && d.created_at) {
            evs.push({ date: d.created_at, type: 'created', title: `“${d.title || name}” created` });
        }
        (d && d.publications || []).forEach(p => {
            const plat = this._plat(p.platform);
            const chap = (p.chapter_index && p.chapter_index > 0)
                ? ` — ${p.chapter_title || 'Ch. ' + p.chapter_index}` : '';
            if (p.first_posted_at) {
                evs.push({
                    date: p.first_posted_at, type: 'post', platform: p.platform,
                    title: `Posted to ${plat.label}${chap}`, url: p.external_url, status: p.status,
                });
            }
            if (p.last_updated_at && p.update_count > 0 && p.last_updated_at !== p.first_posted_at) {
                evs.push({
                    date: p.last_updated_at, type: 'update', platform: p.platform,
                    title: `Updated on ${plat.label}${chap}`, url: p.external_url,
                    detail: p.update_count > 1 ? `${p.update_count} updates in total` : '',
                });
            }
        });
        return evs;
    },

    renderWorkTimeline(container, name, d) {
        this.renderTimeline(container, this.workEvents(name, d), {
            empty: 'No history yet — publish this work and its timeline fills in.',
        });
    },

    /* ── Standalone Activity ledger (#/ledger) ───────────────────── */
    _filter: 'all',      // all | post | poll | issue
    _platform: '',       // '' = all platforms
    _events: [],

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="led-head">
                <div class="led-eyebrow">Almanac</div>
                <h1 class="led-h1">Activity</h1>
                <p class="led-sub">Everything PawPoller has done for you, newest first — polls, posts and
                anything that needs a look. Filter by platform to read one account's history.</p>
            </div>
            <div id="led-controls"></div>
            <div id="led-stream"><div class="loading-spinner">Reading the ledger…</div></div>`;

        let data;
        try { data = await API.getRecentActivity(150); }
        catch (err) {
            const s = document.getElementById('led-stream');
            if (s) s.innerHTML = `<div class="card error">Couldn't load activity: ${this.esc(err.message)}</div>`;
            return;
        }
        this._events = (data && data.events || []).map(e => ({
            date: e.timestamp,
            type: (e.kind === 'edit' ? 'update' : (e.kind || 'poll')),
            title: e.summary || (e.kind || 'Event'),
            detail: e.detail || '',
            platform: e.platform,
            status: e.status,
            _kind: e.kind, _statusRaw: (e.status || '').toLowerCase(),
        }));
        this._paint();
    },

    _paint() {
        const controls = document.getElementById('led-controls');
        const stream = document.getElementById('led-stream');
        if (!controls || !stream) return;

        // Platforms actually present in the feed → filter chips.
        const platsPresent = [...new Set(this._events.map(e => e.platform).filter(c => c && c !== 'posting'))];
        const seg = (id, label) => `<button class="led-seg ${this._filter === id ? 'is-active' : ''}" data-led-filter="${id}">${label}</button>`;
        controls.innerHTML = `
            <div class="led-controls">
                <div class="led-segs">
                    ${seg('all', 'All')}${seg('post', 'Posts')}${seg('poll', 'Polls')}${seg('issue', 'Issues')}
                </div>
                <select class="led-platsel" id="led-platsel">
                    <option value="">All platforms</option>
                    ${platsPresent.map(c => `<option value="${this.esc(c)}" ${this._platform === c ? 'selected' : ''}>${this.esc(this._plat(c).label)}</option>`).join('')}
                </select>
            </div>`;

        const filtered = this._events.filter(e => {
            if (this._platform && e.platform !== this._platform) return false;
            if (this._filter === 'post') return e._kind === 'post' || e.type === 'update';
            if (this._filter === 'poll') return e._kind === 'poll';
            if (this._filter === 'issue') return /err|fail|partial|warn/.test(e._statusRaw);
            return true;
        });
        this.renderTimeline(stream, filtered, {
            empty: this._platform || this._filter !== 'all'
                ? 'Nothing matches this filter.'
                : 'No activity recorded yet — connect an account and start polling.',
        });

        // Bind controls once per paint (fresh nodes each time).
        controls.querySelectorAll('[data-led-filter]').forEach(btn => {
            btn.addEventListener('click', () => { this._filter = btn.dataset.ledFilter; this._paint(); });
        });
        const sel = document.getElementById('led-platsel');
        if (sel) sel.addEventListener('change', () => { this._platform = sel.value; this._paint(); });
    },
};
