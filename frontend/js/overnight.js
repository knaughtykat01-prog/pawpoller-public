/* Overnight (spec 026): what happened while you were away, in one sheet.
 *
 * Opens by itself on the first visit after the "Show after" gap (the server decides: POST
 * /api/overnight/auto, so the mark is shared across devices), or by hand from the Overview's
 * Overnight button (GET /api/overnight = the last 24 hours). Closing it marks you as seen.
 * Comments and names are other people's words: always escaped, never markup (FR-009).
 * Focus trap / Escape / focus return come from a11y.js (it picks up any [role=dialog]).
 */
const Overnight = {
    _open: null,

    /* Never over sign-in, sign-up or setup (4.69.1: it opened over sign-up). Checked again at open time. */
    blocked() { return /^#\/(login|loading|setup|signup|dashboard-)/.test(location.hash); },

    async auto() {
        try {
            const r = await API.post('/api/overnight/auto', {});
            if (!(r && r.show)) return;
            // Never stack on another dialog (What's new, a tour): wait for it to close.
            const busy = () => [...document.querySelectorAll('.modal-overlay.open, [role=dialog], [role=alertdialog]')]
                .some(el => !el.closest('.ov-overlay') && el.getClientRects().length > 0)
                || !!document.querySelector('.pp-tour-blocker');   // 4.69.1: nor under a running tour
            for (let i = 0; busy() && i < 600; i++) await new Promise(res => setTimeout(res, 1000));
            if (!busy() && !this.blocked()) this.open(r);
        } catch (e) { /* the app works without it */ }
    },

    async manual() {
        try {
            this.open(await API.get('/api/overnight'));
        } catch (e) {
            if (window.Components && Components.toast) Components.toast('Couldn\'t load the overnight summary', 'error');
        }
    },

    _esc(s) { return Utils.escapeHtml(s == null ? '' : String(s)); },
    _n(v) { return Number(v || 0).toLocaleString(); },
    _site(d, code) { return (d.names && d.names[code]) || String(code || '').toUpperCase(); },
    _list(items) {
        if (items.length < 2) return items.join('');
        return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
    },

    _vs(n, u) {
        if (u == null) return '';
        if (!u) return n > 0 ? '<span class="ov-vs">new</span>' : '';
        if (n >= 3 * u) return `<span class="ov-vs">${Math.round(n / u)}× usual</span>`;
        const pct = Math.round((n - u) / u * 100);
        if (Math.abs(pct) < 10) return '<span class="ov-vs flat">about usual</span>';
        return pct > 0 ? `<span class="ov-vs">↑ ${pct}% vs usual</span>`
            : `<span class="ov-vs flat">↓ ${-pct}% vs usual</span>`;
    },

    _span(d) {
        const T = Utils.time, s = T.parse(d.since), e = T.parse(d.until);
        if (d.capped) return `Since ${T.format(s, { weekday: 'long' })} (7 days)`;
        const mins = Math.max(0, Math.round((e - s) / 60000));
        const f = v => T.format(v, { weekday: 'short', hour: 'numeric', minute: '2-digit' });
        return `${f(s)} to ${f(e)} · ${Math.floor(mins / 60)} h ${mins % 60} m`;
    },

    _headline(d) {
        const hour = parseInt(Utils.time.format(new Date(), { hour: 'numeric', hourCycle: 'h23' }), 10);
        const hi = hour >= 5 && hour < 12 ? 'Good morning.' : 'Welcome back.';
        const t = d.totals, u = d.usual;
        const quiet = u && ['views', 'faves', 'comments', 'followers'].every(k => (t[k] || 0) <= (u[k] || 0));
        if (quiet) return `${hi} A quiet night.`;
        const top = d.pieces && d.pieces[0];
        if (top) return `${hi} <span class="ov-hl">${this._esc(top.title)}</span> had a big night.`;
        return `${hi} Here's what happened.`;
    },

    _spark(series) {
        if (!series || series.length < 2 || !Math.max(...series)) return '';
        const max = Math.max(...series), w = 84, h = 36, n = series.length - 1;
        const pts = series.map((v, i) => [Math.round(i * w / n), Math.round(h - 2 - (v / max) * (h - 6))]);
        const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ');
        const last = pts[pts.length - 1];
        return `<svg class="ov-spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path class="area" d="${line} L${w} ${h} L0 ${h} Z"/>`
            + `<path class="line" d="${line}"/><circle cx="${last[0]}" cy="${last[1]}" r="2.5"/></svg>`;
    },

    _thumb(p) {
        if (p.thumb) {
            const src = window.Masterpieces && Masterpieces._thumbSrc ? Masterpieces._thumbSrc(p.thumb_platform, p.thumb) : p.thumb;
            return `<img class="ov-thumb" src="${this._esc(src)}" alt="" loading="lazy">`;
        }
        return `<span class="ov-thumb ov-thumb-blank" aria-hidden="true">${this._esc((p.title || '?').charAt(0).toUpperCase())}</span>`;
    },

    _piece(d, p) {
        const g = p.gain, parts = [];
        if (g.views) parts.push(`+${this._n(g.views)} views`);
        if (g.score) parts.push(`+${this._n(g.score)} score`);
        if (g.faves) parts.push(`+${this._n(g.faves)} faves`);
        if (g.comments) parts.push(`+${this._n(g.comments)} comments`);
        const chips = (p.sites || []).map(s => {
            const word = s.label && s.label.toLowerCase() !== 'views' ? ' ' + s.label.toLowerCase() : '';
            return `<span class="ov-chip">${this._esc(this._site(d, s.platform) + word)} <b>+${this._n(s.gain)}</b></span>`;
        }).join('');
        const ms = (p.milestones || []).map(m =>
            `<div class="ov-milestone">Passed ${this._n(m.value)} ${this._esc(m.metric)} on ${this._esc(this._site(d, m.platform))}</div>`).join('');
        const name = this._esc(p.title) + (p.chapter ? ` · chapter ${p.chapter}` : '');
        return `<a class="ov-piece" href="${this._esc(p.href)}" data-ov-go>
            ${this._thumb(p)}
            <span class="ov-piece-body"><span class="ov-name">${name}</span>
              <span class="ov-gain">${parts.join(' · ')}</span>
              <span class="ov-chips">${chips}</span>${ms}</span>
            ${this._spark(p.series)}</a>`;
    },

    _did(d) {
        const x = d.did, rows = [];
        for (const a of x.attention) {
            const site = this._esc(this._site(d, a.platform));
            if (a.kind === 'signin') {
                rows.push(`<li class="attn"><span class="ic bad" aria-hidden="true">!</span><span>${site} needs you to sign in again`
                    + `<small>${this._esc(a.reason)}</small></span><a class="ov-btn-s" href="${this._esc(a.href)}" data-ov-go>Reconnect</a></li>`);
            } else {
                rows.push(`<li class="attn"><span class="ic bad" aria-hidden="true">!</span><span>Couldn't post <b>${this._esc(a.title)}</b> to ${site}`
                    + ` at ${this._esc(Utils.time.fmt.time(a.at))}<small>${this._esc(a.reason || 'The site refused it.')}</small></span>`
                    + `<a class="ov-btn-s" href="${this._esc(a.href)}" data-ov-go>Open</a></li>`);
            }
        }
        for (const p of x.posted) {
            const sites = this._list(p.sites.map(s => this._esc(this._site(d, s))));
            rows.push(`<li><span class="ic ok" aria-hidden="true">✓</span><span>Posted <b>${this._esc(p.title)}</b> to ${sites}`
                + ` at ${this._esc(Utils.time.fmt.time(p.at))}${p.scheduled ? '<small>Scheduled</small>' : ''}</span><span></span></li>`);
        }
        if (x.checks) {
            rows.push(`<li><span class="ic quiet" aria-hidden="true">·</span><span>Checked ${x.check_sites} site${x.check_sites === 1 ? '' : 's'}`
                + ` ${this._n(x.checks)} time${x.checks === 1 ? '' : 's'}</span><span></span></li>`);
        }
        return rows.length ? `<section class="ov-sec" aria-labelledby="ov-h-did"><div class="ov-sec-h">
            <h3 id="ov-h-did">While you were away, PawPoller</h3><a href="#/ledger" data-ov-go>Ledger</a></div>
            <ul class="ov-did">${rows.join('')}</ul></section>` : '';
    },

    _best(b) {
        if (!b) return '';
        const days = ['Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays', 'Sundays'];
        const hr = h => { const x = ((h % 24) + 24) % 24; return (x % 12 || 12) + (x < 12 ? ' am' : ' pm'); };
        const when = b.h0 === b.h1 ? `around ${hr(b.h0)}` : `between ${hr(b.h0)} and ${hr(b.h1 + 1)}`;
        return `<p class="ov-tip"><b>Best time to post:</b> ${days[b.day] || ''} ${when}, from how your posts have done. <a href="#/analytics" data-ov-go>When to post</a></p>`;
    },

    open(d) {
        this.close(false);
        const t = d.totals, u = d.usual || null, cov = d.coverage || {};
        const scoreLine = t.score ? `<span class="ov-l">+${this._n(t.score)} score</span>` : '';
        const total = (n, label, key, extra = '') => `<div class="ov-total"><span class="ov-n">+${this._n(n)}</span>`
            + `<span class="ov-l">${label}</span>${extra}${this._vs(n, u && u[key])}</div>`;
        const cm = d.comments || { count: 0, items: [] };
        const comments = cm.items.length ? `<section class="ov-sec" aria-labelledby="ov-h-said"><div class="ov-sec-h">
            <h3 id="ov-h-said">What people said</h3><a href="#/inbox" data-ov-go>Inbox · ${this._n(cm.count)} new</a></div><div>`
            + cm.items.map(c => `<div class="ov-comment"><div class="ov-who"><b>${this._esc(c.author)}</b>`
                + `${c.title ? ' on ' + this._esc(c.title) : ''} · ${this._esc(this._site(d, c.platform))}</div>`
                + `<q>${this._esc(c.body)}</q><a class="ov-btn-s" href="#/inbox" data-ov-go>Reply</a></div>`).join('')
            + '</div></section>' : '';
        const named = (d.followers && d.followers.named) || [], counted = (d.followers && d.followers.counted) || {};
        const people = named.slice(0, 4).map(f => `<span class="ov-person">${this._esc(f.name)} <span>· ${this._esc(this._site(d, f.platform))}</span></span>`);
        if (named.length > 4) people.push(`<span class="ov-person">+${named.length - 4} more</span>`);
        for (const [code, n] of Object.entries(counted)) people.push(`<span class="ov-person">+${this._n(n)} <span>· ${this._esc(this._site(d, code))}</span></span>`);
        const followers = people.length ? `<section class="ov-sec" aria-labelledby="ov-h-new"><div class="ov-sec-h"><h3 id="ov-h-new">New followers</h3></div>
            <div class="ov-people">${people.join('')}</div></section>` : '';
        const pieces = (d.pieces || []).length ? `<section class="ov-sec" aria-labelledby="ov-h-moved"><div class="ov-sec-h">
            <h3 id="ov-h-moved">What moved</h3><a href="#/analytics" data-ov-go>All pieces</a></div>`
            + d.pieces.map(p => this._piece(d, p)).join('') + '</section>' : '';
        const coverage = cov.sites ? `From ${cov.covered} of ${cov.sites} sites` : 'From your sites';
        const sel = d.show_after || '6';
        const opt = (v, l) => `<option value="${v}"${v === sel ? ' selected' : ''}>${l}</option>`;

        const ov = document.createElement('div');
        ov.className = 'modal-overlay open ov-overlay';
        ov.innerHTML = `<section class="ov-sheet" role="dialog" aria-modal="true" aria-labelledby="ov-title">
          <div class="ov-grab" aria-hidden="true"></div>
          <div class="ov-scroll">
            <header class="ov-head"><div class="ov-span">${this._esc(this._span(d))}</div>
              <button class="ov-x" type="button" data-close aria-label="Close">&times;</button>
              <h2 id="ov-title">${this._headline(d)}</h2>
              <p>${coverage}${u ? ', compared with your usual for these hours this week' : ''}.</p></header>
            <div class="ov-totals">${total(t.views, 'views', 'views', scoreLine)}${total(t.faves, 'faves &amp; likes', 'faves')}
              ${total(t.comments, 'comments', 'comments')}${total(t.followers, 'followers', 'followers')}</div>
            ${pieces}${comments}${followers}${this._did(d)}${this._best(d.best_time)}
          </div>
          <footer class="ov-foot">
            <label>Show after <select id="ov-when">${opt('6', '6 hours away')}${opt('8', '8 hours away')}${opt('12', '12 hours away')}${opt('morning', 'Every morning')}${opt('never', 'Never')}</select></label>
            <div class="ov-btns"><a class="btn" href="#/analytics" data-ov-go>Open Analytics</a>
              <button class="btn btn-primary" type="button" data-close>Got it</button></div>
          </footer></section>`;
        document.body.appendChild(ov);
        this._open = ov;
        ov.addEventListener('click', e => {
            if (e.target === ov || e.target.closest('[data-close]')) { e.preventDefault(); this.close(true); }
            else if (e.target.closest('[data-ov-go]')) this.close(true, true);
        });
        ov.querySelector('#ov-when').addEventListener('change', e =>
            API.post('/api/overnight/seen', { show_after: e.target.value }).catch(() => {}));
        // Phone back gesture closes the sheet and leaves the page where it was.
        try { history.pushState({ overnight: 1 }, ''); } catch (e) { /* ignore */ }
        this._onPop = () => { if (this._open) this.close(true, true); };
        window.addEventListener('popstate', this._onPop);
        const btn = ov.querySelector('.ov-foot [data-close]');
        if (btn) btn.focus();
    },

    close(markSeen, navigating = false) {
        const ov = this._open;
        if (!ov) return;
        this._open = null;
        window.removeEventListener('popstate', this._onPop);
        if (!navigating && history.state && history.state.overnight) {
            try { history.back(); } catch (e) { /* ignore */ }
        }
        ov.remove();
        if (markSeen) API.post('/api/overnight/seen', {}).catch(() => {});
    },
};
window.Overnight = Overnight;
