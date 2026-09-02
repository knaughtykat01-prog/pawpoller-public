/* TagPicker — reusable tag-library picker modal.
 *
 * Gives any screen the story-editor's tag-library experience WITHOUT touching the
 * editor's tightly-coupled tag browser (metadata_editor.js writes straight into
 * `this.metadata.tags`). Instead this is a standalone picker that reuses the same
 * `.tag-browser-*` modal chrome + the same tag database (`/api/editor/tags`), with
 * category filter chips and multi-select. On confirm it hands back the selected
 * tag names; the caller decides what to do with them.
 *
 * Built for the Art module (artwork.js) but reusable anywhere.
 *
 * Usage:
 *   TagPicker.open({
 *     title: 'Tag library',
 *     selected: ['dragon', 'macro'],          // pre-checked (case-insensitive)
 *     onConfirm: (names) => { ... },           // final selected canonical names
 *   });
 */
(function () {
    const CATS = ['physical', 'acts', 'kink', 'meta', 'image', 'user'];
    let _cache = null;

    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    async function loadTags() {
        if (_cache) return _cache;
        try {
            const raw = sessionStorage.getItem('pawpoller_tag_db_v1');
            if (raw) { const c = JSON.parse(raw); if (c && c.tags) { _cache = c.tags; return _cache; } }
        } catch (_) { /* corrupted cache */ }
        const resp = await fetch('/api/editor/tags');
        if (!resp.ok) throw new Error('Tag database failed to load');
        const data = await resp.json();
        try { sessionStorage.setItem('pawpoller_tag_db_v1', JSON.stringify(data)); } catch (_) { /* quota */ }
        _cache = data.tags || [];
        return _cache;
    }

    function open(opts) {
        opts = opts || {};
        const selected = new Set((opts.selected || []).map(s => String(s).toLowerCase()));
        let tags = [];
        let cat = 'all';
        let query = '';
        let searchTimer = null;

        // Match the story editor's tag browser: an "All" + "Selected" pair ahead
        // of the category chips, each with a live count. (2.123.0)
        const chipKeys = ['all', 'selected', ...CATS];
        const chips = chipKeys.map(k => {
            const label = k === 'all' ? 'All' : k === 'selected' ? 'Selected'
                : esc(k.charAt(0).toUpperCase() + k.slice(1));
            return `<button type="button" class="tag-browser-chip${k === cat ? ' tag-browser-chip-active' : ''}" data-tp-cat="${k}"><span class="tag-browser-chip-label">${label}</span> <span class="tag-browser-chip-count" data-tp-count="${k}"></span></button>`;
        }).join('');

        const root = document.createElement('div');
        root.className = 'tp-root';
        root.innerHTML = `
            <div class="tag-browser-backdrop" data-tp-backdrop></div>
            <div class="tag-browser-modal tp-modal" role="dialog" aria-label="${esc(opts.title || 'Tag library')}">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">${esc(opts.title || 'Tag library')}</div>
                        <button type="button" class="tag-browser-close" data-tp-close aria-label="Close">&times;</button>
                    </div>
                    <input type="search" id="tp-search" class="tag-browser-search" placeholder="Search tags…" autocomplete="off">
                    <div class="tag-browser-filters">${chips}</div>
                </div>
                <div class="tag-browser-selected" id="tp-selected"></div>
                <div class="tag-browser-body">
                    <div class="tag-browser-grid" id="tp-grid"><div class="tag-browser-empty">Loading…</div></div>
                </div>
                <div class="tag-browser-footer">
                    <div class="tag-browser-count" id="tp-count">Selected: 0</div>
                    <button type="button" class="btn btn-primary" id="tp-confirm">Done</button>
                </div>
            </div>`;
        document.body.appendChild(root);
        requestAnimationFrame(() => root.querySelector('.tag-browser-modal')?.classList.add('open'));

        const grid = root.querySelector('#tp-grid');
        const countEl = root.querySelector('#tp-count');
        const searchEl = root.querySelector('#tp-search');
        const selectedEl = root.querySelector('#tp-selected');

        const close = () => {
            root.querySelector('.tag-browser-modal')?.classList.remove('open');
            document.removeEventListener('keydown', onKey);
            setTimeout(() => root.remove(), 180);
        };
        function onKey(e) { if (e.key === 'Escape') close(); }
        document.addEventListener('keydown', onKey);
        root.querySelector('[data-tp-backdrop]').addEventListener('click', close);
        root.querySelector('[data-tp-close]').addEventListener('click', close);

        // Canonical-case name lookup for the selected set (DB names win; freeform
        // pre-selected tags not in the DB are preserved with their original text).
        const byLower = new Map();
        const preserve = new Map((opts.selected || []).map(s => [String(s).toLowerCase(), String(s)]));

        const renderSelected = () => {
            if (!selected.size) { selectedEl.innerHTML = '<span class="tag-browser-selected-empty">No tags selected</span>'; return; }
            const pills = Array.from(selected).map(low => {
                const name = byLower.get(low) || preserve.get(low) || low;
                return `<span class="tag-browser-selected-pill">${esc(name)}<button type="button" class="tag-browser-selected-remove" data-tp-unpick="${esc(low)}" aria-label="Remove">&times;</button></span>`;
            }).join('');
            selectedEl.innerHTML = `<span class="tag-browser-selected-label">Selected</span><span class="tag-browser-selected-pills">${pills}</span>`;
        };
        const updateCount = () => {
            countEl.textContent = `Selected: ${selected.size}`;
            renderSelected();
            updateChipCounts();
            if (cat === 'selected') render();   // keep the Selected filter view in sync
        };

        const visible = () => {
            const q = query.toLowerCase();
            return tags.filter(t =>
                (cat === 'all' ? true
                    : cat === 'selected' ? selected.has(t.name.toLowerCase())
                    : t.category === cat) &&
                (!q || t.name.toLowerCase().includes(q)));
        };

        // Live per-category counts on the filter chips (search-aware), matching the
        // story editor's tag browser.
        const updateChipCounts = () => {
            const q = query.toLowerCase();
            const counts = { all: 0, selected: 0 };
            CATS.forEach(c => { counts[c] = 0; });
            for (const t of tags) {
                if (q && !t.name.toLowerCase().includes(q)) continue;
                counts.all++;
                if (counts[t.category] != null) counts[t.category]++;
                if (selected.has(t.name.toLowerCase())) counts.selected++;
            }
            root.querySelectorAll('[data-tp-count]').forEach(el => {
                const k = el.getAttribute('data-tp-count');
                el.textContent = counts[k] != null ? String(counts[k]) : '';
            });
        };

        // Card layout identical to the story editor's tag browser (reuses the same
        // .tag-browser-card* CSS) so the two browsers match. (2.123.0)
        const render = () => {
            const items = visible().slice(0, 400);
            grid.innerHTML = items.length
                ? items.map(t => {
                    const low = t.name.toLowerCase();
                    const isSel = selected.has(low);
                    const desc = t.desc ? `<div class="tag-browser-card-desc">${esc(t.desc)}</div>` : '';
                    const btnCls = isSel ? 'tag-browser-card-btn tag-browser-card-btn-added' : 'tag-browser-card-btn';
                    const btnLabel = isSel ? '&#10003; Added' : '+ Add';
                    return `<div class="tag-browser-card${isSel ? ' tag-browser-card-added' : ''}" data-tp-name="${esc(t.name)}">
                        <div class="tag-browser-card-head">
                            <div class="tag-browser-card-name">${esc(t.name)}</div>
                            <span class="tag-browser-card-cat metadata-tag-cat-${esc(t.category || '')}">${esc(t.category || '')}</span>
                        </div>
                        ${desc}
                        <div class="tag-browser-card-footer">
                            <button type="button" class="${btnCls}" data-tp-toggle="${esc(t.name)}">${btnLabel}</button>
                        </div>
                    </div>`;
                }).join('')
                : '<div class="tag-browser-empty">No tags match.</div>';
            updateChipCounts();
        };

        const _setCardState = (card, isSel) => {
            const btn = card.querySelector('[data-tp-toggle]');
            card.classList.toggle('tag-browser-card-added', isSel);
            if (btn) {
                btn.className = isSel ? 'tag-browser-card-btn tag-browser-card-btn-added' : 'tag-browser-card-btn';
                btn.innerHTML = isSel ? '&#10003; Added' : '+ Add';
            }
        };
        grid.addEventListener('click', (e) => {
            const card = e.target.closest('.tag-browser-card');
            if (!card) return;
            const name = card.getAttribute('data-tp-name');
            const low = name.toLowerCase();
            byLower.set(low, name);
            const nowSel = !selected.has(low);
            if (nowSel) selected.add(low); else selected.delete(low);
            _setCardState(card, nowSel);
            updateCount();
        });
        selectedEl.addEventListener('click', (e) => {
            const rm = e.target.closest('[data-tp-unpick]');
            if (!rm) return;
            const low = rm.getAttribute('data-tp-unpick');
            selected.delete(low);
            const card = grid.querySelector(`.tag-browser-card[data-tp-name="${CSS.escape(byLower.get(low) || low)}"]`);
            if (card) _setCardState(card, false);
            updateCount();
        });
        root.querySelectorAll('[data-tp-cat]').forEach(btn => btn.addEventListener('click', () => {
            cat = btn.getAttribute('data-tp-cat');
            root.querySelectorAll('[data-tp-cat]').forEach(b => b.classList.toggle('tag-browser-chip-active', b === btn));
            render();
        }));
        searchEl.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => { query = searchEl.value.trim(); render(); }, 200);
        });

        root.querySelector('#tp-confirm').addEventListener('click', () => {
            const names = Array.from(selected).map(low => byLower.get(low) || preserve.get(low) || low);
            if (opts.onConfirm) opts.onConfirm(names);
            close();
        });

        (async () => {
            try { tags = await loadTags(); tags.forEach(t => byLower.set(t.name.toLowerCase(), t.name)); render(); }
            catch (err) { grid.innerHTML = `<div class="tag-browser-empty">${esc(err.message || err)}</div>`; }
        })();
        updateCount();
        setTimeout(() => searchEl.focus(), 60);
        return { close };
    }

    window.TagPicker = { open };
})();
