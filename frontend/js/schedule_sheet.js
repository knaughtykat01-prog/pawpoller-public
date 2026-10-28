/* ScheduleSheet — "Schedule chapters" / "Schedule" from a story's page (spec 053).
 *
 * The chapter grid (publish_check.js) could already do this, one cell at a time, and its
 * Drip… button could do the whole run — but both sat inside a modal called "Publish check",
 * and a writer who came to schedule their chapters never found them. This is the same work
 * from the page itself:
 *
 *   ScheduleSheet.openStory(name)            every chapter not yet on the ticked sites
 *   ScheduleSheet.openStory(name, { chapter: 5 })   one chapter, one time
 *
 * Three steps (Amendment A, operator's pick of three mockups, `specs/053-task-home/mockups/`):
 *   1 Where — the sites that can take it, each with what's left and any problem in plain words;
 *   2 When  — first day + time, then the gap (four big buttons, or "Other gap": 1 minute–28 days);
 *   3 Check — a calendar of the run beside what's already scheduled; drag a chapter (or Move it
 *             in the list) to shift just that one. Nothing is booked until Schedule.
 * Back never drops a choice: everything lives in `st`, and each step repaints from it.
 *
 * A chaptered story's run goes through the drip (POST …/drip, only_new): a dry run returns the
 * slots and the per-pair problems, so what the person sees is what the server will do. One
 * chapter, or a story with no chapters, goes through POST …/schedule once per ticked site. Both
 * send the Post publicly / Save as draft choice, which the queue now keeps (before 053 it was dropped).
 *
 * Sites come from the grid's own data (GET …/publish-check): a site is offered when the rows
 * being scheduled have a cell it could post; otherwise it shows why not (not connected, posts
 * the whole work at once, already there).
 */
