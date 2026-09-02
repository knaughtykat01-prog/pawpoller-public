/* ── Laurels — achievements & milestones (concept-layer Slice C · "Den") ──────
 *
 * A motivational view: the reward side of all the numbers. Big view-milestone
 * tracker ("your den has been visited N times") with a progress bar to the next
 * rung, a grid of earned/locked medals, per-persona trophy cards ("account
 * medals"), and a light publishing-rhythm strip.
 *
 * Path A — reuses the real endpoints, adds NO backend:
 *   - API.getPersonas()      → normalized cross-platform totals + per-persona breakdown
 *   - API.getPreferences()   → the app's existing milestone ladders (reused as medal rungs)
 *   - API.getWorks()         → catalogue medals (first story/art, N works, platform spread)
 *   - API.getSummary()       → breakout piece (top-viewed) + watchers
 *   - API.getAggregate()     → tracking-active days (distinct poll dates)
 *   - API.getPostingLog()    → publishing rhythm (weeks with a publish)
 *
 * Milestones read the *current cumulative* totals each platform reports, so they
 * are effectively ALL-TIME (credit for everything achieved) — stated in the page
 * footnote. Template-string rendering, CSP-safe (no inline handlers). */
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

    /* ── Data load + medal computation ───────────────────────────
     * Fetches the 6 read-only endpoints and derives the full model the page
     * renders from — totals, ladders, rhythm and the complete medal set.
     * SHARED by render() and the app-wide achievement watcher so both compute
     * the exact same medal ids from one code path (no drift, one seen-baseline). */
    async _load() {
        const safe = (p, fallback) => p.then(r => r).catch(() => fallback);
        const [personasResp, prefs, worksResp, summary, agg, postLog] = await Promise.all([
            safe(API.getPersonas(), { personas: [], unassigned: [] }),
            safe(API.getPreferences(), {}),
            safe(API.getWorks({ type: 'all' }), { works: [], personas: [] }),
            safe(API.getSummary(), {}),
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

        const works = worksResp.works || [];
        const stories = works.filter(w => w.content_type === 'story');
        const artworks = works.filter(w => w.content_type === 'artwork');
        // Platform spread across the whole catalogue
        const allPlats = new Set();
        let maxPlatsOnWork = 0;
        works.forEach(w => {
            const ps = w.platforms || [];
            ps.forEach(c => allPlats.add(c));
            if (ps.length > maxPlatsOnWork) maxPlatsOnWork = ps.length;
        });
        const totalPlatforms = (window.PLATFORMS || []).length || 16;

        const topViewed = (summary.top_viewed && summary.top_viewed[0]) || null;
        const breakoutViews = topViewed ? (Number(topViewed.views) || 0) : 0;
        const breakoutTitle = topViewed ? topViewed.title : '';
        const watchers = Number(summary.total_watchers) || 0;

        const pV = this._progress(totals.views, ladderV);
        const pF = this._progress(totals.favorites, ladderF);
        const pC = this._progress(totals.comments, ladderC);

        // ── Publishing rhythm + tracking days (also feed the medals) ─
        const logRows = Array.isArray(postLog) ? postLog
            : (postLog.log || postLog.entries || postLog.publications || []);
        const rhythm = this._rhythm(logRows);
        const days = new Set((agg.snapshots || []).map(s => String(s.polled_at || '').slice(0, 10)).filter(Boolean));
        const trackingDays = days.size;

        // Best single-persona view total + how many personas — feed the
        // Persona-tier medals.
        let personaViews = 0;
        personas.forEach(p => {
            const v = Number(p.stats && p.stats.combined && p.stats.combined.views) || 0;
            if (v > personaViews) personaViews = v;
        });

        const medals = this._buildMedals({
            totals, pV, pF, pC, ladderV, ladderF, ladderC,
            stories, artworks, works, allPlats, maxPlatsOnWork, totalPlatforms,
            breakoutViews, breakoutTitle, watchers,
            trackingDays, streak: rhythm.streak,
            personaViews, personaCount: personas.length,
        });

        const empty = (!works.length && totals.views === 0 && totals.favorites === 0);
        return { personas, totals, ladderV, rhythm, trackingDays, pV, pF, pC, medals, empty };
    },

    async render() {
        const app = document.getElementById('app');
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

        const { personas, totals, ladderV, rhythm, trackingDays, pV, pF, pC, medals } = model;
        const earned = medals.filter(m => m.earned);

        body.innerHTML = `
            ${this._heroCard(totals, pV, pF, pC)}

            ${this._medalsSection(medals)}

            ${personas.length ? `
            <section class="lr-section">
                <h2 class="lr-h2">Personas <span class="lr-h2-note">a trophy shelf per identity</span></h2>
                <div class="lr-personas">
                    ${personas.map(p => this._personaCard(p, ladderV)).join('')}
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

        // Animate the hero number + progress bars in, then celebrate anything
        // newly earned since the last visit.
        this._animateIn(totals.views);
        this._celebrateNew(earned);
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

    /* ── New-achievement celebration ─────────────────────────────
     * Diffs the currently-earned medal ids against a localStorage baseline.
     * The FIRST ever check just records the baseline silently (so a returning
     * user isn't buried in confetti); after that, only genuinely new medals
     * pop. Fires when the Laurels page is opened. */
    // Bumped to _v2 with the 100+ medal catalogue so everyone re-baselines
    // silently once (the new per-rung ids would otherwise all read as "new").
    _SEEN_KEY: 'pp_laurels_seen_v2',
    // A jump larger than this at once isn't a single "moment" (it's an upgrade
    // adding medals, or a bulk data catch-up) → absorb silently, no confetti flood.
    _CELEB_BURST_CAP: 3,
    _celebrateNew(earned) {
        const ids = earned.map(x => x.id).filter(Boolean);
        let seen = null;
        try { seen = JSON.parse(localStorage.getItem(this._SEEN_KEY) || 'null'); } catch (e) { seen = null; }
        if (!Array.isArray(seen)) {
            try { localStorage.setItem(this._SEEN_KEY, JSON.stringify(ids)); } catch (e) { /* ignore */ }
            return;
        }
        const seenSet = new Set(seen);
        const fresh = earned.filter(x => x.id && !seenSet.has(x.id));
        if (!fresh.length) return;
        // Advance the baseline first, whatever we decide to show.
        try { localStorage.setItem(this._SEEN_KEY, JSON.stringify([...new Set([...seen, ...ids])])); } catch (e) { /* ignore */ }
        if (fresh.length > this._CELEB_BURST_CAP) return;   // bulk → silent
        fresh.forEach(m => this._enqueueCeleb(m));
    },

    /* ── App-wide achievement watcher ────────────────────────────
     * The page only celebrates when it's open. This runs on every screen:
     * a silent catch-up shortly after login, then a re-check each time a poll
     * completes — so the moment a poll pushes you past 500 faves (or any medal),
     * the celebration pops wherever you are. It reuses _load() + _celebrateNew(),
     * sharing the same `pp_laurels_seen` baseline as the page, so any crossing
     * is celebrated exactly once. Started from App.init() behind the auth gate. */
    startAchievementWatch() {
        if (this._watching) return;
        this._watching = true;
        // Catch-up once the dashboard has painted (covers milestones crossed
        // while the app was closed). First-ever run just records the baseline.
        setTimeout(() => this._achCheck(), 4000);
        // Re-check whenever a poll finishes — detected by the newest
        // last_poll_at across platforms advancing (PlatformHealth ticks 60s;
        // this only fires real work when a poll actually landed).
        if (window.PlatformHealth && PlatformHealth.subscribe) {
            this._achUnsub = PlatformHealth.subscribe((data) => {
                const newest = this._newestPoll(data);
                if (!newest) return;
                if (this._lastPollSeen == null) { this._lastPollSeen = newest; return; }
                if (newest > this._lastPollSeen) {
                    this._lastPollSeen = newest;
                    this._achCheck();
                }
            });
        }
    },
    _newestPoll(data) {
        let max = 0;
        Object.keys(data || {}).forEach(k => {
            const e = data[k];
            const t = (e && e.last_poll_at) ? Date.parse(e.last_poll_at) : 0;
            if (t && t > max) max = t;
        });
        return max || null;
    },
    async _achCheck() {
        if (this._achBusy) return;
        this._achBusy = true;
        try {
            const model = await this._load();
            if (model) this._celebrateNew(model.medals.filter(m => m.earned));
        } catch (e) { /* transient — the next poll re-checks */ }
        finally { this._achBusy = false; }
    },

    _enqueueCeleb(medal) {
        (this._celebQ = this._celebQ || []).push(medal);
        this._drainCeleb();
    },
    _drainCeleb() {
        if (this._celebBusy) return;
        const medal = (this._celebQ || []).shift();
        if (!medal) return;
        this._celebBusy = true;

        const cols = ['#c9822f', '#9a5b34', '#2f8f5b', '#4d6b8a', '#b7791f', '#c0453b'];
        const conf = Array.from({ length: 28 }, (_, i) => {
            const c = cols[i % cols.length];
            const left = Math.round(Math.random() * 100);
            const delay = (Math.random() * 0.3).toFixed(2);
            const dur = (0.9 + Math.random() * 0.8).toFixed(2);
            return `<span class="lr-conf" style="left:${left}%;background:${c};animation-delay:${delay}s;animation-duration:${dur}s"></span>`;
        }).join('');

        const el = document.createElement('div');
        el.className = 'lr-celebrate';
        el.innerHTML = `
            <div class="lr-celebrate-conf" aria-hidden="true">${conf}</div>
            <div class="lr-celebrate-card" role="alert" aria-live="assertive">
                <div class="lr-celebrate-rays" aria-hidden="true"></div>
                <div class="lr-celebrate-ico">${medal.icon || '🏅'}</div>
                <div class="lr-celebrate-label">Achievement unlocked</div>
                <div class="lr-celebrate-name">${this.esc(medal.name)}</div>
                <div class="lr-celebrate-desc">${this.esc(medal.desc || '')}</div>
                <div class="lr-celebrate-dismiss">tap to dismiss</div>
            </div>`;
        document.body.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));

        const close = () => {
            if (this._celebTimer) { clearTimeout(this._celebTimer); this._celebTimer = null; }
            el.classList.remove('show');
            setTimeout(() => {
                el.remove();
                this._celebBusy = false;
                this._drainCeleb();   // next in the queue, if any
            }, 350);
        };
        el.addEventListener('click', close);
        this._celebTimer = setTimeout(close, 4600);
    },

    /* ── Per-work achievements (used by the Bookshelf work detail) ──
     * Pure function over one work's aggregate stats — returns the same medal
     * shape as the account page so it can share the renderer. */
    workMedals(w) {
        w = w || {};
        const views = Number(w.views) || 0, faves = Number(w.faves) || 0, comments = Number(w.comments) || 0;
        const plats = (w.platforms || []).length;
        const chapters = Number(w.chapters) || 0;
        const words = Number(w.words) || 0;
        const gaps = Number(w.incompleteChapters) || 0;
        const m = [];
        const num = (n) => this._num(n);
        const badge = (id, cond, icon, name, desc, sub) => m.push({ id, icon, name, desc, earned: !!cond, sub });
        const tiers = (rows, total, key, unit) => rows.forEach(([r, nm, ic]) =>
            badge(`${key}-${r}`, total >= r, ic, nm, `${num(r)} ${unit} on this work.`, total >= r ? '' : `${num(total)}/${num(r)}`));

        badge('w-published', plats >= 1, '🌱', 'Published', 'Live on at least one platform.');
        badge('w-crossposted', plats >= 3, '🔗', 'Cross-Posted', 'Live on 3 or more platforms.', plats >= 3 ? '' : `${plats}/3`);
        badge('w-wide', plats >= 8, '🌐', 'Wide Reach', 'Live on 8 or more platforms.', plats >= 8 ? '' : `${plats}/8`);
        // Full view / fave / comment ladders — a work always has a next target.
        tiers([[100, 'Seen', '👁'], [500, 'Noticed', '👀'], [1000, '1K Views', '🔥'],
               [5000, '5K Club', '💥'], [10000, '10K Club', '🚀'], [25000, 'Breakout', '🌟']], views, 'w-views', 'views');
        tiers([[10, 'Liked', '⭐'], [100, 'Beloved', '❤'], [500, 'Adored', '💖']], faves, 'w-faves', 'favourites');
        tiers([[10, 'Chatted', '💬'], [25, 'Discussed', '🗨'], [100, 'Talked About', '📢']], comments, 'w-comments', 'comments');
        if (chapters >= 1) {
            badge('w-epic', chapters >= 10, '📕', 'Epic', 'A work of 10+ chapters.', chapters >= 10 ? '' : `${chapters}/10 ch`);
            badge('w-saga', chapters >= 25, '📚', 'Saga', 'A work of 25+ chapters.', chapters >= 25 ? '' : `${chapters}/25 ch`);
        }
        if (words) {
            badge('w-wordsmith', words >= 40000, '✍', 'Novel Length', '40,000+ words.', words >= 40000 ? '' : `${num(words)}/40k`);
            badge('w-tome', words >= 100000, '📜', 'Epic Length', '100,000+ words.', words >= 100000 ? '' : `${num(words)}/100k`);
        }
        if (chapters > 1 && plats >= 1) badge('w-complete', gaps === 0, '✅', 'Complete Run', 'Every chapter reached every platform it should.', gaps === 0 ? '' : `${gaps} gap${gaps === 1 ? '' : 's'}`);
        return m;
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

    /* ── Medal derivation — a big, grouped catalogue (100+), all real, all
     * earned from thresholds on the live totals. Ladders render as a progress
     * track (ascending: earned rungs, then climbing to the locked ones). Every
     * medal has a stable id so the celebration diff can fire each exactly once. */
    _buildMedals(d) {
        const m = [];
        const num = (n) => this._num(n);

        // One medal per rung of a ladder (earned when total >= rung).
        const ladder = (o) => {
            o.rungs.forEach(r => {
                const earned = (o.total || 0) >= r;
                m.push({
                    group: o.group,
                    id: `${o.key}-${r}`,
                    icon: o.icon,
                    name: (o.names && o.names[r]) || `${num(r)} ${o.unit}`,
                    desc: o.desc ? o.desc(r) : `${o.verb} ${num(r)} ${o.unit.toLowerCase()}.`,
                    earned,
                    sub: earned ? '' : `${num(o.total || 0)}/${num(r)}`,
                });
            });
        };
        const badge = (group, id, cond, icon, name, desc, sub) =>
            m.push({ group, id, icon, name, desc, earned: !!cond, sub });

        // ── Views / Favourites / Comments — the big engagement ladders ──
        ladder({ group: 'Views', key: 'views', icon: '👁', unit: 'Views', verb: 'Reach',
            total: d.totals.views,
            rungs: [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000],
            names: { 1000: '1K Views', 10000: '10K Club', 100000: '100K Club', 1000000: 'Millionaire' } });
        ladder({ group: 'Favourites', key: 'faves', icon: '⭐', unit: 'Favourites', verb: 'Reach',
            total: d.totals.favorites,
            rungs: [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000],
            names: { 1000: '1K Favourites', 10000: '10K Favourites' } });
        ladder({ group: 'Comments', key: 'comments', icon: '💬', unit: 'Comments', verb: 'Reach',
            total: d.totals.comments,
            rungs: [10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
            names: { 1000: '1K Comments' } });

        // ── Library: works, stories, artworks ──
        ladder({ group: 'Library', key: 'works', icon: '📚', unit: 'Works', verb: 'Have',
            total: d.works.length, rungs: [1, 3, 5, 10, 25, 50, 100, 250],
            names: { 1: 'First Work', 10: 'Shelf of Ten', 100: 'Century' } });
        ladder({ group: 'Library', key: 'stories', icon: '📖', unit: 'Stories', verb: 'Publish',
            total: d.stories.length, rungs: [1, 5, 10, 25, 50], names: { 1: 'First Words' } });
        ladder({ group: 'Library', key: 'art', icon: '🎨', unit: 'Artworks', verb: 'Publish',
            total: d.artworks.length, rungs: [1, 5, 10, 25, 50], names: { 1: 'First Canvas' } });

        // ── Reach: platform breadth + single-work cross-post depth ──
        ladder({ group: 'Reach', key: 'breadth', icon: '🌐', unit: 'Platforms', verb: 'Publish across',
            total: d.allPlats.size, rungs: [1, 2, 3, 4, 5, 8, 10, 12, 16],
            names: { [d.totalPlatforms]: 'Full Spread' } });
        badge('Reach', 'crosspost-3', d.maxPlatsOnWork >= 3, '🔗', 'Cross-Poster', 'Put one work on 3+ platforms.',
            d.maxPlatsOnWork >= 3 ? '' : `best ${d.maxPlatsOnWork}/3`);
        badge('Reach', 'crosspost-5', d.maxPlatsOnWork >= 5, '🕸', 'Wide Load', 'Put one work on 5+ platforms.',
            d.maxPlatsOnWork >= 5 ? '' : `best ${d.maxPlatsOnWork}/5`);
        badge('Reach', 'crosspost-8', d.maxPlatsOnWork >= 8, '📡', 'Omnipost', 'Put one work on 8+ platforms.',
            d.maxPlatsOnWork >= 8 ? '' : `best ${d.maxPlatsOnWork}/8`);
        badge('Reach', 'crosspost-all', d.maxPlatsOnWork >= d.totalPlatforms, '🌟', 'Everywhere at Once',
            `Put one work on all ${d.totalPlatforms} platforms.`,
            d.maxPlatsOnWork >= d.totalPlatforms ? '' : `best ${d.maxPlatsOnWork}/${d.totalPlatforms}`);

        // ── Following: watchers across platforms ──
        ladder({ group: 'Following', key: 'watchers', icon: '👥', unit: 'Watchers', verb: 'Gather',
            total: d.watchers, rungs: [10, 25, 50, 100, 250, 500, 1000, 5000],
            names: { 100: 'Following of 100', 1000: '1K Followers', 5000: 'Devoted Legion' } });

        // ── Breakouts: your single best work by views ──
        ladder({ group: 'Breakouts', key: 'breakout', icon: '🚀', unit: 'Views', verb: 'Land a single work at',
            total: d.breakoutViews, rungs: [1000, 2500, 5000, 10000, 25000, 50000, 100000],
            names: { 1000: 'Breakout', 2500: 'Rising Star', 5000: 'Hit', 10000: 'Viral',
                     25000: 'Sensation', 50000: 'Phenomenon', 100000: 'Legendary' } });

        // ── Momentum: publishing streak + tracking longevity ──
        ladder({ group: 'Momentum', key: 'streak', icon: '🔥', unit: 'Week Streak', verb: 'Publish',
            total: d.streak, rungs: [2, 3, 4, 6, 8, 12],
            desc: (r) => `Publish in ${r} weeks running.`,
            names: { 2: 'Warming Up', 4: 'On a Roll', 8: 'Relentless', 12: 'Unbroken Quarter' } });
        ladder({ group: 'Momentum', key: 'track', icon: '📅', unit: 'Days Tracked', verb: 'Track',
            total: d.trackingDays, rungs: [7, 30, 90, 180, 365, 730],
            desc: (r) => `Track your work for ${num(r)} days.`,
            names: { 365: 'Dedicated', 730: 'Veteran' } });

        // ── Personas: best persona's metal tier + how many you run ──
        const pv = d.personaViews || 0, pc = d.personaCount || 0;
        badge('Personas', 'persona-silver', pv >= 1000, '🥈', 'Silver Persona', 'Take a persona past 1,000 views.',
            pv >= 1000 ? '' : `${num(pv)}/1,000`);
        badge('Personas', 'persona-gold', pv >= 10000, '🥇', 'Gold Persona', 'Take a persona past 10,000 views.',
            pv >= 10000 ? '' : `${num(pv)}/10,000`);
        badge('Personas', 'persona-plat', pv >= 50000, '🏅', 'Platinum Persona', 'Take a persona past 50,000 views.',
            pv >= 50000 ? '' : `${num(pv)}/50,000`);
        badge('Personas', 'persona-diamond', pv >= 100000, '💎', 'Diamond Persona', 'Take a persona past 100,000 views.',
            pv >= 100000 ? '' : `${num(pv)}/100,000`);
        badge('Personas', 'persona-two', pc >= 2, '🎭', 'Two Faces', 'Run 2 personas at once.',
            pc >= 2 ? '' : `${pc}/2`);
        badge('Personas', 'persona-three', pc >= 3, '🎬', 'Full Cast', 'Run 3 personas at once.',
            pc >= 3 ? '' : `${pc}/3`);

        // ── Milestones: cross-category + collection meta ──
        badge('Milestones', 'all-rounder', d.stories.length >= 1 && d.artworks.length >= 1, '🎪',
            'All-Rounder', 'Publish both a story and an artwork.');
        const earnedSoFar = m.filter(x => x.earned).length;
        [[15, 'Decorated', '🎖'], [30, 'Distinguished', '🏵'], [50, 'Illustrious', '🎗'],
         [75, 'Hall of Fame', '🏛'], [100, 'Completionist', '🏆']].forEach(([n, nm, ic]) => {
            badge('Milestones', `decorated-${n}`, earnedSoFar >= n, ic, nm, `Earn ${n} achievements.`,
                earnedSoFar >= n ? '' : `${earnedSoFar}/${n}`);
        });
        return m;
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
                <div class="lr-medal-ico" aria-hidden="true">${m.icon}</div>
                <div class="lr-medal-name">${this.esc(m.name)}</div>
                <div class="lr-medal-desc">${this.esc(m.desc)}</div>
                ${m.sub ? `<div class="lr-medal-sub">${this.esc(m.sub)}</div>` : ''}
                ${m.earned ? '<div class="lr-medal-check" aria-hidden="true">✓</div>' : ''}
            </div>`;
    },

    /* ── Per-persona trophy card ─────────────────────────────────── */
    _personaCard(p, ladderV) {
        const c = (p.stats && p.stats.combined) || { views: 0, favorites: 0, comments: 0, submissions: 0 };
        const views = Number(c.views) || 0;
        const tier = this._tier(views);
        const level = ladderV.filter(t => t <= views).length;  // rungs cleared
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
            const dt = new Date(ts);
            if (isNaN(dt)) return;
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
