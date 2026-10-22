/* Levels (spec 050, XPLEVELS) — the level bar in the sidebar footer, the "+N XP" chip on a
 * post's result toast, and the level-up moment.
 *
 *   Levels.refresh({toast, sites})  re-read /api/xp, redraw, chip the toast, celebrate a new level
 *   Levels.state()                  the last /api/xp body (Laurels' level card)
 *
 * Every number is the server's (xp.py sums one ledger). The browser keeps only
 * `pp-xp-seen-level`, the last level it celebrated: a visible tab that sees a higher level
 * writes it and plays the moment, so a second tab never repeats it (US1 #2). No seen-level
 * yet = first open after the update: one "You're level N" note, no fanfare (US2). */
(function () {
    'use strict';

    const SEEN = 'pp-xp-seen-level';
    const FUN = 'pp-gamification';        // last known on/off, so the first paint doesn't flash it
    let _s = null;

    /* Gamification on/off on <html data-gamification>: CSS hides Laurels and every level/medal
     * surface while it is off (operator, 22 Oct: a choice, not a default). */
    function mark(on) {
        const root = document.documentElement;
        if (root) root.dataset.gamification = on ? 'on' : 'off';
        try { localStorage.setItem(FUN, on ? '1' : '0'); } catch (e) { /* storage blocked */ }
        paintSwitch(on);
    }
    /* The account menu's switch (NOFUN): "On" / "Off" and its checked state. */
    function paintSwitch(on) {
        const item = document.querySelector && document.querySelector('[data-acct="gamification"]');
        if (!item) return;
        item.setAttribute('aria-checked', on ? 'true' : 'false');
        const hint = item.querySelector('#acct-fun-state');
        if (hint) hint.textContent = on ? 'On' : 'Off';
    }
    (function () {
        const root = document.documentElement;
        if (!root) return;
        let on = false;
        try { on = localStorage.getItem(FUN) === '1'; } catch (e) { /* storage blocked: off */ }
        root.dataset.gamification = on ? 'on' : 'off';
        paintSwitch(on);
    })();

    function read() { try { return parseInt(localStorage.getItem(SEEN), 10); } catch (e) { return NaN; } }
    function write(n) { try { localStorage.setItem(SEEN, String(n)); } catch (e) { /* storage blocked */ } }
    const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

    /* Rewards (spec 051) ride on the same body: `_s.rewards` (contracts/rewards-api.md). */
    const rw = () => (_s && _s.rewards) || null;
    const KIND_WORD = { theme: 'theme', ring: '', style: 'level-up style' };

    /* "Next: Ember theme at level 20 · 1,335 XP to go" — the account menu and Laurels (US1). */
    function nextLine() {
        const n = _s && _s.enabled && rw() && rw().next;
        if (!n) return '';
        const word = KIND_WORD[n.kind] ? ' ' + KIND_WORD[n.kind] : '';
        return `Next: ${n.name}${word} at level ${n.level} · ${n.xp_to_go.toLocaleString()} XP to go`;
    }

    function draw() {
        // The equipped ring goes on the avatar cluster only, so a preview on the shelf or a reveal
        // card shows its own ring, not the one being worn. Stays on with Levels off (FR-013).
        const bar = document.getElementById('bar-controls');
        if (bar && rw()) {
            const ring = _s.enabled && rw().equipped.ring;   // a reward: hidden with gamification off
            if (ring) bar.dataset.ring = ring; else delete bar.dataset.ring;
        }
        if (window.App && App._paintAvatars) App._paintAvatars();
        const n = document.getElementById('acct-next');
        if (n) { n.textContent = nextLine(); n.hidden = !n.textContent; }
        const b = document.getElementById('pp-level');
        const m = document.getElementById('acct-level');          // the account menu's line under your name
        if (m) m.hidden = !_s || !_s.enabled;
        if (!b) return;
        if (!_s || !_s.enabled) { b.hidden = true; return; }
        const have = _s.xp - _s.level_start, need = _s.next_level_at - _s.level_start;
        const label = `Level ${_s.level}, ${have} of ${need} XP`;
        if (m) m.textContent = `${rw() ? rw().title + ' · ' : ''}Level ${_s.level} · ${have.toLocaleString()} / ${need.toLocaleString()} XP`;
        b.hidden = false;
        b.setAttribute('role', 'img');
        b.setAttribute('aria-label', label);
        b.title = label;
        b.innerHTML = `<span class="pp-level-n" aria-hidden="true">Lv ${_s.level}</span>`
            + `<span class="pp-level-track" aria-hidden="true"><span class="pp-level-fill"></span></span>`;
        b.querySelector('.pp-level-fill').style.width = `${Math.max(0, Math.min(100, (have / need) * 100))}%`;
    }

    function chip(toast, gained, sites) {
        const row = toast && toast.querySelector('.act-chips');
        if (!row || !_s || !_s.enabled) return;
        const capped = (sites || []).filter(k => (_s.today.sites_capped || []).includes(k));
        const text = gained > 0 ? `+${gained} XP`
            : _s.today.xp >= _s.today.cap ? 'Daily XP reached'
            : capped.length ? 'Daily XP reached for this site' : '';
        if (text) row.insertAdjacentHTML('beforeend', `<span class="act-chip xp-chip">${esc(text)}</span>`);
    }

    function announce(text) {
        if (window.toast && toast.success) toast.success(text);   // the toast stack is the polite live region
    }

    const unrevealed = (catchup) => ((rw() && rw().unrevealed) || []).filter(r => (r.source === 'catchup') === catchup);

    function celebrate() {
        if (!_s || !_s.enabled || _held) return;        // held: the Overnight sheet is open (spec 052 US5)
        const seen = read();
        const visible = document.visibilityState === 'visible';
        if (unrevealed(true).length) {                   // long-time user, first build with rewards (US5)
            if (!visible) return;
            write(_s.level);
            return catchUp();
        }
        if (!Number.isFinite(seen)) {                    // first open with levels: one quiet note
            write(_s.level);
            if (_s.level > 1) announce(`You're level ${_s.level}. Keep posting to level up.`);
            return;
        }
        if (_s.level <= seen || !visible) {
            if (visible && unrevealed(false).length) revealAlone();   // a surprise, no level-up (US6)
            return;
        }
        write(_s.level);
        if (window.Sound) Sound.play('level_up');
        const b = document.getElementById('pp-level');
        if (b) { b.classList.remove('is-up'); void b.offsetWidth; b.classList.add('is-up'); }
        pop(seen, _s);
    }

    /* Mark rewards shown, for every device (POST /api/xp/rewards/reveal), and forget them here. */
    function reveal(items) {
        if (!items.length) return;
        const ids = items.map(r => r.id);
        if (rw()) rw().unrevealed = rw().unrevealed.filter(r => !ids.includes(r.id));
        API.post('/api/xp/rewards/reveal', { ids }).catch(() => {});
    }

    /* "Use it now": themes go through the theme preference, rings and styles through equip. */
    async function use(r) {
        if (r.kind === 'theme') {
            const t = ((window.App && App.THEMES) || []).find(x => x.rewardId === r.id);
            if (t) App.applyTheme(t.id);
            return;
        }
        try {
            const eq = await API.post('/api/xp/rewards/equip', { kind: r.kind, id: r.id });
            if (rw()) rw().equipped = eq;
            draw();
        } catch (e) { /* the shelf can still equip it */ }
    }

    function preview(r) {
        if (r.kind === 'theme') {
            const t = ((window.App && App.THEMES) || []).find(x => x.rewardId === r.id);
            const sw = t ? t.swatch : ['#222', '#333', '#999', '#ccc', '#fff'];
            return `<span class="lv-rv-theme" style="--sa:${sw[0]};--sb:${sw[1]};--sc:${sw[2]};--sd:${sw[3]}"></span>`;
        }
        if (r.kind === 'ring') {
            const inner = (document.getElementById('acct-av') || {}).innerHTML || 'P';   // the picture or the letter (US8)
            return `<span class="lv-rv-ring" data-ring="${esc(r.id)}"><span class="acct-av acct-av--lg">${inner}</span></span>`;
        }
        return `<span class="lv-rv-style" data-style="${esc(r.id)}">${'<i></i>'.repeat(8)}</span>`;
    }

    /* The reveal card: one item at a time ("1 of 2", research R8), a live preview, Use it now / Later.
     * Either button moves on; past the last one it calls done(). */
    function revealCard(items, done) {
        const card = document.createElement('div');
        card.className = 'lv-rv';
        let i = 0;
        const show = () => {
            const r = items[i];
            card.innerHTML = `
                <div class="lv-rv-head">${r.source === 'surprise' ? 'Surprise!' : 'Unlocked'}${items.length > 1 ? ` <span class="lv-rv-step">${i + 1} of ${items.length}</span>` : ''}</div>
                <div class="lv-rv-preview" aria-hidden="true">${preview(r)}</div>
                <div class="lv-rv-name">${esc(r.name)}${KIND_WORD[r.kind] ? ` <small>${KIND_WORD[r.kind]}</small>` : ''}</div>
                <div class="lv-rv-actions">
                    <button type="button" class="btn btn-primary" data-rv="use">Use it now</button>
                    <button type="button" class="btn btn-secondary" data-rv="later">${i + 1 < items.length ? 'Next' : 'Later'}</button>
                </div>`;
            card.querySelector('[data-rv="use"]').focus();
        };
        card.addEventListener('click', async e => {
            e.stopPropagation();                          // the backdrop click would close the whole moment
            const b = e.target.closest('[data-rv]');
            if (!b || i >= items.length) return;          // a second click on a card already finished
            const r = items[i++];
            if (b.dataset.rv === 'use') await use(r);
            if (i < items.length) show(); else done();
        });
        show();
        return card;
    }

    /* A full-screen layer that closes on a click, Esc or after `ms` (0 = stays). */
    function overlay(cls, html, onClose, ms) {
        document.querySelector('.lv-pop')?.remove();
        const el = document.createElement('div');
        el.className = cls;
        el.innerHTML = html;
        document.body.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        let timer = null, closed = false;
        const close = () => {
            if (closed) return;
            closed = true;
            clearTimeout(timer);
            document.removeEventListener('keydown', onKey);
            el.classList.remove('show');
            setTimeout(() => el.remove(), 320);
            if (onClose) onClose();
        };
        const onKey = e => { if (e.key === 'Escape') close(); };
        el.addEventListener('click', close);
        document.addEventListener('keydown', onKey);
        if (ms) timer = setTimeout(close, ms);
        return { el, close, hold: () => clearTimeout(timer) };
    }

    /* One card for everything a long-time user already earned (US5): no level-up pop. */
    function catchUp() {
        const items = unrevealed(true);
        if (!document.body || !document.createElement) return reveal(items);
        const names = items.map(r => `<li>${esc(r.name)}</li>`).join('');
        const o = overlay('lv-pop lv-quiet', `
            <div class="lv-pop-card lv-cu" role="alert">
                <div class="lv-cu-n">You've unlocked ${items.length} reward${items.length === 1 ? '' : 's'}</div>
                <div class="lv-pop-sub">Your title: <strong>${esc(rw().title)}</strong> · Level ${_s.level}</div>
                <ul class="lv-cu-list">${names}</ul>
                <button type="button" class="btn btn-primary" data-cu="see">See them</button>
            </div>`, () => reveal(items), 0);
        o.el.querySelector('[data-cu="see"]').addEventListener('click', () => {
            if (window.Laurels) Laurels._jump = 'rewards';     // the shelf (US4), scrolled to after render
            if (location.hash === '#/laurels') document.getElementById('rewards')?.scrollIntoView();
            else location.hash = '#/laurels';
        });
        o.el.querySelector('[data-cu="see"]').focus();
    }

    /* A reward that arrived without a level-up (a surprise, or a reveal another tab never showed). */
    function revealAlone() {
        if (document.querySelector('.lv-pop') || !document.body || !document.createElement) return;
        const items = unrevealed(false);
        if (window.Sound) Sound.play('level_up');
        const o = overlay('lv-pop lv-quiet', '<div class="lv-pop-card" role="alert"></div>', () => reveal(items), 0);
        o.el.querySelector('.lv-pop-card').appendChild(revealCard(items, o.close));
    }

    /* The full-screen level-up (levels.css): old number charges, new one slams in with a shockwave,
     * a radial burst and turning rays, then the bar refills toward the next level. Every fifth level
     * is gold. Click, Esc or 5.5 s closes it. role="alert" carries the announcement. */
    function pop(from, s) {
        if (!document.body || !document.createElement) return announce(`Level up! You're level ${s.level}.`);
        document.querySelector('.lv-pop')?.remove();
        const cols = ['var(--lv-hot)', 'var(--lv-gold)', '#ffffff', '#2f8f5b', '#4d9be6', '#e8577a'];
        const burst = Array.from({ length: 90 }, (_, i) => {
            const a = (i / 90) * Math.PI * 2 + Math.random() * 0.25, d = 160 + Math.random() * 360;
            return `<i style="--dx:${Math.round(Math.cos(a) * d)}px;--dy:${Math.round(Math.sin(a) * d)}px;`
                + `--r:${Math.round(Math.random() * 900 - 450)}deg;--d:${(1 + Math.random() * 0.9).toFixed(2)}s;--c:${cols[i % cols.length]}"></i>`;
        }).join('');
        const have = s.xp - s.level_start, need = s.next_level_at - s.level_start;
        const pct = Math.round(Math.max(0, Math.min(1, have / need)) * 100);
        const r = s.rewards;
        const newTitle = r && from < r.title_level && r.title_level <= s.level ? r.title : '';   // US2
        const items = r ? unrevealed(false) : [];
        const o = overlay('lv-pop' + (s.level % 5 === 0 ? ' is-milestone' : ''),
            `<div class="lv-pop-rays" aria-hidden="true"></div><div class="lv-pop-flash" aria-hidden="true"></div>
            <div class="lv-pop-ring" aria-hidden="true"></div><div class="lv-pop-ring r2" aria-hidden="true"></div>
            <div class="lv-pop-burst" aria-hidden="true">${burst}</div>
            <div class="lv-pop-card" role="alert">
                <span class="sr-only">Level up! You're level ${s.level}.</span>
                <div class="lv-pop-label" aria-hidden="true">${[...'LEVEL UP'].map((ch, i) => `<span style="--i:${i}">${ch === ' ' ? '&nbsp;' : ch}</span>`).join('')}</div>
                <div class="lv-pop-num" aria-hidden="true"><span class="lv-old">${from}</span><span class="lv-new">${s.level}</span></div>
                ${newTitle ? `<div class="lv-pop-title">New title: ${esc(newTitle)}</div>` : ''}
                <div class="lv-pop-track" aria-hidden="true"><span class="lv-pop-fill" style="--pct:${pct}%"></span></div>
                <div class="lv-pop-sub">${(need - have).toLocaleString()} XP to level ${s.level + 1}</div>
                <div class="lv-pop-hint" aria-hidden="true">tap to continue</div>
            </div>`, () => reveal(items), 5500);
        if (r && r.equipped.style) o.el.dataset.style = r.equipped.style;
        if (items.length) {                               // after the number lands, the reward flips in (US3)
            setTimeout(() => {
                if (!o.el.isConnected) return;
                o.hold();                                 // no auto-close while a choice is open
                o.el.querySelector('.lv-pop-hint')?.remove();
                o.el.querySelector('.lv-pop-card').appendChild(revealCard(items, o.close));
            }, 1600);
        }
    }

    async function refresh(opts = {}) {
        const before = _s ? _s.xp : null;
        try { _s = await API.get('/api/xp'); } catch (e) { return _s; }
        mark(_s.enabled);
        draw();
        if (opts.toast) chip(opts.toast, before === null ? 0 : _s.xp - before, opts.sites);
        celebrate();
        return _s;
    }

    /* ── Medals and work level-ups (spec 052, research R7) ─────────────
     * The server earns them, dated, during polls; GET /api/progress hands back what no device has
     * announced yet. One or two items are toasts with the medal sound; three or more become one
     * "While you were away" card. The first build with server medals shows one catch-up line.
     * Then POST /api/progress/announce, so no other device repeats them. */
    let _held = false, _checking = false;

    function itemText(it) {
        return it.kind === 'work' ? `${it.title} reached level ${it.to}! +${it.xp.toLocaleString()} XP`
            : `Medal: ${it.name}${it.work ? ` (${it.work})` : ''}`;
    }

    function awayCard(items) {
        if (!document.body || !document.createElement) return items.forEach(it => announce(itemText(it)));
        const o = overlay('lv-pop lv-quiet', `
            <div class="lv-pop-card lv-cu" role="alert">
                <div class="lv-cu-n">While you were away</div>
                <ul class="lv-cu-list">${items.map(it => `<li>${it.kind === 'work' ? '' : esc(it.icon || '🏅') + ' '}${esc(itemText(it))}</li>`).join('')}</ul>
                <button type="button" class="btn btn-primary" data-cu="see">See Laurels</button>
            </div>`, null, 0);
        o.el.querySelector('[data-cu="see"]').addEventListener('click', () => { location.hash = '#/laurels'; });
        o.el.querySelector('[data-cu="see"]').focus();
    }

    async function checkProgress() {
        if (_held || _checking || !_s || !_s.enabled || document.visibilityState !== 'visible' || !window.Progress) return;
        _checking = true;
        try {
            const a = (await Progress.load(true)).announce || {};
            const body = {
                medals: (a.medals || []).map(m => ({ id: m.id, subject: m.subject })),
                works: (a.works || []).map(w => ({ type: w.type, name: w.name, level: w.to })),
            };
            if (a.catchup_count || a.backpay_xp) {
                body.catchup = true;
                announce([a.catchup_count ? `Your medals now live with your account. All ${a.catchup_count} kept.` : '',
                          a.backpay_xp ? `Your works earned ${a.backpay_xp.toLocaleString()} XP from your audience.` : '']
                    .filter(Boolean).join(' '));
            }
            const items = [...(a.works || []).map(w => ({ kind: 'work', ...w })), ...(a.medals || []).map(m => ({ kind: 'medal', ...m }))];
            if (items.length) {
                if (window.Sound) Sound.play('medal');
                if (items.length >= 3) awayCard(items); else items.forEach(it => announce(itemText(it)));
            }
            if (body.catchup || items.length) await API.post('/api/progress/announce', body);
            if ((a.works || []).length || a.backpay_xp) await refresh();          // the bar moves
        } catch (e) { /* the next poll checks again */ }
        finally { _checking = false; }
    }

    /* The Overnight sheet holds the level-up and announcements while it is open (US5 #2). */
    function hold() { _held = true; }
    function release() { _held = false; celebrate(); checkProgress(); }

    /* After sign-in, and whenever a poll lands (the newest last_poll_at across sites moves). */
    function watch() {
        if (watch.on) return;
        watch.on = true;
        setTimeout(checkProgress, 4000);
        if (!(window.PlatformHealth && PlatformHealth.subscribe)) return;
        let last = null;
        PlatformHealth.subscribe(data => {
            const newest = Math.max(0, ...Object.values(data || {}).map(e => (e && e.last_poll_at) ? Utils.time.ms(e.last_poll_at) : 0));
            if (!newest) return;
            if (last !== null && newest > last) refresh().then(checkProgress);
            last = Math.max(last || 0, newest);
        });
    }

    /* Settings → Diagnostics → Replay level-up: the current level's moment, without touching
     * `pp-xp-seen-level`, so the real next level-up still plays. */
    async function replay() {
        const s = _s || await refresh();
        if (!s) return;
        if (window.Sound) Sound.play('level_up');
        pop(Math.max(1, s.level - 1), s);
    }

    window.addEventListener('storage', e => { if (e.key === SEEN) draw(); });
    window.Levels = { refresh, replay, nextLine, preview, state: () => _s, hold, release, watch, checkProgress };
})();
