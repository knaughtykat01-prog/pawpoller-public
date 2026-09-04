/* ArtistPicker — pick (or add) the artist who drew a piece, or (4.6.0) any
 * other person in it: who commissioned it, whose character it is, who worked
 * on it with the artist.
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
 * 4.6.0 — the registry is a PEOPLE registry (docs/specs/people_registry.md):
 *   - a person row may be one of the operator's own personas; those cards carry
 *     a "you" badge, and *My own work* asks which persona when there are several,
 *     so the piece can say WHICH you drew it (an e621 artist tag needs that);
 *   - `mode: 'person'` picks someone in a ROLE — commissioner / character owner /
 *     collaborator — for the featuring line. Same registry, same cards, same
 *     handles; a role chip row instead of the no-artist state cards.
 *
 * Usage:
 *   ArtistPicker.open({
 *     artist: {key?, name, handles},   // current, may be null
 *     status: '' | 'own' | 'unknown',  // current no-artist state
 *     personas: [{persona_id, name}],  // the operator's personas (for "own")
 *     personaId: 2,                    // the persona already linked, if any
 *     onConfirm: ({artist, status, persona_id}) => { ... },
 *   });
 *   ArtistPicker.open({
 *     mode: 'person', role: 'commissioner' | 'owner' | 'collaborator',
 *     characters: ['Sample Character'],   // for the owner role
 *     onConfirm: ({person: {key, name}, role, character}) => { ... },
 *   });
 */
