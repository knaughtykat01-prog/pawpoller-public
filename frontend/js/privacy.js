/*
 * Settings → Privacy (4.44.0, spec 011 — data classification).
 *
 * What this install holds, grouped by how sensitive it is, with how each kind is
 * protected, where it can go and how to remove it. Everything comes from
 * GET /api/privacy/holdings, which is generated from datamap.py — so a newly
 * classified kind of data appears here with no change to this file.
 *
 * Counts only, never values: the API sends none, and this page shows none.
 * Written for people who are not IT experts; the technical names sit under
 * "Details" for the curious, and for self-hosters keeping their own records.
 */
(function () {
    'use strict';

    const esc = (s) => (window.Utils && Utils.escapeHtml ? Utils.escapeHtml(String(s ?? '')) : String(s ?? ''));
    const num = (n) => Number(n || 0).toLocaleString('en-AU');
    const UNIT = { table: ['record', 'records'], setting: ['setting', 'settings'], path: ['file', 'files'] };

    function amount(items) {
        const by = { table: 0, setting: 0, path: 0 };
        items.forEach(i => { by[i.kind] = (by[i.kind] || 0) + (i.count || 0); });
        const parts = Object.keys(UNIT).filter(k => by[k]).map(k => `${num(by[k])} ${UNIT[k][by[k] === 1 ? 0 : 1]}`);
        return parts.length ? parts.join(' · ') : 'nothing yet';
    }

    function itemLine(i) {
        const what = i.count == null ? 'could not be counted'
            : i.kind === 'setting' ? 'set'
            : `${num(i.count)} ${UNIT[i.kind][i.count === 1 ? 0 : 1]}`;
        return `<li><code>${esc(i.name)}</code> — ${esc(what)}</li>`;
    }

    function group(g) {
        return `
            <div class="privacy-group">
                <div class="privacy-group-head"><strong>${esc(g.group)}</strong><span class="privacy-amount">${esc(amount(g.items))}</span></div>
                <p>${esc(g.about)}</p>
                <p class="privacy-meta"><span>Where it can go:</span> ${esc(g.goes_to)}</p>
                <p class="privacy-meta"><span>To remove it:</span> ${esc(g.remove)}</p>
                <details class="privacy-details"><summary>Details</summary><ul>${g.items.map(itemLine).join('')}</ul></details>
            </div>`;
    }

    function cls(c) {
        const body = c.groups.length
            ? c.groups.map(group).join('')
            : '<p class="privacy-empty">Nothing of this kind is held on this install.</p>';
        return `
            <section class="settings-section privacy-class privacy-${esc(c.cls)}" data-cls="${esc(c.cls)}">
                <h3>${esc(c.label)}</h3>
                <p class="privacy-meaning">${esc(c.meaning)}</p>
                ${body}
                <details class="privacy-handling"><summary>How ${esc(c.label.toLowerCase())} data is handled</summary>
                    <dl>${c.handling.map(h => `<dt>${esc(h.situation)}</dt><dd>${esc(h.rule)}</dd>`).join('')}</dl>
                </details>
            </section>`;
    }

    function unknown(list) {
        if (!list || !list.length) return '';
        return `
            <section class="settings-section privacy-unknown" role="status">
                <h3>${list.length === 1 ? 'One thing' : esc(num(list.length)) + ' things'} this install holds ${list.length === 1 ? "isn't" : "aren't"} in the list yet</h3>
                <p>They are still protected the usual way, but nobody has decided how sensitive they are.
                   Please tell the developer the names below, so they get a class in the next release.</p>
                <ul>${list.map(u => `<li><code>${esc(u.name)}</code> (${esc(u.kind)})</li>`).join('')}</ul>
            </section>`;
    }

    const Privacy = {
        /** Pure: the page's HTML for one /api/privacy/holdings answer. */
        render(data) {
            const rules = (data.row_rules || []).map(r =>
                `<li><strong>${esc(r.rule)}</strong>: ${esc(r.why)}</li>`).join('');
            return `
                <div class="privacy-page">
                    <p class="privacy-intro">Everything PawPoller keeps falls into one of four kinds, from most to least
                       sensitive. Each kind has fixed rules for where it's stored, whether it can appear in a log,
                       and where it's allowed to go. Numbers only — nothing on this page shows the data itself.</p>
                    ${unknown(data.unclassified)}
                    ${(data.classes || []).map(cls).join('')}
                    ${rules ? `<section class="settings-section privacy-rules"><h3>Special cases</h3><ul>${rules}</ul></section>` : ''}
                </div>`;
        },

        async mount(section) {
            if (!section) return;
            let root = section.querySelector('.privacy-mount');
            if (!root) {
                root = document.createElement('div');
                root.className = 'privacy-mount';
                section.appendChild(root);
            }
            root.innerHTML = '<p class="privacy-loading">Counting what this install holds…</p>';
            try {
                root.innerHTML = this.render(await API.get('/api/privacy/holdings'));
            } catch (e) {
                root.innerHTML = `<p class="privacy-error">Couldn't load this page: ${esc(e.message)}</p>`;
            }
        },
    };

    if (typeof window !== 'undefined') window.Privacy = Privacy;
})();
