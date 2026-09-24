/* ── The item frame — one shape for every item page (4.34.4) ──────────────
 *
 * UNIFORMITEM phase 2. The back-bar left first, as `item_nav.js` in 4.34.3; this
 * is the rest of the shell: the hero wrapper, the headline stat row and the
 * section head.
 *
 * ⚠ What this is NOT. It does not render an item — it renders the SHAPE around
 * one. The hero's tile is a cover, an image, a mosaic or a thumbnail depending on
 * the type, and that stays with the type: the ask was explicitly "keeping the way
 * the individual story looks". So every helper here takes finished HTML for the
 * parts that differ and owns only the scaffolding that should not.
 *
 * Why now: four item pages exist (story board, masterpiece, collection,
 * commission) and the headline row had been hand-written three times over — with
 * the labels already drifting ("Platforms" on one, "Sites" on another, and the
 * story board counting Chapters/Reads where the others count Views/Favourites).
 * That drift is the thing the spec's fixed vocabulary exists to stop, and it is
 * what makes the extraction honest rather than speculative (§3: doing it at one
 * consumer is guessing).
 *
 * The CSS already agreed — `.board-hero`, `.board-hero-tile`, `.board-stats` and
 * `.sec-title` all live in board.css and both board pages used them. Only the
 * markup that produced them was duplicated.
 */
(function () {
    const esc = (s) => (window.Utils && Utils.escapeHtml)
        ? Utils.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    /* Thousands separators, or an em dash when there is nothing to show. A 0 is a
     * real measurement and prints as 0; null/undefined means "not known", which is
     * a different statement and must not read as zero. */
    const fmt = (n) => (n == null || n === '' || (typeof n === 'number' && isNaN(n)))
        ? '—' : Number(n).toLocaleString();

    const ItemFrame = {
        /* The back-bar every item page opens with. `ItemNav.mount()` appends the
         * prev/next arrows to whatever this produces. */
        backBar({ href = '#/library', label = 'Library' } = {}) {
            return `<div class="work-back"><a href="${esc(href)}">&larr; ${esc(label)}</a></div>`;
        },

        /* The headline row: `[{value, label, title?}, …]`.
         *
         * Pass the numbers, not the markup — the point is that every page renders
         * them identically. A slot with nothing to report is omitted by the caller
         * rather than printed as 0 (§2: a type fills the slots it has). */
        statRow(stats) {
            const cells = (stats || []).filter(Boolean).map(s => {
                const t = s.title ? ` title="${esc(s.title)}"` : '';
                return `<div class="n"${t}>${esc(s.raw ? s.value : fmt(s.value))}<small>${esc(s.label)}</small></div>`;
            }).join('');
            return cells ? `<div class="board-stats">${cells}</div>` : '';
        },

        /* The hero wrapper. `tile` and the text bits arrive as finished HTML
         * because they are the type-specific part; everything else is the frame. */
        hero({ tile = '', bg = '', eyebrow = '', title = '', pills = '', stats = '',
               actions = '', extra = '' } = {}) {
            return `
                <div class="board-hero">
                    ${bg}
                    ${tile}
                    <div class="board-hero-mid">
                        ${eyebrow ? `<div class="shelf-eyebrow">${eyebrow}</div>` : ''}
                        ${title ? `<h1 class="board-hero-title">${title}</h1>` : ''}
                        ${pills}
                        ${stats}
                        ${extra}
                    </div>
                    ${actions ? `<div class="board-hero-actions">${actions}</div>` : ''}
                </div>`;
        },

        /* A section head: the heading plus its optional right-hand actions.
         *
         * ⚠ The existing pages' `.sec-title` blocks were NOT rewritten to call
         * this. They already emit exactly this markup against shared CSS, so a
         * mechanical sweep of ~30 call sites would have been churn with real
         * regression risk and no behaviour change. This exists so new surfaces
         * (the post item page) cannot invent a thirty-first spelling. */
        sectionHead({ id = '', title = '', actions = '' } = {}) {
            return `<div class="sec-title"><h2${id ? ` id="${esc(id)}"` : ''}>${esc(title)}</h2>`
                + (actions ? `<div class="acts">${actions}</div>` : '') + `</div>`;
        },

        /* A whole card section. Returns '' for an empty body so a type that has
         * nothing for a slot omits it rather than showing an empty card. */
        section({ id = '', title = '', actions = '', body = '', cls = '' } = {}) {
            if (!body) return '';
            return `<div class="card ${esc(cls)}">`
                + this.sectionHead({ id, title, actions }) + body + `</div>`;
        },

        /* The contract's section vocabulary (§2). Exported so a new page names its
         * sections from one list instead of inventing a synonym — "Locations" and
         * "Platforms" were both this concept before phase 3 renamed them. */
        SECTIONS: {
            record: 'Record',
            publishedTo: 'Published to',
            publishTo: 'Publish to more',
            linked: 'Linked',
            growth: 'Growth',
            related: 'Related',
            history: 'History',
        },

        fmt,
    };

    window.ItemFrame = ItemFrame;
})();
