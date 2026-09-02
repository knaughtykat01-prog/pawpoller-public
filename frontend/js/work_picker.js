/* WorkPicker — reusable visual picker modal for selecting works / submissions.
 *
 * Built to look and behave like the story-editor tag browser (metadata_editor.js):
 * it reuses the `.tag-browser-*` modal chrome (backdrop, sticky header with search
 * + filter chips, selected strip, footer) so it feels native, but the grid holds
 * VISUAL cards (thumbnail + title + badge) instead of tag chips.
 *
 * Replaces the old title-only scroll lists + `prompt()` selectors, and scales to
 * thousands of works via server-side search (`/api/works?search=&type=`).
 * Selection survives re-searches (kept in a Map keyed by member_ref).
 *
 * Usage:
 *   WorkPicker.open({
 *     title: 'Add to collection',
 *     confirmLabel: 'Add selected',
 *     multi: true,                              // default true
 *     onConfirm: async (items) => { ... },      // items: [{member_type, member_ref, title, badge, thumb}]
 *   });
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // filter → {type for /api/works, includeDiscovered}
    const FILTERS = {
        all:         { label: 'All',          type: 'all',     disc: true },
        story:       { label: 'Stories',      type: 'story',   disc: false },
        artwork:     { label: 'Artwork',      type: 'artwork', disc: false },
        // Masterpieces on their own chip — NOT folded into 'all', since they share
        // the same folders as artwork works and would double-list.
        masterpiece: { label: 'Masterpieces', type: null,      disc: false, mp: true },
        discovered:  { label: 'Discovered',   type: null,      disc: true },
    };

    async function fetchItems(query, filterKey) {
        const f = FILTERS[filterKey] || FILTERS.all;
        const items = [];
        if (f.type) {
            const r = await API.getWorks({ search: query || '', type: f.type }).catch(() => ({ works: [] }));
            (r.works || []).forEach(w => items.push({
                member_type: 'work',
                member_ref: `${w.content_type}:${w.name}`,
                title: w.title || w.name,
                badge: w.content_type,
                thumb: w.thumb_url || '',
            }));
        }
        if (f.disc) {
            const r = await API.getDiscovered().catch(() => ({ discovered: [] }));
            const q = (query || '').toLowerCase();
            (r.discovered || []).forEach(d => {
                const title = d.title || String(d.submission_id);
                if (q && !title.toLowerCase().includes(q)) return;
                items.push({
                    member_type: 'submission',
                    member_ref: `${d.platform}:${d.submission_id}`,
                    title,
                    badge: d.platform,
                    thumb: d.thumbnail_url || '',
                });
            });
        }
        if (f.mp) {
            // Masterpieces as pickable Collection members (member_ref = bare name;
            // member_type disambiguates, no colon prefix). Cover = canonical image.
            const r = await API.getMasterpieces().catch(() => ({ masterpieces: [] }));
            const q = (query || '').toLowerCase();
            (r.masterpieces || []).forEach(m => {
                const title = m.title || m.name;
                if (q && !(title.toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q))) return;
                const thumb = m.image
                    ? `/api/artwork/image?name=${encodeURIComponent(m.name)}&file=${encodeURIComponent(m.image)}`
                    : ((m.summary && m.summary.cover_thumb) || '');
                items.push({
                    member_type: 'masterpiece',
                    member_ref: m.name,
                    title,
                    badge: 'masterpiece',
                    thumb,
                });
            });
        }
        return items;
    }

    function cardHtml(it, selected) {
        const thumb = it.thumb
            ? `<img class="wp-thumb" src="${esc(it.thumb)}" alt="" loading="lazy">`
            : `<div class="wp-thumb wp-thumb--none">${esc(it.badge)}</div>`;
        return `<button type="button" class="wp-card${selected ? ' is-selected' : ''}" data-ref="${esc(it.member_ref)}">
            ${thumb}
            <span class="wp-badge">${esc(it.badge)}</span>
            <span class="wp-title" title="${esc(it.title)}">${esc(it.title)}</span>
            <span class="wp-check" aria-hidden="true">✓</span>
        </button>`;
    }

    function open(opts) {
        opts = opts || {};
        const multi = opts.multi !== false;
        const confirmLabel = opts.confirmLabel || (multi ? 'Add selected' : 'Select');
        const selected = new Map();   // member_ref -> item
        let byRef = new Map();        // member_ref -> item (current results)
        // Restrict which type chips show (2.162.0): a caller like "link this tweet
        // to a work" wants stories/artwork/masterpieces but NOT other tweets. An
        // unknown key is dropped; empty/absent = every filter (the old behaviour).
        // A single allowed filter hides the chip row entirely — nothing to switch.
        const allowed = (Array.isArray(opts.filters) && opts.filters.length)
            ? opts.filters.filter(k => FILTERS[k]) : Object.keys(FILTERS);
        let filterKey = allowed[0] || 'all';
        let searchTimer = null;

        const chips = allowed.length > 1 ? allowed.map(k =>
            `<button type="button" class="tag-browser-chip${k === filterKey ? ' tag-browser-chip-active' : ''}" data-wp-filter="${k}">
                <span class="tag-browser-chip-label">${esc(FILTERS[k].label)}</span></button>`).join('') : '';

        const root = document.createElement('div');
        root.className = 'wp-root';
        root.innerHTML = `
            <div class="tag-browser-backdrop" data-wp-backdrop></div>
            <div class="tag-browser-modal wp-modal" role="dialog" aria-label="${esc(opts.title || 'Select works')}">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">${esc(opts.title || 'Select works')}</div>
                        <button type="button" class="tag-browser-close" data-wp-close aria-label="Close">&times;</button>
                    </div>
                    <input type="search" id="wp-search" class="tag-browser-search" placeholder="Search by title…" autocomplete="off">
                    <div class="tag-browser-filters">${chips}</div>
                </div>
                <div class="tag-browser-selected" id="wp-selected"></div>
                <div class="tag-browser-body">
                    <div class="wp-grid" id="wp-grid"><div class="tag-browser-empty">Loading…</div></div>
                </div>
                <div class="tag-browser-footer">
                    <div class="tag-browser-count" id="wp-count">0 selected</div>
                    <button type="button" class="btn btn-primary" id="wp-confirm" disabled>${esc(confirmLabel)}</button>
                </div>
            </div>`;
        document.body.appendChild(root);
        requestAnimationFrame(() => root.querySelector('.tag-browser-modal')?.classList.add('open'));

        const grid = root.querySelector('#wp-grid');
        const countEl = root.querySelector('#wp-count');
        const confirmBtn = root.querySelector('#wp-confirm');
        const searchEl = root.querySelector('#wp-search');
        const selectedEl = root.querySelector('#wp-selected');

        const close = () => {
            root.querySelector('.tag-browser-modal')?.classList.remove('open');
            document.removeEventListener('keydown', onKey);
            setTimeout(() => root.remove(), 180);
        };
        function onKey(e) { if (e.key === 'Escape') close(); }
        document.addEventListener('keydown', onKey);
        root.querySelector('[data-wp-backdrop]').addEventListener('click', close);
        root.querySelector('[data-wp-close]').addEventListener('click', close);

        const renderSelected = () => {
            if (!selected.size) { selectedEl.innerHTML = '<span class="tag-browser-selected-empty">Nothing selected yet</span>'; return; }
            const pills = Array.from(selected.values()).map(it =>
                `<span class="tag-browser-selected-pill">${esc(it.title)}<button type="button" class="tag-browser-selected-remove" data-wp-unpick="${esc(it.member_ref)}" aria-label="Remove">&times;</button></span>`).join('');
            selectedEl.innerHTML = `<span class="tag-browser-selected-label">Selected</span><span class="tag-browser-selected-pills">${pills}</span>`;
        };

        const updateCount = () => {
            countEl.textContent = `${selected.size} selected`;
            confirmBtn.disabled = selected.size === 0;
            renderSelected();
        };

        const render = (items) => {
            byRef = new Map(items.map(it => [it.member_ref, it]));
            grid.innerHTML = items.length
                ? items.map(it => cardHtml(it, selected.has(it.member_ref))).join('')
                : '<div class="tag-browser-empty">Nothing matches.</div>';
        };

        const load = async () => {
            grid.innerHTML = '<div class="tag-browser-empty">Loading…</div>';
            try { render(await fetchItems(searchEl.value.trim(), filterKey)); }
            catch (err) { grid.innerHTML = `<div class="tag-browser-empty">Failed to load: ${esc(err.message || err)}</div>`; }
        };

        const toggle = (ref) => {
            const it = byRef.get(ref) || selected.get(ref);
            if (!it) return;
            if (selected.has(ref)) selected.delete(ref);
            else {
                if (!multi) selected.clear();
                selected.set(ref, it);
            }
            grid.querySelectorAll('.wp-card').forEach(c => c.classList.toggle('is-selected', selected.has(c.dataset.ref)));
            updateCount();
        };

        grid.addEventListener('click', (e) => {
            const card = e.target.closest('.wp-card');
            if (card) toggle(card.dataset.ref);
        });
        selectedEl.addEventListener('click', (e) => {
            const rm = e.target.closest('[data-wp-unpick]');
            if (rm) toggle(rm.getAttribute('data-wp-unpick'));
        });
        root.querySelectorAll('[data-wp-filter]').forEach(btn => btn.addEventListener('click', () => {
            filterKey = btn.getAttribute('data-wp-filter');
            root.querySelectorAll('[data-wp-filter]').forEach(b => b.classList.toggle('tag-browser-chip-active', b === btn));
            load();
        }));
        searchEl.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(load, 250); });

        confirmBtn.addEventListener('click', async () => {
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Working…';
            try {
                if (opts.onConfirm) await opts.onConfirm(Array.from(selected.values()));
                close();
            } catch (err) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = confirmLabel;
                if (window.toast) window.toast.error(err.message || String(err));
            }
        });

        updateCount();
        load();
        setTimeout(() => searchEl.focus(), 60);
        return { close };
    }

    window.WorkPicker = { open };
})();
