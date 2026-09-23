/* CharacterPicker — the character registry as a picker (4.33.0, backlog CHARREG).
 *
 * Characters used to be a comma-separated text box on each piece: no canonical
 * spelling, no dedup across works, no owner, and nothing that reached a post. This is
 * the same modal the tag library uses, over `/api/characters` — pick from the people
 * you have already drawn, add a new one inline, and hand the names back.
 *
 * Deliberately the tag picker's chrome (`.tag-browser-*`) and the artist picker's
 * "+ Add <query>" card: three pickers that look and behave alike beat three that each
 * invent their own.
 *
 * Usage:
 *   CharacterPicker.open({
 *     selected: ['Kii'],                  // pre-checked, matched case-insensitively
 *     onConfirm: (names) => { ... },      // the canonical names, in registry order
 *   });
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    /* The registry is small and changes while the picker is open (you can add from
       here), so it is fetched per open rather than cached like the tag database. */
    async function load() {
        const resp = await fetch('/api/characters');
        if (!resp.ok) throw new Error('Character registry failed to load');
        return (await resp.json()).characters || [];
    }

    async function create(name) {
        const resp = await fetch('/api/characters', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) throw new Error('Could not add ' + name);
        return (await resp.json()).character;
    }

    function open(opts) {
        opts = opts || {};
        // Selection is by lowercased name: a piece may already carry a spelling that
        // differs from the registry's, and losing it on open would silently rewrite
        // somebody's record.
        const selected = new Set((opts.selected || []).map(s => String(s).toLowerCase()));
        const preserve = new Map((opts.selected || []).map(s => [String(s).toLowerCase(), String(s)]));
        let rows = [];
        let query = '';
        let mine = false;                    // "With an owner" filter

        const root = document.createElement('div');
        root.className = 'cp-root';
        root.innerHTML = `
            <div class="tag-browser-backdrop" data-cp-backdrop></div>
            <div class="tag-browser-modal cp-modal" role="dialog" aria-label="Characters">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">Characters</div>
                        <button type="button" class="tag-browser-close" data-cp-close aria-label="Close">&times;</button>
                    </div>
                    <input type="search" id="cp-search" class="tag-browser-search" placeholder="Search characters…" autocomplete="off">
                    <div class="tag-browser-filters">
                        <button type="button" class="tag-browser-chip tag-browser-chip-active" data-cp-filter="all"><span class="tag-browser-chip-label">All</span> <span class="tag-browser-chip-count" data-cp-count="all"></span></button>
                        <button type="button" class="tag-browser-chip" data-cp-filter="selected"><span class="tag-browser-chip-label">Selected</span> <span class="tag-browser-chip-count" data-cp-count="selected"></span></button>
                        <button type="button" class="tag-browser-chip" data-cp-filter="owned"><span class="tag-browser-chip-label">Has an owner</span> <span class="tag-browser-chip-count" data-cp-count="owned"></span></button>
                    </div>
                </div>
                <div class="tag-browser-selected" id="cp-selected"></div>
                <div class="tag-browser-body">
                    <div class="tag-browser-grid" id="cp-grid"><div class="tag-browser-empty">Loading…</div></div>
                </div>
                <div class="tag-browser-footer">
                    <div class="tag-browser-count" id="cp-count">Selected: 0</div>
                    <button type="button" class="btn btn-primary" id="cp-confirm">Done</button>
                </div>
            </div>`;
        document.body.appendChild(root);
        requestAnimationFrame(() => root.querySelector('.tag-browser-modal')?.classList.add('open'));

        const grid = root.querySelector('#cp-grid');
        const selectedEl = root.querySelector('#cp-selected');
        const countEl = root.querySelector('#cp-count');
        const searchEl = root.querySelector('#cp-search');
        let filter = 'all';

        const close = () => {
            root.querySelector('.tag-browser-modal')?.classList.remove('open');
            document.removeEventListener('keydown', onKey);
            setTimeout(() => root.remove(), 180);
        };
        function onKey(e) {
            if (e.key === 'Escape') close();
            if (e.key === 'Enter' && document.activeElement === searchEl) addTyped();
        }
        document.addEventListener('keydown', onKey);
        root.querySelector('[data-cp-backdrop]').addEventListener('click', close);
        root.querySelector('[data-cp-close]').addEventListener('click', close);

        const nameOf = (lower) => {
            const row = rows.find(r => r.name.toLowerCase() === lower);
            return row ? row.name : (preserve.get(lower) || lower);
        };

        const visible = () => rows.filter(r => {
            if (query && !(r.name.toLowerCase().includes(query)
                || (r.booru_tag || '').toLowerCase().includes(query)
                || (r.species || '').toLowerCase().includes(query))) return false;
            if (filter === 'selected') return selected.has(r.name.toLowerCase());
            if (filter === 'owned') return !!r.owner;
            return true;
        });

        function draw() {
            const list = visible();
            const typed = searchEl.value.trim();
            const exact = rows.some(r => r.name.toLowerCase() === typed.toLowerCase());
            const addCard = (typed && !exact)
                ? `<button type="button" class="tag-browser-card cp-add" data-cp-add>
                     <span class="tag-browser-card-name">+ Add “${esc(typed)}”</span>
                     <span class="tag-browser-card-desc">A new character in the registry</span>
                   </button>`
                : '';
            grid.innerHTML = addCard + (list.length
                ? list.map(r => {
                    const on = selected.has(r.name.toLowerCase());
                    const sub = [r.species, r.owner ? `${esc(r.owner.name)}’s` : '', r.booru_tag]
                        .filter(Boolean).map(esc).join(' · ');
                    return `<button type="button" class="tag-browser-card${on ? ' is-selected' : ''}" data-cp-pick="${esc(r.name)}">
                        <span class="tag-browser-card-name">${esc(r.name)}</span>
                        <span class="tag-browser-card-desc">${sub || 'No owner or tag yet'}</span>
                    </button>`;
                }).join('')
                : (addCard ? '' : '<div class="tag-browser-empty">Nobody here yet — type a name to add one.</div>'));

            selectedEl.innerHTML = [...selected].map(l => `
                <span class="tag-browser-pill">${esc(nameOf(l))}
                    <button type="button" data-cp-un="${esc(l)}" aria-label="Remove">&times;</button>
                </span>`).join('') || '<span class="muted" style="font-size:12px">Nobody selected</span>';
            countEl.textContent = `Selected: ${selected.size}`;
            root.querySelector('[data-cp-count="all"]').textContent = rows.length;
            root.querySelector('[data-cp-count="selected"]').textContent = selected.size;
            root.querySelector('[data-cp-count="owned"]').textContent = rows.filter(r => r.owner).length;
        }

        async function addTyped() {
            const typed = searchEl.value.trim();
            if (!typed) return;
            const existing = rows.find(r => r.name.toLowerCase() === typed.toLowerCase());
            if (!existing) {
                try {
                    const row = await create(typed);
                    rows.push({ ...row, owner: null });
                    rows.sort((a, b) => a.name.localeCompare(b.name));
                } catch (err) {
                    grid.innerHTML = `<div class="tag-browser-empty">${esc(err.message || err)}</div>`;
                    return;
                }
            }
            selected.add(typed.toLowerCase());
            preserve.set(typed.toLowerCase(), typed);
            searchEl.value = '';
            query = '';
            draw();
        }

        grid.addEventListener('click', (e) => {
            if (e.target.closest('[data-cp-add]')) { addTyped(); return; }
            const pick = e.target.closest('[data-cp-pick]');
            if (!pick) return;
            const lower = pick.dataset.cpPick.toLowerCase();
            selected.has(lower) ? selected.delete(lower) : selected.add(lower);
            draw();
        });
        selectedEl.addEventListener('click', (e) => {
            const un = e.target.closest('[data-cp-un]');
            if (!un) return;
            selected.delete(un.dataset.cpUn);
            draw();
        });
        root.querySelectorAll('[data-cp-filter]').forEach(b => b.addEventListener('click', () => {
            filter = b.dataset.cpFilter;
            root.querySelectorAll('[data-cp-filter]').forEach(x =>
                x.classList.toggle('tag-browser-chip-active', x === b));
            draw();
        }));
        let t = null;
        searchEl.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => { query = searchEl.value.trim().toLowerCase(); draw(); }, 120);
        });
        root.querySelector('#cp-confirm').addEventListener('click', () => {
            // Registry order, so the same cast always reads the same way on a piece.
            const out = rows.filter(r => selected.has(r.name.toLowerCase())).map(r => r.name);
            for (const l of selected) {
                if (!rows.some(r => r.name.toLowerCase() === l)) out.push(preserve.get(l) || l);
            }
            close();
            if (opts.onConfirm) opts.onConfirm(out);
        });

        load().then(data => { rows = data; draw(); searchEl.focus(); })
            .catch(err => { grid.innerHTML = `<div class="tag-browser-empty">${esc(err.message || err)}</div>`; });
    }

    window.CharacterPicker = { open };
})();
