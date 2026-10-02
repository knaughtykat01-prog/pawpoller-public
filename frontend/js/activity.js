/* Activity — the pill, the tray, the result toast and the publish grid (4.51.0, spec 017).
 *
 * One read: GET /api/activity → { jobs, polls }. Jobs are what the server is doing for you
 * (a publish to five sites, a scheduled slot firing); each has one line per site with the
 * step it is on (posting/activity.py). Polls come from the pollers' own progress.
 *
 *   pill   appears under the top bar while something runs; ring = sites done / sites;
 *          turns green for a few seconds when a job finishes, or red and STAYS until opened.
 *   tray   opens from the pill: each job's sites with their step, Retry for a failed site,
 *          Cancel the rest, the polls, what's queued next. A bottom sheet on a phone.
 *   toast  once per finished job this tab watched: a chip per site (click → the post).
 *   grid   Activity.renderGrid(el, jobId) — the publish screen's site × step view.
 *
 * Polls the server once a second while anything runs, every 15 s otherwise, and not at all
 * while the tab is hidden. Plain page loads never show the pill.
 */
(function () {
    'use strict';

    const FAST = 1000, SLOW = 15000;
    const SEEN_KEY = 'pp-activity-seen';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    let _data = { jobs: [], polls: {} };
    let _timer = null, _pill = null, _tray = null, _open = false;
    const _watched = new Set();     // jobs this tab saw running → toast when they finish
    const _toasted = new Set();
    const _grids = new Map();       // element → job id (publish screens)
    const _log = new Map();         // job id → [{t, text, cls}] (the grid's running log)
    const _lastStep = new Map();    // `${job}|${key}` → step text (to log transitions)

    function seen() {
        try { return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')); } catch (e) { return new Set(); }
    }
    function markSeen(ids) {
        try {
            const s = [...seen(), ...ids].slice(-100);
            localStorage.setItem(SEEN_KEY, JSON.stringify(s));
        } catch (e) { /* private mode: the pill just forgets */ }
    }

    function label(code) {
        const p = (window.PLATFORMS || []).find(x => x.code === code);
        if (p) return p.label;
        const lbl = window.App && App._platformLabels && App._platformLabels[code];
        return lbl || String(code || '').toUpperCase();
    }
    const lineLabel = ln => (ln.label && ln.label !== ln.key ? ln.label : label(ln.key));
    const counts = j => {
        const n = j.lines.length, d = j.lines.filter(l => ['done', 'failed', 'cancelled'].includes(l.state)).length;
        return { n, d, bad: j.lines.filter(l => l.state === 'failed').length };
    };
    const verb = j => ({ publish: 'Posting', update: 'Updating', post: 'Posting', scheduled: 'Posting' }[j.kind] || 'Working on');
    const lineText = ln => {
        if (ln.state === 'done') return ln.step === 'Done' ? 'Done' : ln.step;
        if (ln.state === 'failed') return ln.error || 'Failed';
        if (ln.state === 'cancelled') return 'Not sent';
        return ln.step + (ln.detail ? ` · ${ln.detail}` : '') + (ln.pct != null ? ` · ${Math.round(ln.pct)}%` : '');
    };

    // ── polling ────────────────────────────────────────────────────
    async function poll() {
        clearTimeout(_timer);
        if (document.hidden) return;
        try {
            const r = await fetch('/api/activity', { cache: 'no-store' });
            if (r.ok) ingest(await r.json());
        } catch (e) { /* offline: try again on the slow beat */ }
        const busy = _data.jobs.some(j => j.state === 'running') || Object.keys(_data.polls || {}).length;
        _timer = setTimeout(poll, busy ? FAST : SLOW);
    }

    function ingest(d) {
        _data = { jobs: d.jobs || [], polls: d.polls || {} };
        settleWaiters();   // before the toast check: a screen that ran the job reports it itself
        const now = new Date();
        for (const j of _data.jobs) {
            if (j.state === 'running') _watched.add(j.id);
            for (const ln of j.lines) {
                const k = `${j.id}|${ln.key}`, txt = lineText(ln);
                if (_lastStep.get(k) !== txt && ln.state !== 'waiting') {
                    _lastStep.set(k, txt);
                    const L = _log.get(j.id) || [];
                    L.unshift({ t: now, text: `${lineLabel(ln)} · ${txt}`, cls: ln.state === 'done' ? 'ok' : ln.state === 'failed' ? 'bad' : '' });
                    _log.set(j.id, L.slice(0, 40));
                }
            }
            if (j.finished && _watched.has(j.id) && !_toasted.has(j.id)) {
                _toasted.add(j.id);
                resultToast(j);
                if (window.NotificationCenter && NotificationCenter.poll) NotificationCenter.poll();
            }
        }
        drawPill();
        if (_open) drawTray();
        for (const [el, jid] of _grids) {
            if (!document.body.contains(el)) { _grids.delete(el); continue; }
            drawGrid(el, jid);
        }
    }

    // ── the pill ───────────────────────────────────────────────────
    function ring(frac, size, cls) {
        const r = size / 2 - 2.5, c = 2 * Math.PI * r;
        return `<svg width="${size}" height="${size}" aria-hidden="true"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="act-ring-bg" stroke-width="3" fill="none"/>
            <circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="act-ring ${cls || ''}" stroke-width="3" fill="none"
             stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${(c * (1 - frac)).toFixed(1)}"/></svg>`;
    }

    function drawPill() {
        const running = _data.jobs.filter(j => j.state === 'running');
        const polls = Object.entries(_data.polls || {});
        const unseen = seen();
        // Finished in the last minute and watched here (green flash), or failed and not yet opened (red, stays).
        const failed = _data.jobs.filter(j => j.state === 'failed' && !unseen.has(j.id));
        const fresh = _data.jobs.filter(j => j.finished && j.state !== 'failed' && _watched.has(j.id)
            && Date.now() / 1000 - j.finished < 6);
        if (!running.length && !polls.length && !failed.length && !fresh.length) {
            if (_pill) _pill.hidden = true;
            return;
        }
        if (!_pill) {
            _pill = document.createElement('button');
            _pill.type = 'button';
            _pill.id = 'pp-activity-pill';
            _pill.className = 'act-pill';
            _pill.setAttribute('aria-haspopup', 'dialog');
            _pill.addEventListener('click', e => { e.stopPropagation(); toggleTray(); });
            document.body.appendChild(_pill);
        }
        _pill.hidden = false;
        let cls = '', t1 = '', t2 = '', frac = 0, n = '';
        const things = running.length + (polls.length ? 1 : 0);
        if (running.length) {
            const all = running.reduce((a, j) => { const c = counts(j); return { n: a.n + c.n, d: a.d + c.d }; }, { n: 0, d: 0 });
            frac = all.n ? all.d / all.n : 0;
            n = things > 1 ? String(things) : `${all.d}/${all.n}`;
            const j = running[0];
            t1 = things > 1 ? `${things} things running` : `${verb(j)} "${j.title}"`;
            const now = running.flatMap(x => x.lines.filter(l => l.state === 'active'))
                .map(l => `${lineLabel(l)}: ${lineText(l).toLowerCase()}`);
            t2 = now.join(' · ') || (polls.length ? `Polling ${polls.length} site${polls.length > 1 ? 's' : ''}` : 'Starting…');
        } else if (failed.length) {
            const j = failed[0], c = counts(j);
            cls = 'fail'; frac = 1; n = '!';
            t1 = c.n > 1 ? `Posted to ${c.n - c.bad} of ${c.n} sites` : `"${j.title}" failed`;
            const bad = j.lines.filter(l => l.state === 'failed').map(lineLabel);
            t2 = `${bad.join(', ')} failed · open to retry`;
        } else if (fresh.length) {
            const j = fresh[0], c = counts(j);
            cls = 'done'; frac = 1; n = '✓';
            t1 = j.state === 'cancelled' ? `Stopped "${j.title}"` : `"${j.title}" done`;
            t2 = c.n > 1 ? `${c.n - c.bad} of ${c.n} sites` : '';
        } else {
            const [code, p] = polls[0];
            frac = p.total ? Math.min(1, (p.current || 0) / p.total) : 0;
            n = String(polls.length);
            t1 = polls.length > 1 ? `Polling ${polls.length} sites` : `Polling ${label(code)}`;
            t2 = p.message || p.phase || '';
        }
        const segs = running.length === 1 && !polls.length
            ? `<span class="act-segs" aria-hidden="true">${running[0].lines.map(l =>
                `<i class="${l.state === 'done' ? 'ok' : l.state === 'failed' ? 'bad' : l.state === 'active' ? 'run' : ''}"></i>`).join('')}</span>` : '';
        _pill.className = `act-pill ${cls}`;
        _pill.innerHTML = `<span class="act-mini">${ring(frac, 26, cls)}<span class="act-n">${esc(n)}</span></span>
            <span class="act-txt"><span class="act-t1">${esc(t1)}</span><span class="act-t2">${esc(t2)}</span>${segs}</span>`;
        _pill.setAttribute('aria-label', `${t1}. ${t2}. Open activity`);
        _pill.setAttribute('aria-expanded', String(_open));
    }

    // ── the tray ───────────────────────────────────────────────────
    function toggleTray() { _open ? closeTray() : openTray(); }

    async function openTray() {
        _open = true;
        if (!_tray) {
            _tray = document.createElement('div');
            _tray.id = 'pp-activity-tray';
            _tray.className = 'act-tray';
            _tray.setAttribute('role', 'dialog');
            _tray.setAttribute('aria-label', 'Activity');
            _tray.addEventListener('click', onTrayClick);
            document.body.appendChild(_tray);
            document.addEventListener('click', e => {
                if (_open && !_tray.contains(e.target) && !(_pill && _pill.contains(e.target))) closeTray();
            });
            document.addEventListener('keydown', e => { if (_open && e.key === 'Escape') { closeTray(); _pill && _pill.focus(); } });
        }
        _tray.hidden = false;
        document.querySelectorAll('.act-toast').forEach(t => t.remove());   // the tray says the same
        markSeen(_data.jobs.filter(j => j.finished).map(j => j.id));
        drawTray();
        drawPill();
        const first = _tray.querySelector('button, a');
        if (first) first.focus();
        loadQueue();
    }

    function closeTray() {
        _open = false;
        if (_tray) _tray.hidden = true;
        drawPill();
    }

    let _queue = [];
    async function loadQueue() {
        try {
            const r = await fetch('/api/posting/queue');
            const d = r.ok ? await r.json() : null;
            const rows = (d && (d.queue || d.items)) || (Array.isArray(d) ? d : []);
            _queue = rows.filter(q => q.status === 'pending' && q.scheduled_at)
                .sort((a, b) => Utils.time.ms(a.scheduled_at) - Utils.time.ms(b.scheduled_at)).slice(0, 3);
        } catch (e) { _queue = []; }
        if (_open) drawTray();
    }

    function drawTray() {
        if (!_tray) return;
        const running = _data.jobs.filter(j => j.state === 'running');
        const polls = Object.entries(_data.polls || {});
        const today = Utils.time.dayKey(new Date());
        const earlier = _data.jobs.filter(j => j.finished && Utils.time.dayKey(j.finished * 1000) === today);
        const shown = _data.jobs.filter(j => j.state === 'running' || Date.now() / 1000 - (j.finished || 0) < 3600).slice(0, 8);
        const jobs = shown.map(j => {
            const c = counts(j);
            const ago = Math.max(0, Math.round(Date.now() / 1000 - j.started));
            const agoTxt = ago < 60 ? `${ago}s ago` : `${Math.round(ago / 60)} min ago`;
            const sub = j.state === 'running'
                ? `${verb(j)} · ${c.d} of ${c.n} site${c.n === 1 ? '' : 's'} · started ${agoTxt}`
                : `${{ done: 'Done', failed: 'Finished with a problem', cancelled: 'Stopped' }[j.state] || j.state} · ${Utils.time.fmt.time(j.finished * 1000)}`;
            const lines = j.lines.map(l => {
                const mark = l.state === 'done' ? '✓' : l.state === 'failed' ? '✗' : l.state === 'active' ? '<span class="act-spin" aria-hidden="true"></span>' : '';
                const act = l.state === 'failed' && j.can_retry && l.retryable !== false && j.state !== 'running'
                    ? `<button type="button" class="btn btn-sm" data-act-retry="${esc(j.id)}" data-key="${esc(l.key)}">Retry</button>`
                    : l.url ? `<a href="${esc(Utils.safeUrl(l.url))}" target="_blank" rel="noopener">view</a>` : '';
                return `<li><span class="act-nm">${esc(lineLabel(l))}</span>
                    <span class="act-st ${l.state === 'done' ? 'ok' : l.state === 'failed' ? 'bad' : ''}">${esc(lineText(l))}</span>
                    <span class="act-mark">${mark}</span>${act}</li>`;
            }).join('');
            return `<div class="act-job">
                <div class="act-hd"><div style="flex:1;min-width:0"><div class="act-ttl">${esc(j.title)}</div><div class="act-sub">${esc(sub)}</div></div>
                ${j.state === 'running' && !j.cancel ? `<button type="button" class="btn btn-sm" data-act-cancel="${esc(j.id)}">Cancel the rest</button>` : ''}
                ${j.cancel && j.state === 'running' ? '<span class="act-sub">Stopping after this site…</span>' : ''}</div>
                ${j.error ? `<div class="act-st bad">${esc(j.error)}</div>` : ''}
                <div class="act-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${c.n}" aria-valuenow="${c.d}" aria-label="${esc(j.title)}">
                    <span style="width:${c.n ? Math.round(c.d / c.n * 100) : 0}%"></span></div>
                <ul class="act-sites">${lines}</ul></div>`;
        }).join('');
        const pollRow = polls.length ? `<div class="act-job"><div class="act-ttl">Polling stats</div>
            <div class="act-sub">${polls.map(([code, p]) => esc(`${label(code)}${p.total ? ` ${p.current || 0} of ${p.total}` : ''}${p.message ? ` · ${p.message}` : ''}`)).join('<br>')}</div></div>` : '';
        const queue = _queue.length ? `<div class="act-job">${_queue.map(q => `<div class="act-queued">
            <span>${esc(q.content_type === 'post' ? (q.title_override || 'Post') : String(q.story_name).replace(/_/g, ' '))} → ${esc(label(q.platform))}</span>
            <span>${esc(Utils.time.fmt.dateTime(q.scheduled_at))}</span></div>`).join('')}</div>` : '';
        const ok = earlier.filter(j => j.state === 'done').length, bad = earlier.filter(j => j.state === 'failed').length;
        _tray.innerHTML = `<header><b>Activity</b><span>${running.length} running${_queue.length ? ` · ${_queue.length} up next` : ''}</span>
                <button type="button" class="act-close" data-act-close aria-label="Close">×</button></header>
            ${jobs || pollRow ? jobs + pollRow : '<div class="act-job act-sub">Nothing running right now.</div>'}
            ${queue}
            <div class="act-ftr"><span>${earlier.length ? `Earlier today: ${ok} done${bad ? `, ${bad} with a problem` : ''}` : ''}</span>
                <a href="#/posting/queue" data-act-close>Open queue →</a></div>`;
    }

    async function onTrayClick(e) {
        const t = e.target.closest('[data-act-retry],[data-act-cancel],[data-act-close]');
        if (!t) return;
        if (t.dataset.actClose !== undefined) { closeTray(); return; }
        t.disabled = true;
        const url = t.dataset.actRetry
            ? `/api/activity/${encodeURIComponent(t.dataset.actRetry)}/retry/${encodeURIComponent(t.dataset.key)}`
            : `/api/activity/${encodeURIComponent(t.dataset.actCancel)}/cancel`;
        try {
            await fetch(url, { method: 'POST' });
            if (t.dataset.actRetry) _toasted.delete(t.dataset.actRetry), _watched.add(t.dataset.actRetry);
        } catch (err) { /* the next poll shows the truth */ }
        kick();
    }

    // ── the result toast ───────────────────────────────────────────
    function resultToast(j) {
        if (_open) { markSeen([j.id]); return; }   // the open tray already shows it
        const c = counts(j);
        const okN = c.n - c.bad - j.lines.filter(l => l.state === 'cancelled').length;
        const head = j.state === 'done' ? (c.n > 1 ? `"${j.title}" is live on ${c.n} sites` : `"${j.title}" is live`)
            : j.state === 'cancelled' ? `Stopped "${j.title}" after ${okN} of ${c.n} sites`
            : `Posted to ${okN} of ${c.n} sites`;
        const took = j.finished && j.started ? Math.round(j.finished - j.started) : 0;
        const why = j.lines.filter(l => l.state === 'failed').map(l => `${lineLabel(l)}: ${l.error || 'failed'}`).join(' · ')
            || j.error || '';
        const el = document.createElement('div');
        el.className = `act-toast ${j.state === 'done' ? '' : 'fail'}`;
        el.setAttribute('role', j.state === 'failed' ? 'alert' : 'status');
        el.innerHTML = `<b>${esc(head)}</b><span class="act-sub">${esc(why || (took ? `Took ${took >= 60 ? `${Math.floor(took / 60)} m ` : ''}${took % 60} s` : ''))}</span>
            <div class="act-chips">${j.lines.map(l => {
                const inner = `${l.state === 'done' ? '✓' : l.state === 'failed' ? '✗' : '–'} ${esc(lineLabel(l))}`;
                return l.url ? `<a class="act-chip ${l.state === 'failed' ? 'bad' : ''}" href="${esc(Utils.safeUrl(l.url))}" target="_blank" rel="noopener">${inner}</a>`
                    : `<span class="act-chip ${l.state === 'failed' ? 'bad' : ''}">${inner}</span>`;
            }).join('')}</div>
            ${j.state === 'failed' ? '<button type="button" class="btn btn-sm btn-primary" data-open-tray>Open activity</button>' : ''}
            <button type="button" class="act-close" aria-label="Dismiss">×</button>`;
        el.querySelector('.act-close').addEventListener('click', () => el.remove());
        el.querySelector('[data-open-tray]')?.addEventListener('click', () => { el.remove(); openTray(); });
        let stack = document.getElementById('act-toasts');
        if (!stack) { stack = document.createElement('div'); stack.id = 'act-toasts'; document.body.appendChild(stack); }
        stack.prepend(el);
        // A failure stays on the red pill until opened, so its toast need not stay too.
        setTimeout(() => el.remove(), j.state === 'failed' ? 15000 : 9000);
    }

    // ── the publish grid (US3) ─────────────────────────────────────
    const COLS = ['Preparing', 'Sending', 'Done'];
    function col(ln) {   // which column the line has reached: -1 waiting, 0 preparing, 1 sending, 2 finished
        if (ln.state === 'waiting') return -1;
        if (['done', 'failed', 'cancelled'].includes(ln.state)) return 2;
        return ln.step === 'Preparing' ? 0 : 1;
    }

    function renderGrid(el, jobId) {
        _grids.set(el, jobId);
        el.innerHTML = '<div class="act-grid-wrap"><p class="act-sub">Starting…</p></div>';
        el.addEventListener('click', async e => {
            const b = e.target.closest('[data-grid-min],[data-grid-cancel]');
            if (!b) return;
            // Minimise hands the job to the pill: the box goes, so run() resolves null and the
            // result toast reports instead of the screen.
            if (b.dataset.gridMin !== undefined) { _grids.delete(el); el.remove(); return; }
            b.disabled = true;
            try { await fetch(`/api/activity/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }); } catch (err) { /* next poll */ }
            kick();
        });
        _watched.add(jobId);
        kick();
    }

    function drawGrid(el, jobId) {
        const j = _data.jobs.find(x => x.id === jobId);
        if (!j) return;
        const c = counts(j);
        const dot = (ln, k) => {
            const at = col(ln);
            const cls = at > k || (k === 2 && ln.state === 'done') ? 'ok'
                : k === 2 && ln.state === 'failed' ? 'bad'
                : k === 2 && ln.state === 'cancelled' ? 'skip'
                : at === k ? 'run' : '';
            return `<span class="act-dot ${cls}" aria-hidden="true"></span>`;
        };
        const rows = j.lines.map(ln => `<div class="act-g-site">${esc(lineLabel(ln))}</div>
            ${COLS.map((_, k) => `<div class="act-g-c">${dot(ln, k)}</div>`).join('')}
            <div class="act-g-st act-st ${ln.state === 'done' ? 'ok' : ln.state === 'failed' ? 'bad' : ''}">${esc(lineText(ln))}${ln.url ? ` · <a href="${esc(Utils.safeUrl(ln.url))}" target="_blank" rel="noopener">view</a>` : ''}</div>`).join('');
        const log = (_log.get(jobId) || []).slice(0, 8).map(x =>
            `<div><time>${esc(Utils.time.fmt.time(x.t))}</time><span class="${x.cls}">${esc(x.text)}</span></div>`).join('');
        const running = j.state === 'running';
        el.innerHTML = `<div class="act-grid-wrap">
            <div class="act-ttl">${esc(verb(j))} "${esc(j.title)}"</div>
            <div class="act-sub" aria-live="polite">${c.d} of ${c.n} site${c.n === 1 ? '' : 's'} done${running ? ' · you can leave this page — the activity pill keeps track' : ''}</div>
            <div class="act-grid" role="table" aria-label="Progress by site">
                <div class="act-g-h">Site</div>${COLS.map(h => `<div class="act-g-h act-g-c">${h}</div>`).join('')}<div class="act-g-h">Now</div>
                ${rows}
            </div>
            ${log ? `<div class="act-log">${log}</div>` : ''}
            <div class="act-actions">
                ${running ? '<button type="button" class="btn btn-sm btn-outline" data-grid-min>Minimise</button>' : ''}
                ${running && !j.cancel ? '<button type="button" class="btn btn-sm btn-outline" data-grid-cancel>Cancel the rest</button>' : ''}
            </div></div>`;
    }

    // ── run: a publish screen's call, with the grid while it works ──
    /* `send(extra)` is the screen's own publish call with `...extra` spread into its body (the
     * call keeps its literal confirm_live — test_publish_guards reads it). run() sends
     * {background: true}, draws the grid just below `host`, and resolves when the job finishes
     * with the classic reply shape
     * ({successes, failures, results}) so the screen's own result handling carries on as
     * before. Resolves null if the grid was minimised or the screen left — the result toast
     * reports then, and the screen must not act (navigate, re-render) on a page that moved on.
     * A server too old for background jobs answers the classic way; that reply passes through. */
    const _waiters = new Map();   // job id → {resolve, box}
    async function run(send, host) {
        const r = await send({ background: true });
        if (!r || !r.job_id) return r;
        const box = document.createElement('div');
        if (host && host.parentNode) host.insertAdjacentElement('afterend', box);
        renderGrid(box, r.job_id);
        return new Promise(resolve => _waiters.set(r.job_id, { resolve, box }));
    }

    function settleWaiters() {
        for (const [jid, w] of _waiters) {
            const j = _data.jobs.find(x => x.id === jid);
            if (!j || !j.finished) continue;
            _waiters.delete(jid);
            if (!document.body.contains(w.box)) { w.resolve(null); continue; }
            _toasted.add(jid);            // the screen shows the result itself
            w.box.remove();
            const results = j.lines.map(l => ({
                platform: l.key, success: l.state === 'done', external_url: l.url,
                error: l.state === 'cancelled' ? 'Not sent (cancelled)' : (l.error || ''),
                queued_desktop: l.step === 'Queued for desktop',
            }));
            const ok = results.filter(x => x.success).length;
            w.resolve({ status: 'completed', job_id: jid, results, successes: ok, failures: results.length - ok,
                        total: results.length });
        }
    }

    // ── start ──────────────────────────────────────────────────────
    function kick() { poll(); }

    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    window.Activity = { poll, kick, run, renderGrid, openTray, closeTray, _ingest: ingest, _state: () => _data };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(poll, 1500));
    else setTimeout(poll, 1500);
})();
