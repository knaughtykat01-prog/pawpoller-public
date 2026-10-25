/* ── Laurels — achievements & milestones (concept-layer Slice C · "Den") ──────
 *
 * A motivational view: the reward side of all the numbers. Big view-milestone
 * tracker ("your den has been visited N times") with a progress bar to the next
 * rung, a grid of earned/locked medals, per-persona trophy cards ("account
 * medals"), and a light publishing-rhythm strip.
 *
 * Reads:
 *   - Progress.load()        → GET /api/progress: the medals, earned and dated by the server
 *                              (medals.py, spec 052), and persona levels
 *   - API.getPersonas()      → normalized cross-platform totals + per-persona breakdown
 *   - API.getPreferences()   → the app's milestone ladders (the hero's progress bars)
 *   - API.getAggregate()     → tracking-active days (distinct poll dates)
 *   - API.getPostingLog()    → publishing rhythm (weeks with a publish)
 *
 * Medals are earned once and kept, even if a site later recounts lower (FR-004). New ones
 * are announced by levels.js on whichever device sees them first, not by this page.
 * Template-string rendering, CSP-safe (no inline handlers). */
window.Laurels = {

    esc(s) {
        return (window.Utils && Utils.escapeHtml)
            ? Utils.escapeHtml(String(s == null ? '' : s))
            : String(s == null ? '' : s).replace(/[&<>"']/g, c =>
                ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    _num(n) {
        return (window.Utils && Utils.formatNumber) ? Utils.formatNumber(n || 0) : String(n || 0);
    },

    // Default rungs mirror the server defaults (routes/api.py) so the medals line
    // up with the milestone alerts even if /preferences omits them.
    _LADDER_V: [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000],
    _LADDER_F: [10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
    _LADDER_C: [10, 25, 50, 100, 250, 500, 1000],

    /* next rung above `total`, the rung just below, and % of the way there */
    _progress(total, ladder) {
        const next = ladder.find(t => t > total);
        const prevArr = ladder.filter(t => t <= total);
        const prev = prevArr.length ? prevArr[prevArr.length - 1] : 0;
        const pct = next ? Math.min(100, Math.round(((total - prev) / (next - prev)) * 100)) : 100;
        return { next, prev, pct, crossed: prevArr, top: prevArr.length ? prevArr[prevArr.length - 1] : 0 };
    },

    /* metal tier for a persona, by its view total */
    _tier(views) {
        if (views >= 100000) return { name: 'Diamond', cls: 'is-diamond' };
        if (views >= 50000)  return { name: 'Platinum', cls: 'is-plat' };
        if (views >= 10000)  return { name: 'Gold', cls: 'is-gold' };
        if (views >= 1000)   return { name: 'Silver', cls: 'is-silver' };
        return { name: 'Bronze', cls: 'is-bronze' };
    },

    /* ── Data load ────────────────────────────────────────────────
     * Fetches the read-only endpoints the page renders from: totals, ladders, rhythm, and the
     * server's medals. A load from the last 30 s is reused (OVERVIEWSPEED). */
    _load() {
        const now = Date.now();
        if (this._loaded && now - this._loaded.at < 30000) return this._loaded.p;
        const p = this._loadFresh();
        this._loaded = { at: now, p };
        p.catch(() => { this._loaded = null; });
        return p;
    },

    async _loadFresh() {
        const safe = (p, fallback) => p.then(r => r).catch(() => fallback);
        const [personasResp, prefs, prog, agg, postLog] = await Promise.all([
            safe(API.getPersonas(), { personas: [], unassigned: [] }),
            safe(API.getPreferences(), {}),
            safe(window.Progress ? Progress.load() : Promise.reject(), { medals: [], personas: [] }),
            safe(API.getAggregate(), { snapshots: [] }),
            safe(API.getPostingLog({ limit: 250, content_type: null }), { log: [] }),
        ]);

        // ── Aggregate the grand totals from the normalized persona stats ──
        const personas = personasResp.personas || [];
        const unassigned = personasResp.unassigned || [];
        const totals = { views: 0, favorites: 0, comments: 0, submissions: 0 };
        const addStats = (c) => {
            if (!c) return;
            totals.views += Number(c.views) || 0;
            totals.favorites += Number(c.favorites) || 0;
            totals.comments += Number(c.comments) || 0;
            totals.submissions += Number(c.submissions) || 0;
        };
        personas.forEach(p => addStats(p.stats && p.stats.combined));
        unassigned.forEach(a => addStats(a.stats));  // unassigned accounts, if they carry stats

        const ladderV = (Array.isArray(prefs.milestone_views) && prefs.milestone_views.length) ? prefs.milestone_views : this._LADDER_V;
        const ladderF = (Array.isArray(prefs.milestone_faves) && prefs.milestone_faves.length) ? prefs.milestone_faves : this._LADDER_F;
        const ladderC = (Array.isArray(prefs.milestone_comments) && prefs.milestone_comments.length) ? prefs.milestone_comments : this._LADDER_C;

        const pV = this._progress(totals.views, ladderV);
        const pF = this._progress(totals.favorites, ladderF);
        const pC = this._progress(totals.comments, ladderC);

        // ── Publishing rhythm + tracking days ─
        const logRows = Array.isArray(postLog) ? postLog
            : (postLog.log || postLog.entries || postLog.publications || []);
        const rhythm = this._rhythm(logRows);
        const days = new Set((agg.snapshots || []).map(s => String(s.polled_at || '').slice(0, 10)).filter(Boolean));
        const trackingDays = days.size;

        const medals = prog.medals || [];
        const levels = {};
        (prog.personas || []).forEach(l => { levels[l.subject] = l.level; });
        const hasWork = medals.some(m => m.id === 'works-1' && m.earned);
        const empty = (!hasWork && totals.views === 0 && totals.favorites === 0);
        return { personas, totals, ladderV, rhythm, trackingDays, pV, pF, pC, medals, levels, empty };
    },

    /* Level card (spec 050): the level, XP to the next one, and this week's XP by kind.
     * Numbers are the server's (/api/xp via Levels); nothing is computed here. */
    _levelSection(x) {
        if (!x || !x.enabled) return '';
        const have = x.xp - x.level_start, need = x.next_level_at - x.level_start;
        const pct = Math.round(Math.max(0, Math.min(1, have / need)) * 100);
        const kinds = [['posting', 'Posting'], ['writing', 'Writing'], ['community', 'Replies'], ['care', 'Tidying']];
        return `
            <section class="lr-section">
                <h2 class="lr-h2">${x.rewards ? Utils.escapeHtml(x.rewards.title) + ' · ' : ''}Level ${x.level} <span class="lr-h2-note">${this._num(x.next_level_at - x.xp)} XP to level ${x.level + 1} · ${this._num(x.xp)} XP in all</span></h2>
                <div class="lr-rhythm-card">
                    ${Levels.nextLine() ? `<div class="lr-rhythm-lead">${Utils.escapeHtml(Levels.nextLine())}</div>` : ''}
                    <div class="lr-mini-bar" role="img" aria-label="${have} of ${need} XP"><div class="lr-mini-fill" data-pct="${pct}" style="width:0"></div></div>
                    <div class="lr-rhythm-lead">This week: <strong>${this._num(x.week.xp)} XP</strong>${x.week.xp ? ' · ' + kinds.filter(([k]) => x.week.by_kind[k]).map(([k, n]) => `${n} ${this._num(x.week.by_kind[k])}`).join(' · ') : ''}</div>
                    <div class="lr-rhythm-small">Most points come from what you do (posting, writing, replying, tidying). Your audience adds a little: when a work reaches a new level, you get 10 × that level${x.audience_xp ? `, ${this._num(x.audience_xp)} XP so far` : ''}.</div>
                    <details class="lr-history" id="lr-history"><summary>History</summary><div class="lr-history-body">Loading…</div></details>
                </div>
            </section>`;
    },

    /* Level history (US7): fetched only when the disclosure is first opened. */
    _wireHistory() {
        const d = document.getElementById('lr-history');
        if (!d) return;
        d.addEventListener('toggle', async () => {
            if (!d.open || d.dataset.loaded) return;
            d.dataset.loaded = '1';
            const box = d.querySelector('.lr-history-body');
            try {
                const h = await API.get('/api/xp/history');
                const fmt = (s) => Utils.time.format(s, { day: 'numeric', month: 'short', year: 'numeric' });   // a date-only string stays that day
                box.innerHTML = `
                    <p class="lr-rhythm-lead">${Utils.escapeHtml(h.month.this_label)}: <strong>${this._num(h.month.this)} XP</strong>
                        · ${Utils.escapeHtml(h.month.last_label)}: ${this._num(h.month.last)} XP</p>
                    ${h.levels.length ? `<ol class="lr-history-list">${h.levels.slice().reverse().map(l =>
                        `<li><span>Level ${l.level}</span><span>${Utils.escapeHtml(fmt(l.date))}</span></li>`).join('')}</ol>`
                        : '<p class="lr-rhythm-small">No levels reached yet.</p>'}`;
            } catch (e) {
                delete d.dataset.loaded;
                box.textContent = 'Couldn’t load your history. Close and open it to try again.';
            }
        });
    },

    /* The rewards shelf (US4): everything earned, usable here; what's next as silhouettes.
     * Shown with levels off too (FR-013): equipped things stay and can still be changed. */
    _SHELF_ROWS: [['theme', 'Themes'], ['ring', 'Rings'], ['style', 'Level-up styles']],

    _shelfSection(x) {
        const r = x && x.rewards;
        if (!r || !window.Levels) return '';
        const esc = (s) => Utils.escapeHtml(String(s ?? ''));
        const have = new Set(r.unlocked);
        const themeOf = (id) => ((window.App && App.THEMES) || []).find(t => t.rewardId === id);
        const inUse = (c) => c.kind === 'theme' ? (themeOf(c.id) || {}).id === r.equipped.theme : r.equipped[c.kind] === c.id;
        const tile = (c) => {
            const open = have.has(c.id);
            if (!open && c.surprise) {
                return `<li class="lr-rw is-unfound"><span class="lr-rw-prev lr-rw-q" aria-hidden="true">???</span>
                    <span class="lr-rw-name">Surprise</span><span class="lr-rw-sub">${esc(c.hint)}</span></li>`;
            }
            if (!open) {
                return `<li class="lr-rw is-locked"><span class="lr-rw-prev" aria-hidden="true">${Levels.preview(c)}<span class="lr-rw-lock">🔒</span></span>
                    <span class="lr-rw-name">${esc(c.name)}</span><span class="lr-rw-sub">Locked · Level ${c.level}</span></li>`;
            }
            const on = inUse(c);
            const canUse = c.kind !== 'theme' || themeOf(c.id);
            const btn = on
                ? (c.kind === 'theme' ? '<span class="lr-rw-on">In use</span>'
                    : `<span class="lr-rw-on">In use</span><button type="button" class="btn btn-secondary btn-sm" data-rw-off="${esc(c.kind)}">Take off</button>`)
                : canUse ? `<button type="button" class="btn btn-primary btn-sm" data-rw-use="${esc(c.id)}">Use</button>` : '';
            return `<li class="lr-rw${on ? ' is-on' : ''}"><span class="lr-rw-prev" aria-hidden="true">${Levels.preview(c)}</span>
                <span class="lr-rw-name">${esc(c.name)}${c.retired ? ' <small class="lr-rw-tag">Retired</small>' : ''}</span>
                <span class="lr-rw-sub">${c.surprise ? 'Surprise' : c.level ? `Level ${c.level}` : ''}</span>${btn}</li>`;
        };
        const none = (kind) => {
            const on = !r.equipped[kind];
            return `<li class="lr-rw lr-rw-none${on ? ' is-on' : ''}"><span class="lr-rw-prev" aria-hidden="true">—</span>
                <span class="lr-rw-name">None</span><span class="lr-rw-sub"></span>${on ? '<span class="lr-rw-on">In use</span>'
                : `<button type="button" class="btn btn-secondary btn-sm" data-rw-off="${kind}">Use</button>`}</li>`;
        };
        const rows = this._SHELF_ROWS.map(([kind, label]) => {
            const items = r.catalog.filter(c => c.kind === kind);
            return `<div class="lr-shelf-row"><h3 class="lr-shelf-h">${label}</h3>
                <ul class="lr-shelf">${kind === 'theme' ? '' : none(kind)}${items.map(tile).join('')}</ul></div>`;
        }).join('');
        return `
            <section class="lr-section" id="rewards" tabindex="-1">
                <h2 class="lr-h2">Rewards <span class="lr-h2-note">${have.size} of ${r.catalog.length} unlocked</span></h2>
                ${rows}
            </section>`;
    },

    _wireShelf() {
        const sec = document.getElementById('rewards');
        if (!sec) return;
        sec.addEventListener('click', async (e) => {
            const use = e.target.closest('[data-rw-use]'), off = e.target.closest('[data-rw-off]');
            if (!use && !off) return;
            const s = Levels.state(), r = s && s.rewards;
            if (!r) return;
            if (use) {
                const c = r.catalog.find(x => x.id === use.dataset.rwUse);
                if (!c) return;
                if (c.kind === 'theme') {
                    const t = App.THEMES.find(x => x.rewardId === c.id);
                    if (t) { App.applyTheme(t.id); r.equipped.theme = t.id; }
                } else {
                    try { r.equipped = await API.post('/api/xp/rewards/equip', { kind: c.kind, id: c.id }); } catch (err) { return; }
                }
            } else {
                try { r.equipped = await API.post('/api/xp/rewards/equip', { kind: off.dataset.rwOff, id: null }); } catch (err) { return; }
            }
            await Levels.refresh();
            const fresh = document.createElement('div');
            fresh.innerHTML = this._shelfSection(Levels.state());
            if (fresh.firstElementChild) { sec.replaceWith(fresh.firstElementChild); this._wireShelf(); }
            document.querySelector('#rewards [data-rw-use], #rewards [data-rw-off]')?.focus();
        });
    },

    async render() {
        const _rt = App._routeToken();   // route race guard (App._stale)
        const app = document.getElementById('app');
        if (App._stale(_rt)) return;
        // Gamification off (a choice, 22 Oct): no medals or levels, just the way back on.
        const st = window.Levels ? (Levels.state() || await Levels.refresh()) : null;
        if (App._stale(_rt)) return;
        if (st && !st.enabled) {
            app.innerHTML = `
                <div class="lr-head"><h1 class="lr-title">Laurels</h1>
                    <p class="lr-sub">Levels, medals and rewards are turned off. PawPoller still counts them quietly,
                    so if you turn them on, everything you've earned is there.</p></div>
                <button class="btn btn-primary" type="button" id="lr-turn-on">Turn on levels, medals and rewards</button>`;
            document.getElementById('lr-turn-on').addEventListener('click', async () => {
                try { await API.savePreferences({ levels_enabled: true }); await Levels.refresh(); this.render(); }
                catch (e) { window.toast?.error("Couldn't save that: " + e.message); }
            });
            return;
        }
        app.innerHTML = `
            <div class="lr-head">
                <div class="lr-eyebrow">Your den</div>
                <h1 class="lr-title">Laurels</h1>
                <p class="lr-sub">Every view, fave and comment you've ever earned — turned into
                milestones and medals. The reward side of the numbers.</p>
            </div>
            <div id="lr-body"><div class="loading-spinner">Tallying your laurels…</div></div>`;

        const model = await this._load();

        const body = document.getElementById('lr-body');
        if (!body) return;  // navigated away

        // Empty state — nothing published yet
        if (model.empty) {
            body.innerHTML = `
                <div class="lr-empty">
                    <div class="lr-empty-emoji">🌱</div>
                    <h2>No laurels yet — but that's where everyone starts.</h2>
                    <p>Publish and connect your accounts, and this page fills with milestones,
                    medals and per-persona trophies as the views come in.</p>
                    <a class="btn btn-primary" href="#/library">Go to your Library</a>
                </div>`;
            return;
        }

        const { personas, totals, ladderV, rhythm, trackingDays, pV, pF, pC, medals, levels } = model;
        const xpBody = window.Levels ? await Levels.refresh() : null;

        body.innerHTML = `
            ${this._heroCard(totals, pV, pF, pC)}

            ${this._levelSection(xpBody)}

            ${this._shelfSection(xpBody)}

            ${this._medalsSection(medals)}

            ${personas.length ? `
            <section class="lr-section">
                <h2 class="lr-h2">Personas <span class="lr-h2-note">a trophy shelf per identity</span></h2>
                <div class="lr-personas">
                    ${personas.map(p => this._personaCard(p, levels[`persona:${p.persona_id}`])).join('')}
                </div>
            </section>` : ''}

            <section class="lr-section">
                <h2 class="lr-h2">Rhythm <span class="lr-h2-note">momentum, not just totals</span></h2>
                <div class="lr-rhythm-wrap">
                    <div class="lr-rhythm-card">
                        <div class="lr-rhythm-lead">${rhythm.active} of the last 12 weeks had a publish${rhythm.streak >= 2 ? ` · <strong>${rhythm.streak}-week streak</strong>` : ''}</div>
                        <div class="lr-weeks">
                            ${rhythm.weeks.map(w => `<span class="lr-week ${w.on ? 'is-on' : ''}" title="${w.label}"></span>`).join('')}
                        </div>
                    </div>
                    <div class="lr-rhythm-card">
                        <div class="lr-rhythm-big">${this._num(trackingDays)}</div>
                        <div class="lr-rhythm-small">days PawPoller has been tracking your work</div>
                    </div>
                </div>
            </section>

            <p class="lr-foot">Milestones reflect your <strong>all-time</strong> totals as each platform
            currently reports them — you keep credit for everything you've earned.</p>`;

        this._wireHistory();
        this._wireShelf();
        if (this._jump) {                                  // "See them" on the catch-up card
            document.getElementById(this._jump)?.scrollIntoView();
            document.getElementById(this._jump)?.focus({ preventScroll: true });
            this._jump = null;
        }

        // Medal filter (All / Earned) — CSP-safe, wired after render.
        const sec = document.getElementById('lr-medals-section');
        if (sec) {
            sec.querySelectorAll('.lr-filt').forEach(btn => {
                btn.addEventListener('click', () => {
                    sec.querySelectorAll('.lr-filt').forEach(b => b.classList.remove('is-active'));
                    btn.classList.add('is-active');
                    sec.classList.toggle('filt-earned', btn.dataset.filt === 'earned');
                });
            });
        }

        this._animateIn(totals.views);   // the hero number + progress bars
    },

    /* ── Count-up + progress-fill entrance animation ─────────────── */
    _animateIn(target) {
        // Fill every progress bar from 0 → its data-pct (CSS transitions width).
        requestAnimationFrame(() => {
            document.querySelectorAll('.lr-hero-fill[data-pct], .lr-mini-fill[data-pct]').forEach(el => {
                el.style.width = (Number(el.dataset.pct) || 0) + '%';
            });
        });
        const el = document.querySelector('.lr-hero-count');
        if (!el || !target || target < 50) return;
        const dur = 1100;
        let start = null;
        const tick = (now) => {
            if (start == null) start = now;
            const t = Math.min(1, (now - start) / dur);
            const eased = 1 - Math.pow(1 - t, 3);   // ease-out cubic
            el.textContent = this._num(Math.round(target * eased));
            if (t < 1) requestAnimationFrame(tick);
            else el.textContent = this._num(target);
        };
        requestAnimationFrame(tick);
    },

    /* ── Hero milestone card ─────────────────────────────────────── */
    _heroCard(totals, pV, pF, pC) {
        const nextLine = pV.next
            ? `${this._num(pV.next - totals.views)} more to <strong>${this._num(pV.next)}</strong>`
            : `every view milestone cleared`;
        return `
            <div class="lr-hero">
                <div class="lr-hero-main">
                    <div class="lr-hero-eyebrow">Your den has been visited</div>
                    <div class="lr-hero-num"><span class="lr-hero-count" data-count="${totals.views}">${this._num(totals.views)}</span><span class="lr-hero-unit">times</span></div>
                    <div class="lr-hero-bar">
                        <div class="lr-hero-fill" data-pct="${pV.pct}" style="width:0"></div>
                        ${pV.prev ? `<span class="lr-hero-prev">${this._num(pV.prev)}</span>` : ''}
                        ${pV.next ? `<span class="lr-hero-next">${this._num(pV.next)}</span>` : ''}
                    </div>
                    <div class="lr-hero-caption">${nextLine}</div>
                </div>
                <div class="lr-hero-side">
                    ${this._miniTrack('Favourites', totals.favorites, pF, '❤')}
                    ${this._miniTrack('Comments', totals.comments, pC, '💬')}
                </div>
            </div>`;
    },

    _miniTrack(label, total, p, icon) {
        return `
            <div class="lr-mini">
                <div class="lr-mini-top"><span class="lr-mini-ico" aria-hidden="true">${icon}</span>
                    <span class="lr-mini-val">${this._num(total)}</span>
                    <span class="lr-mini-label">${label}</span></div>
                <div class="lr-mini-bar"><div class="lr-mini-fill" data-pct="${p.pct}" style="width:0"></div></div>
                <div class="lr-mini-cap">${p.next ? `${this._num(p.next)} next` : 'maxed'}</div>
            </div>`;
    },

    /* Group order for the medal grid (any medal whose group isn't listed falls
     * to the end under its own heading). */
    _GROUP_ORDER: ['Views', 'Favourites', 'Comments', 'Library', 'Reach',
                   'Following', 'Breakouts', 'Momentum', 'Personas', 'Milestones'],

    /* Grouped, filterable medals section. */
    _medalsSection(medals) {
        const totalEarned = medals.filter(m => m.earned).length;
        const byGroup = {};
        medals.forEach(md => { (byGroup[md.group] = byGroup[md.group] || []).push(md); });
        const order = this._GROUP_ORDER.slice();
        Object.keys(byGroup).forEach(g => { if (order.indexOf(g) < 0) order.push(g); });
        const groups = order.filter(g => byGroup[g]).map(g => {
            const list = byGroup[g];
            const e = list.filter(x => x.earned).length;
            return `
                <div class="lr-mgroup" data-earned="${e}">
                    <h3 class="lr-mg-title">${this.esc(g)} <span class="lr-mg-count">${e}/${list.length}</span></h3>
                    <div class="lr-medals">${list.map(md => this._medal(md)).join('')}</div>
                </div>`;
        }).join('');
        return `
            <section class="lr-section" id="lr-medals-section">
                <h2 class="lr-h2">Medals
                    <span class="lr-h2-note">${totalEarned} of ${medals.length} earned</span>
                    <span class="lr-mfilter">
                        <button type="button" class="lr-filt is-active" data-filt="all">All</button>
                        <button type="button" class="lr-filt" data-filt="earned">Earned</button>
                    </span>
                </h2>
                ${groups}
            </section>`;
    },

    _medal(m) {
        return `
            <div class="lr-medal ${m.earned ? 'is-earned' : 'is-locked'}">
                <div class="lr-medal-ico" aria-hidden="true">${this.esc(m.icon)}</div>
                <div class="lr-medal-name">${this.esc(m.name)}</div>
                <div class="lr-medal-desc">${this.esc(m.desc)}</div>
                ${m.earned && m.earned_at ? `<div class="lr-medal-sub">${m.dated_exactly ? 'Earned' : 'Before'} ${this.esc(Utils.time.format(m.earned_at, { day: 'numeric', month: 'short', year: 'numeric' }))}</div>`
                    : m.sub ? `<div class="lr-medal-sub">${this.esc(m.sub)}</div>` : ''}
                ${m.earned ? '<div class="lr-medal-check" aria-hidden="true">✓</div>' : ''}
            </div>`;
    },

    /* ── Per-persona trophy card ─────────────────────────────────── */
    /* `level` is the persona's level on the work scale (spec 052 US6), from /api/progress. */
    _personaCard(p, level) {
        const c = (p.stats && p.stats.combined) || { views: 0, favorites: 0, comments: 0, submissions: 0 };
        const views = Number(c.views) || 0;
        const tier = this._tier(views);
        level = level || 1;
        const color = p.color || 'var(--accent)';
        return `
            <div class="lr-persona">
                <div class="lr-persona-top">
                    <span class="lr-persona-dot" style="background:${this.esc(color)}"></span>
                    <span class="lr-persona-name">${this.esc(p.name)}</span>
                    <span class="lr-persona-tier ${tier.cls}">${tier.name}</span>
                </div>
                <div class="lr-persona-level">Level ${level} <span>· ${this._num(c.submissions || 0)} works</span></div>
                <div class="lr-persona-stats">
                    <div><span class="lr-ps-v">${this._num(views)}</span><span class="lr-ps-l">views</span></div>
                    <div><span class="lr-ps-v">${this._num(c.favorites || 0)}</span><span class="lr-ps-l">faves</span></div>
                    <div><span class="lr-ps-v">${this._num(c.comments || 0)}</span><span class="lr-ps-l">comments</span></div>
                </div>
            </div>`;
    },

    /* ── Publishing rhythm: last 12 ISO weeks that saw a publish ──── */
    _rhythm(rows) {
        // Bucket event timestamps to a YYYY-Www key; build 12 trailing weeks.
        const keyOf = (d) => {
            // ISO week number
            const dt = new Date(d.getTime());
            dt.setHours(0, 0, 0, 0);
            dt.setDate(dt.getDate() + 3 - ((dt.getDay() + 6) % 7));  // nearest Thursday
            const week1 = new Date(dt.getFullYear(), 0, 4);
            const wk = 1 + Math.round(((dt - week1) / 86400000 - 3 + ((week1.getDay() + 6) % 7)) / 7);
            return `${dt.getFullYear()}-W${String(wk).padStart(2, '0')}`;
        };
        const active = new Set();
        (rows || []).forEach(r => {
            const ts = r.created_at || r.first_posted_at || r.posted_at || r.timestamp || r.last_updated_at;
            if (!ts) return;
            const dt = Utils.time.parse(ts);
            if (!dt) return;
            active.add(keyOf(dt));
        });
        // Walk back 12 weeks from now
        const weeks = [];
        const now = new Date();
        for (let i = 11; i >= 0; i--) {
            const d = new Date(now.getTime());
            d.setDate(d.getDate() - i * 7);
            const k = keyOf(d);
            weeks.push({ key: k, on: active.has(k), label: k });
        }
        const activeCount = weeks.filter(w => w.on).length;
        // Current streak: consecutive active weeks ending at the current week
        let streak = 0;
        for (let i = weeks.length - 1; i >= 0; i--) {
            if (weeks[i].on) streak++; else break;
        }
        return { weeks, active: activeCount, streak };
    },
};
