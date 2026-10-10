/* ── Choose platforms (spec 020) ─────────────────────────────────
 *
 * The ONE "which sites do I use" control: the Platforms page opens it as a side panel
 * (a bottom sheet on a phone), Settings → Platforms shows it inline. Stored as the
 * HIDDEN codes (`hidden_platforms`, a server preference) so a site connected later
 * shows up by default. Hidden = gone from menus, pickers, analytics and the Platforms
 * page; nothing is deleted, and switching it back on brings everything back.
 *
 * Hiding a CONNECTED site asks whether to keep checking it for new stats in the
 * background (ticked by default). Unticked uses the existing per-platform polling pause
 * (`polling_paused_platforms`, which the server cycle and the desktop pollers skip);
 * showing the site again offers to resume. Scheduled posts always still go out.
 */
window.PlatformPicker = (function () {
    const GROUPS = [
        { name: 'Art sites', codes: ['fa', 'ib', 'da', 'e621', 'ws', 'fbr', 'r34', 'fn', 'pix', 'ik'] },
        { name: 'Story sites', codes: ['ao3', 'sf', 'sqw', 'wp'] },
        { name: 'Social', codes: ['bsky', 'tw', 'mast', 'thr', 'tum', 'ig', 'fb'] },
        { name: 'Video & audio', codes: ['yt', 'sc', 'ng', 'pic', 'pod'] },
        { name: 'Messaging', codes: ['tg'] },
    ];
    const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));

    function groups() {
        const all = (window.PLATFORMS || []).map(p => p.code);
        const listed = new Set(GROUPS.flatMap(g => g.codes));
        const out = GROUPS.map(g => ({ name: g.name, codes: g.codes.filter(c => all.includes(c)) }))
            .filter(g => g.codes.length);
        const rest = all.filter(c => !listed.has(c));
        if (rest.length) out.push({ name: 'Other', codes: rest });
        return out;
    }

    function logo(code) {
        const p = (window.platformByCode && window.platformByCode(code)) || {};
        return p.logo ? `<img class="pk-ico" src="${esc(p.logo)}" alt="" aria-hidden="true">`
            : `<span class="pk-ico pk-ico--emoji" aria-hidden="true">${esc(p.emoji || '•')}</span>`;
    }

    function label(code) {
        const p = (window.platformByCode && window.platformByCode(code)) || {};
        return p.label || code;
    }

    /* What the picker knows about each site: connected, polled, paused, scheduled posts.
       From the Platforms page's overview when it has one, else fetched. */
    async function facts(given) {
        if (given) return given;
        try {
            const d = await API.getPlatformsOverview();
            const m = {};
            (d.platforms || []).forEach(p => { m[p.code] = p; });
            return m;
        } catch (e) {
            return {};
        }
    }

    async function saveHidden(next) {
        await API.savePreferences({ hidden_platforms: next });
        window.HIDDEN_PLATFORMS = next;
    }

    /* Mount the picker into `host`. opts: { facts?: {code: overviewEntry}, filter?: 'all'|'connected'|'hidden',
       onChange?: () => void, compact?: bool } — returns { refresh }. */
    async function mount(host, opts = {}) {
        const info = await facts(opts.facts);
        let filter = opts.filter || 'all';
        let ask = null;   // { code, kind: 'hide'|'resume' } — the inline question being asked

        const hiddenSet = () => new Set(window.HIDDEN_PLATFORMS || []);
        const visible = (code) => {
            if (ask && ask.code === code) return true;   // a pending question keeps its row on screen
            const f = info[code] || {};
            if (filter === 'connected') return !!f.configured;
            if (filter === 'hidden') return hiddenSet().has(code);
            return true;
        };

        function status(code) {
            const f = info[code] || {};
            if (!f.configured) return '<span class="pk-st">Not set up</span>';
            if (f.paused) return '<span class="pk-st pk-st--paused">Connected · checking paused</span>';
            return '<span class="pk-st pk-st--ok">Connected</span>';
        }

        function question(code) {
            if (!ask || ask.code !== code) return '';
            const f = info[code] || {};
            const name = esc(label(code));
            if (ask.kind === 'hide') {
                const q = f.queued ? ` It has ${f.queued} scheduled post${f.queued === 1 ? '' : 's'}; they still go out.` : '';
                return `<div class="pk-ask" role="group" aria-label="Hide ${name}">
                    <p><b>${name} is still connected.</b> Hide it from view only, or stop checking it too?${esc(q)}</p>
                    <label class="pk-check"><input type="checkbox" data-keep checked> Keep checking it for new stats in the background</label>
                    <div class="pk-row"><button type="button" class="btn btn-sm btn-primary" data-ask-yes>Hide</button>
                        <button type="button" class="btn btn-sm" data-ask-no>Cancel</button></div></div>`;
            }
            return `<div class="pk-ask" role="group" aria-label="Resume ${name}">
                <p>Checking ${name} for new stats is paused. Resume it?</p>
                <div class="pk-row"><button type="button" class="btn btn-sm btn-primary" data-ask-yes>Resume checking</button>
                    <button type="button" class="btn btn-sm" data-ask-no>Not now</button></div></div>`;
        }

        function paint() {
            const hidden = hiddenSet();
            const all = (window.PLATFORMS || []).map(p => p.code);
            const shown = all.filter(c => !hidden.has(c)).length;
            const unset = all.filter(c => !(info[c] || {}).configured && !hidden.has(c)).length;
            const seg = (key, text) => `<button type="button" data-filter="${key}" aria-pressed="${filter === key}">${text}</button>`;
            host.innerHTML = `
                <div class="pk-top">
                    <span class="pk-count">${shown} of ${all.length} shown</span>
                    <div class="pk-seg" role="group" aria-label="Show">${seg('all', 'All')}${seg('connected', 'Connected')}${seg('hidden', 'Hidden')}</div>
                    <button type="button" class="pk-link" data-hide-unset ${unset ? '' : 'disabled'}>Hide everything not set up</button>
                </div>
                ${groups().map(g => {
                    const codes = g.codes.filter(visible);
                    if (!codes.length) return '';
                    return `<section class="pk-group" aria-label="${esc(g.name)}">
                        <div class="pk-ghead"><h3>${esc(g.name)}</h3>
                            <button type="button" class="pk-link" data-hide-group="${esc(g.codes.join(','))}">Hide group</button></div>
                        ${codes.map(code => `
                            <div class="pk-item" data-code="${esc(code)}">
                                ${logo(code)}
                                <div class="pk-name"><b>${esc(label(code))}</b>${status(code)}</div>
                                <label class="pk-switch"><input type="checkbox" role="switch" data-show="${esc(code)}"${hidden.has(code) ? '' : ' checked'}>
                                    <span class="pk-switch-ui" aria-hidden="true"></span><span class="sr-only">Show ${esc(label(code))}</span></label>
                            </div>${question(code)}`).join('')}
                    </section>`;
                }).join('') || '<p class="muted pk-empty">Nothing here.</p>'}
                <p class="pk-msg muted" role="status" data-msg>Saved as you go, on every device.</p>`;
        }

        async function setHidden(next, note) {
            const msg = host.querySelector('[data-msg]');
            try {
                await saveHidden(next);
                if (msg) msg.textContent = note || 'Saved.';
                if (opts.onChange) opts.onChange();
            } catch (err) {
                if (msg) msg.textContent = 'Could not save: ' + (err.message || err);
            }
        }

        async function hide(codes) {
            const next = [...new Set([...(window.HIDDEN_PLATFORMS || []), ...codes])];
            await setHidden(next, codes.length > 1 ? `Hid ${codes.length} sites.` : 'Hidden.');
        }

        host.addEventListener('click', async (e) => {
            const f = e.target.closest('[data-filter]');
            if (f) { filter = f.dataset.filter; ask = null; paint(); return; }
            if (e.target.closest('[data-hide-unset]')) {
                const codes = (window.PLATFORMS || []).map(p => p.code).filter(c => !(info[c] || {}).configured);
                await hide(codes); paint(); return;
            }
            const g = e.target.closest('[data-hide-group]');
            if (g) {
                // A group can hold connected sites; hide those only from view (they keep being checked).
                await hide(g.dataset.hideGroup.split(',')); paint(); return;
            }
            if (e.target.closest('[data-ask-no]')) { ask = null; paint(); return; }
            if (e.target.closest('[data-ask-yes]') && ask) {
                const code = ask.code;
                if (ask.kind === 'hide') {
                    const keep = host.querySelector('[data-keep]');
                    if (keep && !keep.checked) {
                        try { await API.pausePlatformPolling(code); (info[code] || {}).paused = true; } catch (err) { /* reported below */ }
                    }
                    ask = null;
                    await hide([code]);
                } else {
                    try { await API.resumePlatformPolling(code); (info[code] || {}).paused = false; } catch (err) { /* ignore */ }
                    ask = null;
                    if (opts.onChange) opts.onChange();
                }
                paint();
            }
        });
        host.addEventListener('change', async (e) => {
            const box = e.target.closest('[data-show]');
            if (!box) return;
            const code = box.dataset.show;
            const f = info[code] || {};
            if (!box.checked) {
                if (f.configured && f.can_poll && !f.paused) {
                    box.checked = true;              // not hidden until they answer
                    ask = { code, kind: 'hide' };
                    paint();
                    host.querySelector('[data-ask-yes]')?.focus();
                    return;
                }
                await hide([code]);
            } else {
                await setHidden((window.HIDDEN_PLATFORMS || []).filter(c => c !== code), 'Shown.');
                if (f.paused) { ask = { code, kind: 'resume' }; }
            }
            paint();
            if (ask) host.querySelector('[data-ask-yes]')?.focus();
        });
        paint();
        return { refresh: paint, hide: (code) => { ask = { code, kind: 'hide' }; paint(); } };
    }

    /* Hide one site from anywhere (a tile's ⋯ menu): a connected, checked site asks first,
       in the panel; anything else hides straight away. */
    async function hideOne(code, factsMap, onChange) {
        const f = (factsMap || {})[code] || {};
        if (f.configured && f.can_poll && !f.paused) {
            const api = await openPanel({ facts: factsMap, onChange });
            if (api) api.hide(code);
            return;
        }
        await saveHidden([...new Set([...(window.HIDDEN_PLATFORMS || []), code])]);
        if (onChange) onChange();
    }

    /* The side panel (a bottom sheet ≤ 768 px): a dialog; Esc and Done close it. */
    async function openPanel(opts = {}) {
        document.querySelector('.pk-overlay')?.remove();
        const back = document.activeElement;
        const ov = document.createElement('div');
        ov.className = 'pk-overlay';
        ov.innerHTML = `<div class="pk-panel" role="dialog" aria-modal="true" aria-labelledby="pk-title">
            <div class="pk-phead"><h2 id="pk-title">Choose platforms</h2>
                <button type="button" class="pk-x" data-close aria-label="Close">✕</button></div>
            <p class="pk-sub muted">Hidden platforms disappear from menus, pickers, analytics and the Platforms page.
                Nothing is deleted; switch them back on any time.</p>
            <div class="pk-body"></div>
            <div class="pk-foot"><button type="button" class="btn btn-primary" data-close>Done</button></div></div>`;
        document.body.appendChild(ov);
        let changed = false;
        const close = () => {
            document.removeEventListener('keydown', onKey);
            ov.remove();
            if (back && back.focus) back.focus();
            if (changed && opts.onClose) opts.onClose();
        };
        const onKey = (e) => { if (e.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        ov.addEventListener('click', (e) => { if (e.target === ov || e.target.closest('[data-close]')) close(); });
        const api = await mount(ov.querySelector('.pk-body'), {
            facts: opts.facts, filter: opts.filter,
            onChange: () => { changed = true; if (opts.onChange) opts.onChange(); },
        });
        ov.querySelector('.pk-x').focus();
        return api;
    }

    return { mount, openPanel, hideOne, groups };
})();