(function () {
    // Display order matches the registry's coverage, most-covered first.
    const PLATFORMS = [
        ['fa', 'FurAffinity'], ['e621', 'e621'], ['da', 'DeviantArt'], ['tw', 'X / Twitter'],
        ['bsky', 'Bluesky'], ['ib', 'Inkbunny'], ['ws', 'Weasyl'], ['sf', 'SoFurry'],
        ['fn', 'FurryNetwork'], ['ik', 'Itaku'], ['ig', 'Instagram'], ['tg', 'Telegram'],
    ];
    // The roles a person can have on a piece other than "drew it" (the artist
    // stays its own field). Order = the featuring line's order.
    const ROLES = [
        ['commissioner', 'Commissioned by', 'for …'],
        ['owner', 'Character owner', "featuring …'s character"],
        ['collaborator', 'Collaborator', 'with …'],
    ];

    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const keyOf = (name) => String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '');

    function open(opts) {
        opts = opts || {};
        const personMode = opts.mode === 'person';
        const personas = Array.isArray(opts.personas) ? opts.personas : [];
        const characters = Array.isArray(opts.characters) ? opts.characters : [];
        let artists = [];
        let cat = 'all';
        let query = '';
        let searchTimer = null;

        // The working selection. `mode` is what Done will apply.
        //   'artist'  — sel.name + sel.handles (a person, in person mode)
        //   'own' | 'unknown' | 'none'
        const startArtist = opts.artist && opts.artist.name ? opts.artist : null;
        const sel = {
            mode: startArtist ? 'artist' : (personMode ? 'none' : (opts.status || 'none')),
            name: startArtist ? startArtist.name : '',
            handles: Object.assign({}, (startArtist && startArtist.handles) || {}),
            warnings: [], context: [], isNew: false,
            // The registry key (when this artist is in it) and the handles the
            // registry ACTUALLY stores. Both are needed to remove one: an upsert
            // merges, so clearing a field cannot say "this handle was wrong" —
            // the merge keeps it and the picker re-fills it on the next open.
            key: (startArtist && startArtist.key) || '', stored: {},
            // Which of the operator's personas this is — for "own" (which you
            // drew it) and shown as a badge on a person who IS you.
            personaId: opts.personaId != null ? opts.personaId
                : (personas.length === 1 ? personas[0].persona_id : null),
            role: personMode ? (ROLES.some(r => r[0] === opts.role) ? opts.role : 'commissioner') : '',
            character: characters.length === 1 ? characters[0] : '',
        };

        const chipKeys = ['all', 'you', 'flagged', 'nohandles'];
        const chipLabel = { all: 'All', you: 'You', flagged: '⚠ Warnings', nohandles: 'Name only' };
        const chips = chipKeys.map(k =>
            `<button type="button" class="tag-browser-chip${k === cat ? ' tag-browser-chip-active' : ''}" data-ap-cat="${k}">` +
            `<span class="tag-browser-chip-label">${chipLabel[k]}</span> ` +
            `<span class="tag-browser-chip-count" data-ap-count="${k}"></span></button>`).join('');
        const roleChips = personMode ? `
            <div class="tag-browser-filters ap-roles" role="radiogroup" aria-label="Role">
                ${ROLES.map(([code, label, hint]) =>
                    `<button type="button" class="tag-browser-chip ap-role${code === sel.role ? ' tag-browser-chip-active' : ''}"
                             data-ap-role="${code}" title="${esc(hint)}">${label}</button>`).join('')}
            </div>` : '';

        const root = document.createElement('div');
        root.className = 'ap-root';
        root.innerHTML = `
            <div class="tag-browser-backdrop" data-ap-backdrop></div>
            <div class="tag-browser-modal ap-modal" role="dialog" aria-label="${personMode ? 'Person' : 'Artist'}">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">${personMode ? 'Who else is in this?' : 'Who drew this?'}</div>
                        <button type="button" class="tag-browser-close" data-ap-close aria-label="Close">&times;</button>
                    </div>
                    ${roleChips}
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

        const personaName = (id) => {
            const p = personas.find(x => String(x.persona_id) === String(id));
            return p ? p.name : '';
        };
        const roleLabel = (code) => (ROLES.find(r => r[0] === code) || [])[1] || code;

        // ── selection strip ──────────────────────────────────────────────
        //
        // Not a pill like the tag picker's: an artist's handles are the payload,
        // and they have to be visible and correctable right where you pick.
        const renderSelected = () => {
            if (sel.mode === 'own') {
                // Which of you: only a question when there is more than one
                // persona. The answer is what lets a self-drawn piece carry the
                // right e621 artist tag and no "Art by" line.
                const who = personas.length > 1
                    ? `<label class="ap-persona"><span>Which of you?</span>
                        <select data-ap-persona>
                            <option value=""${sel.personaId == null ? ' selected' : ''}>— just "mine" —</option>
                            ${personas.map(p => `<option value="${esc(p.persona_id)}"${String(p.persona_id) === String(sel.personaId) ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}
                        </select></label>`
                    : (personas.length === 1 ? `<span class="muted"> (${esc(personas[0].name)})</span>` : '');
                selectedEl.innerHTML = `<span class="ap-sel-note">Marked as <strong>your own work</strong> — no credit line; on the boorus the artist tag is yours.${who}</span>`;
                return;
            }
            if (sel.mode === 'unknown') {
                selectedEl.innerHTML = `<span class="ap-sel-note">Marked as <strong>artist unknown</strong> — no credit, and it stays findable.</span>`;
                return;
            }
            if (sel.mode !== 'artist' || !sel.name) {
                selectedEl.innerHTML = `<span class="tag-browser-selected-empty">${personMode ? 'Nobody chosen' : 'No artist chosen'}</span>`;
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
            const you = sel.personaId != null
                ? `<span class="ap-you" title="This person is one of your personas">you${personaName(sel.personaId) ? ' · ' + esc(personaName(sel.personaId)) : ''}</span>` : '';
            // The owner role needs the character: one → taken as read; several →
            // choose; none → say so rather than save an owner of nothing.
            let who = '';
            if (personMode && sel.role === 'owner') {
                who = characters.length
                    ? `<label class="ap-persona"><span>Whose character?</span>
                        <select data-ap-char>
                            ${characters.length > 1 ? `<option value=""${!sel.character ? ' selected' : ''}>— pick —</option>` : ''}
                            ${characters.map(c => `<option value="${esc(c)}"${c === sel.character ? ' selected' : ''}>${esc(c)}</option>`).join('')}
                        </select></label>`
                    : `<div class="ap-warn"><div>⚠ This piece lists no characters yet — add them to the record first, then say whose they are.</div></div>`;
            }
            const mentionNote = personMode
                ? `<div class="ap-h-note">They are <strong>linked</strong> on a site only where that handle has <em>mention</em> switched on (People page) — an @-mention that notifies them where the site has one, a profile link elsewhere; otherwise the line carries just the name. Names are free, links are consent.</div>`
                : `<div class="ap-h-note">Handles save to the registry, so every
                    piece by ${esc(sel.name)} gets them. Use &times; to forget a wrong one.</div>`;
            selectedEl.innerHTML = `
                <div class="ap-sel-head">
                    <span class="ap-sel-name">${esc(sel.name)}</span>
                    ${you}
                    ${personMode ? `<span class="ap-sel-meta">${esc(roleLabel(sel.role))}</span>` : ''}
                    <span class="ap-sel-meta">${sel.isNew ? (personMode ? 'new person' : 'new artist') : `${n} handle${n === 1 ? '' : 's'}`}</span>
                    <button type="button" class="ap-sel-clear" data-ap-pick="none" title="Choose nobody">&times;</button>
                </div>
                ${who}
                ${warn}${ctx}
                <div class="ap-handles">${rows}</div>
                ${mentionNote}`;
        };

        const updateFooter = () => {
            countEl.textContent =
                sel.mode === 'artist' && sel.name ? (personMode ? `${roleLabel(sel.role)}: ${sel.name}` : `Artist: ${sel.name}`)
                : sel.mode === 'own' ? 'Your own work'
                : sel.mode === 'unknown' ? 'Artist unknown'
                : (personMode ? 'Nobody' : 'No artist');
        };

        const refresh = () => { renderSelected(); updateFooter(); };

        // ── grid ─────────────────────────────────────────────────────────
        const hit = (a, q) => !q || a.name.toLowerCase().includes(q)
            || (a.aliases || []).some(x => String(x).toLowerCase().includes(q))
            || Object.values(a.handles || {}).some(h => String(h).toLowerCase().includes(q));

        const matches = () => {
            const q = query.toLowerCase();
            return artists.filter(a => {
                if (cat === 'flagged' && !(a.warnings || []).length) return false;
                if (cat === 'nohandles' && Object.keys(a.handles || {}).length) return false;
                if (cat === 'you' && a.persona_id == null) return false;
                return hit(a, q);
            });
        };

        const updateChipCounts = () => {
            const q = query.toLowerCase();
            const counts = { all: 0, you: 0, flagged: 0, nohandles: 0 };
            for (const a of artists) {
                if (!hit(a, q)) continue;
                counts.all++;
                if (a.persona_id != null) counts.you++;
                if ((a.warnings || []).length) counts.flagged++;
                if (!Object.keys(a.handles || {}).length) counts.nohandles++;
            }
            root.querySelectorAll('[data-ap-count]').forEach(el => {
                el.textContent = String(counts[el.getAttribute('data-ap-count')] ?? '');
            });
        };

        // The three no-artist answers are cards, not a dropdown: "nobody drew
        // this for me" is as valid an answer as any name in the list, and burying
        // it is what made every artist-less piece warn forever. (Not in person
        // mode — a role either has someone or it does not exist.)
        const stateCards = () => {
            if (query || personMode) return '';
            const card = (mode, icon, title, desc) => `
                <div class="tag-browser-card ap-card ap-card-state${sel.mode === mode ? ' tag-browser-card-added' : ''}"
                     data-ap-pick="${mode}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${icon} ${title}</div>
                    </div>
                    <div class="tag-browser-card-desc">${desc}</div>
                </div>`;
            return card('own', '✍', 'My own work', 'Drawn by you — no credit line, and the booru artist tag is yours.')
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
                const you = a.persona_id != null
                    ? `<span class="ap-you" title="One of your personas">you${personaName(a.persona_id) ? ' · ' + esc(personaName(a.persona_id)) : ''}</span>` : '';
                return `<div class="tag-browser-card ap-card${isSel ? ' tag-browser-card-added' : ''}"
                             data-ap-name="${esc(a.name)}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${esc(a.name)} ${you}</div>
                    </div>
                    <div class="ap-badges">${badges}</div>
                    ${warn}
                </div>`;
            }).join('');
            const body = stateCards() + newCard() + cards;
            grid.innerHTML = body || `<div class="tag-browser-empty">${personMode ? 'Nobody matches.' : 'No artists match.'}</div>`;
            updateChipCounts();
        };

        const pickRow = (a, name) => {
            sel.mode = 'artist';
            sel.name = a ? a.name : name;
            sel.handles = Object.assign({}, (a && a.handles) || {});
            sel.warnings = (a && a.warnings) || [];
            sel.context = (a && a.context) || [];
            sel.stored = Object.assign({}, (a && a.handles) || {});
            sel.key = (a && a.key) || '';
            sel.personaId = a && a.persona_id != null ? a.persona_id : (personMode ? null : sel.personaId);
            sel.isNew = !a;
        };
        const clearSel = (mode) => {
            sel.mode = mode;
            sel.name = ''; sel.handles = {}; sel.warnings = []; sel.context = [];
            sel.stored = {}; sel.key = ''; sel.isNew = false;
            if (mode !== 'own') sel.personaId = personas.length === 1 ? personas[0].persona_id : null;
        };

        // ── interaction ──────────────────────────────────────────────────
        grid.addEventListener('click', (e) => {
            const state = e.target.closest('[data-ap-pick]');
            if (state) {
                clearSel(state.getAttribute('data-ap-pick'));
                if (sel.mode === 'own' && opts.personaId != null) sel.personaId = opts.personaId;
                render(); refresh();
                return;
            }
            const add = e.target.closest('[data-ap-new]');
            if (add) {
                pickRow(null, add.getAttribute('data-ap-new'));
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
            pickRow(artists.find(x => keyOf(x.name) === keyOf(name)), name);
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
        selectedEl.addEventListener('change', (e) => {
            const who = e.target.closest('[data-ap-persona]');
            if (who) { sel.personaId = who.value ? Number(who.value) : null; updateFooter(); return; }
            const ch = e.target.closest('[data-ap-char]');
            if (ch) { sel.character = ch.value; }
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
            clearSel(clear.getAttribute('data-ap-pick'));
            render(); refresh();
        });

        root.querySelectorAll('[data-ap-cat]').forEach(btn => btn.addEventListener('click', () => {
            cat = btn.getAttribute('data-ap-cat');
            root.querySelectorAll('[data-ap-cat]').forEach(b =>
                b.classList.toggle('tag-browser-chip-active', b === btn));
            render();
        }));
        root.querySelectorAll('[data-ap-role]').forEach(btn => btn.addEventListener('click', () => {
            sel.role = btn.getAttribute('data-ap-role');
            root.querySelectorAll('[data-ap-role]').forEach(b =>
                b.classList.toggle('tag-browser-chip-active', b === btn));
            refresh();
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
            pickRow(artists.find(x => keyOf(x.name) === keyOf(q)), q);
            render(); refresh();
        });

        root.querySelector('#ap-confirm').addEventListener('click', async () => {
            if (!opts.onConfirm) { close(); return; }
            if (personMode) {
                if (sel.mode !== 'artist' || !sel.name) { close(); return; }
                if (sel.role === 'owner' && !sel.character) {
                    countEl.textContent = characters.length ? 'Pick whose character it is first.' : 'Add the character to the record first.';
                    return;
                }
                // A role points at a registry ROW, so a person typed fresh is
                // saved first — that is also what gives them a handle panel on
                // the People page. An existing person whose handles were edited
                // in the strip is saved too (upserts merge), the same as the
                // artist path does server-side; dropping the edit would read
                // as saved.
                let key = sel.key;
                const edited = Object.keys(sel.handles).some(k => (sel.handles[k] || '') !== (sel.stored[k] || ''));
                if (!key || edited) {
                    try {
                        const saved = await API.saveArtist({ name: sel.name, handles: sel.handles });
                        key = saved.key;
                    } catch (err) {
                        countEl.textContent = 'Could not save them: ' + (err.message || err);
                        return;
                    }
                }
                opts.onConfirm({ person: { key, name: sel.name }, role: sel.role,
                                 character: sel.role === 'owner' ? sel.character : '' });
                close();
                return;
            }
            opts.onConfirm(sel.mode === 'artist' && sel.name
                ? { artist: { key: sel.key, name: sel.name, handles: sel.handles }, status: '', persona_id: null }
                : { artist: null, status: sel.mode === 'none' ? '' : sel.mode,
                    persona_id: sel.mode === 'own' ? sel.personaId : null });
            close();
        });

        (async () => {
            try {
                const r = await API.listArtists();
                artists = r.artists || [];
                // The registry answers with the operator's personas too (4.6.0),
                // so "which of you" works even when the caller passed none.
                if (!personas.length && Array.isArray(r.personas)) {
                    personas.push(...r.personas);
                    if (sel.personaId == null && personas.length === 1) sel.personaId = personas[0].persona_id;
                }
                // Carry the registry's research onto a pre-existing selection, so
                // opening the picker on an already-credited piece still shows the
                // warnings for that artist.
                if (sel.mode === 'artist' && sel.name) {
                    const a = artists.find(x => (sel.key && x.key === sel.key) || keyOf(x.name) === keyOf(sel.name));
                    if (a) {
                        sel.warnings = a.warnings || [];
                        sel.context = a.context || [];
                        sel.stored = Object.assign({}, a.handles || {});
                        sel.key = a.key || '';
                        if (a.persona_id != null) sel.personaId = a.persona_id;
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

    window.ArtistPicker = { open, ROLES };
})();
