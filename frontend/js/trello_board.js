/* Trello inside PawPoller (spec 006) — board picker, board, card view.
 *
 * Every read here is PawPoller's own copy of the board; every write updates that
 * copy and queues the change for Trello, returning at once (`pending: true`). So a
 * write never waits on Trello, and this file never talks to Trello itself.
 *
 * Routes (wired in app.js):
 *   #/boards                      board picker + sync strip
 *   #/boards/<id>                 the board
 *   #/boards/<id>/c/<card>        the card, as a modal over the board
 *   #/commissions[/c/<card>]      the commission board, same view (renderCommissions)
 *
 * Drag is SortableJS (vendored, MIT) — HTML5 drag-and-drop does not fire on
 * touch, and a phone is where the operator actually moves cards.
 *
 * CSP-safe: no inline handlers. The board and the modal each have ONE delegated
 * click/keydown listener keyed on data-tb-* attributes. Every piece of board text
 * (card and list names, descriptions, comments, label names, member names) goes
 * through esc() before it reaches innerHTML. */
window.TrelloBoard = {
    // Trello's colour names → the swatch we paint. Trello also has _dark / _light
    // variants ("green_dark"); those paint as their base colour.
    COLORS: {
        green: '#4bce97', yellow: '#f5cd47', orange: '#fea362', red: '#f87168',
        purple: '#9f8fef', blue: '#579dff', sky: '#6cc3e0', lime: '#94c748',
        pink: '#e774bb', black: '#8590a2',
    },
    LABEL_COLORS: ['green', 'yellow', 'orange', 'red', 'purple', 'blue', 'sky', 'lime', 'pink', 'black'],
    COVER_COLORS: ['pink', 'yellow', 'lime', 'blue', 'black', 'orange', 'red', 'purple', 'sky', 'green'],
    POLL_MS: 15000,

    _boardId: null,
    _base: null,          // hash the board lives at: '#/boards/<id>' or '#/commissions'
    _opts: {},
    _data: null,          // last GET /api/trello/boards/<id>
    _showArchived: false,
    _dragging: false,
    _sortables: [],
    _timer: null,
    _composeList: null,   // list id whose "Add a card" composer is open
    _composeNewList: false,
    _card: null,          // last GET /api/trello/cards/<id>
    _cardId: null,
    _pop: null,           // open card-view popover: labels|dates|checklist|cover|commission
    _labelEdit: null,     // null | 'new' | label id (inside the labels popover)
    _labelColor: null,
    _editing: null,       // what is open for editing in the card view: 'desc' | 'cl:<id>' | 'it:<id>' | 'add:<id>' | 'cm:<id>'
    _showHidden: false,

    // ── small helpers ────────────────────────────────────────────────────
    esc(s) { return Utils.escapeHtml(String(s == null ? '' : s)); },

    _color(name) {
        if (!name) return '#6b778c';
        return this.COLORS[String(name).split('_')[0]] || '#6b778c';
    },

    /* API errors arrive as "API 400: {"detail": "..."}" — show the detail. */
    _errText(e) {
        const m = String((e && e.message) || e || '');
        const i = m.indexOf('{');
        if (i >= 0) {
            try { const j = JSON.parse(m.slice(i)); if (j && j.detail) return String(j.detail); } catch (x) { /* not JSON */ }
        }
        return m.replace(/^API \d+:\s*/, '') || 'Something went wrong.';
    },
    _toastErr(e, what) {
        const msg = `${what}: ${this._errText(e)}`;
        if (window.toast) window.toast.error(msg); else console.error(msg);
    },
    /* Run one write; toast on failure. Returns the result, or null when it failed. */
    async _do(what, fn) {
        try { return await fn(); } catch (e) { this._toastErr(e, what); return null; }
    },

    _bgStyle(color, imageUrl) {
        const img = imageUrl ? Utils.cssUrl(imageUrl) : '';
        if (img) return `background-image:url('${img}');background-size:cover;background-position:center;`;
        if (color) return `background:${this._color(color)};`;
        return '';
    },

    /* Trello board backgrounds are named colours ("blue") or hex. */
    _boardBg(b) {
        const img = b.bg_image_url ? Utils.cssUrl(b.bg_image_url) : '';
        if (img) return `background-image:url('${img}');background-size:cover;background-position:center;`;
        const c = String(b.bg_color || '');
        if (/^#[0-9a-f]{3,8}$/i.test(c)) return `background:${c};`;
        const named = { blue: '#0079bf', orange: '#d29034', green: '#519839', red: '#b04632', purple: '#89609e',
            pink: '#cd5a91', lime: '#4bbf6b', sky: '#00aecc', grey: '#838c91' };
        return named[c] ? `background:${named[c]};` : '';
    },

    _dueState(due, done) {
        if (!due) return '';
        if (done) return 'done';
        const t = new Date(due).getTime();
        if (isNaN(t)) return '';
        if (t < Date.now()) return 'over';
        if (t - Date.now() < 24 * 3600 * 1000) return 'soon';
        return '';
    },
    _fmtDate(iso, withTime) {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        const o = { month: 'short', day: 'numeric' };
        if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
        let s = d.toLocaleDateString(undefined, o);
        if (withTime) s += ', ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
        return s;
    },
    /* ISO → the value an <input type=date> / type=time shows, in local time. */
    _localParts(iso) {
        const d = iso ? new Date(iso) : null;
        if (!d || isNaN(d.getTime())) return { date: '', time: '' };
        const p = n => String(n).padStart(2, '0');
        return { date: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`, time: `${p(d.getHours())}:${p(d.getMinutes())}` };
    },

    // ── status strip (picker) ────────────────────────────────────────────
    _statusHtml(st) {
        const parts = [];
        const mode = st.mode === 'webhook'
            ? '<span class="tb-mode tb-mode--live">&#9679; Live updates</span>'
            : `<span class="tb-mode">&#8635; Checks Trello every ${Number(st.poll_seconds || 0)} s</span>`;
        parts.push(mode);
        const imp = st.import || {};
        if (imp.running || (imp.total && imp.done < imp.total)) {
            const pct = imp.total ? Math.round(100 * (imp.done || 0) / imp.total) : 0;
            parts.push(`<span class="tb-import">Importing boards&hellip; ${Number(imp.done || 0)} of ${Number(imp.total || 0)}
                <span class="tb-progress"><span style="width:${pct}%"></span></span></span>`);
        }
        const ob = st.outbox || {};
        if (ob.pending) parts.push(`<span>${Number(ob.pending)} change(s) on the way to Trello</span>`);
        if (ob.held) parts.push(`<span class="tb-warn">${Number(ob.held)} change(s) waiting for Trello to be reachable</span>`);
        if (ob.failed) parts.push(`<span class="tb-bad">${Number(ob.failed)} change(s) Trello refused</span>`);
        if (st.conflicts) parts.push(`<span class="tb-warn">&#9888; ${Number(st.conflicts)} field(s) changed in both places &mdash; open the marked card to choose</span>`);
        if (st.is_owner === false) parts.push('<span class="muted">Your other PawPoller install talks to Trello; this one shows its copy.</span>');
        let html = `<div class="tb-status-line">${parts.join('<span class="tb-dot">&middot;</span>')}</div>`;
        if (st.last_error) html += `<div class="tb-status-err">${this.esc(st.last_error)}</div>`;
        const failed = st.failed || [];
        if (failed.length) {
            html += `<ul class="tb-failed">${failed.map(f => `
                <li><span class="tb-failed-op">${this.esc(f.op)}</span>
                    <span class="tb-failed-err">${this.esc(f.error)}</span>
                    <button class="btn btn-sm btn-outline" data-tb-dismiss="${this.esc(f.seq)}">Dismiss</button></li>`).join('')}</ul>`;
        }
        return html;
    },

    // ── board picker ─────────────────────────────────────────────────────
    async renderPicker() {
        this._stopPoll();
        this.closeCard(true);
        this._boardId = null;
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>Boards</h1>
                <p class="muted">Your Trello boards, kept in step both ways.</p>
            </div>
            <div class="tb-picker" id="tb-picker">
                <div class="tb-status" id="tb-status"></div>
                <div class="tb-picker-tools">
                    <label class="tb-check"><input type="checkbox" id="tb-show-hidden"${this._showHidden ? ' checked' : ''}> Show hidden boards</label>
                </div>
                <div class="tb-tiles" id="tb-tiles"><div class="loading-spinner">Loading&hellip;</div></div>
            </div>`;
        const root = document.getElementById('tb-picker');
        root.addEventListener('change', (e) => {
            if (e.target.id === 'tb-show-hidden') { this._showHidden = e.target.checked; this._loadPicker(); }
        });
        root.addEventListener('click', async (e) => {
            const hide = e.target.closest('[data-tb-hide]');
            if (hide) {
                e.preventDefault();
                const hidden = hide.dataset.hidden !== '1';
                if (await this._do(hidden ? 'Could not hide that board' : 'Could not show that board',
                    () => API.setTrelloBoardHidden(hide.dataset.tbHide, hidden))) this._loadPicker();
                return;
            }
            const dis = e.target.closest('[data-tb-dismiss]');
            if (dis) {
                dis.disabled = true;
                if (await this._do('Could not dismiss that', () => API.dismissTrelloOp(dis.dataset.tbDismiss))) this._loadPicker();
                else dis.disabled = false;
            }
        });
        await this._loadPicker();
        this._startPoll(() => document.getElementById('tb-picker') ? this._loadPicker() : false);
    },

    async _loadPicker() {
        const stHost = document.getElementById('tb-status');
        const tiles = document.getElementById('tb-tiles');
        if (!stHost || !tiles) return;
        let st = null;
        try { st = await API.getTrelloStatus(); } catch (e) { st = null; }
        if (!st || !st.connected) {
            stHost.innerHTML = '';
            tiles.className = '';
            tiles.innerHTML = `
                <div class="empty-state">
                    <h3>Trello is not connected yet</h3>
                    <p class="muted">Connect your Trello account once and every board you belong to appears here by itself.</p>
                    <a class="btn btn-primary" href="#/settings/trello">Open Settings &rarr; Trello</a>
                </div>`;
            return;
        }
        stHost.innerHTML = this._statusHtml(st);
        let boards = [];
        try {
            boards = (await API.getTrelloBoards(this._showHidden)).boards || [];
        } catch (e) {
            tiles.innerHTML = `<div class="card error">Could not load your boards: ${this.esc(this._errText(e))}</div>`;
            return;
        }
        boards = boards.filter(b => !b.closed);
        if (!boards.length) {
            tiles.className = '';
            tiles.innerHTML = `<div class="empty-state"><h3>No boards yet</h3>
                <p class="muted">${(st.import || {}).running ? 'Your boards are being imported &mdash; they appear here as each one arrives.'
                    : 'Boards you make in Trello appear here on their own.'}</p></div>`;
            return;
        }
        tiles.className = 'tb-tiles';
        tiles.innerHTML = boards.map(b => `
            <div class="tb-tile${b.hidden ? ' is-hidden' : ''}" style="${this.esc(this._boardBg(b))}">
                <a class="tb-tile-link" href="#/boards/${encodeURIComponent(b.id)}">${this.esc(b.name)}</a>
                <div class="tb-tile-tags">
                    ${b.is_inbox ? '<span class="tb-tag">Inbox</span>' : ''}
                    ${b.is_commission_board ? '<span class="tb-tag">Commissions</span>' : ''}
                    ${b.hidden ? '<span class="tb-tag">Hidden</span>' : ''}
                    ${b.imported === false ? '<span class="tb-tag">Importing&hellip;</span>' : ''}
                </div>
                <button class="tb-tile-hide" type="button" data-tb-hide="${this.esc(b.id)}" data-hidden="${b.hidden ? '1' : '0'}"
                        title="${b.hidden ? 'Show this board in the list again' : 'Hide this board from the list (nothing is deleted)'}">
                    ${b.hidden ? 'Unhide' : 'Hide'}</button>
            </div>`).join('');
    },

    // ── polling ──────────────────────────────────────────────────────────
    /* One timer at a time. `fn` returns false when its page is gone, which stops
       the timer — app.js's router does not know about this one. */
    _startPoll(fn) {
        this._stopPoll();
        this._timer = setInterval(() => {
            if (document.visibilityState !== 'visible') return;
            if (fn() === false) this._stopPoll();
        }, this.POLL_MS);
    },
    _stopPoll() {
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },

    // ── routing entry points ─────────────────────────────────────────────
    /* #/boards/<id>[/c/<card>]. Opening or closing a card over a board already
       on screen must not redraw the board — that would drop the scroll position. */
    async route(boardId, cardId, opts = {}) {
        const base = opts.base || `#/boards/${encodeURIComponent(boardId)}`;
        const onScreen = document.getElementById('tb-board');
        if (!(onScreen && this._boardId === boardId && this._base === base)) {
            await this.renderBoard(boardId, Object.assign({}, opts, { base }));
        }
        if (cardId) this.openCard(cardId);
        else if (this._cardId) this.closeCard(false);
    },

    /* #/commissions: the commission board, when one is set and imported.
       Resolves false when it is not, so the caller can fall back to the list. */
    async renderCommissions(cardId) {
        let cfg, boards;
        try {
            cfg = await API.getTrelloConfig();
            if (!cfg || !cfg.commission_board_id) return false;
            boards = (await API.getTrelloBoards(true)).boards || [];
        } catch (e) {
            return false;
        }
        const b = boards.find(x => x.id === cfg.commission_board_id && x.imported);
        if (!b) return false;
        if (!/^#\/commissions(\/c\/|$)/.test(window.location.hash.replace(/\?.*$/, ''))) return true;   // navigated away meanwhile
        await this.route(b.id, cardId, { base: '#/commissions', title: 'Commissions', commissions: true });
        return true;
    },

    // ── the board ────────────────────────────────────────────────────────
    async renderBoard(boardId, opts = {}) {
        this._stopPoll();
        this.closeCard(true);
        this._boardId = boardId;
        this._base = opts.base || `#/boards/${encodeURIComponent(boardId)}`;
        this._opts = opts;
        this._data = null;
        this._composeList = null;
        this._composeNewList = false;
        const back = opts.commissions
            ? '<a class="btn btn-sm btn-outline" href="#/commissions/list">&#9776; List view</a>'
            : '<a class="btn btn-sm" href="#/boards">&larr; Boards</a>';
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="tb-page">
                <div class="tb-head">
                    ${back}
                    <h1 class="tb-title" id="tb-title">${this.esc(opts.title || '')}</h1>
                    <span class="tb-head-sp"></span>
                    <label class="tb-check"><input type="checkbox" id="tb-show-archived"${this._showArchived ? ' checked' : ''}> Show archived</label>
                    <a class="btn btn-sm btn-outline" id="tb-open-trello" href="#" target="_blank" rel="noopener" hidden>Open in Trello</a>
                </div>
                <div class="tb-board" id="tb-board" data-board-id="${this.esc(boardId)}">
                    <div class="loading-spinner">Loading&hellip;</div>
                </div>
            </div>`;
        const board = document.getElementById('tb-board');
        board.addEventListener('click', (e) => this._onBoardClick(e));
        board.addEventListener('keydown', (e) => this._onBoardKey(e));
        document.getElementById('tb-show-archived').addEventListener('change', (e) => {
            this._showArchived = e.target.checked;
            this._loadBoard();
        });
        await this._loadBoard();
        this._startPoll(() => {
            const el = document.getElementById('tb-board');
            if (!el || el.dataset.boardId !== this._boardId) return false;
            if (!this._isBusy()) this._loadBoard();
            return true;
        });
    },

    /* A drag, an open composer or a rename in progress: a refresh would throw
       away what the operator is in the middle of. */
    _isBusy() {
        if (this._dragging) return true;
        const board = document.getElementById('tb-board');
        if (!board) return true;
        if (board.querySelector('.tb-inline-edit')) return true;
        const a = document.activeElement;
        return !!(a && board.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
    },

    async _loadBoard() {
        let board = document.getElementById('tb-board');
        if (!board) return;
        const boardId = this._boardId;
        let d;
        try {
            d = await API.getTrelloBoard(boardId, this._showArchived);
        } catch (e) {
            if (!this._data) board.innerHTML = `<div class="card error">Could not open this board: ${this.esc(this._errText(e))}</div>`;
            return;
        }
        // A drag may have started, or another board opened, while the request was in flight.
        board = document.getElementById('tb-board');
        if (this._dragging || !board || this._boardId !== boardId) return;
        this._data = d;
        const b = d.board || {};
        const title = document.getElementById('tb-title');
        if (title) title.textContent = this._opts.title || b.name || 'Board';
        const link = document.getElementById('tb-open-trello');
        const url = Utils.safeUrl(b.url || '');
        if (link && url) { link.href = url; link.hidden = false; }
        board.setAttribute('style', this._boardBg(b));
        this._drawBoard();
    },

    _drawBoard() {
        const board = document.getElementById('tb-board');
        if (!board || !this._data) return;
        const d = this._data;
        // Keep where the operator was scrolled to, across the redraw.
        const scrollLeft = board.scrollLeft;
        const listScroll = {};
        board.querySelectorAll('.tb-cards[data-list-id]').forEach(el => { listScroll[el.dataset.listId] = el.scrollTop; });

        const labels = {};
        (d.labels || []).forEach(l => { labels[l.id] = l; });
        const byList = {};
        (d.cards || []).forEach(c => { (byList[c.list_id] || (byList[c.list_id] = [])).push(c); });
        const lists = (d.lists || []).filter(l => this._showArchived || !l.closed);

        this._sortables.forEach(s => { try { s.destroy(); } catch (e) { /* already gone */ } });
        this._sortables = [];

        board.innerHTML = `
            <div class="tb-row">
                <div class="tb-lists" id="tb-lists">${lists.map(l => this._listHtml(l, byList[l.id] || [], labels)).join('')}</div>
                <div class="tb-add-list">${this._composeNewList
                    ? `<div class="tb-composer tb-inline-edit">
                           <input class="search-input tb-composer-input" data-tb-newlist-input maxlength="512" placeholder="Enter list name&hellip;">
                           <div class="tb-composer-actions">
                               <button class="btn btn-primary btn-sm" type="button" data-tb-newlist-save>Add list</button>
                               <button class="btn btn-sm btn-ghost" type="button" data-tb-newlist-cancel aria-label="Cancel">&#10005;</button>
                           </div></div>`
                    : '<button class="tb-add-list-btn" type="button" data-tb-newlist>+ Add another list</button>'}</div>
            </div>`;
        board.scrollLeft = scrollLeft;
        board.querySelectorAll('.tb-cards[data-list-id]').forEach(el => {
            if (listScroll[el.dataset.listId]) el.scrollTop = listScroll[el.dataset.listId];
        });
        this._wireSortables();
        const focus = board.querySelector('.tb-composer-input');
        if (focus) focus.focus();
    },

    _listHtml(l, cards, labels) {
        const shown = cards.filter(c => this._showArchived || !c.closed);
        const open = shown.filter(c => !c.closed).length;
        const composing = this._composeList === l.id;
        return `
            <section class="tb-list${l.closed ? ' is-archived' : ''}" data-list-id="${this.esc(l.id)}">
                <header class="tb-list-head">
                    <h2 class="tb-list-name" data-tb-list-rename="${this.esc(l.id)}" title="Click to rename">${this.esc(l.name)}</h2>
                    <span class="tb-count" title="Cards in this list">${open}</span>
                    <button class="tb-icon-btn" type="button" data-tb-list-menu="${this.esc(l.id)}" aria-label="List actions" title="List actions">&hellip;</button>
                </header>
                ${l.closed ? '<div class="tb-archived-note">Archived list</div>' : ''}
                <div class="tb-cards" data-list-id="${this.esc(l.id)}">${shown.map(c => this._cardHtml(c, labels)).join('')}</div>
                <div class="tb-list-foot">${composing
                    ? `<div class="tb-composer tb-inline-edit">
                           <textarea class="search-input tb-composer-input" data-tb-newcard-input="${this.esc(l.id)}" rows="3" maxlength="16384"
                                     placeholder="Enter a title for this card&hellip;"></textarea>
                           <div class="tb-composer-actions">
                               <button class="btn btn-primary btn-sm" type="button" data-tb-newcard-save="${this.esc(l.id)}">Add card</button>
                               <button class="btn btn-sm btn-ghost" type="button" data-tb-newcard-cancel aria-label="Cancel">&#10005;</button>
                           </div></div>`
                    : (l.closed ? '' : `<button class="tb-add-card" type="button" data-tb-newcard="${this.esc(l.id)}">+ Add a card</button>`)}</div>
            </section>`;
    },

    _cardHtml(c, labels) {
        const cover = c.cover || null;
        const coverUrl = c.cover_url ? Utils.safeUrl(c.cover_url) : '';
        const hasCover = !!(cover && (cover.color || coverUrl));
        const full = hasCover && cover.size === 'full';
        const marks = `${c.pending ? '<span class="tb-pending" title="Not in Trello yet — on its way"></span>' : ''}${c.conflict ? '<span class="tb-conflict" title="Changed in both places — open the card to choose">&#9888;</span>' : ''}`;
        const cls = `tb-card${c.closed ? ' is-archived' : ''}${full ? ' tb-card--full' : ''}${full && coverUrl ? ' has-image' : ''}`;
        const attrs = `data-card-id="${this.esc(c.id)}" tabindex="0" role="button"`;
        if (full) {
            return `<div class="${cls}" ${attrs} style="${this.esc(this._bgStyle(cover.color, coverUrl))}">
                ${marks ? `<div class="tb-card-marks">${marks}</div>` : ''}
                <div class="tb-card-title">${this.esc(c.name)}</div></div>`;
        }
        const band = hasCover
            ? (coverUrl ? `<img class="tb-cover-img" src="${this.esc(coverUrl)}" alt="" loading="lazy">`
                        : `<div class="tb-cover-band" style="background:${this._color(cover.color)}"></div>`)
            : '';
        const chips = (c.labels || []).map(id => labels[id]).filter(Boolean).map(l =>
            `<span class="tb-label" style="background:${this._color(l.color)}" title="${this.esc(l.name || l.color || '')}">${this.esc(l.name || '')}</span>`).join('');
        const dueState = this._dueState(c.due, c.due_complete);
        const badges = [];
        if (c.due) badges.push(`<span class="tb-badge tb-due${dueState ? ' tb-due--' + dueState : ''}" title="${c.due_complete ? 'Done' : 'Due'}">&#128339; ${this.esc(this._fmtDate(c.due))}</span>`);
        if (c.has_desc) badges.push('<span class="tb-badge" title="This card has a description">&#8801;</span>');
        if (c.comment_count) badges.push(`<span class="tb-badge" title="Comments">&#128172; ${Number(c.comment_count)}</span>`);
        const ck = c.checklist || {};
        if (ck.total) badges.push(`<span class="tb-badge${ck.done === ck.total ? ' tb-badge--done' : ''}" title="Checklist items">&#9745; ${Number(ck.done || 0)}/${Number(ck.total)}</span>`);
        if (c.is_commission) badges.push('<span class="tb-badge tb-badge--comm" title="Marked as a commission">&#128188; commission</span>');
        if (c.closed) badges.push('<span class="tb-badge">Archived</span>');
        return `<div class="${cls}" ${attrs}>
            ${band}
            <div class="tb-card-body">
                ${chips ? `<div class="tb-labels">${chips}</div>` : ''}
                <div class="tb-card-title">${this.esc(c.name)}${marks ? ` <span class="tb-card-marks">${marks}</span>` : ''}</div>
                ${badges.length ? `<div class="tb-badges">${badges.join('')}</div>` : ''}
            </div></div>`;
    },

    // ── drag ─────────────────────────────────────────────────────────────
    _wireSortables() {
        if (!window.Sortable) return;   // vendor file missing — the board still reads fine
        const common = {
            animation: 150,
            delay: 150, delayOnTouchOnly: true,   // a touch must hold briefly, so a swipe still scrolls
            ghostClass: 'tb-ghost', chosenClass: 'tb-chosen', dragClass: 'tb-dragging',
            scroll: true, bubbleScroll: true, scrollSensitivity: 80, scrollSpeed: 18,
            filter: '.tb-inline-edit, input, textarea, button', preventOnFilter: false,
            onStart: () => { this._dragging = true; },
        };
        const lists = document.getElementById('tb-lists');
        if (lists) {
            this._sortables.push(Sortable.create(lists, Object.assign({}, common, {
                draggable: '.tb-list', handle: '.tb-list-head', direction: 'horizontal',
                onEnd: (evt) => this._onListDrop(evt),
            })));
        }
        document.querySelectorAll('#tb-board .tb-cards').forEach(el => {
            this._sortables.push(Sortable.create(el, Object.assign({}, common, {
                group: 'cards', draggable: '.tb-card',
                onEnd: (evt) => this._onCardDrop(evt),
            })));
        });
    },

    /* The id of the nearest sibling matching `cls` in direction `dir` — the
       item now above/left of the dropped one (dir = previous) or below/right. */
    _sib(el, cls, dir, key) {
        let s = el[dir];
        while (s && !s.classList.contains(cls)) s = s[dir];
        return s ? s.dataset[key] : null;
    },

    async _onCardDrop(evt) {
        this._dragging = false;
        if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
        const el = evt.item;
        const before = this._sib(el, 'tb-card', 'previousElementSibling', 'cardId');
        const after = this._sib(el, 'tb-card', 'nextElementSibling', 'cardId');
        await this._do('Could not move that card',
            () => API.moveTrelloCard(el.dataset.cardId, evt.to.dataset.listId, before, after));
        this._loadBoard();
    },

    async _onListDrop(evt) {
        this._dragging = false;
        if (evt.oldIndex === evt.newIndex) return;
        const el = evt.item;
        const before = this._sib(el, 'tb-list', 'previousElementSibling', 'listId');
        const after = this._sib(el, 'tb-list', 'nextElementSibling', 'listId');
        await this._do('Could not move that list', () => API.moveTrelloList(el.dataset.listId, before, after));
        this._loadBoard();
    },

    // ── board clicks / keys ──────────────────────────────────────────────
    _closeMenus() {
        document.querySelectorAll('#tb-board .tb-menu').forEach(m => m.remove());
    },

    async _onBoardClick(e) {
        const t = e.target;
        if (!t.closest('.tb-menu') && !t.closest('[data-tb-list-menu]')) this._closeMenus();

        const menuBtn = t.closest('[data-tb-list-menu]');
        if (menuBtn) {
            const had = menuBtn.parentElement.querySelector('.tb-menu');
            this._closeMenus();
            if (had) return;
            const list = (this._data.lists || []).find(l => l.id === menuBtn.dataset.tbListMenu);
            if (!list) return;
            const m = document.createElement('div');
            m.className = 'tb-menu';
            m.innerHTML = `
                <button type="button" data-tb-list-rename="${this.esc(list.id)}">Rename list</button>
                <button type="button" data-tb-list-archive="${this.esc(list.id)}" data-closed="${list.closed ? '0' : '1'}">
                    ${list.closed ? 'Restore list' : 'Archive this list'}</button>`;
            menuBtn.parentElement.appendChild(m);
            return;
        }
        const arch = t.closest('[data-tb-list-archive]');
        if (arch) {
            this._closeMenus();
            const closed = arch.dataset.closed === '1';
            if (await this._do(closed ? 'Could not archive that list' : 'Could not restore that list',
                () => API.updateTrelloList(arch.dataset.tbListArchive, { closed }))) this._loadBoard();
            return;
        }
        const ren = t.closest('[data-tb-list-rename]');
        if (ren) {
            this._closeMenus();
            this._startListRename(ren.dataset.tbListRename);
            return;
        }
        const nc = t.closest('[data-tb-newcard]');
        if (nc) { this._composeList = nc.dataset.tbNewcard; this._composeNewList = false; this._drawBoard(); return; }
        if (t.closest('[data-tb-newcard-cancel]')) { this._composeList = null; this._drawBoard(); return; }
        const ncs = t.closest('[data-tb-newcard-save]');
        if (ncs) { this._saveNewCard(ncs.dataset.tbNewcardSave); return; }
        if (t.closest('[data-tb-newlist]')) { this._composeNewList = true; this._composeList = null; this._drawBoard(); return; }
        if (t.closest('[data-tb-newlist-cancel]')) { this._composeNewList = false; this._drawBoard(); return; }
        if (t.closest('[data-tb-newlist-save]')) { this._saveNewList(); return; }

        const card = t.closest('.tb-card[data-card-id]');
        if (card && !t.closest('.tb-inline-edit')) this._openCardRoute(card.dataset.cardId);
    },

    _onBoardKey(e) {
        const t = e.target;
        if (t.matches('[data-tb-newcard-input]')) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._saveNewCard(t.dataset.tbNewcardInput); }
            else if (e.key === 'Escape') { e.stopPropagation(); this._composeList = null; this._drawBoard(); }
            return;
        }
        if (t.matches('[data-tb-newlist-input]')) {
            if (e.key === 'Enter') { e.preventDefault(); this._saveNewList(); }
            else if (e.key === 'Escape') { e.stopPropagation(); this._composeNewList = false; this._drawBoard(); }
            return;
        }
        if (t.matches('.tb-card[data-card-id]') && e.key === 'Enter') {
            e.preventDefault();
            this._openCardRoute(t.dataset.cardId);
        }
    },

    _openCardRoute(cardId) {
        window.location.hash = `${this._base}/c/${encodeURIComponent(cardId)}`;
    },

    async _saveNewCard(listId) {
        const input = document.querySelector(`#tb-board [data-tb-newcard-input="${CSS.escape(listId)}"]`);
        const name = input ? input.value.trim() : '';
        if (!name) { if (input) input.focus(); return; }
        input.disabled = true;
        const r = await this._do('Could not add that card', () => API.createTrelloCard(listId, name));
        if (!r) { input.disabled = false; return; }
        // Like Trello: the composer stays open under the new card for the next one.
        this._composeList = listId;
        await this._loadBoard();
    },

    async _saveNewList() {
        const input = document.querySelector('#tb-board [data-tb-newlist-input]');
        const name = input ? input.value.trim() : '';
        if (!name) { if (input) input.focus(); return; }
        input.disabled = true;
        const r = await this._do('Could not add that list', () => API.createTrelloList(this._boardId, name));
        if (!r) { input.disabled = false; return; }
        this._composeNewList = true;
        await this._loadBoard();
    },

    _startListRename(listId) {
        const h = document.querySelector(`#tb-board .tb-list-name[data-tb-list-rename="${CSS.escape(listId)}"]`);
        const list = (this._data.lists || []).find(l => l.id === listId);
        if (!h || !list) return;
        const input = document.createElement('input');
        input.className = 'search-input tb-list-name-input tb-inline-edit';
        input.maxLength = 512;
        input.value = list.name || '';
        h.replaceWith(input);
        input.focus();
        input.select();
        let done = false;
        const finish = async (save) => {
            if (done) return;
            done = true;
            const name = input.value.trim();
            if (save && name && name !== list.name) {
                await this._do('Could not rename that list', () => API.updateTrelloList(listId, { name }));
            }
            input.classList.remove('tb-inline-edit');
            this._loadBoard();
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            else if (e.key === 'Escape') { e.stopPropagation(); finish(false); }
        });
        input.addEventListener('blur', () => finish(true));
    },

    // ── the card view ────────────────────────────────────────────────────
    async openCard(cardId) {
        const fresh = this._cardId !== cardId;
        this._cardId = cardId;
        if (fresh) { this._pop = null; this._labelEdit = null; this._card = null; this._editing = null; }
        let root = document.getElementById('tb-modal');
        if (!root) {
            root = document.createElement('div');
            root.id = 'tb-modal';
            root.className = 'tb-modal-overlay';
            root.innerHTML = '<div class="tb-modal" role="dialog" aria-modal="true" aria-label="Card"><div class="loading-spinner">Loading&hellip;</div></div>';
            document.body.appendChild(root);
            document.body.classList.add('tb-modal-open');
            root.addEventListener('click', (e) => this._onModalClick(e));
            root.addEventListener('change', (e) => this._onModalChange(e));
            root.addEventListener('keydown', (e) => this._onModalKey(e));
            this._escHandler = (e) => {
                if (e.key !== 'Escape' || !document.getElementById('tb-modal')) return;
                const a = document.activeElement;
                if (a && root.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;   // the field's own Esc cancels it
                if (this._pop) { this._pop = null; this._labelEdit = null; this._drawCard(); return; }
                this._closeRoute();
            };
            document.addEventListener('keydown', this._escHandler);
        }
        await this._loadCard();
    },

    closeCard(silent) {
        const root = document.getElementById('tb-modal');
        if (root) root.remove();
        document.body.classList.remove('tb-modal-open');
        if (this._escHandler) { document.removeEventListener('keydown', this._escHandler); this._escHandler = null; }
        const wasOpen = !!this._cardId;
        this._cardId = null;
        this._card = null;
        this._pop = null;
        // Anything edited in the card shows on its face once it is closed. Silent =
        // the page is being replaced, so there is no board to refresh.
        if (wasOpen && !silent && document.getElementById('tb-board')) this._loadBoard();
    },

    _closeRoute() {
        if (this._base && window.location.hash.indexOf(this._base + '/c/') === 0) window.location.hash = this._base;
        else this.closeCard();
    },

    async _loadCard() {
        const id = this._cardId;
        let d;
        try {
            d = await API.getTrelloCard(id);
        } catch (e) {
            const m = document.querySelector('#tb-modal .tb-modal');
            if (m && !this._card) m.innerHTML = `<button class="tb-modal-x" type="button" data-tb-close aria-label="Close">&#10005;</button>
                <div class="card error">Could not open this card: ${this.esc(this._errText(e))}</div>`;
            return;
        }
        if (this._cardId !== id) return;   // another card was opened meanwhile
        this._card = d;
        this._drawCard();
    },

    /* After a write: re-read the card, keep the open popover open. */
    async _afterWrite(r) {
        if (r) await this._loadCard();
    },

    _drawCard() {
        const m = document.querySelector('#tb-modal .tb-modal');
        if (!m || !this._card) return;
        const d = this._card;
        const c = d.card || {};
        const labels = {};
        (d.labels || []).forEach(l => { labels[l.id] = l; });
        const cover = c.cover || null;
        const coverUrl = c.cover_url ? Utils.safeUrl(c.cover_url) : '';
        const coverHtml = cover && coverUrl
            ? `<div class="tb-modal-cover tb-modal-cover--img"><img src="${this.esc(coverUrl)}" alt=""></div>`
            : (cover && cover.color ? `<div class="tb-modal-cover" style="background:${this._color(cover.color)}"></div>` : '');
        const listOpts = (d.lists || []).map(l =>
            `<option value="${this.esc(l.id)}"${l.id === c.list_id ? ' selected' : ''}>${this.esc(l.name)}</option>`).join('');
        const chips = (c.labels || []).map(id => labels[id]).filter(Boolean).map(l =>
            `<span class="tb-label tb-label--lg" style="background:${this._color(l.color)}">${this.esc(l.name || '')}</span>`).join('');
        const dueState = this._dueState(c.due, c.due_complete);
        const url = Utils.safeUrl(c.url || '');

        m.innerHTML = `
            ${coverHtml}
            <button class="tb-modal-x" type="button" data-tb-close aria-label="Close">&#10005;</button>
            ${this._conflictsHtml(d.conflicts || [])}
            ${c.closed ? '<div class="tb-archived-banner">&#128230; This card is archived.</div>' : ''}
            <div class="tb-modal-head">
                <h2 class="tb-card-name" data-tb-edit="name" title="Click to rename">${this.esc(c.name)}</h2>
                <div class="tb-inlist">in list
                    <select class="search-input tb-inlist-select" id="tb-card-list" aria-label="Move to list">${listOpts}</select>
                    ${c.pending ? '<span class="tb-pending-text"><span class="tb-pending"></span> sending to Trello&hellip;</span>' : ''}
                </div>
            </div>
            <div class="tb-modal-grid">
                <div class="tb-main">
                    ${chips || c.due || c.start ? `<div class="tb-meta-row">
                        ${chips ? `<div class="tb-meta"><div class="tb-meta-h">Labels</div><div class="tb-labels">${chips}
                            <button class="tb-label-add" type="button" data-tb-pop="labels" aria-label="Edit labels">+</button></div></div>` : ''}
                        ${c.start ? `<div class="tb-meta"><div class="tb-meta-h">Start date</div>
                            <button class="tb-date-chip" type="button" data-tb-pop="dates">${this.esc(this._fmtDate(c.start))}</button></div>` : ''}
                        ${c.due ? `<div class="tb-meta"><div class="tb-meta-h">Due date</div>
                            <span class="tb-due-row"><input type="checkbox" id="tb-due-done"${c.due_complete ? ' checked' : ''} aria-label="Mark done">
                            <button class="tb-date-chip${dueState ? ' tb-due--' + dueState : ''}" type="button" data-tb-pop="dates">
                                ${this.esc(this._fmtDate(c.due, true))}${c.due_complete ? ' <b>Done</b>' : dueState === 'over' ? ' <b>Overdue</b>' : ''}</button></span></div>` : ''}
                    </div>` : ''}

                    <section class="tb-sec">
                        <h3 class="tb-sec-h">&#8801; Description</h3>
                        ${this._editing === 'desc'
                            ? `<div class="tb-inline-edit"><textarea class="search-input tb-desc-input" id="tb-desc-input" rows="8" maxlength="16384">${this.esc(c.desc || '')}</textarea>
                               <div class="tb-composer-actions"><button class="btn btn-primary btn-sm" type="button" data-tb-desc-save>Save</button>
                               <button class="btn btn-sm" type="button" data-tb-desc-cancel>Cancel</button></div></div>`
                            : `<div class="tb-desc${c.desc ? '' : ' is-empty'}" data-tb-edit="desc" role="button" tabindex="0">${c.desc ? this.esc(c.desc) : 'Add a more detailed description&hellip;'}</div>`}
                    </section>

                    ${(d.checklists || []).map(cl => this._checklistHtml(cl)).join('')}

                    ${this._commissionHtml(d)}

                    <section class="tb-sec">
                        <h3 class="tb-sec-h">&#128172; Comments and activity</h3>
                        <div class="tb-comment-new">
                            <textarea class="search-input" id="tb-comment-input" rows="2" maxlength="16384" placeholder="Write a comment&hellip;"></textarea>
                            <button class="btn btn-primary btn-sm" type="button" data-tb-comment-save>Save</button>
                        </div>
                        ${this._commentsHtml(d.comments || [])}
                    </section>
                </div>
                <aside class="tb-side">
                    <div class="tb-side-h">Add to card</div>
                    <button class="tb-side-btn${this._pop === 'labels' ? ' is-on' : ''}" type="button" data-tb-pop="labels">&#127991; Labels</button>
                    <button class="tb-side-btn${this._pop === 'dates' ? ' is-on' : ''}" type="button" data-tb-pop="dates">&#128339; Dates</button>
                    <button class="tb-side-btn${this._pop === 'checklist' ? ' is-on' : ''}" type="button" data-tb-pop="checklist">&#9745; Checklist</button>
                    <button class="tb-side-btn${this._pop === 'cover' ? ' is-on' : ''}" type="button" data-tb-pop="cover">&#9632; Cover</button>
                    <div id="tb-pop">${this._popHtml()}</div>
                    <div class="tb-side-h">Actions</div>
                    ${c.closed
                        ? '<button class="tb-side-btn" type="button" data-tb-archive="0">&#8634; Restore</button>'
                        : '<button class="tb-side-btn" type="button" data-tb-archive="1">&#128230; Archive</button>'}
                    ${url ? `<a class="tb-side-btn" href="${this.esc(url)}" target="_blank" rel="noopener">&#8599; Open in Trello</a>` : ''}
                </aside>
            </div>`;
        this._wireChecklistSort();
        const focus = m.querySelector('[data-tb-autofocus]');
        if (focus) { focus.focus(); if (focus.select) focus.select(); }
    },

    _valText(v) {
        if (v == null || v === '') return '(empty)';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    },

    _conflictsHtml(list) {
        if (!list.length) return '';
        return `<div class="tb-conflicts">
            <div class="tb-conflicts-h">&#9888; Changed in both places &mdash; pick which one to keep</div>
            ${list.map((x, i) => `
                <div class="tb-conflict-row">
                    <div class="tb-conflict-field">${this.esc(x.object_type)} &middot; ${this.esc(x.field)}</div>
                    <div class="tb-conflict-vals">
                        <div><span class="tb-conflict-side">PawPoller</span><span class="tb-conflict-val">${this.esc(this._valText(x.local))}</span></div>
                        <div><span class="tb-conflict-side">Trello</span><span class="tb-conflict-val">${this.esc(this._valText(x.remote))}</span></div>
                    </div>
                    <div class="tb-conflict-btns">
                        <button class="btn btn-sm" type="button" data-tb-keep="pawpoller" data-i="${i}">Keep PawPoller's</button>
                        <button class="btn btn-sm" type="button" data-tb-keep="trello" data-i="${i}">Keep Trello's</button>
                    </div>
                </div>`).join('')}
        </div>`;
    },

    _checklistHtml(cl) {
        const items = (cl.items || []).slice().sort((a, b) => (a.pos || 0) - (b.pos || 0));
        const done = items.filter(i => i.state === 'complete').length;
        const pct = items.length ? Math.round(100 * done / items.length) : 0;
        const editingName = this._editing === 'cl:' + cl.id;
        return `
            <section class="tb-sec tb-checklist" data-checklist-id="${this.esc(cl.id)}">
                <div class="tb-cl-head">
                    ${editingName
                        ? `<input class="search-input tb-inline-edit" data-tb-cl-name-input="${this.esc(cl.id)}" data-tb-autofocus maxlength="512" value="${this.esc(cl.name)}">`
                        : `<h3 class="tb-sec-h" data-tb-cl-rename="${this.esc(cl.id)}" title="Click to rename">&#9745; ${this.esc(cl.name)}</h3>`}
                    <button class="btn btn-sm btn-outline" type="button" data-tb-cl-delete="${this.esc(cl.id)}">Delete</button>
                </div>
                <div class="tb-cl-progress"><span class="tb-cl-pct">${pct}%</span>
                    <span class="tb-progress${pct === 100 ? ' is-done' : ''}"><span style="width:${pct}%"></span></span></div>
                <div class="tb-items" data-checklist-id="${this.esc(cl.id)}">
                    ${items.map(it => `
                        <div class="tb-item${it.state === 'complete' ? ' is-done' : ''}" data-item-id="${this.esc(it.id)}">
                            <input type="checkbox" data-tb-item-check="${this.esc(it.id)}"${it.state === 'complete' ? ' checked' : ''} aria-label="Done">
                            ${this._editing === 'it:' + it.id
                                ? `<input class="search-input tb-inline-edit tb-item-input" data-tb-item-name-input="${this.esc(it.id)}" data-tb-autofocus maxlength="16384" value="${this.esc(it.name)}">`
                                : `<span class="tb-item-name" data-tb-item-rename="${this.esc(it.id)}" title="Click to rename">${this.esc(it.name)}</span>`}
                            <button class="tb-icon-btn" type="button" data-tb-item-delete="${this.esc(it.id)}" aria-label="Delete item" title="Delete item">&#10005;</button>
                        </div>`).join('')}
                </div>
                ${this._editing === 'add:' + cl.id
                    ? `<div class="tb-inline-edit tb-item-add"><input class="search-input" data-tb-item-add-input="${this.esc(cl.id)}" data-tb-autofocus maxlength="16384" placeholder="Add an item">
                       <div class="tb-composer-actions"><button class="btn btn-primary btn-sm" type="button" data-tb-item-add-save="${this.esc(cl.id)}">Add</button>
                       <button class="btn btn-sm" type="button" data-tb-edit-cancel>Cancel</button></div></div>`
                    : `<button class="btn btn-sm" type="button" data-tb-item-add="${this.esc(cl.id)}">Add an item</button>`}
            </section>`;
    },

    _commissionHtml(d) {
        const cm = d.commission;
        if (cm) {
            const price = Number(cm.price || 0);
            return `<section class="tb-sec tb-comm">
                <h3 class="tb-sec-h">&#128188; Commission</h3>
                <dl class="tb-comm-fields">
                    <div><dt>Client</dt><dd>${this.esc(cm.client_name || '—')}</dd></div>
                    <div><dt>Price</dt><dd>${price ? `${this.esc(price % 1 === 0 ? price : price.toFixed(2))} ${this.esc(cm.currency || '')}` : '—'}</dd></div>
                    <div><dt>Status</dt><dd>${this.esc(cm.status || '—')}</dd></div>
                </dl>
                <div class="tb-comm-actions">
                    <a class="btn btn-sm" href="#/commissions/${encodeURIComponent(cm.id)}">Open commission</a>
                    <button class="btn btn-sm btn-outline" type="button" data-tb-comm-unmark>Unmark</button>
                </div>
                <p class="muted tb-small">Client, price and notes stay in PawPoller &mdash; they are not written to the card in Trello.</p>
            </section>`;
        }
        if (this._pop === 'commission') {
            const c = d.card || {};
            return `<section class="tb-sec tb-comm tb-inline-edit">
                <h3 class="tb-sec-h">&#128188; Mark as commission</h3>
                <label class="tb-field">Client name<input class="search-input" id="tb-comm-client" maxlength="200" value="${this.esc(c.name || '')}" data-tb-autofocus></label>
                <div class="tb-field-row">
                    <label class="tb-field">Price<input class="search-input" id="tb-comm-price" type="number" min="0" step="0.01" inputmode="decimal"></label>
                    <label class="tb-field">Currency<input class="search-input" id="tb-comm-currency" maxlength="8" value="USD"></label>
                </div>
                <div class="tb-composer-actions">
                    <button class="btn btn-primary btn-sm" type="button" data-tb-comm-save>Mark as commission</button>
                    <button class="btn btn-sm" type="button" data-tb-pop-close>Cancel</button>
                </div>
            </section>`;
        }
        return `<section class="tb-sec"><button class="btn btn-sm btn-outline" type="button" data-tb-pop="commission">&#128188; Mark as commission</button></section>`;
    },

    _commentsHtml(comments) {
        const list = comments.slice().sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
        if (!list.length) return '';
        return `<ul class="tb-comments">${list.map(cm => `
            <li class="tb-comment" data-comment-id="${this.esc(cm.id)}">
                <div class="tb-comment-meta"><strong>${this.esc(cm.author_name || 'Someone')}</strong>
                    <span class="muted" title="${this.esc(cm.date ? new Date(cm.date).toLocaleString() : '')}">${this.esc(cm.date ? Utils.timeAgo(cm.date) : '')}</span>
                    ${cm.pending ? '<span class="tb-pending" title="Not in Trello yet — on its way"></span>' : ''}</div>
                ${this._editing === 'cm:' + cm.id
                    ? `<div class="tb-inline-edit"><textarea class="search-input" data-tb-comment-edit-input="${this.esc(cm.id)}" data-tb-autofocus rows="3" maxlength="16384">${this.esc(cm.text)}</textarea>
                       <div class="tb-composer-actions"><button class="btn btn-primary btn-sm" type="button" data-tb-comment-edit-save="${this.esc(cm.id)}">Save</button>
                       <button class="btn btn-sm" type="button" data-tb-edit-cancel>Cancel</button></div></div>`
                    : `<div class="tb-comment-text">${this.esc(cm.text)}</div>`}
                ${cm.is_mine && this._editing !== 'cm:' + cm.id ? `<div class="tb-comment-actions">
                    <button type="button" class="tb-link" data-tb-comment-edit="${this.esc(cm.id)}">Edit</button> &middot;
                    <button type="button" class="tb-link" data-tb-comment-delete="${this.esc(cm.id)}">Delete</button></div>` : ''}
            </li>`).join('')}</ul>`;
    },

    _swatches(colors, current, attr) {
        return `<div class="tb-swatches">${colors.map(col => `
            <button type="button" class="tb-swatch${col === current ? ' is-on' : ''}" style="background:${this._color(col)}"
                    ${attr}="${col}" title="${col}" aria-label="${col}"></button>`).join('')}</div>`;
    },

    _popHtml() {
        const d = this._card;
        if (!d || !this._pop || this._pop === 'commission') return '';
        const c = d.card || {};
        const close = '<button class="tb-icon-btn tb-pop-x" type="button" data-tb-pop-close aria-label="Close">&#10005;</button>';
        if (this._pop === 'labels') {
            if (this._labelEdit) {
                const l = this._labelEdit === 'new' ? null : (d.labels || []).find(x => x.id === this._labelEdit);
                const col = this._labelColor || (l && l.color) || 'green';
                return `<div class="tb-pop tb-inline-edit">${close}
                    <div class="tb-pop-h">${l ? 'Edit label' : 'Create a new label'}</div>
                    <div class="tb-label tb-label--lg tb-label-preview" style="background:${this._color(col)}">${this.esc(l ? l.name : '')}</div>
                    <label class="tb-field">Title<input class="search-input" id="tb-label-name" maxlength="100" value="${this.esc(l ? l.name : '')}" data-tb-autofocus></label>
                    <div class="tb-field">Colour${this._swatches(this.LABEL_COLORS, col, 'data-tb-label-color')}</div>
                    <div class="tb-composer-actions">
                        <button class="btn btn-primary btn-sm" type="button" data-tb-label-save>${l ? 'Save' : 'Create'}</button>
                        <button class="btn btn-sm" type="button" data-tb-label-back>Back</button>
                        ${l ? '<button class="btn btn-sm btn-danger" type="button" data-tb-label-delete>Delete</button>' : ''}
                    </div></div>`;
            }
            const on = new Set(c.labels || []);
            return `<div class="tb-pop">${close}
                <div class="tb-pop-h">Labels</div>
                <ul class="tb-label-list">${(d.labels || []).map(l => `
                    <li><label class="tb-label-row"><input type="checkbox" data-tb-label-toggle="${this.esc(l.id)}"${on.has(l.id) ? ' checked' : ''}>
                        <span class="tb-label tb-label--lg" style="background:${this._color(l.color)}">${this.esc(l.name || '')}</span></label>
                        <button class="tb-icon-btn" type="button" data-tb-label-edit="${this.esc(l.id)}" aria-label="Edit label" title="Edit label">&#9998;</button></li>`).join('')}
                </ul>
                <button class="btn btn-sm" type="button" data-tb-label-new>Create a new label</button></div>`;
        }
        if (this._pop === 'dates') {
            const s = this._localParts(c.start);
            const du = this._localParts(c.due);
            return `<div class="tb-pop tb-inline-edit">${close}
                <div class="tb-pop-h">Dates</div>
                <label class="tb-field">Start date<input class="search-input" type="date" id="tb-start-date" value="${s.date}"></label>
                <label class="tb-field">Due date
                    <span class="tb-field-row"><input class="search-input" type="date" id="tb-due-date" value="${du.date}">
                    <input class="search-input" type="time" id="tb-due-time" value="${du.time || '12:00'}"></span></label>
                <label class="tb-check"><input type="checkbox" id="tb-due-complete"${c.due_complete ? ' checked' : ''}> Done</label>
                <div class="tb-composer-actions">
                    <button class="btn btn-primary btn-sm" type="button" data-tb-dates-save>Save</button>
                    <button class="btn btn-sm" type="button" data-tb-dates-remove>Remove</button>
                </div></div>`;
        }
        if (this._pop === 'checklist') {
            return `<div class="tb-pop tb-inline-edit">${close}
                <div class="tb-pop-h">Add checklist</div>
                <label class="tb-field">Title<input class="search-input" id="tb-cl-new-name" maxlength="512" value="Checklist" data-tb-autofocus></label>
                <div class="tb-composer-actions"><button class="btn btn-primary btn-sm" type="button" data-tb-cl-add>Add</button></div></div>`;
        }
        if (this._pop === 'cover') {
            const cv = c.cover || {};
            const has = !!(cv.color || c.cover_url);
            return `<div class="tb-pop">${close}
                <div class="tb-pop-h">Cover</div>
                ${has ? `<div class="tb-field">Size<div class="tb-size-row">
                    <button type="button" class="tb-size${cv.size !== 'full' ? ' is-on' : ''}" data-tb-cover-size="normal">Normal</button>
                    <button type="button" class="tb-size${cv.size === 'full' ? ' is-on' : ''}" data-tb-cover-size="full">Full</button></div></div>` : ''}
                <div class="tb-field">Colours${this._swatches(this.COVER_COLORS, cv.color, 'data-tb-cover-color')}</div>
                <label class="tb-field">Upload a cover image
                    <input type="file" id="tb-cover-file" accept="image/png,image/jpeg,image/gif,image/webp"></label>
                ${has ? '<button class="btn btn-sm" type="button" data-tb-cover-remove>Remove cover</button>' : ''}</div>`;
        }
        return '';
    },

    _wireChecklistSort() {
        if (!window.Sortable) return;
        document.querySelectorAll('#tb-modal .tb-items').forEach(el => {
            Sortable.create(el, {
                draggable: '.tb-item', animation: 150, delay: 150, delayOnTouchOnly: true,
                ghostClass: 'tb-ghost', filter: 'input, button, .tb-inline-edit', preventOnFilter: false,
                onEnd: async (evt) => {
                    if (evt.oldIndex === evt.newIndex) return;
                    const it = evt.item;
                    const before = this._sib(it, 'tb-item', 'previousElementSibling', 'itemId');
                    const after = this._sib(it, 'tb-item', 'nextElementSibling', 'itemId');
                    this._afterWrite(await this._do('Could not move that item',
                        () => API.updateTrelloCheckItem(it.dataset.itemId, { before_id: before, after_id: after })));
                },
            });
        });
    },

    _val(id) { const el = document.getElementById(id); return el ? el.value : ''; },

    _setEditing(what) { this._editing = what; this._drawCard(); },

    async _onModalClick(e) {
        const t = e.target;
        const c = (this._card && this._card.card) || {};
        const cardId = this._cardId;
        if (t === e.currentTarget || t.closest('[data-tb-close]')) { this._closeRoute(); return; }

        // Popovers
        const pop = t.closest('[data-tb-pop]');
        if (pop) {
            const which = pop.dataset.tbPop;
            this._pop = this._pop === which ? null : which;
            this._labelEdit = null;
            this._labelColor = null;
            this._drawCard();
            return;
        }
        if (t.closest('[data-tb-pop-close]')) { this._pop = null; this._labelEdit = null; this._drawCard(); return; }

        // Title + description + generic cancel
        const ed = t.closest('[data-tb-edit]');
        if (ed && ed.dataset.tbEdit === 'name') { this._editTitle(ed); return; }
        if (ed && ed.dataset.tbEdit === 'desc') { this._setEditing('desc'); return; }
        if (t.closest('[data-tb-desc-cancel]') || t.closest('[data-tb-edit-cancel]')) { this._setEditing(null); return; }
        if (t.closest('[data-tb-desc-save]')) {
            const desc = this._val('tb-desc-input');
            const r = await this._do('Could not save the description', () => API.updateTrelloCard(cardId, { desc }));
            if (r) this._editing = null;
            this._afterWrite(r);
            return;
        }

        // Archive / restore
        const arch = t.closest('[data-tb-archive]');
        if (arch) {
            const closed = arch.dataset.tbArchive === '1';
            this._afterWrite(await this._do(closed ? 'Could not archive this card' : 'Could not restore this card',
                () => API.updateTrelloCard(cardId, { closed })));
            return;
        }

        // Conflicts
        const keep = t.closest('[data-tb-keep]');
        if (keep) {
            const x = (this._card.conflicts || [])[Number(keep.dataset.i)];
            if (!x) return;
            this._afterWrite(await this._do('Could not resolve that', () => API.resolveTrelloConflict({
                object_type: x.object_type, object_id: x.object_id, field: x.field, keep: keep.dataset.tbKeep,
            })));
            return;
        }

        // Labels
        if (t.closest('[data-tb-label-new]')) { this._labelEdit = 'new'; this._labelColor = 'green'; this._drawCard(); return; }
        const le = t.closest('[data-tb-label-edit]');
        if (le) { this._labelEdit = le.dataset.tbLabelEdit; this._labelColor = null; this._drawCard(); return; }
        if (t.closest('[data-tb-label-back]')) { this._labelEdit = null; this._drawCard(); return; }
        const lc = t.closest('[data-tb-label-color]');
        if (lc) {
            this._labelColor = lc.dataset.tbLabelColor;
            const name = this._val('tb-label-name');   // keep what was typed across the redraw
            this._drawCard();
            const inp = document.getElementById('tb-label-name');
            if (inp) inp.value = name;
            return;
        }
        if (t.closest('[data-tb-label-save]')) {
            const name = this._val('tb-label-name').trim();
            const isNew = this._labelEdit === 'new';
            const l = isNew ? null : (this._card.labels || []).find(x => x.id === this._labelEdit);
            const color = this._labelColor || (l && l.color) || 'green';
            const r = await this._do(isNew ? 'Could not create that label' : 'Could not save that label',
                () => isNew ? API.createTrelloLabel(c.board_id || this._boardId, name, color)
                            : API.updateTrelloLabel(this._labelEdit, { name, color }));
            if (r) { this._labelEdit = null; this._labelColor = null; }
            this._afterWrite(r);
            return;
        }
        if (t.closest('[data-tb-label-delete]')) {
            if (!confirm('Delete this label from the whole board? It comes off every card that has it, in Trello too. This cannot be undone.')) return;
            const r = await this._do('Could not delete that label', () => API.deleteTrelloLabel(this._labelEdit));
            if (r) this._labelEdit = null;
            this._afterWrite(r);
            return;
        }

        // Dates
        if (t.closest('[data-tb-dates-save]')) {
            const sd = this._val('tb-start-date');
            const dd = this._val('tb-due-date');
            const tm = this._val('tb-due-time') || '12:00';
            const body = {
                start: sd ? new Date(sd + 'T00:00').toISOString() : null,
                due: dd ? new Date(dd + 'T' + tm).toISOString() : null,
                due_complete: !!(document.getElementById('tb-due-complete') || {}).checked,
            };
            const r = await this._do('Could not save the dates', () => API.updateTrelloCard(cardId, body));
            if (r) this._pop = null;
            this._afterWrite(r);
            return;
        }
        if (t.closest('[data-tb-dates-remove]')) {
            const r = await this._do('Could not remove the dates',
                () => API.updateTrelloCard(cardId, { start: null, due: null, due_complete: false }));
            if (r) this._pop = null;
            this._afterWrite(r);
            return;
        }

        // Cover
        const cc = t.closest('[data-tb-cover-color]');
        if (cc) {
            this._afterWrite(await this._do('Could not set the cover', () => API.setTrelloCover(cardId,
                { color: cc.dataset.tbCoverColor, size: (c.cover && c.cover.size) || 'normal' })));
            return;
        }
        const cs = t.closest('[data-tb-cover-size]');
        if (cs) {
            const body = { size: cs.dataset.tbCoverSize };
            if (c.cover && c.cover.color) body.color = c.cover.color;
            this._afterWrite(await this._do('Could not change the cover size', () => API.setTrelloCover(cardId, body)));
            return;
        }
        if (t.closest('[data-tb-cover-remove]')) {
            this._afterWrite(await this._do('Could not remove the cover', () => API.setTrelloCover(cardId, {})));
            return;
        }

        // Checklists
        if (t.closest('[data-tb-cl-add]')) {
            const name = this._val('tb-cl-new-name').trim() || 'Checklist';
            const r = await this._do('Could not add that checklist', () => API.createTrelloChecklist(cardId, name));
            if (r) this._pop = null;
            this._afterWrite(r);
            return;
        }
        const clr = t.closest('[data-tb-cl-rename]');
        if (clr) { this._setEditing('cl:' + clr.dataset.tbClRename); return; }
        const cld = t.closest('[data-tb-cl-delete]');
        if (cld) {
            if (!confirm('Delete this checklist and all its items? This removes it in Trello too and cannot be undone.')) return;
            this._afterWrite(await this._do('Could not delete that checklist', () => API.deleteTrelloChecklist(cld.dataset.tbClDelete)));
            return;
        }
        const ia = t.closest('[data-tb-item-add]');
        if (ia) { this._setEditing('add:' + ia.dataset.tbItemAdd); return; }
        const ias = t.closest('[data-tb-item-add-save]');
        if (ias) { this._addItem(ias.dataset.tbItemAddSave); return; }
        const ir = t.closest('[data-tb-item-rename]');
        if (ir) { this._setEditing('it:' + ir.dataset.tbItemRename); return; }
        const idl = t.closest('[data-tb-item-delete]');
        if (idl) {
            if (!confirm('Delete this item? This removes it in Trello too and cannot be undone.')) return;
            this._afterWrite(await this._do('Could not delete that item', () => API.deleteTrelloCheckItem(idl.dataset.tbItemDelete)));
            return;
        }

        // Comments
        if (t.closest('[data-tb-comment-save]')) {
            const text = this._val('tb-comment-input').trim();
            if (!text) return;
            this._afterWrite(await this._do('Could not post that comment', () => API.addTrelloComment(cardId, text)));
            return;
        }
        const ce = t.closest('[data-tb-comment-edit]');
        if (ce) { this._setEditing('cm:' + ce.dataset.tbCommentEdit); return; }
        const ces = t.closest('[data-tb-comment-edit-save]');
        if (ces) {
            const id = ces.dataset.tbCommentEditSave;
            const inp = document.querySelector(`#tb-modal [data-tb-comment-edit-input="${CSS.escape(id)}"]`);
            const text = inp ? inp.value.trim() : '';
            if (!text) return;
            const r = await this._do('Could not save that comment', () => API.updateTrelloComment(id, text));
            if (r) this._editing = null;
            this._afterWrite(r);
            return;
        }
        const cdl = t.closest('[data-tb-comment-delete]');
        if (cdl) {
            if (!confirm('Delete this comment? It is removed in Trello too and cannot be undone.')) return;
            this._afterWrite(await this._do('Could not delete that comment', () => API.deleteTrelloComment(cdl.dataset.tbCommentDelete)));
            return;
        }

        // Commission
        if (t.closest('[data-tb-comm-save]')) {
            const client_name = this._val('tb-comm-client').trim();
            if (!client_name) { document.getElementById('tb-comm-client')?.focus(); return; }
            const priceRaw = this._val('tb-comm-price');
            const body = { client_name, price: priceRaw === '' ? null : Number(priceRaw), currency: this._val('tb-comm-currency').trim() || 'USD' };
            const r = await this._do('Could not mark this as a commission', () => API.markTrelloCommission(cardId, body));
            if (r) this._pop = null;
            this._afterWrite(r);
            return;
        }
        if (t.closest('[data-tb-comm-unmark]')) {
            if (!confirm('Unmark this card as a commission?\n\nThe commission record (client, price, notes, files) is kept in PawPoller — only its link to this card is removed.')) return;
            this._afterWrite(await this._do('Could not unmark this card', () => API.unmarkTrelloCommission(cardId)));
        }
    },

    async _onModalChange(e) {
        const t = e.target;
        const cardId = this._cardId;
        if (t.id === 'tb-card-list') {
            // Move to the bottom of the chosen list, as Trello's own "Move" does.
            const listId = t.value;
            const last = ((this._data && this._data.cards) || [])
                .filter(x => x.list_id === listId && !x.closed && x.id !== cardId).pop();
            this._afterWrite(await this._do('Could not move this card',
                () => API.moveTrelloCard(cardId, listId, last ? last.id : null, null)));
            return;
        }
        if (t.id === 'tb-due-done') {
            this._afterWrite(await this._do('Could not update the due date',
                () => API.updateTrelloCard(cardId, { due_complete: t.checked })));
            return;
        }
        if (t.dataset.tbLabelToggle) {
            const id = t.dataset.tbLabelToggle;
            this._afterWrite(await this._do(t.checked ? 'Could not add that label' : 'Could not remove that label',
                () => t.checked ? API.addTrelloCardLabel(cardId, id) : API.removeTrelloCardLabel(cardId, id)));
            return;
        }
        if (t.dataset.tbItemCheck) {
            this._afterWrite(await this._do('Could not update that item',
                () => API.updateTrelloCheckItem(t.dataset.tbItemCheck, { state: t.checked ? 'complete' : 'incomplete' })));
            return;
        }
        if (t.id === 'tb-cover-file' && t.files && t.files[0]) {
            const file = t.files[0];
            t.disabled = true;
            this._afterWrite(await this._do('Could not upload that cover', () => API.uploadTrelloCover(cardId, file)));
        }
    },

    _onModalKey(e) {
        const t = e.target;
        const enter = e.key === 'Enter' && !e.shiftKey;
        const esc = e.key === 'Escape';
        if (!enter && !esc) return;
        if (esc && t.closest('.tb-inline-edit, .tb-desc-input')) {
            e.stopPropagation();
            this._editing = null;
            if (t.closest('.tb-pop')) { this._pop = null; this._labelEdit = null; }
            this._drawCard();
            return;
        }
        if (!enter) return;
        if (t.dataset.tbItemAddInput) { e.preventDefault(); this._addItem(t.dataset.tbItemAddInput); return; }
        if (t.dataset.tbItemNameInput) { e.preventDefault(); this._renameItem(t.dataset.tbItemNameInput, t.value); return; }
        if (t.dataset.tbClNameInput) { e.preventDefault(); this._renameChecklist(t.dataset.tbClNameInput, t.value); return; }
        if (t.id === 'tb-cl-new-name') { e.preventDefault(); document.querySelector('#tb-modal [data-tb-cl-add]')?.click(); return; }
        if (t.id === 'tb-label-name') { e.preventDefault(); document.querySelector('#tb-modal [data-tb-label-save]')?.click(); return; }
        if (t.dataset.tbEdit && t.tagName !== 'TEXTAREA' && t.tagName !== 'INPUT') {
            e.preventDefault();
            if (t.dataset.tbEdit === 'desc') this._setEditing('desc');
        }
    },

    async _addItem(checklistId) {
        const inp = document.querySelector(`#tb-modal [data-tb-item-add-input="${CSS.escape(checklistId)}"]`);
        const name = inp ? inp.value.trim() : '';
        if (!name) { if (inp) inp.focus(); return; }
        // The composer stays open for the next item, as in Trello.
        this._afterWrite(await this._do('Could not add that item', () => API.addTrelloCheckItem(checklistId, name)));
    },

    async _renameItem(id, value) {
        const name = String(value || '').trim();
        this._editing = null;
        if (!name) { this._drawCard(); return; }
        this._afterWrite(await this._do('Could not rename that item', () => API.updateTrelloCheckItem(id, { name })));
    },

    async _renameChecklist(id, value) {
        const name = String(value || '').trim();
        this._editing = null;
        if (!name) { this._drawCard(); return; }
        this._afterWrite(await this._do('Could not rename that checklist', () => API.renameTrelloChecklist(id, name)));
    },

    /* Click the title to edit it; Enter or leaving the box saves, Esc cancels. */
    _editTitle(h) {
        const c = this._card.card || {};
        const ta = document.createElement('textarea');
        ta.className = 'search-input tb-card-name-input tb-inline-edit';
        ta.rows = 2;
        ta.maxLength = 16384;
        ta.value = c.name || '';
        h.replaceWith(ta);
        ta.focus();
        ta.select();
        let done = false;
        const finish = async (save) => {
            if (done) return;
            done = true;
            const name = ta.value.replace(/\s+/g, ' ').trim();
            if (save && name && name !== c.name) {
                this._afterWrite(await this._do('Could not rename this card', () => API.updateTrelloCard(this._cardId, { name })));
            } else {
                this._drawCard();
            }
        };
        ta.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            else if (e.key === 'Escape') { e.stopPropagation(); finish(false); }
        });
        ta.addEventListener('blur', () => finish(true));
    },
};
