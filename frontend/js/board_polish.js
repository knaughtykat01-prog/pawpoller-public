/* Board polish — C2 redesign phase 3 (spec docs/specs/c2_detail_redesign.md §8).
 *
 * Shared by the masterpiece board (masterpieces.js) and the story board
 * (story_board.js), which each render their own markup:
 *
 *   chipSort(host, onOrder)  — tag chips drag to reorder (SortableJS, vendored
 *                              4.37.0). Order matters: a site that caps tags keeps
 *                              the FIRST ones. Alt+←/→ on a chip's × moves it for
 *                              keyboard users (§13).
 *   collapse(root, page)     — every card can fold to its title, and the page
 *                              remembers what you folded (per page type, per card),
 *                              including the native <details> cards (Renders, More).
 *
 * CSP-safe: no inline handlers. Storage is a per-viewer convenience, wrapped in
 * try/catch so a blocked localStorage just means nothing is remembered.
 */
window.BoardPolish = {
    _KEY: 'pp.board.collapsed.',

    _load(page) {
        try { return new Set(JSON.parse(localStorage.getItem(this._KEY + page) || '[]')); }
        catch (_) { return new Set(); }
    },

    _save(page, set) {
        try { localStorage.setItem(this._KEY + page, JSON.stringify([...set])); } catch (_) { /* not remembered */ }
    },

    /* The card's stable id: its <h2 id>, or the <details> class for the two
       collapsed-by-default cards. */
    _cardId(card) {
        const h = card.querySelector(':scope > .sec-title h2[id], :scope > summary h2[id]');
        if (h) return h.id;
        if (card.tagName === 'DETAILS') return 'details:' + (card.className || '').split(/\s+/).filter(Boolean).join('.');
        return '';
    },

    collapse(root, page) {
        if (!root) return;
        const folded = this._load(page);
        root.querySelectorAll('.board-col > .card').forEach(card => {
            const id = this._cardId(card);
            if (!id) return;
            if (card.tagName === 'DETAILS') {
                // Remembered as "open" rather than "folded": these start closed.
                const key = 'open:' + id;
                if (folded.has(key)) card.open = true;
                card.addEventListener('toggle', () => {
                    const s = this._load(page);
                    if (card.open) s.add(key); else s.delete(key);
                    this._save(page, s);
                });
                return;
            }
            const title = card.querySelector(':scope > .sec-title');
            if (!title || title.querySelector('.card-fold')) return;
            const h2 = title.querySelector('h2');
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'card-fold';
            if (card.id) btn.setAttribute('aria-controls', card.id);
            const label = (h2 && h2.textContent.trim()) || 'this card';
            const paint = () => {
                const on = card.classList.contains('is-folded');
                btn.setAttribute('aria-expanded', on ? 'false' : 'true');
                btn.setAttribute('aria-label', (on ? 'Show ' : 'Fold ') + label);
                btn.title = on ? 'Show' : 'Fold';
                btn.textContent = on ? '▸' : '▾';
            };
            if (folded.has(id)) card.classList.add('is-folded');
            paint();
            btn.addEventListener('click', () => {
                card.classList.toggle('is-folded');
                const s = this._load(page);
                if (card.classList.contains('is-folded')) s.add(id); else s.delete(id);
                this._save(page, s);
                paint();
            });
            title.insertBefore(btn, title.firstChild);
        });
    },

    /* The chips' current order, read off the DOM after a drag. */
    readChips(host) {
        return Array.from(host.querySelectorAll(':scope > li.tagchip > b')).map(b => b.textContent);
    },

    /* Move one tag by `delta` places (keyboard reorder). Pure — returns a new list. */
    moveTag(list, tag, delta) {
        const out = list.slice();
        const i = out.findIndex(x => x.toLowerCase() === String(tag).toLowerCase());
        const j = i + delta;
        if (i === -1 || j < 0 || j >= out.length) return out;
        [out[i], out[j]] = [out[j], out[i]];
        return out;
    },

    /* Wire drag + Alt+arrow reordering on a chip strip. Safe to call after every
       re-render: the strip element persists (only its children are replaced), so
       Sortable and the key listener attach once. `xAttr` is the ×-button's data
       attribute (e.g. 'data-mp-chip-x') holding the tag. */
    chipSort(host, xAttr, getList, onOrder) {
        if (!host || host.dataset.chipSort) return;
        host.dataset.chipSort = '1';
        if (window.Sortable) {
            window.Sortable.create(host, {
                animation: 120,
                draggable: '.tagchip',
                filter: '.x, .tagchip-slot, .tag-empty',
                preventOnFilter: false,
                delay: 150,
                delayOnTouchOnly: true,
                ghostClass: 'tagchip--ghost',
                onEnd: (ev) => { if (ev.oldIndex !== ev.newIndex) onOrder(this.readChips(host)); },
            });
        }
        host.addEventListener('keydown', (e) => {
            if (!e.altKey || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
            const x = e.target.closest && e.target.closest('[' + xAttr + ']');
            if (!x) return;
            e.preventDefault();
            const tag = x.getAttribute(xAttr);
            const next = this.moveTag(getList(), tag, e.key === 'ArrowLeft' ? -1 : 1);
            onOrder(next);
            // Keep focus on the moved chip, or a keyboard user loses their place (§13).
            const again = Array.from(host.querySelectorAll('[' + xAttr + ']'))
                .find(b => b.getAttribute(xAttr).toLowerCase() === String(tag).toLowerCase());
            if (again) again.focus();
        });
    },
};
