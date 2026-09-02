/* ArtistPicker — pick (or add) the artist who drew a piece.
 *
 * 3.10.0 shipped the artist as an editable field with a plain inline form. This
 * replaces that with the tag browser's experience, deliberately reusing the same
 * `.tag-browser-*` modal chrome so the two pickers look and behave alike —
 * search box, filter chips with live counts, a card grid, a selected strip and a
 * Done button.
 *
 * What makes an artist different from a tag, and shapes the design:
 *
 *   - It is SINGLE select. One piece has one artist.
 *   - "No artist" is a real answer, in three flavours (own work / unknown /
 *     clear), so those are first-class cards rather than a dropdown hidden
 *     somewhere else.
 *   - An artist carries HANDLES — eleven platforms' worth — and the whole point of
 *     the registry is that picking a known name fills them in. So the selected
 *     artist expands into an editable handle panel rather than being a pill.
 *   - The lookup's warnings (dead account, look-alike handle, typo, binding
 *     repost policy) are shown on the card AND on the selection, because the
 *     moment they matter is while you are choosing.
 *
 * Usage:
 *   ArtistPicker.open({
 *     artist: {name, handles},        // current, may be null
 *     status: '' | 'own' | 'unknown', // current no-artist state
 *     onConfirm: ({artist, status}) => { ... },
 *   });
 */
