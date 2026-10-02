/* ── The Platforms page (#/platforms, spec 020) ──────────────────
 *
 * Three parts, top to bottom: Needs attention (each problem in plain words with its fix),
 * Your platforms (calm tiles or a list: what the site does, its headline number in its own
 * word, works, 30-day change, a small trend line, when it was last checked), and Not set
 * up (small chips, out of the way). Honours "platforms I use" (`window.visiblePlatforms`)
 * and opens the shared Choose-platforms panel (platform_picker.js).
 *
 * One request: GET /api/platforms/overview (it used to fire one summary call per site).
 * What a site does comes from the server's real poster / publisher lists, not a flag.
 */
window.PlatformsHub = (function () {
    const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
    const SORT_KEY = 'pp-plat-sort', VIEW_KEY = 'pp-plat-view';
    const ls = (k, v) => {
        try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* no storage */ }
        return null;
    };
    let data = null, byCode = {}, sort = 'audience', view = 'tiles', listSort = null;

    const plat = (code) => (window.platformByCode && window.platformByCode(code)) || { code, label: code, emoji: '', color: '#888' };
    const num = (n) => (n == null ? '—' : Utils.formatCompact(n));
    function ago(v) {
        const ms = Utils.time.ms(v);
        if (isNaN(ms)) return '';
        const m = Math.round((Date.now() - ms) / 60000);
        if (m < 1) return 'just now';
        if (m < 60) return `${m} min ago`;
        if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
        return Utils.time.fmt.date(v);
    }
    function until(v) {
        const ms = Utils.time.ms(v);
        if (isNaN(ms)) return '';
        const m = Math.max(0, Math.round((ms - Date.now()) / 60000));
        if (m < 60) return `${m} min`;
        return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')}`;
    }
    function logo(code, cls = 'ph-ico') {
        const p = plat(code);
        return p.logo ? `<img class="${cls}" src="${esc(p.logo)}" alt="" aria-hidden="true">`
            : `<span class="${cls} ph-ico--emoji" aria-hidden="true">${esc(p.emoji || '•')}</span>`;
    }
    const route = (code) => (window.platformRoute ? window.platformRoute(code) : '#/' + code);
    const accountHref = (code) => (code === 'tg' ? '#/settings/telegram' : `#/settings/platforms/${code}`);

    /* Status in words beside every dot (spec 012): ok / problem / paused / not polled. */
    function status(p) {
        const a = (p.attention || [])[0];
        if (p.paused) return { cls: 'paused', text: 'Checking paused' };
        if (a) return { cls: a.level === 'error' ? 'bad' : (a.level === 'warn' ? 'warn' : 'info'),
                        text: a.title.replace(/^[^:]+:\s*/, '').replace(/^./, c => c.toUpperCase()) };
        if (!p.can_poll) return { cls: 'ok', text: 'Ready to post' };
        const h = p.health || {};
        if (h.last_poll_status === 'running') return { cls: 'ok', text: 'Checking now…' };
        return h.last_poll_at ? { cls: 'ok', text: `Polled ${ago(h.last_poll_at)}` } : { cls: 'info', text: 'Not checked yet' };
    }

    function change(p) {
        if (p.change_pct == null) return '<span class="ph-chg muted" title="Not enough history yet">–</span>';
        const v = p.change_pct, cls = v > 0 ? 'up' : (v < 0 ? 'down' : '');
        return `<span class="ph-chg ${cls}">${v > 0 ? '+' : ''}${Math.round(v)}% <span class="sr-only">over</span>30 d</span>`;
    }

    /* A small trend line from the 30 daily totals — inline SVG, no chart library. */
    function spark(p) {
        const pts = (p.series || []).map((d, i) => [i, d.value]).filter(x => x[1] != null);
        if (pts.length < 2) {
            return '<svg class="ph-spark ph-spark--flat" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">'
                + '<path d="M0 22 H100"/></svg><span class="ph-spark-note">not enough history</span>';
        }
        const vals = pts.map(x => x[1]);
        const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
        const n = (p.series || []).length - 1 || 1;
        const xy = pts.map(([i, v]) => `${(100 * i / n).toFixed(1)} ${(26 - 22 * (v - lo) / span).toFixed(1)}`);
        return `<svg class="ph-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
            <path class="ph-spark-fill" d="M${xy[0].split(' ')[0]} 30 L${xy.join(' L')} L${xy[xy.length - 1].split(' ')[0]} 30 Z"/>
            <path d="M${xy.join(' L')}"/></svg>`;
    }

    function menu(p) {
        const code = esc(p.code);
        const guide = window.PlatformGuides && window.PlatformGuides.has(p.code);
        return `<button type="button" class="ph-more" data-menu="${code}" aria-haspopup="menu" aria-expanded="false"
                aria-controls="ph-menu-${code}" title="More">⋯<span class="sr-only"> More for ${esc(p.label)}</span></button>
            <div class="ph-menu" id="ph-menu-${code}" role="menu" hidden>
                <a role="menuitem" href="${route(p.code)}">Open ${esc(plat(p.code).label)}</a>
                ${p.can_poll ? `<button type="button" role="menuitem" data-act="poll" data-code="${code}">Poll now</button>` : ''}
                <a role="menuitem" href="${accountHref(p.code)}">Accounts</a>
                ${guide ? `<button type="button" role="menuitem" data-guide="${code}">Setup guide</button>` : ''}
                <button type="button" role="menuitem" data-act="up" data-code="${code}">Move up</button>
                <button type="button" role="menuitem" data-act="down" data-code="${code}">Move down</button>
                <button type="button" role="menuitem" class="ph-menu-hide" data-act="hide" data-code="${code}">Hide from PawPoller</button>
            </div>`;
    }

    function tile(p) {
        const P = plat(p.code), st = status(p);
        const a = (p.attention || [])[0];
        const fix = a && a.action === 'reconnect'
            ? `<a class="btn btn-sm btn-primary ph-fix" href="${accountHref(p.code)}">Reconnect</a>` : '';
        const head = p.headline
            ? `<div class="ph-num">${num(p.headline.value)}</div>
               <div class="ph-sub">${esc(p.headline.label.toLowerCase())} · ${p.works} work${p.works === 1 ? '' : 's'} ${change(p)}</div>`
            : `<div class="ph-num ph-num--none">—</div><div class="ph-sub">${p.can_poll ? 'no numbers yet' : 'nothing to count'}</div>`;
        return `<article class="ph-tile" data-code="${esc(p.code)}" style="--pc:${esc(P.color)}">
            <div class="ph-top">${logo(p.code, 'ph-logo')}
                <div class="ph-name"><a class="ph-open" href="${route(p.code)}">${esc(P.label)}</a><small>${esc(p.role)}</small></div>
                ${menu(p)}</div>
            ${head}
            ${p.can_poll ? spark(p) : '<div class="ph-spark-gap"></div>'}
            <div class="ph-foot"><span class="ph-dot ph-dot--${st.cls}" aria-hidden="true"></span><span class="ph-st ph-st--${st.cls}">${esc(st.text)}</span></div>
            ${fix}
        </article>`;
    }

    function row(p) {
        const P = plat(p.code), st = status(p);
        return `<tr data-code="${esc(p.code)}">
            <td><span class="ph-cell-name">${logo(p.code)}<a href="${route(p.code)}">${esc(P.label)}</a></span></td>
            <td>${esc(p.role)}</td>
            <td><span class="ph-dot ph-dot--${st.cls}" aria-hidden="true"></span> ${esc(st.text)}</td>
            <td class="ph-numcell">${p.headline ? `${num(p.headline.value)} <span class="muted">${esc(p.headline.label.toLowerCase())}</span>` : '—'}</td>
            <td class="ph-numcell">${change(p)}</td>
            <td class="ph-numcell">${p.works}</td>
            <td>${p.health && p.health.last_poll_at ? esc(ago(p.health.last_poll_at)) : '—'}</td>
            <td class="ph-menucell">${menu(p)}</td></tr>`;
    }

    function ordered(list) {
        const audience = (p) => (p.headline ? p.headline.value : -1);
        if (sort === 'az') return list.slice().sort((a, b) => plat(a.code).label.localeCompare(plat(b.code).label));
        if (sort === 'mine') {
            const order = data.order || [];
            const at = (c) => { const i = order.indexOf(c); return i < 0 ? 1e6 : i; };
            return list.slice().sort((a, b) => at(a.code) - at(b.code) || audience(b) - audience(a));
        }
        return list.slice().sort((a, b) => audience(b) - audience(a));
    }

    function listSorted(list) {
        if (!listSort) return ordered(list);
        const { key, dir } = listSort;
        const val = {
            name: (p) => plat(p.code).label.toLowerCase(), does: (p) => p.role, status: (p) => status(p).text,
            headline: (p) => (p.headline ? p.headline.value : -1), change: (p) => (p.change_pct == null ? -1e9 : p.change_pct),
            works: (p) => p.works, polled: (p) => Utils.time.ms((p.health || {}).last_poll_at) || 0,
        }[key];
        return list.slice().sort((a, b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
    }

    function attentionRow(a) {
        const P = plat(a.code);
        let actions = '';
        if (a.action === 'reconnect') actions = `<a class="btn btn-sm btn-primary" href="${accountHref(a.code)}">Reconnect</a>`;
        else if (a.action === 'poll') actions = `<button type="button" class="btn btn-sm btn-primary" data-act="poll" data-code="${esc(a.code)}">Poll now</button>`;
        else if (a.action === 'pause') actions = `<a class="btn btn-sm" href="${route(a.code)}">Details</a>
            <button type="button" class="btn btn-sm" data-act="pause" data-code="${esc(a.code)}">Pause checking</button>`;
        else if (a.action === 'wait') actions = `<span class="muted">Resumes at ${esc(Utils.time.fmt.time(a.until))}</span>`;
        return `<div class="ph-alert ph-alert--${esc(a.level)}" style="--pc:${esc(P.color)}">
            ${logo(a.code)}<div class="ph-alert-text"><b>${esc(a.title)}</b><span>${esc(a.detail)}</span></div>
            <div class="ph-alert-act">${actions}</div></div>`;
    }

    function paint(app) {
        const all = data.platforms || [];
        const shownCodes = new Set((window.visiblePlatforms ? window.visiblePlatforms() : window.PLATFORMS || []).map(p => p.code));
        const known = all.filter(p => window.platformByCode && window.platformByCode(p.code));   // registry sites only
        const visible = known.filter(p => shownCodes.has(p.code));
        const mine = visible.filter(p => p.configured);
        const unset = visible.filter(p => !p.configured);
        const hidden = known.filter(p => !shownCodes.has(p.code));
        const attention = (data.attention || []).filter(a => shownCodes.has(a.code));
        const s = data.summary || {};
        const bits = [`<span class="ph-dot ph-dot--ok" aria-hidden="true"></span><b>${s.working || 0}</b> working`];
        if (attention.length) bits.push(`<span class="ph-dot ph-dot--bad" aria-hidden="true"></span><b>${new Set(attention.map(a => a.code)).size}</b> need you`);
        if (s.paused) bits.push(`<b>${s.paused}</b> paused`);
        if (s.last_poll_at) bits.push(`Last poll <b>${esc(ago(s.last_poll_at))}</b>`);
        if (s.next_poll_at) bits.push(`next in ${esc(until(s.next_poll_at))}`);
        const seg = (attr, cur, opts) => opts.map(([k, t]) => `<button type="button" ${attr}="${k}" aria-pressed="${cur === k}">${t}</button>`).join('');

        let body;
        if (!mine.length && !unset.length) {
            body = `<div class="ph-empty"><p>All platforms are hidden.</p>
                <button type="button" class="btn btn-primary" data-act="show-all">Show all</button></div>`;
        } else if (!mine.length) {
            body = `<div class="ph-empty"><p>No platforms set up yet.</p>
                <p class="muted">New here? <a href="#/getting-started">Getting Started</a> walks you through connecting your first site.</p></div>`;
        } else if (view === 'list') {
            const th = (key, text, num) => {
                const on = listSort && listSort.key === key;
                const aria = on ? (listSort.dir > 0 ? 'ascending' : 'descending') : 'none';
                return `<th aria-sort="${aria}"${num ? ' class="ph-numcell"' : ''}><button type="button" data-lsort="${key}">${text}${on ? (listSort.dir > 0 ? ' ▲' : ' ▼') : ''}</button></th>`;
            };
            body = `<div class="ph-table-wrap"><table class="ph-table"><thead><tr>
                ${th('name', 'Platform')}${th('does', 'Does')}${th('status', 'Status')}${th('headline', 'Headline', 1)}
                ${th('change', '30 days', 1)}${th('works', 'Works', 1)}${th('polled', 'Last polled')}<th><span class="sr-only">More</span></th>
                </tr></thead><tbody>${listSorted(mine).map(row).join('')}</tbody></table></div>`;
        } else {
            body = `<div class="ph-grid">${ordered(mine).map(tile).join('')}</div>`;
        }

        const chips = unset.length ? `
            <section class="ph-sec" aria-labelledby="ph-unset-h">
                <h2 class="ph-h" id="ph-unset-h">Not set up <span class="ph-count">${unset.length}</span></h2>
                <div class="ph-chips">${unset.map(p => `<span class="ph-chip">${logo(p.code)}${esc(plat(p.code).label)}
                    ${window.PlatformGuides && window.PlatformGuides.has(p.code)
                        ? `<button type="button" class="ph-chip-link" data-guide="${esc(p.code)}">Set up</button>`
                        : `<a class="ph-chip-link" href="${accountHref(p.code)}">Set up</a>`}
                    <button type="button" class="ph-chip-x" data-act="hide" data-code="${esc(p.code)}" aria-label="Hide ${esc(plat(p.code).label)}">✕</button></span>`).join('')}
                    <button type="button" class="ph-chip-all" data-act="hide-unset">Hide all ${unset.length}</button></div>
            </section>` : '';
        const hiddenLine = hidden.length ? `<p class="ph-hidden-line"><span class="ph-dot ph-dot--info" aria-hidden="true"></span>
            ${hidden.length} platform${hidden.length === 1 ? '' : 's'} hidden (${esc(hidden.map(p => plat(p.code).label).join(', '))}) ·
            <button type="button" class="pk-link" data-act="show-hidden">Show them</button></p>` : '';

        app._setContent(`
            <div class="ph-page">
                <div class="page-header ph-head">
                    <div><h1>Platforms</h1><p class="ph-summary">${bits.join('<span class="ph-sep" aria-hidden="true">·</span>')}</p></div>
                    <div class="ph-tools">
                        <div class="ph-seg" role="group" aria-label="Layout">${seg('data-view', view, [['tiles', '▦ Tiles'], ['list', '☰ List']])}</div>
                        <div class="ph-seg" role="group" aria-label="Sort">${seg('data-sort', sort, [['audience', 'Biggest audience'], ['az', 'A–Z'], ['mine', 'My order']])}</div>
                        <button type="button" class="btn" data-act="poll-all">Poll all now</button>
                        <button type="button" class="btn btn-primary" data-act="choose">Choose platforms</button>
                    </div>
                </div>
                ${attention.length ? `<section class="ph-sec" aria-labelledby="ph-att-h">
                    <h2 class="ph-h" id="ph-att-h">Needs attention <span class="ph-count">${attention.length}</span></h2>
                    <div class="ph-alerts">${attention.map(attentionRow).join('')}</div></section>` : ''}
                ${mine.length ? `<section class="ph-sec" aria-labelledby="ph-mine-h">
                    <h2 class="ph-h" id="ph-mine-h">Your platforms <span class="ph-count">${mine.length}</span></h2>${body}</section>` : body}
                ${chips}
                ${hiddenLine}
                <p class="logo-disclaimer">Platform names and logos are trademarks of their respective owners.
                PawPoller is an independent tool, not affiliated with or endorsed by any of these platforms;
                their logos are shown solely to identify each service.</p>
            </div>`);
        wire(app);
    }

    function closeMenus(refocus) {
        document.querySelectorAll('.ph-menu:not([hidden])').forEach(m => {
            m.hidden = true;
            const b = document.querySelector(`[aria-controls="${m.id}"]`);
            if (b) { b.setAttribute('aria-expanded', 'false'); if (refocus) b.focus(); }
        });
    }

    async function move(code, delta, app) {
        const current = ordered((data.platforms || []).filter(p => p.configured)).map(p => p.code);
        const i = current.indexOf(code), j = i + delta;
        if (i < 0 || j < 0 || j >= current.length) return;
        [current[i], current[j]] = [current[j], current[i]];
        data.order = current;
        sort = 'mine'; ls(SORT_KEY, sort);
        paint(app);
        try { await API.savePreferences({ platform_order: current }); } catch (e) { window.toast?.error('Could not save the order'); }
        document.querySelector(`.ph-tile[data-code="${code}"] .ph-more, tr[data-code="${code}"] .ph-more`)?.focus();
    }

    function wire(app) {
        const root = document.querySelector('.ph-page');
        if (!root) return;
        const reload = () => render(app, null, true);
        root.addEventListener('click', async (e) => {
            const mb = e.target.closest('[data-menu]');
            if (mb) {
                const open = mb.getAttribute('aria-expanded') === 'true';
                closeMenus();
                if (!open) {
                    const m = document.getElementById(mb.getAttribute('aria-controls'));
                    m.hidden = false; mb.setAttribute('aria-expanded', 'true');
                    m.querySelector('[role="menuitem"]')?.focus();
                }
                return;
            }
            const v = e.target.closest('[data-view]');
            if (v) { view = v.dataset.view; ls(VIEW_KEY, view); paint(app); return; }
            const so = e.target.closest('[data-sort]');
            if (so) { sort = so.dataset.sort; ls(SORT_KEY, sort); listSort = null; paint(app); return; }
            const lsBtn = e.target.closest('[data-lsort]');
            if (lsBtn) {
                const key = lsBtn.dataset.lsort;
                listSort = listSort && listSort.key === key ? { key, dir: -listSort.dir } : { key, dir: key === 'name' || key === 'does' ? 1 : -1 };
                paint(app);
                document.querySelector(`[data-lsort="${key}"]`)?.focus();
                return;
            }
            const act = e.target.closest('[data-act]');
            if (!act) return;
            const code = act.dataset.code;
            closeMenus();
            if (act.dataset.act === 'choose') {
                PlatformPicker.openPanel({ facts: byCode, onClose: reload });
            } else if (act.dataset.act === 'show-hidden') {
                PlatformPicker.openPanel({ facts: byCode, filter: 'hidden', onClose: reload });
            } else if (act.dataset.act === 'show-all') {
                await API.savePreferences({ hidden_platforms: [] }); window.HIDDEN_PLATFORMS = []; reload();
            } else if (act.dataset.act === 'hide') {
                PlatformPicker.hideOne(code, byCode, reload);
            } else if (act.dataset.act === 'hide-unset') {
                const codes = (data.platforms || []).filter(p => !p.configured).map(p => p.code);
                const next = [...new Set([...(window.HIDDEN_PLATFORMS || []), ...codes])];
                await API.savePreferences({ hidden_platforms: next }); window.HIDDEN_PLATFORMS = next; paint(app);
            } else if (act.dataset.act === 'poll') {
                act.disabled = true;
                try { await API.triggerAccountPoll(code, null); window.toast?.success(`Checking ${plat(code).label} now`); }
                catch (err) { window.toast?.error('Could not start: ' + (err.message || err)); act.disabled = false; }
            } else if (act.dataset.act === 'poll-all') {
                act.disabled = true;
                try { await API.triggerPoll(); window.toast?.success('Checking every platform now'); }
                catch (err) { window.toast?.error('Could not start: ' + (err.message || err)); }
                act.disabled = false;
            } else if (act.dataset.act === 'pause') {
                try { await API.pausePlatformPolling(code); window.toast?.info(`Checking ${plat(code).label} is paused`); reload(); }
                catch (err) { window.toast?.error('Could not pause: ' + (err.message || err)); }
            } else if (act.dataset.act === 'up' || act.dataset.act === 'down') {
                move(code, act.dataset.act === 'up' ? -1 : 1, app);
            }
        });
        root.addEventListener('keydown', (e) => {
            const m = e.target.closest('.ph-menu');
            if (!m) return;
            const items = [...m.querySelectorAll('[role="menuitem"]')];
            const i = items.indexOf(document.activeElement);
            if (e.key === 'Escape') { e.preventDefault(); closeMenus(true); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
            else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
            else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
            else if (e.key === 'Tab') closeMenus();
        });
        if (!PlatformsHub._docWired) {
            PlatformsHub._docWired = true;
            document.addEventListener('click', (e) => { if (!e.target.closest('.ph-menu, [data-menu]')) closeMenus(); });
        }
    }

    async function render(app, rt, quiet) {
        sort = ls(SORT_KEY) || 'audience';
        view = ls(VIEW_KEY) === 'list' ? 'list' : 'tiles';
        let d;
        try { d = await API.getPlatformsOverview(); }
        catch (err) {
            if (rt && app._stale(rt)) return;
            app._setContent(`<div class="page-header"><h1>Platforms</h1></div>
                <div class="card error">Couldn't load the platforms: ${esc(err.message || err)}</div>`);
            return;
        }
        if (rt && app._stale(rt)) return;
        if (quiet && !document.querySelector('.ph-page')) return;   // navigated away meanwhile
        data = d;
        byCode = {};
        (d.platforms || []).forEach(p => { byCode[p.code] = p; });
        paint(app);
    }

    return { render };
})();
