// @ts-check
/* Saved descriptions and tag groups (spec 060 US3).
 *
 * - A button with data-saved-desc="<textarea id>" opens a list of saved descriptions and inserts
 *   the chosen one at the cursor.
 * - A button with data-tag-group="<textarea id>" adds a group's tags to that comma list, without
 *   duplicates. The piece page's tag box is a hidden textarea under chips, so it is redrawn after.
 * - Settings → Saved text: rename, edit and delete both (mountSettings).
 * Stored server side (/api/library/*), so every device sees the same lists.
 */
(function () {
    const esc = (v) => Utils.escapeHtml(String(v ?? ''));
    const PATH = { descriptions: '/api/library/saved-descriptions', groups: '/api/library/tag-groups' };

    function closeMenu() { document.querySelector('.saved-pick')?.remove(); }

    async function openMenu(btn, kind) {
        closeMenu();
        const target = document.getElementById(kind === 'descriptions' ? btn.dataset.savedDesc : btn.dataset.tagGroup);
        if (!target) return;
        let items = [];
        try { items = (await API.get(PATH[kind])).items || []; } catch (e) { /* shown as empty below */ }
        const menu = document.createElement('div');
        menu.className = 'saved-pick';
        menu.setAttribute('role', 'menu');
        menu.innerHTML = items.length
            ? items.map((it, i) => `<button type="button" role="menuitem" data-i="${i}">${esc(it.name)}${kind === 'groups' ? ` <span class="muted">(${it.tags.length})</span>` : ''}</button>`).join('')
            : `<p class="muted">None saved yet. Add some in <a href="#/settings/savedtext">Settings → Saved text</a>.</p>`;
        btn.insertAdjacentElement('afterend', menu);
        menu.querySelector('button, a')?.focus();
        menu.addEventListener('click', (e) => {
            const b = e.target.closest('button[data-i]');
            if (e.target.closest('a')) { closeMenu(); return; }
            if (!b) return;
            const it = items[+b.dataset.i];
            if (kind === 'descriptions') {
                const s = target.selectionStart ?? target.value.length, end = target.selectionEnd ?? s;
                target.value = target.value.slice(0, s) + it.text + target.value.slice(end);
                target.selectionStart = target.selectionEnd = s + it.text.length;
            } else {
                const have = target.value.split(',').map(x => x.trim()).filter(Boolean);
                const lower = new Set(have.map(x => x.toLowerCase()));
                it.tags.forEach(t => { if (!lower.has(t.toLowerCase())) { lower.add(t.toLowerCase()); have.push(t); } });
                target.value = have.join(', ');
                if (target.id === 'mp-e-tags') window.Masterpieces?._tagChips();
            }
            target.dispatchEvent(new Event('input', { bubbles: true }));   // drafts and dirty checks see it
            closeMenu();
            target.focus();
        });
    }

    document.addEventListener('click', (e) => {
        const d = e.target.closest('[data-saved-desc]'), g = e.target.closest('[data-tag-group]');
        if (d || g) { e.preventDefault(); openMenu(d || g, d ? 'descriptions' : 'groups'); return; }
        if (!e.target.closest('.saved-pick')) closeMenu();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });

    async function mountSettings(section) {
        if (!section) return;
        let root = section.querySelector('.saved-text-settings');
        if (!root) { root = document.createElement('div'); root.className = 'saved-text-settings'; section.appendChild(root); }
        const draft = {};
        for (const kind of Object.keys(PATH)) {
            try { draft[kind] = (await API.get(PATH[kind])).items || []; } catch (e) { draft[kind] = []; }
        }
        render(root, draft);
    }

    function render(root, draft) {
        const block = (kind, title, hint, field) => `
            <div class="settings-block" data-kind="${kind}">
                <h4>${title}</h4>
                <p class="muted">${hint}</p>
                ${draft[kind].map((it, i) => `<div class="pp-ctpl" data-i="${i}">
                    <label><span>Name</span><input type="text" maxlength="80" value="${esc(it.name)}" data-st-name></label>
                    ${field(it)}
                    <button type="button" class="btn btn-xs btn-outline" data-st-del aria-label="Delete ${esc(it.name)}">Delete</button>
                </div>`).join('') || '<p class="muted">None yet.</p>'}
                <button type="button" class="btn btn-sm btn-outline" data-st-add>+ Add one</button>
                <div class="pp-csave"><button type="button" class="btn btn-primary" data-st-save>Save</button>
                    <span class="muted" data-st-msg role="status"></span></div>
            </div>`;
        root.innerHTML =
            block('descriptions', 'Saved descriptions', 'Text you use often. On a piece, press <strong>Saved description</strong> by the description box to drop one in.',
                  it => `<label><span>Text</span><textarea rows="3" maxlength="20000" data-st-text>${esc(it.text)}</textarea></label>`)
            + block('groups', 'Tag groups', 'Tags that go together. On a piece, press <strong>Tag group</strong> by the tags box to add them all; ones already there are skipped.',
                  it => `<label><span>Tags (comma-separated)</span><textarea rows="2" data-st-tags>${esc(it.tags.join(', '))}</textarea></label>`);
        root.querySelectorAll('.settings-block[data-kind]').forEach(box => {
            const kind = box.dataset.kind, list = draft[kind];
            const sync = () => box.querySelectorAll('.pp-ctpl').forEach(el => {
                const it = list[+el.dataset.i];
                it.name = el.querySelector('[data-st-name]').value;
                if (kind === 'descriptions') it.text = el.querySelector('[data-st-text]').value;
                else it.tags = el.querySelector('[data-st-tags]').value.split(',').map(x => x.trim()).filter(Boolean);
            });
            box.querySelector('[data-st-add]').addEventListener('click', () => {
                sync(); list.push(kind === 'descriptions' ? { name: '', text: '' } : { name: '', tags: [] }); render(root, draft);
            });
            box.querySelectorAll('[data-st-del]').forEach(b => b.addEventListener('click', () => {
                sync(); list.splice(+b.closest('.pp-ctpl').dataset.i, 1); render(root, draft);
            }));
            box.querySelector('[data-st-save]').addEventListener('click', async () => {
                sync();
                const msg = box.querySelector('[data-st-msg]');
                try {
                    draft[kind] = (await API.put(PATH[kind], { items: list })).items || [];
                    render(root, draft);
                    root.querySelector(`[data-kind="${kind}"] [data-st-msg]`).textContent = 'Saved.';
                } catch (e) { msg.textContent = e.message; }
            });
        });
    }

    window.SavedText = { mountSettings, openMenu };
})();