(function () {
    // Display order matches the registry's coverage, most-covered first.
    const PLATFORMS = [
        ['fa', 'FurAffinity'], ['e621', 'e621'], ['da', 'DeviantArt'], ['tw', 'X / Twitter'],
        ['bsky', 'Bluesky'], ['ib', 'Inkbunny'], ['ws', 'Weasyl'], ['sf', 'SoFurry'],
        ['fn', 'FurryNetwork'], ['ik', 'Itaku'], ['ig', 'Instagram'],
    ];

    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const keyOf = (name) => String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '');

    function open(opts) {
        opts = opts || {};
        let artists = [];
        let cat = 'all';
        let query = '';
        let searchTimer = null;

        // The working selection. `mode` is what Done will apply.
        //   'artist'  — sel.name + sel.handles
        //   'own' | 'unknown' | 'none'
        const startArtist = opts.artist && opts.artist.name ? opts.artist : null;
        const sel = {
            mode: startArtist ? 'artist' : (opts.status || 'none'),
            name: startArtist ? startArtist.name : '',
            handles: Object.assign({}, (startArtist && startArtist.handles) || {}),
            warnings: [], context: [], isNew: false,
            // The registry key (when this artist is in it) and the handles the
            // registry ACTUALLY stores. Both are needed to remove one: an upsert
            // merges, so clearing a field cannot say "this handle was wrong" —
            // the merge keeps it and the picker re-fills it on the next open.
            key: '', stored: {},
        };

        const chipKeys = ['all', 'flagged', 'nohandles'];
        const chipLabel = { all: 'All', flagged: '⚠ Warnings', nohandles: 'Name only' };
        const chips = chipKeys.map(k =>
            `<button type="button" class="tag-browser-chip${k === cat ? ' tag-browser-chip-active' : ''}" data-ap-cat="${k}">` +
            `<span class="tag-browser-chip-label">${chipLabel[k]}</span> ` +
            `<span class="tag-browser-chip-count" data-ap-count="${k}"></span></button>`).join('');

        const root = document.createElement('div');
        root.className = 'ap-root';
        root.innerHTML = `
            <div class="tag-browser-backdrop" data-ap-backdrop></div>
            <div class="tag-browser-modal ap-modal" role="dialog" aria-label="Artist">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">Who drew this?</div>
                        <button type="button" class="tag-browser-close" data-ap-close aria-label="Close">&times;</button>
                    </div>
                    <input type="search" id="ap-search" class="tag-browser-search"
                           placeholder="Search by name or handle…" autocomplete="off">
                    <div class="tag-browser-filters">${chips}</div>
                </div>
                <div class="tag-browser-selected ap-selected" id="ap-selected"></div>
                <div class="tag-browser-body">
                    <div class="tag-browser-grid ap-grid" id="ap-grid"><div class="tag-browser-empty">Loading…</div></div>
                </div>
                <div class="tag-browser-footer">
                    <div class="tag-browser-count" id="ap-count"></div>
                    <button type="button" class="btn btn-primary" id="ap-confirm">Done</button>
                </div>
            </div>`;
        document.body.appendChild(root);
        requestAnimationFrame(() => root.querySelector('.tag-browser-modal')?.classList.add('open'));

        const grid = root.querySelector('#ap-grid');
        const countEl = root.querySelector('#ap-count');
        const searchEl = root.querySelector('#ap-search');
        const selectedEl = root.querySelector('#ap-selected');

        const close = () => {
            root.querySelector('.tag-browser-modal')?.classList.remove('open');
            document.removeEventListener('keydown', onKey);
            setTimeout(() => root.remove(), 180);
        };
        function onKey(e) { if (e.key === 'Escape') close(); }
        document.addEventListener('keydown', onKey);
        root.querySelector('[data-ap-backdrop]').addEventListener('click', close);
        root.querySelector('[data-ap-close]').addEventListener('click', close);

        // ── selection strip ──────────────────────────────────────────────
        //
        // Not a pill like the tag picker's: an artist's handles are the payload,
        // and they have to be visible and correctable right where you pick.
        const renderSelected = () => {
            if (sel.mode === 'own') {
                selectedEl.innerHTML = `<span class="ap-sel-note">Marked as <strong>your own work</strong> — nothing will be credited.</span>`;
                return;
            }
            if (sel.mode === 'unknown') {
                selectedEl.innerHTML = `<span class="ap-sel-note">Marked as <strong>artist unknown</strong> — no credit, and it stays findable.</span>`;
                return;
            }
            if (sel.mode !== 'artist' || !sel.name) {
                selectedEl.innerHTML = `<span class="tag-browser-selected-empty">No artist chosen</span>`;
                return;
            }
            const rows = PLATFORMS.map(([code, label]) => {
                const inRegistry = !!sel.stored[code];
                const rm = inRegistry
                    ? `<button type="button" class="ap-h-rm" data-ap-rmhandle="${code}"
                               title="Forget this handle for ${esc(sel.name)} everywhere, not just here">&times;</button>`
                    : '';
                return `
                <label class="ap-h">
                    <span>${label}${rm}</span>
                    <input type="text" data-ap-handle="${code}" value="${esc(sel.handles[code] || '')}"
                           placeholder="—" autocomplete="off" spellcheck="false">
                </label>`;
            }).join('');
            const warn = sel.warnings.length
                ? `<div class="ap-warn">${sel.warnings.map(w => `<div>⚠ ${esc(w)}</div>`).join('')}</div>` : '';
            const ctx = sel.context.length
                ? `<div class="ap-ctx">${sel.context.map(w => `<div>${esc(w)}</div>`).join('')}</div>` : '';
            const n = Object.keys(sel.handles).filter(k => (sel.handles[k] || '').trim()).length;
            selectedEl.innerHTML = `
                <div class="ap-sel-head">
                    <span class="ap-sel-name">${esc(sel.name)}</span>
                    <span class="ap-sel-meta">${sel.isNew ? 'new artist' : `${n} handle${n === 1 ? '' : 's'}`}</span>
                    <button type="button" class="ap-sel-clear" data-ap-pick="none" title="Choose nobody">&times;</button>
                </div>
                ${warn}${ctx}
                <div class="ap-handles">${rows}</div>
                <div class="ap-h-note">Handles save to the registry, so every
                    piece by ${esc(sel.name)} gets them. Use &times; to forget a wrong one.</div>`;
        };

        const updateFooter = () => {
            countEl.textContent =
                sel.mode === 'artist' && sel.name ? `Artist: ${sel.name}`
                : sel.mode === 'own' ? 'Your own work'
                : sel.mode === 'unknown' ? 'Artist unknown'
                : 'No artist';
        };

        const refresh = () => { renderSelected(); updateFooter(); };

        // ── grid ─────────────────────────────────────────────────────────
        const matches = () => {
            const q = query.toLowerCase();
            return artists.filter(a => {
                if (cat === 'flagged' && !(a.warnings || []).length) return false;
                if (cat === 'nohandles' && Object.keys(a.handles || {}).length) return false;
                if (!q) return true;
                if (a.name.toLowerCase().includes(q)) return true;
                if ((a.aliases || []).some(x => String(x).toLowerCase().includes(q))) return true;
                return Object.values(a.handles || {}).some(h => String(h).toLowerCase().includes(q));
            });
        };

        const updateChipCounts = () => {
            const q = query.toLowerCase();
            const hit = (a) => !q || a.name.toLowerCase().includes(q) ||
                (a.aliases || []).some(x => String(x).toLowerCase().includes(q)) ||
                Object.values(a.handles || {}).some(h => String(h).toLowerCase().includes(q));
            const counts = { all: 0, flagged: 0, nohandles: 0 };
            for (const a of artists) {
                if (!hit(a)) continue;
                counts.all++;
                if ((a.warnings || []).length) counts.flagged++;
                if (!Object.keys(a.handles || {}).length) counts.nohandles++;
            }
            root.querySelectorAll('[data-ap-count]').forEach(el => {
                el.textContent = String(counts[el.getAttribute('data-ap-count')] ?? '');
            });
        };

        // The three no-artist answers are cards, not a dropdown: "nobody drew
        // this for me" is as valid an answer as any name in the list, and burying
        // it is what made every artist-less piece warn forever.
        const stateCards = () => {
            if (query) return '';
            const card = (mode, icon, title, desc) => `
                <div class="tag-browser-card ap-card ap-card-state${sel.mode === mode ? ' tag-browser-card-added' : ''}"
                     data-ap-pick="${mode}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${icon} ${title}</div>
                    </div>
                    <div class="tag-browser-card-desc">${desc}</div>
                </div>`;
            return card('own', '✍', 'My own work', 'Drawn by you — there is nobody to credit.')
                 + card('unknown', '?', 'Artist unknown', 'Commissioned or gifted, but not recoverable.');
        };

        const newCard = () => {
            if (!query.trim()) return '';
            const exact = artists.some(a => keyOf(a.name) === keyOf(query));
            if (exact) return '';
            return `
                <div class="tag-browser-card ap-card ap-card-new" data-ap-new="${esc(query.trim())}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">+ Add “${esc(query.trim())}”</div>
                    </div>
                    <div class="tag-browser-card-desc">Not in the registry yet — add them and fill in their handles.</div>
                </div>`;
        };

        const render = () => {
            const items = matches().slice(0, 400);
            const cards = items.map(a => {
                const isSel = sel.mode === 'artist' && keyOf(a.name) === keyOf(sel.name);
                const hs = Object.keys(a.handles || {});
                const badges = hs.length
                    ? hs.slice(0, 6).map(p => `<span class="ap-badge">${esc(p)}</span>`).join('') +
                      (hs.length > 6 ? `<span class="ap-badge ap-badge-more">+${hs.length - 6}</span>` : '')
                    : `<span class="ap-badge ap-badge-none">name only</span>`;
                const warn = (a.warnings || []).length
                    ? `<div class="ap-card-warn">⚠ ${esc(a.warnings[0])}</div>` : '';
                return `<div class="tag-browser-card ap-card${isSel ? ' tag-browser-card-added' : ''}"
                             data-ap-name="${esc(a.name)}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${esc(a.name)}</div>
                    </div>
                    <div class="ap-badges">${badges}</div>
                    ${warn}
                </div>`;
            }).join('');
            const body = stateCards() + newCard() + cards;
            grid.innerHTML = body || '<div class="tag-browser-empty">No artists match.</div>';
            updateChipCounts();
        };

        // ── interaction ──────────────────────────────────────────────────
        grid.addEventListener('click', (e) => {
            const state = e.target.closest('[data-ap-pick]');
            if (state) {
                sel.mode = state.getAttribute('data-ap-pick');
                sel.name = ''; sel.handles = {}; sel.warnings = []; sel.context = [];
                sel.stored = {}; sel.key = ''; sel.isNew = false;
                render(); refresh();
                return;
            }
            const add = e.target.closest('[data-ap-new]');
            if (add) {
                sel.mode = 'artist';
                sel.name = add.getAttribute('data-ap-new');
                sel.handles = {}; sel.warnings = []; sel.context = [];
                sel.stored = {}; sel.key = ''; sel.isNew = true;
                render(); refresh();
                // Straight into the first handle field — adding an artist means
                // typing handles, and making that one click instead of two is the
                // difference between the registry growing and not.
                setTimeout(() => selectedEl.querySelector('[data-ap-handle]')?.focus(), 40);
                return;
            }
            const card = e.target.closest('[data-ap-name]');
            if (!card) return;
            const name = card.getAttribute('data-ap-name');
            const a = artists.find(x => keyOf(x.name) === keyOf(name));
            sel.mode = 'artist';
            sel.name = a ? a.name : name;
            sel.handles = Object.assign({}, (a && a.handles) || {});
            sel.warnings = (a && a.warnings) || [];
            sel.context = (a && a.context) || [];
            sel.stored = Object.assign({}, (a && a.handles) || {});
            sel.key = (a && a.key) || '';
            sel.isNew = false;
            render(); refresh();
        });

        // Handle edits live on the selection, so switching artists and back does
        // not silently discard a correction.
        selectedEl.addEventListener('input', (e) => {
            const el = e.target.closest('[data-ap-handle]');
            if (!el) return;
            const v = (el.value || '').trim();
            if (v) sel.handles[el.dataset.apHandle] = v;
            else delete sel.handles[el.dataset.apHandle];
        });
        selectedEl.addEventListener('click', async (e) => {
            // Forgetting a handle is a REGISTRY edit, not a per-piece one, so it
            // is a deliberate button rather than a side effect of blanking a
            // field — "losing a credit is worse than carrying a stale handle".
            const rm = e.target.closest('[data-ap-rmhandle]');
            if (rm) {
                e.preventDefault();
                const code = rm.getAttribute('data-ap-rmhandle');
                if (!sel.key) {                       // not in the registry yet
                    delete sel.handles[code]; delete sel.stored[code];
                    renderSelected(); return;
                }
                try {
                    const updated = await API.deleteArtistHandle(sel.key, code);
                    sel.stored = Object.assign({}, updated.handles || {});
                    delete sel.handles[code];
                    const a = artists.find(x => x.key === sel.key);
                    if (a) a.handles = Object.assign({}, sel.stored);
                    renderSelected(); render();
                } catch (err) {
                    // Leave the field alone on failure: pretending it went is
                    // worse than the handle still being there.
                    console.warn('handle removal failed', err);
                }
                return;
            }
            const clear = e.target.closest('[data-ap-pick]');
            if (!clear) return;
            sel.mode = clear.getAttribute('data-ap-pick');
            sel.name = ''; sel.handles = {}; sel.warnings = []; sel.context = [];
            sel.stored = {}; sel.key = ''; sel.isNew = false;
            render(); refresh();
        });

        root.querySelectorAll('[data-ap-cat]').forEach(btn => btn.addEventListener('click', () => {
            cat = btn.getAttribute('data-ap-cat');
            root.querySelectorAll('[data-ap-cat]').forEach(b =>
                b.classList.toggle('tag-browser-chip-active', b === btn));
            render();
        }));
        searchEl.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => { query = searchEl.value.trim(); render(); }, 180);
        });
        // Enter on a search with no exact match adds that artist — the fast path
        // for the 24 unattributed pieces.
        searchEl.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            const q = searchEl.value.trim();
            if (!q) return;
            const a = artists.find(x => keyOf(x.name) === keyOf(q));
            sel.mode = 'artist';
            sel.name = a ? a.name : q;
            sel.handles = Object.assign({}, (a && a.handles) || {});
            sel.warnings = (a && a.warnings) || [];
            sel.context = (a && a.context) || [];
            sel.stored = Object.assign({}, (a && a.handles) || {});
            sel.key = (a && a.key) || '';
            sel.isNew = !a;
            render(); refresh();
        });

        root.querySelector('#ap-confirm').addEventListener('click', () => {
            if (opts.onConfirm) {
                opts.onConfirm(sel.mode === 'artist' && sel.name
                    ? { artist: { name: sel.name, handles: sel.handles }, status: '' }
                    : { artist: null, status: sel.mode === 'none' ? '' : sel.mode });
            }
            close();
        });

        (async () => {
            try {
                const r = await API.listArtists();
                artists = r.artists || [];
                // Carry the registry's research onto a pre-existing selection, so
                // opening the picker on an already-credited piece still shows the
                // warnings for that artist.
                if (sel.mode === 'artist' && sel.name) {
                    const a = artists.find(x => keyOf(x.name) === keyOf(sel.name));
                    if (a) {
                        sel.warnings = a.warnings || [];
                        sel.context = a.context || [];
                        sel.stored = Object.assign({}, a.handles || {});
                        sel.key = a.key || '';
                        for (const [p, h] of Object.entries(a.handles || {})) {
                            if (!sel.handles[p]) sel.handles[p] = h;
                        }
                    }
                }
                render(); refresh();
            } catch (err) {
                grid.innerHTML = `<div class="tag-browser-empty">${esc(err.message || err)}</div>`;
            }
        })();
        refresh();
        setTimeout(() => searchEl.focus(), 60);
        return { close };
    }

    window.ArtistPicker = { open };
})();