(function () {
    const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
    const plat = (code) => (window.platformByCode && window.platformByCode(code)) || { code, label: code, emoji: '' };
    const NEEDS = new Set(['ready', 'ready_retry', 'deleted_upstream', 'blocked', 'failed_prev']);
    const enc = encodeURIComponent;
    const T = Utils.time;
    // Gaps the operator asked for (23 Oct), in minutes. The first four also get a big button.
    const EVERY = [[1, '1 minute'], [5, '5 minutes'], [10, '10 minutes'], [15, '15 minutes'], [30, '30 minutes'],
        [60, '1 hour'], [120, '2 hours'], [240, '4 hours'], [360, '6 hours'], [720, '12 hours'],
        [1440, '1 day'], [2880, '2 days'], [10080, '7 days'], [20160, '14 days'], [30240, '21 days'], [40320, '28 days']];
    const TILES = [[1440, 'Day', 'daily serial'], [10080, 'Week', 'the usual'],
        [20160, '2 weeks', 'slow burn'], [40320, '4 weeks', 'monthly']];
    const gapName = (m) => ({ 1440: 'Daily', 10080: 'Weekly', 20160: 'Every 2 weeks', 40320: 'Every 4 weeks' })[m]
        || 'Every ' + (EVERY.find(e => e[0] === m) || [0, m + ' minutes'])[1];

    const ScheduleSheet = {
        async openStory(name, opts = {}) {
            let data, sched, queue;
            try {
                [data, sched, queue] = await Promise.all([
                    API.get(`/api/editor/stories/${enc(name)}/publish-check`),
                    API.get(`/api/editor/stories/${enc(name)}/scheduled`).catch(() => ({ items: [] })),
                    API.getPostingQueue().catch(() => ({ queue: [] })),
                ]);
            } catch (err) {
                if (window.toast) toast.error(`Couldn't open scheduling: ${err.message || err}`);
                return;
            }
            const chaptered = data.total_chapters > 0;
            const one = opts.chapter != null ? Number(opts.chapter) : null;
            const series = chaptered && one == null;        // the drip path
            const rows = data.matrix.filter(r => one != null ? r.chapter_index === one
                : chaptered ? r.chapter_index > 0 : r.chapter_index === 0);
            const titles = Object.fromEntries(rows.map(r => [r.chapter_index, r.chapter_title || '']));
            const pending = new Set((sched.items || []).filter(i => i.action === 'post')
                .map(i => `${i.chapter_index}:${i.platform}`));
            const draftable = new Set(data.draft_platforms || []);
            // Everything else already waiting, for the calendar (this story's own rows included:
            // they're the chapters already scheduled elsewhere).
            const waiting = ((queue && queue.queue) || []).filter(q => q.status === 'pending' && q.scheduled_at);

            const sites = data.platforms.map(p => {
                const cells = rows.map(r => [r.chapter_index, r.cells[p.id] || {}]);
                const open = cells.filter(([ch, c]) => NEEDS.has(c.status) && !pending.has(`${ch}:${p.id}`));
                const done = cells.length - open.length;
                const st = cells.length ? cells[0][1].status : '';
                let reason = '';
                if (!open.length) {
                    reason = st === 'no_credentials' ? 'not connected (add it in Accounts)'
                        : st === 'not_supported' ? 'post the whole story at once (use the chapter grid)'
                        : cells.some(([ch]) => pending.has(`${ch}:${p.id}`)) ? 'already scheduled'
                        : 'already there';
                }
                return { id: p.id, name: p.name, left: open.length, done: open.length ? done : 0, reason };
            });
            const offered = sites.filter(s => s.left);

            const title = data.story_title || name.replace(/_/g, ' ');
            const what = one != null ? `Chapter ${one}${titles[one] ? ' · ' + titles[one] : ''}`
                : series ? `${rows.length} chapters` : 'The whole story';
            const st = {
                step: 1, picked: new Set(), start: T.pickerTomorrowAt('20:00'), every: 10080, vis: 'public',
                moves: {}, skips: new Set(), problems: [], slots: null, error: '', seq: 0,
            };

            const ov = document.createElement('div');
            ov.className = 'modal-overlay open';
            ov.setAttribute('role', 'dialog');
            ov.setAttribute('aria-modal', 'true');
            ov.setAttribute('aria-labelledby', 'ss-title');
            ov.innerHTML = `
                <div class="modal-panel ss-sheet">
                    <div class="ss-steps" aria-hidden="true"><span></span><span></span><span></span></div>
                    <div class="ss-body">
                        <section data-step="1">
                            <h3 id="ss-title">Where should ${series ? 'they' : 'it'} go?</h3>
                            <div class="muted ss-sub">Step 1 of 3 · ${esc(title)} · ${esc(what)}</div>
                            <div id="ss-persona" class="persona-picker persona-picker--inline" data-persona-picker hidden></div>
                            <div class="ss-lbl">Sites</div>
                            <div class="ss-sites">
                                ${offered.map(s => `<label class="ss-site" data-platform="${esc(s.id)}">
                                    <input type="checkbox" class="ss-site-check" value="${esc(s.id)}">
                                    <span class="ss-site-name">${esc(plat(s.id).emoji || '')} ${esc(s.name)}</span>
                                    ${series ? `<span class="ss-site-left">${s.left} to go</span>` : ''}
                                    ${s.done ? `<span class="ss-pill">${s.done} already there or scheduled</span>` : ''}
                                    <span class="ss-acct-slot" data-platform="${esc(s.id)}"></span>
                                    <span class="ss-site-problem" data-problem="${esc(s.id)}" hidden></span>
                                </label>`).join('') || '<p class="muted">No connected site can take this right now.</p>'}
                            </div>
                            ${Object.entries(sites.filter(s => !s.left).reduce((m, s) => ((m[s.reason] = m[s.reason] || []).push(s.name), m), {}))
                                .map(([why, names]) => `<p class="ss-site-off"><b>${esc(names.join(', '))}</b>: ${esc(why)}</p>`).join('')}
                        </section>
                        <section data-step="2" hidden>
                            <h3>When should ${series ? 'they' : 'it'} go?</h3>
                            <div class="muted ss-sub" data-ss-sub2></div>
                            <div class="ss-lbl">${series ? 'First chapter' : 'When'}</div>
                            <input type="datetime-local" id="ss-start" class="ss-start">
                            ${series ? `<div class="ss-lbl">Then one every</div>
                            <div class="ss-tiles">${TILES.map(([m, l, s]) =>
                                `<button type="button" class="ss-tile" data-every="${m}">${l}<small>${s}</small></button>`).join('')}</div>
                            <label class="ss-other">Other gap <select id="ss-every">${EVERY.map(([m, l]) =>
                                `<option value="${m}">${l}</option>`).join('')}</select></label>` : ''}
                            <div data-ss-visbox hidden>
                                <div class="ss-lbl">Visibility</div>
                                <div class="ss-seg" role="radiogroup" aria-label="Visibility">
                                    <button type="button" role="radio" data-vis="public">Public</button>
                                    <button type="button" role="radio" data-vis="draft">Draft</button>
                                </div>
                                <p class="muted ss-note" data-ss-visnote></p>
                            </div>
                        </section>
                        <section data-step="3" hidden>
                            <h3>Does this look right?</h3>
                            <div class="muted ss-sub" data-ss-sub3></div>
                            <div class="ss-check" aria-live="polite"></div>
                        </section>
                    </div>
                    <div class="ss-bar">
                        <button type="button" class="btn btn-secondary" data-ss-back>Cancel</button>
                        <div class="ss-sum" data-ss-sum></div>
                        <button type="button" class="btn btn-primary" data-ss-next disabled>Next: when</button>
                    </div>
                </div>`;
            document.body.appendChild(ov);

            const $ = (sel) => ov.querySelector(sel);
            const close = () => { document.removeEventListener('keydown', onKey); ov.remove(); };
            const onKey = (e) => { if (e.key === 'Escape') close(); };
            document.addEventListener('keydown', onKey);
            ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
            $('#ss-start').value = st.start;

            if (window.Components && Components.personaPicker) {
                Components.personaPicker({
                    host: $('#ss-persona'), platforms: offered.map(s => s.id),
                    selectClass: 'ss-acct', storageKey: 'pp-persona-stories',
                    slot: c => $(`.ss-acct-slot[data-platform="${c}"]`),
                    row: c => $(`.ss-site[data-platform="${c}"]`),
                    onChange: () => { if (series) check(); },
                }).catch(() => { /* platform default accounts */ });
            }

            const accts = () => Object.fromEntries([...ov.querySelectorAll('.ss-acct')]
                .filter(el => st.picked.has(el.dataset.platform) && el.value)
                .map(el => [el.dataset.platform, parseInt(el.value, 10)]));
            const personaId = () => { const h = $('#ss-persona'); return h && h.dataset.personaId ? parseInt(h.dataset.personaId, 10) : null; };
            const draftFor = (code) => st.vis === 'draft' && draftable.has(code);
            const labels = (codes) => codes.map(c => plat(c).label).join(' + ');
            const startDate = () => T.toUtc(st.start);
            const dripBody = (dry) => ({
                platforms: [...st.picked], start: startDate().toISOString(), interval_minutes: st.every,
                account_ids: accts(), persona_id: personaId(), only_new: true, moves: st.moves, skip: [...st.skips],
                ...(dry ? { dry_run: true } : { draft: [...st.picked].some(draftFor) }),
            });

            /* One dry run: the slots for step 3 and the per-site problems for step 1. The seq
               counter drops a reply that a newer change has already overtaken. */
            const check = async () => {
                // The account picker can untick a site with no event (no account for that persona).
                st.picked = new Set([...ov.querySelectorAll('.ss-site-check:checked')].map(c => c.value));
                const mine = ++st.seq;
                st.error = '';
                if (!series || !st.picked.size || !startDate()) { st.problems = []; st.slots = null; paint(); return; }
                st.busy = true;
                paint();
                try {
                    const r = await API.post(`/api/editor/stories/${enc(name)}/drip`, dripBody(true));
                    if (mine !== st.seq) return;
                    st.problems = r.problems || [];
                    st.slots = r.slots;
                    st.skipped = r.skipped || [];
                    st.rows = r.rows;
                } catch (err) {
                    if (mine !== st.seq) return;
                    st.slots = null;
                    st.error = err.message || String(err);
                }
                st.busy = false;
                paint();
            };

            const problemsFor = (code) => st.problems.filter(p => p.platform === code);
            const blocked = () => [...st.picked].some(c => problemsFor(c).length);
            const singleSlots = () => [{ chapter: one, scheduled_at: startDate(), platforms: [...st.picked] }];

            const paintSites = () => {
                for (const el of ov.querySelectorAll('[data-problem]')) {
                    const ps = st.picked.has(el.dataset.problem) ? problemsFor(el.dataset.problem) : [];
                    el.hidden = !ps.length;
                    // Each problem names its chapter and offers to leave just that chapter off this site,
                    // so one refusal doesn't hold the whole run (WIREFLOW job 5).
                    el.innerHTML = ps.length ? `${ps.map(p => `${esc(`Ch ${p.chapter}: ${p.message}`)}
                        <button type="button" class="linkish" data-ss-skip="${esc(p.chapter + ':' + p.platform)}">Leave Ch ${esc(String(p.chapter))} off ${esc(plat(p.platform).label)}</button>`).join(' · ')}
                        · <a href="#/editor/${enc(name)}" data-ss-fix>Fix in the editor</a>` : '';
                }
            };

            const paintWhen = () => {
                $('[data-ss-sub2]').textContent = `Step 2 of 3 · ${labels([...st.picked])}`;
                ov.querySelectorAll('.ss-tile').forEach(b => b.classList.toggle('is-on', Number(b.dataset.every) === st.every));
                const sel = $('#ss-every');
                if (sel) sel.value = String(st.every);
                const dr = [...st.picked].filter(c => draftable.has(c));
                $('[data-ss-visbox]').hidden = !dr.length;
                ov.querySelectorAll('[data-vis]').forEach(b => {
                    b.classList.toggle('is-on', b.dataset.vis === st.vis);
                    b.setAttribute('aria-checked', String(b.dataset.vis === st.vis));
                });
                const nodraft = [...st.picked].filter(c => !draftable.has(c));
                $('[data-ss-visnote]').textContent = dr.length && nodraft.length
                    ? `Draft only applies on ${labels(dr)}; ${labels(nodraft)} ${nodraft.length === 1 ? 'has' : 'have'} no drafts and post${nodraft.length === 1 ? 's' : ''} publicly.` : '';
            };

            /* Step 3: the weeks the run touches (a quiet "⋯" where it skips weeks), this story's
               chapters filled, anything already waiting outlined. */
            const calendar = (slots) => {
                const byDay = {};
                for (const s of slots) (byDay[T.dayKey(s.scheduled_at)] = byDay[T.dayKey(s.scheduled_at)] || []).push(s);
                const other = {};
                for (const q of waiting) { const k = T.dayKey(q.scheduled_at); other[k] = (other[k] || 0) + 1; }
                const keys = Object.keys(byDay).sort();
                const weeks = [...new Set(keys.map(k => T.weekStart(k)))].sort();
                let html = '<div class="ss-cal"><div class="ss-cal-row ss-cal-head">'
                    + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(d => `<span>${d}</span>`).join('') + '</div>';
                weeks.forEach((w, i) => {
                    const gap = i ? T.dayGap(weeks[i - 1], w) / 7 - 1 : 0;   // whole weeks with nothing of this story
                    if (gap > 0) html += `<div class="ss-cal-gap">⋯ ${gap} quiet week${gap === 1 ? '' : 's'}</div>`;
                    html += '<div class="ss-cal-row">';
                    for (let d = 0; d < 7; d++) {
                        const k = T.addDays(w, d).slice(0, 10);
                        const mine = byDay[k] || [];
                        const n = other[k] || 0;
                        const label = mine.length === 1 ? `Ch ${mine[0].chapter}` : mine.length ? `${mine.length} ch` : '';
                        html += `<div class="ss-cal-day${mine.length ? ' is-mine' : ''}${n ? ' is-other' : ''}" data-day="${k}"
                            title="${esc(k)}${n ? ` · ${n} already scheduled` : ''}">
                            <span class="ss-cal-num">${Number(k.slice(8))}</span>
                            ${mine.length ? `<span class="ss-cal-ch" draggable="${mine.length === 1}" data-ch="${mine[0].chapter}">${esc(label)}</span>`
                                : n ? `<span class="ss-cal-n">${n}</span>` : ''}
                        </div>`;
                    }
                    html += '</div>';
                });
                return html + '</div>';
            };

            const paintCheck = () => {
                const box = $('.ss-check');
                $('[data-ss-sub3]').textContent = `Step 3 of 3 · ${series ? gapName(st.every).toLowerCase() + ' · ' : ''}${st.vis === 'draft' ? 'draft where possible' : 'public'}`;
                if (st.error) { box.innerHTML = `<p class="publish-action-error">${esc(st.error)}</p>`; return; }
                const slots = series ? st.slots : singleSlots();
                if (!slots) { box.innerHTML = '<span class="muted">Checking every chapter…</span>'; return; }
                const many = slots.length > 1 && slots.some((s, i) => i && T.dayKey(s.scheduled_at) === T.dayKey(slots[i - 1].scheduled_at));
                box.innerHTML = calendar(slots)
                    + `<p class="muted ss-note">Filled = this story · outlined = something already scheduled.${series ? ' Drag a chapter to another day, or use Move below.' : ''}${many ? ' Several chapters share a day: the times are in the list.' : ''}</p>`
                    + `<ol class="ss-list">${slots.map(s => `<li data-ch="${s.chapter}">
                        <b>${one != null || !chaptered ? esc(what) : `Ch ${s.chapter}${titles[s.chapter] ? ' · ' + esc(titles[s.chapter]) : ''}`}</b>:
                        ${esc(T.fmt.dateTime(s.scheduled_at))}${st.moves[s.chapter] ? ' <span class="ss-pill">moved</span>' : ''}
                        <span class="muted">${esc(labels(s.platforms))}</span>
                        ${series ? `<button type="button" class="linkish" data-ss-move="${s.chapter}">Move</button>
                        <span class="ss-move" data-ss-movebox="${s.chapter}" hidden><input type="datetime-local" value="${esc(T.toPicker(s.scheduled_at))}">
                        ${st.moves[s.chapter] ? `<button type="button" class="linkish" data-ss-unmove="${s.chapter}">Put back</button>` : ''}</span>` : ''}
                    </li>`).join('')}</ol>`
                    + (st.skipped && st.skipped.length ? `<p class="muted ss-note">Skipped: ${st.skipped.map(s =>
                        `ch ${s.chapter} on ${esc(plat(s.platform).label)} (${s.reason === 'posted' ? 'already there' : s.reason === 'left out' ? 'left out by you' : 'already scheduled'})`).join(', ')}</p>` : '')
                    + (series && !blocked() ? `<p class="ss-ok">✓ All ${st.rows} posts passed the site checks.</p>` : '');
            };

            const paint = () => {
                ov.querySelectorAll('[data-step]').forEach(s => { s.hidden = Number(s.dataset.step) !== st.step; });
                ov.querySelectorAll('.ss-steps span').forEach((s, i) => s.classList.toggle('is-on', i < st.step));
                paintSites();
                if (st.step === 2) paintWhen();
                if (st.step === 3) paintCheck();
                const back = $('[data-ss-back]'), next = $('[data-ss-next]'), sum = $('[data-ss-sum]');
                back.textContent = st.step === 1 ? 'Cancel' : '‹ Back';
                const n = st.picked.size;
                const posts = series ? (st.rows != null && st.slots ? st.rows : null) : n;
                if (st.step === 1) {
                    sum.innerHTML = n ? `<b>${n} site${n === 1 ? '' : 's'}</b>${posts != null ? ` · ${posts} post${posts === 1 ? '' : 's'}` : ''}<br>${esc(labels([...st.picked]))}` : 'Tick where it goes';
                    next.textContent = 'Next: when';
                    next.disabled = !n || blocked() || !!st.busy;
                } else if (st.step === 2) {
                    const d = startDate();
                    sum.innerHTML = d ? `<b>${series ? esc(gapName(st.every)) : 'At'}</b> ${series ? 'from ' : ''}${esc(T.fmt.dateTime(d))}` : 'Pick a date and time';
                    next.textContent = 'Next: check';
                    next.disabled = !d || (series && (blocked() || !!st.busy));
                } else {
                    const slots = series ? st.slots : singleSlots();
                    sum.innerHTML = slots ? `<b>${posts} post${posts === 1 ? '' : 's'}</b>` : '';
                    next.textContent = 'Schedule';
                    next.disabled = !slots || !!st.error || blocked() || !!st.busy;
                }
            };

            const go = async () => {
                const next = $('[data-ss-next]');
                next.disabled = true;
                next.textContent = 'Scheduling…';
                try {
                    let n = 0;
                    if (series) {
                        // One call, so the rows match the check. Sites without drafts never read
                        // the flag, so "Draft" only changes the ones that have them.
                        n = (await API.post(`/api/editor/stories/${enc(name)}/drip`, dripBody(false))).rows;
                    } else {
                        const a = accts();
                        for (const code of st.picked) {
                            await API.post(`/api/editor/stories/${enc(name)}/schedule`, {
                                platform: code, chapter: one != null ? one : 0, action: 'post',
                                scheduled_at: startDate().toISOString(), draft: draftFor(code),
                                account_id: a[code] || null, persona_id: personaId(),
                            });
                            n++;
                        }
                    }
                    close();
                    if (window.Sound) Sound.play('scheduled');
                    if (window.toast) toast.success(`Scheduled ${n} post${n === 1 ? '' : 's'}. They're listed under Scheduled.`);
                    if (opts.onDone) opts.onDone();
                } catch (err) {
                    st.error = err.message || String(err);
                    paint();
                }
            };

            const move = (ch, local) => {
                const d = T.toUtc(local);
                if (!d) return;
                st.moves[ch] = d.toISOString();
                check();
            };

            ov.addEventListener('change', (e) => {
                const t = e.target;
                if (t.matches('.ss-site-check')) {
                    t.checked ? st.picked.add(t.value) : st.picked.delete(t.value);
                    st.moves = {};
                    check();
                } else if (t.id === 'ss-start') {
                    st.start = t.value; st.moves = {}; check();
                } else if (t.id === 'ss-every') {
                    st.every = Number(t.value); st.moves = {}; check();
                } else if (t.closest('[data-ss-movebox]')) {
                    move(Number(t.closest('[data-ss-movebox]').dataset.ssMovebox), t.value);
                }
            });
            ov.addEventListener('click', (e) => {
                const b = e.target.closest('button, a');
                if (!b) return;
                if (b.matches('[data-ss-fix]')) { close(); return; }
                if (b.matches('[data-ss-skip]')) { e.preventDefault(); st.skips.add(b.dataset.ssSkip); check(); return; }   // inside the site's <label>: don't untick it
                if (b.matches('[data-ss-back]')) { if (st.step === 1) close(); else { st.step--; paint(); } return; }
                if (b.matches('[data-ss-next]')) { if (st.step === 3) go(); else { st.step++; paint(); } return; }
                if (b.dataset.every) { st.every = Number(b.dataset.every); st.moves = {}; check(); return; }
                if (b.dataset.vis) { st.vis = b.dataset.vis; paint(); return; }
                if (b.dataset.ssMove) { const box = $(`[data-ss-movebox="${b.dataset.ssMove}"]`); box.hidden = !box.hidden; return; }
                if (b.dataset.ssUnmove) { delete st.moves[b.dataset.ssUnmove]; check(); }
            });
            // Drag a chapter to another day: same time of day, new date.
            ov.addEventListener('dragstart', (e) => {
                const c = e.target.closest && e.target.closest('.ss-cal-ch[draggable="true"]');
                if (c) e.dataTransfer.setData('text/plain', c.dataset.ch);
            });
            ov.addEventListener('dragover', (e) => { if (e.target.closest && e.target.closest('.ss-cal-day')) e.preventDefault(); });
            ov.addEventListener('drop', (e) => {
                const day = e.target.closest && e.target.closest('.ss-cal-day');
                const ch = Number(e.dataTransfer.getData('text/plain'));
                if (!day || !ch || !st.slots) return;
                e.preventDefault();
                const s = st.slots.find(x => x.chapter === ch);
                if (s) move(ch, day.dataset.day + T.toPicker(s.scheduled_at).slice(10));
            });

            paint();
            const first = ov.querySelector('.ss-site-check');
            if (first) first.focus();
        },
    };

    window.ScheduleSheet = ScheduleSheet;
})();
