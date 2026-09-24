/* ── Item prev/next — the back-bar every item page shares (4.34.3) ────────
 *
 * UNIFORMITEM contract §2, "fixed navigation": every item page carries prev/next
 * over the list you arrived from, arrow-key stepping, and a position counter.
 *
 * Until now this existed on masterpieces and nowhere else (spec §1.3), so every
 * other item type was a dead end: back to the grid, find your place, click the
 * next one. `Masterpieces._renderDetailNav` was the working implementation and
 * the spec said it generalises — this is that generalisation, with the
 * masterpiece-specific parts (its own cache, its junk filter, its route shape)
 * lifted out into the caller's hands.
 *
 * ⚠ Extracted at TWO consumers, not one. The spec is explicit that pulling a
 * frame out before there is a second user is guessing (§3 phase 2); masterpieces
 * plus the story board is the point where the shape is observed rather than
 * predicted. Nothing else is extracted here — this is the navigation only, not
 * the hero or the section scaffold.
 *
 * Usage:
 *     ItemNav.mount({
 *         names: ['A', 'B', 'C'],        // list order, already filtered
 *         current: 'B',
 *         href: n => `#/library/work/${encodeURIComponent(n)}`,
 *         routeTest: h => /^#\/library\/work\//.test(h),
 *     });
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const ItemNav = {
        // Only ever one item page is open, so one slot is the whole state.
        _prev: null,
        _next: null,
        _href: null,
        _routeTest: null,

        /* Render the counter + arrows into the page's existing `.work-back`.
         *
         * Returns true when it mounted. A caller that cannot supply a list (a deep
         * link with nothing cached and a failed fetch) simply does not call this,
         * and the page keeps the plain back link it already had — an absent
         * counter is honest, whereas arrows that cannot step are not. */
        mount({ names, current, href, routeTest, container } = {}) {
            this._reset();
            if (!Array.isArray(names) || !names.length || typeof href !== 'function') return false;
            const idx = names.indexOf(current);
            if (idx === -1) return false;

            this._prev = idx > 0 ? names[idx - 1] : null;
            this._next = idx < names.length - 1 ? names[idx + 1] : null;
            this._href = href;
            this._routeTest = typeof routeTest === 'function' ? routeTest : null;

            const back = container || document.querySelector('.work-back');
            // Re-render guard: the board repaints on save, and a second mount
            // would stack a second set of arrows in the same bar.
            if (!back || back.querySelector('.item-nav')) return false;
            back.classList.add('item-detail-topnav');

            const btn = (n, cls, label, title) => n
                ? `<a class="btn btn-sm ${cls}" href="${esc(href(n))}" title="${title}">${label}</a>`
                : `<span class="btn btn-sm is-disabled" aria-disabled="true">${label}</span>`;
            back.insertAdjacentHTML('beforeend', `
                <span class="item-nav">
                    ${btn(this._prev, 'item-nav-prev', '&lsaquo; Prev', 'Previous (←)')}
                    <span class="item-nav-pos muted">${idx + 1} / ${names.length}</span>
                    ${btn(this._next, 'item-nav-next', 'Next &rsaquo;', 'Next (→)')}
                </span>`);
            return true;
        },

        _reset() {
            this._prev = this._next = this._href = this._routeTest = null;
        },

        /* ←/→ step through the list. The listener is bound once, at the bottom of
         * this file, so a page only has to call mount(). */
        onKey(e) {
            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
            if (!this._href) return;
            // Still on the page that mounted this? A hash change does not clear
            // state on its own, so without this the arrows would keep firing on
            // whatever page came next.
            if (this._routeTest && !this._routeTest(location.hash || '')) return;
            // Never while typing — ←/→ belong to the caret inside a field.
            if (e.target && e.target.closest
                && e.target.closest('input, textarea, select, [contenteditable]')) return;
            const to = e.key === 'ArrowLeft' ? this._prev : this._next;
            if (!to) return;
            e.preventDefault();
            location.hash = this._href(to);
        },
    };

    window.ItemNav = ItemNav;
    document.addEventListener('keydown', (e) => ItemNav.onKey(e));
})();
