/* Progress (spec 052, WORKLEVELS): the level badge on Library covers, each work's Laurels card
 * (level, next step, "N× your usual", its medals) and the shared /api/progress read.
 *
 *   Progress.badge(work)       "Lv 6" for a Library cover ('' with Levels off)
 *   Progress.card(type, name)  the Laurels card's shell; Progress.fill(root) loads it
 *   Progress.load()            GET /api/progress, reused for 30 s (Laurels, Overview, Accounts)
 *
 * Every number is the server's (progress.py / medals.py): nothing is worked out here, so two
 * screens can't disagree (FR-015). Neutral wording only: a quiet piece is compared, never
 * judged (FR-010). */
(function () {
    'use strict';

    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
    const num = n => Number(n || 0).toLocaleString();
    const AGE = { 48: '2 days', 168: '7 days', 720: '30 days' };

    /* Gamification off hides everything here (FR-014); before the first /api/xp answer, the last
     * known choice (levels.js keeps it on <html data-gamification>). */
    function on() {
        const s = window.Levels && Levels.state();
        return s ? !!s.enabled : document.documentElement.dataset.gamification === 'on';
    }

    function siteName(code) {
        const p = (window.PLATFORMS || []).find(x => x.code === code);
        return p ? p.label : code;
    }

    function day(s) {
        return (window.Utils && Utils.time) ? Utils.time.format(s, { day: 'numeric', month: 'short', year: 'numeric' }) : s;
    }

    function badge(w) {
        if (!on() || !w || !w.level) return '';
        return `<span class="pg-badge" role="img" aria-label="Level ${w.level}">Lv ${w.level}</span>`;
    }

    function card(type, name) {
        if (!on()) return '';
        return `
            <section class="card pg-work" data-pg-type="${esc(type)}" data-pg-name="${esc(name)}" aria-labelledby="pg-sec-work">
                <div class="sec-title"><h2 id="pg-sec-work">Laurels</h2><span class="pg-note"></span></div>
                <div class="pg-body"><p class="muted pg-small">Loading…</p></div>
            </section>`;
    }

    /* "3.1× your usual at 2 days" (US4). */
    function perfLine(p) {
        if (!p) return 'How it compares with your usual shows from 2 days after posting.';
        if (p.blended == null) return 'Not enough history yet to compare it with your usual.';
        return `${p.blended}× your usual at ${AGE[p.at_age_h] || p.at_age_h + ' hours'}${p.top_pct ? ` · Top ${p.top_pct}% of your pieces` : ''}`;
    }

    function breakdown(p) {
        if (!p || !p.sites || !p.sites.length) return '';
        const rows = p.sites.map(r => `<tr><th scope="row">${esc(siteName(r.code))}</th>
            <td>${r.value == null ? '—' : num(r.value)}</td><td>${r.usual == null ? '—' : num(r.usual)}</td>
            <td>${r.ratio == null ? esc(r.reason || '—') : r.ratio + '×'}</td></tr>`).join('');
        const early = Object.keys(p.early || {}).filter(k => p.early[k] != null)
            .map(k => `${p.early[k]}× at ${AGE[k]}`).join(' · ');
        const extra = [
            early && `How fast it started: ${early}`,
            p.depth != null && `Faves and comments per view: ${p.depth}${p.usual_depth != null ? ` (your usual ${p.usual_depth})` : ''}`,
            p.per_follower != null && `Response per follower: ${p.per_follower}`,
            p.spread && `On ${p.spread.posted} site${p.spread.posted === 1 ? '' : 's'}, at or above your usual on ${p.spread.at_or_above}`,
        ].filter(Boolean).map(t => `<li>${esc(t)}</li>`).join('');
        return `
            <details class="pg-breakdown"><summary>How it compares, site by site</summary>
                <div class="pg-table-wrap"><table class="pg-table">
                    <caption class="sr-only">Score at ${esc(AGE[p.at_age_h] || '')} against your usual piece on each site</caption>
                    <thead><tr><th scope="col">Site</th><th scope="col">This piece</th><th scope="col">Your usual</th><th scope="col">Compared</th></tr></thead>
                    <tbody>${rows}</tbody></table></div>
                ${extra ? `<ul class="pg-facts">${extra}</ul>` : ''}
                <p class="muted pg-small">Score = views + 5 × faves + 20 × comments, at the same age after posting. Your usual is the middle value of your earlier pieces on that site.</p>
            </details>`;
    }

    function medalChip(m) {
        const when = m.dated_exactly ? `Earned ${day(m.earned_at)}` : `Before ${day(m.earned_at)}`;
        return `<li class="pg-medal" title="${esc(m.desc || '')}"><span class="pg-medal-ico" aria-hidden="true">${esc(m.icon || '🏅')}</span>
            <span class="pg-medal-name">${esc(m.name)}</span><span class="pg-medal-when">${esc(when)}</span></li>`;
    }

    function paint(sec, d) {
        const body = sec.querySelector('.pg-body');
        const start = d.peak_level > 1 ? 10 * 2 ** (d.peak_level - 1) : 0;
        const pct = Math.round(Math.max(0, Math.min(1, (d.score - start) / (d.next_level_score - start))) * 100);
        sec.querySelector('.pg-note').textContent = `Level ${d.peak_level}`;
        const noViews = (d.no_view_sites || []).map(siteName);
        body.innerHTML = `
            <div class="pg-bar" role="img" aria-label="Score ${num(d.score)} of ${num(d.next_level_score)} for level ${d.peak_level + 1}"><i style="width:${pct}%"></i></div>
            <p class="pg-small">Next level: ${esc(d.next_hint)}</p>
            <p class="pg-perf">${esc(perfLine(d.performance))}</p>
            ${breakdown(d.performance)}
            ${noViews.length ? `<p class="muted pg-small">${esc(noViews.join(', '))} ${noViews.length === 1 ? 'gives' : 'give'} no view count; likes and comments there still count.</p>` : ''}
            ${d.medals && d.medals.length ? `<ul class="pg-medals">${d.medals.map(medalChip).join('')}</ul>`
                : '<p class="muted pg-small">No medals for this piece yet. First comment, On 3 sites and 100 views are the nearest.</p>'}`;
    }

    async function fill(root) {
        const sec = root && root.querySelector('.pg-work');
        if (!sec) return;
        if (!on()) { sec.remove(); return; }
        try {
            const d = await API.get(`/api/progress/work/${encodeURIComponent(sec.dataset.pgType)}/${encodeURIComponent(sec.dataset.pgName)}`);
            if (sec.isConnected) paint(sec, d);
        } catch (e) {
            const body = sec.querySelector('.pg-body');
            if (body) body.innerHTML = /^API 404/.test((e && e.message) || '')
                ? '<p class="muted pg-small">This piece gets its level after the next poll of the sites it is on.</p>'
                : '<p class="muted pg-small">Couldn’t load its Laurels just now.</p>';
        }
    }

    let _cache = null;
    function load(fresh) {
        if (!fresh && _cache && Date.now() - _cache.at < 30000) return _cache.p;
        const p = API.get('/api/progress');
        _cache = { at: Date.now(), p };
        p.catch(() => { _cache = null; });
        return p;
    }

    /* The Overview's Progress card (US8): the same level and next reward as the account menu. */
    function overviewCard(x, p) {
        if (!on() || !x) return '';
        const next = window.Levels ? Levels.nextLine() : '';
        const wk = (p && p.week) || {};
        const bits = [wk.work_levelups ? `${wk.work_levelups} work level-up${wk.work_levelups === 1 ? '' : 's'}` : '',
                      wk.medals ? `${wk.medals} medal${wk.medals === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
        return `
            <a class="card pg-overview" href="#/laurels">
                <span class="pg-ov-level">${x.rewards ? esc(x.rewards.title) + ' · ' : ''}Level ${x.level}</span>
                ${next ? `<span class="pg-small">${esc(next)}</span>` : ''}
                <span class="pg-small">This week: ${bits || 'nothing new yet'}</span>
            </a>`;
    }

    window.Progress = { badge, card, fill, load, overviewCard, perfLine, on };
})();
