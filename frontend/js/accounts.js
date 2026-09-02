/* ── Accounts page (multi-account registry) ──────────────────────
 *
 * Manages multiple accounts per platform. Each platform's *default* account
 * (badge "default") owns the legacy flat credentials and the pre-multi-account
 * history; additional accounts are added here and store their credentials under
 * namespaced keys server-side. Renders into #app and is dispatched from the SPA
 * router on #/accounts.
 */
window.Accounts = {

    _meta: null,   // { platform_names, platform_fields } from the last fetch

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header"><h2>Accounts</h2></div>
            <p class="acct-intro muted">Run more than one account per platform. Each platform's
            default account keeps your existing credentials; add extra accounts below. Group accounts
            across platforms into a <strong>persona</strong> for scoped views and per-persona digests.</p>
            <div class="acct-page">
                <section id="personas-card" class="acct-section">Loading…</section>
                <section id="accounts-add" class="acct-section"></section>
                <div id="accounts-list">Loading…</div>
                <section id="fa-polling-card" class="acct-section"></section>
                <p class="logo-disclaimer">Platform names and logos are trademarks of their respective
                owners, shown only to identify each service. PawPoller is not affiliated with them.</p>
            </div>`;

        this._renderFaPollingToggle(document.getElementById('fa-polling-card'));
        await this._refresh();
    },

    /* Re-fetch accounts + personas and re-fill the data sub-sections IN PLACE.
     * Unlike render() (which rebuilds the whole #app shell and so drops the
     * scroll position back to the top), this leaves the page structure
     * untouched — so calling it after a mutation (assign persona, rename,
     * toggle, delete, add) refreshes the list without the jarring
     * jump-to-top. (2.51.6) */
    async _refresh() {
        let data, personas;
        try {
            [data, personas] = await Promise.all([
                API.getAccounts(),
                API.getPersonas().catch(() => ({ personas: [] })),
            ]);
        } catch (err) {
            const list = document.getElementById('accounts-list');
            if (list) list.innerHTML =
                `<section class="acct-section">Failed to load accounts: ${this.esc(err.message)}</section>`;
            return;
        }
        this._meta = data;
        this._personas = (personas && personas.personas) || [];
        this._renderPersonasCard(document.getElementById('personas-card'));
        this._renderAddForm(document.getElementById('accounts-add'), data);
        this._renderList(document.getElementById('accounts-list'), data);
    },

    _renderPersonasCard(el) {
        if (!el) return;
        const rows = (this._personas || []).map(p => {
            const n = (p.accounts || []).length;
            return `
            <div class="persona-row">
                <span class="persona-dot" style="background:${this.esc(p.color || 'var(--accent)')}"></span>
                <a class="persona-name" href="#/persona/${p.persona_id}" title="Open persona overview">${this.esc(p.name)}</a>
                <span class="persona-meta">${n} account${n === 1 ? '' : 's'}</span>
                <span class="acct-stats">${this._statChips(p.stats && p.stats.combined)}</span>
                <span class="spacer"></span>
                <span class="acct-actions">
                    <a class="btn btn-sm" href="#/persona/${p.persona_id}">Overview</a>
                    <button class="btn btn-sm" data-persona-rename="${p.persona_id}" data-name="${this.esc(p.name)}">Rename</button>
                    <button class="btn btn-sm btn-danger" data-persona-delete="${p.persona_id}">Delete</button>
                </span>
            </div>`;
        }).join('');
        el.innerHTML = `
            <h3>Personas</h3>
            <p class="acct-section-sub">A persona bundles accounts across platforms into one identity.
            Assign accounts to a persona in the list below.</p>
            ${rows ? `<div class="persona-list">${rows}</div>` : '<p class="muted">No personas yet.</p>'}
            <div class="acct-form" style="margin-top:14px;">
                <label class="acct-field"><span>New persona</span>
                    <input class="acct-input" id="persona-name" type="text" placeholder="e.g. SecondFur"></label>
                <label class="acct-field"><span>Colour</span>
                    <input class="acct-color" id="persona-color" type="color" value="#9a5b34"></label>
                <button id="persona-create-btn" class="btn btn-primary">Create persona</button>
                <span id="persona-msg" class="muted"></span>
            </div>`;
        el.querySelector('#persona-create-btn').addEventListener('click', () => this._createPersona(el));
        el.querySelectorAll('[data-persona-delete]').forEach(btn =>
            btn.addEventListener('click', () => this._deletePersona(btn.dataset.personaDelete)));
        el.querySelectorAll('[data-persona-rename]').forEach(btn =>
            btn.addEventListener('click', () => this._renamePersona(btn.dataset.personaRename, btn.dataset.name)));
    },

    async _createPersona(el) {
        const name = el.querySelector('#persona-name').value.trim();
        const color = el.querySelector('#persona-color').value || '#6c8cff';
        const msg = el.querySelector('#persona-msg');
        if (!name) { msg.textContent = 'Enter a name.'; return; }
        msg.textContent = 'Creating…';
        try {
            await API.createPersona({ name, color });
            this._refresh();
        } catch (err) { msg.textContent = 'Error: ' + err.message; }
    },

    async _renamePersona(id, current) {
        const name = prompt('Rename persona', current || '');
        if (name == null || !name.trim()) return;
        try {
            await API.updatePersona(id, { name: name.trim() });
            this._refresh();
        } catch (err) { alert('Failed to rename: ' + err.message); }
    },

    async _deletePersona(id) {
        if (!confirm('Delete this persona? Its accounts will be unassigned (not deleted).')) return;
        try {
            await API.deletePersona(id);
            this._refresh();
        } catch (err) { alert('Failed to delete persona: ' + err.message); }
    },

    _personaSelect(a) {
        const opts = ['<option value="">Unassigned</option>'].concat(
            (this._personas || []).map(p =>
                `<option value="${p.persona_id}"${a.persona_id === p.persona_id ? ' selected' : ''}>${this.esc(p.name)}</option>`)
        ).join('');
        return `<select class="acct-select sm persona-assign" data-account="${a.account_id}">${opts}</select>`;
    },

    async _assignPersona(accountId, value) {
        try {
            await API.assignAccountPersona(accountId, value === '' ? null : Number(value));
            this._refresh();
        } catch (err) { alert('Failed to assign persona: ' + err.message); }
    },

    async _renderFaPollingToggle(el) {
        if (!el || !window.API) return;
        // Render synchronously (the toggle state fills in after the fetch) so
        // this is never a blank card while preferences load.
        el.innerHTML = `
            <h3>FurAffinity polling</h3>
            <p class="acct-section-sub">FAExport (the proxy PawPoller normally uses for FA stats) is
            blocked by Cloudflare. Enable this to scrape FA directly with your cookies instead.
            <strong>Only works from the desktop app</strong> — FA blocks the datacenter server's IP.</p>
            <label class="acct-setting-row">
                <span class="toggle-switch"><input type="checkbox" id="fa-direct-toggle"><span class="toggle-slider"></span></span>
                <span>Poll FurAffinity directly (bypass FAExport)</span>
            </label>`;
        const cb = el.querySelector('#fa-direct-toggle');
        try {
            const prefs = await API.getPreferences();
            cb.checked = !!prefs.fa_direct_polling;
        } catch (e) { /* default off */ }
        cb.addEventListener('change', async () => {
            try {
                await API.savePreferences({ fa_direct_polling: cb.checked });
            } catch (err) {
                alert('Failed to save: ' + err.message);
                cb.checked = !cb.checked;
            }
        });
    },

    _renderAddForm(el, data) {
        const names = data.platform_names || {};
        const options = Object.keys(names).map(p =>
            `<option value="${p}">${this.esc(names[p])}</option>`).join('');
        el.innerHTML = `
            <h3>Add account</h3>
            <p class="acct-section-sub">Pick a platform, give the account a label, and enter its
            credentials. The first account on a platform becomes its default.</p>
            <div class="acct-form">
                <label class="acct-field"><span>Platform</span>
                    <select class="acct-select" id="acct-platform">${options}</select></label>
                <label class="acct-field"><span>Label</span>
                    <input class="acct-input" id="acct-label" type="text" placeholder="e.g. Alt account"></label>
            </div>
            <div id="acct-cred-fields" class="acct-form" style="margin-top:12px;"></div>
            <div class="acct-form" style="margin-top:14px;">
                <button id="acct-create-btn" class="btn btn-primary">Create account</button>
                <span id="acct-create-msg" class="muted"></span>
            </div>`;

        const platformSel = el.querySelector('#acct-platform');
        const renderFields = () => this._renderCredFields(
            el.querySelector('#acct-cred-fields'), platformSel.value, data);
        platformSel.addEventListener('change', renderFields);
        renderFields();

        el.querySelector('#acct-create-btn').addEventListener('click', () => this._create(el));
    },

    _renderCredFields(el, platform, data) {
        const fields = (data.platform_fields || {})[platform] || [];
        el.innerHTML = fields.map(f =>
            `<label class="acct-field"><span>${this.esc(this._prettyField(platform, f.field))}</span>
                <input class="acct-input acct-cred" data-field="${this.esc(f.field)}"
                       type="${f.secret ? 'password' : 'text'}" autocomplete="off"></label>`
        ).join('') || '<span class="muted">No credential fields for this platform.</span>';
    },

    /* Turn a canonical field name into a human label: drop the platform prefix
     * and the underscores (e.g. "tw_auth_token" → "auth token"). */
    _prettyField(platform, field) {
        let f = String(field || '');
        if (f.startsWith(platform + '_')) f = f.slice(platform.length + 1);
        return f.replace(/_/g, ' ');
    },

    async _create(el) {
        const platform = el.querySelector('#acct-platform').value;
        const label = el.querySelector('#acct-label').value.trim();
        const credentials = {};
        el.querySelectorAll('.acct-cred').forEach(inp => {
            if (inp.value) credentials[inp.dataset.field] = inp.value;
        });
        const msg = el.querySelector('#acct-create-msg');
        msg.textContent = 'Creating…';
        try {
            await API.createAccount({ platform, label, credentials });
            msg.textContent = '';
            this._refresh();   // re-fill the list in place (keeps scroll position)
        } catch (err) {
            msg.textContent = 'Error: ' + err.message;
        }
    },

    _renderList(el, data) {
        const accounts = data.accounts || [];
        const names = data.platform_names || {};
        if (!accounts.length) {
            el.innerHTML = '<section class="acct-section"><p class="muted">No accounts configured yet.</p></section>';
            return;
        }
        // Group by platform.
        const byPlatform = {};
        accounts.forEach(a => { (byPlatform[a.platform] ||= []).push(a); });

        el.innerHTML = Object.keys(byPlatform).map(platform => {
            const meta = (window.platformByCode && window.platformByCode(platform)) || null;
            const color = meta ? meta.color : 'var(--accent)';
            const emoji = meta ? meta.emoji : '';
            const logo = meta ? meta.logo : '';
            const label = (meta && meta.label) || names[platform] || platform;
            const icon = logo
                ? `<span class="plat-logo"><img src="${logo}" alt="${this.esc(label)} logo" loading="lazy"></span>`
                : (emoji ? `<span class="plat-emoji">${emoji}</span>` : '');
            const list = byPlatform[platform];
            const rows = list.map(a => this._accountRow(a, color)).join('');
            return `<div class="acct-plat-card" style="--pc:${color}">
                        <div class="acct-plat-head">
                            ${icon}
                            <span class="plat-name">${this.esc(label)}</span>
                            <span class="plat-count">${list.length} account${list.length === 1 ? '' : 's'}</span>
                        </div>
                        ${rows}
                    </div>`;
        }).join('');

        // Enabled/disabled is a toggle switch now — listen for change, not click.
        el.querySelectorAll('[data-toggle]').forEach(cb =>
            cb.addEventListener('change', () => this._toggle(cb.dataset.toggle, cb.dataset.enabled === '1')));
        el.querySelectorAll('[data-delete]').forEach(btn =>
            btn.addEventListener('click', () => this._delete(btn.dataset.delete)));
        this._wireDaAuthorise(el);
        el.querySelectorAll('[data-creds]').forEach(btn =>
            btn.addEventListener('click', () => this._editCredentials(
                btn.dataset.creds, btn.dataset.platform, btn)));
        el.querySelectorAll('[data-test-login]').forEach(btn =>
            btn.addEventListener('click', () => this._testLogin(btn.dataset.testLogin)));
        el.querySelectorAll('[data-rename]').forEach(btn =>
            btn.addEventListener('click', () => this._renameAccount(btn.dataset.rename, btn.dataset.label)));
        el.querySelectorAll('[data-view-acct]').forEach(btn =>
            btn.addEventListener('click', () => this._viewAccount(btn.dataset.viewAcct, btn.dataset.plat)));
        el.querySelectorAll('.persona-assign').forEach(sel =>
            sel.addEventListener('change', () => this._assignPersona(sel.dataset.account, sel.value)));
    },

    _fmt(n) {
        n = Number(n) || 0;
        if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
        if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
        return String(n);
    },

    /* Per-platform noun for the submissions count — X has "tweets", not "subs". */
    _unit(platform) {
        return {
            tw: 'tweets', bsky: 'posts', mast: 'posts', tum: 'posts', thr: 'posts', ig: 'posts', ik: 'posts', da: 'deviations',
            pix: 'works', ao3: 'works', sqw: 'works', wp: 'stories', e621: 'posts',
            ib: 'submissions', fa: 'submissions', ws: 'submissions', sf: 'submissions',
        }[platform] || 'subs';
    },

    _statsCell(s, platform) {
        if (!s) return '<span class="muted">—</span>';
        return `${this._fmt(s.submissions)} ${this._unit(platform)} · ${this._fmt(s.views)} views · `
             + `${this._fmt(s.favorites)} faves · ${this._fmt(s.comments)} comments`;
    },

    /* Stat chips for the account/persona rows. With platform + accountId, the
     * count chip becomes a link that opens that platform's submissions list
     * scoped to the account — so you can pull up the actual tweets/posts. */
    _statChips(s, platform, accountId, followerCount) {
        if (!s) return '<span class="muted">No data yet</span>';
        const unit = this._unit(platform);
        const count = this._fmt(s.submissions);
        const subs = (platform && accountId)
            ? `<button class="acct-stat acct-stat-link" data-view-acct="${accountId}" data-plat="${this.esc(platform)}" title="View ${unit}"><b>${count}</b> ${unit} →</button>`
            : `<span class="acct-stat"><b>${count}</b> ${unit}</span>`;
        // Follower count only populates for platforms whose poller records one
        // (the 8 in database.followers.FOLLOWER_PLATFORMS); stays 0 → chip hidden
        // for the rest, so this is safe to render unconditionally.
        const followers = (followerCount != null && followerCount > 0)
            ? `<span class="acct-stat"><b>${this._fmt(followerCount)}</b> followers</span>`
            : '';
        return subs
             + `<span class="acct-stat"><b>${this._fmt(s.views)}</b> views</span>`
             + `<span class="acct-stat"><b>${this._fmt(s.favorites)}</b> faves</span>`
             + `<span class="acct-stat"><b>${this._fmt(s.comments)}</b> comments</span>`
             + followers;
    },

    /* Open a platform's submissions list scoped to one account. */
    _viewAccount(accountId, platform) {
        App._accountFilter = App._accountFilter || {};
        App._accountFilter[platform] = Number(accountId);
        window.location.hash = window.platformRoute
            ? window.platformRoute(platform, 'submissions')
            : '#/' + platform;
    },

    _accountRow(a, color) {
        const badge = a.is_default
            ? '<span class="badge badge-default" title="Owns the legacy credentials and history">default</span>' : '';
        const del = a.is_default ? ''
            : `<button class="btn btn-sm btn-danger" data-delete="${a.account_id}">Delete</button>`;
        const toggle = `<label class="toggle-switch" title="${a.enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}">
                <input type="checkbox" data-toggle="${a.account_id}" data-enabled="${a.enabled ? 1 : 0}" ${a.enabled ? 'checked' : ''}>
                <span class="toggle-slider"></span></label>`;
        const rename = `<button class="btn btn-sm" data-rename="${a.account_id}" data-label="${this.esc(a.label || '')}">Rename</button>`;
        /* Per-account credential re-entry (3.20.0). Until this existed, the
           only visible place to paste a renewed cookie/token was the main
           per-platform credentials form — which writes the DEFAULT account's
           keys. Renewing a non-default account's expired FA cookies there
           looked like it worked and changed nothing for the account that
           needed it; that is a mistake a real user made, not a hypothetical. */
        const creds = `<button class="btn btn-sm" data-creds="${a.account_id}" data-platform="${a.platform}"
                       title="Paste renewed cookies or tokens for THIS account">🔑
                       Credentials</button>`;
        const test = `<button class="btn btn-sm" data-test-login="${a.account_id}"
                      title="Check whether this account's stored login still works">Test</button>
                      <span class="acct-test-status" data-test-status="${a.account_id}"></span>`;
        // DeviantArt posting needs an authorization-code token, which cannot be
        // typed into the credential boxes above because it does not exist until
        // the user approves it in a browser. Every DA account needs its own —
        // the token authorises posting AS that account, so one cannot stand in
        // for another. Status is filled in after render (see _wireDaAuthorise).
        const daAuth = a.platform === 'da'
            ? `<button class="btn btn-sm" data-da-authorise="${a.account_id}"
                       title="DeviantArt must be approved in a browser before this account can post">
                 Authorise posting</button>
               <span class="acct-da-status" data-da-status="${a.account_id}"></span>`
            : '';
        return `<div class="acct-card${a.enabled ? '' : ' disabled'}" style="--pc:${color || 'var(--accent)'}">
            <div class="acct-id">
                <span class="acct-name">${this.esc(a.label || '(unnamed)')} ${badge}</span>
                ${a.handle ? `<span class="acct-handle">${this.esc(a.handle)}</span>` : ''}
            </div>
            <span class="acct-stats">${this._statChips(a.stats, a.platform, a.account_id, a.follower_count)}</span>
            <span class="acct-actions">
                <span class="persona-wrap"><span>Persona</span>${this._personaSelect(a)}</span>
                ${toggle}
                ${rename}
                ${creds}
                ${test}
                ${daAuth}
                ${del}
            </span>
        </div>`;
    },

    /* DeviantArt posting authorisation, per account.
     *
     * Reading a DA account's stats only needs the registered app; posting AS
     * that account needs an authorization-code token that DeviantArt will only
     * issue after a human approves it in a browser. So there is nothing to type
     * into the credential fields, and each account needs its own approval — a
     * token authorises posting as one specific account and cannot stand in for
     * another.
     *
     * Status is fetched per row after render rather than served with the account
     * list, because it is a settings lookup the accounts endpoint does not do
     * and this keeps the two independent. */
    /* ── Per-account credential re-entry (3.20.0) ──
     *
     * Empty fields mean "leave unchanged" — the backend persists only the
     * fields provided — so renewing ONE expired cookie does not force
     * re-typing everything else. Secrets are write-only: nothing prefills. */
    _editCredentials(accountId, platform, btn) {
        const row = btn.closest('.acct-card');
        if (!row) return;
        // The panel used to open with no indication of which account it
        // belonged to — it just appeared under a row, headed "this account".
        // With several accounts on one platform that is a coin flip you cannot
        // check, and a mis-aimed paste looks identical to a working one.
        const acctName = (row.querySelector('.acct-name')?.textContent || '').trim()
            || ('account ' + accountId);
        const existing = row.nextElementSibling;
        if (existing && existing.classList.contains('acct-cred-editor')) {
            existing.remove();                       // toggle closed
            return;
        }
        document.querySelectorAll('.acct-cred-editor').forEach(e => e.remove());

        const fields = ((this._meta || {}).platform_fields || {})[platform] || [];
        const panel = document.createElement('div');
        panel.className = 'acct-cred-editor acct-form';
        panel.style.cssText = 'margin:6px 0 12px;padding:12px;border:1px solid var(--border);border-radius:8px';
        panel.innerHTML = `
            <p style="font-size:13px;margin:0 0 4px">
                Credentials for <strong>${this.esc(acctName)}</strong>
                <span class="muted">(#${this.esc(String(accountId))})</span>
            </p>
            <p class="muted" style="font-size:12px;margin:0 0 8px">
                Fields left empty keep their current value — paste only what
                changed. For FurAffinity, copy cookies <code>a</code> and
                <code>b</code> from a browser signed in
                <strong>as ${this.esc(acctName)}</strong>: FurAffinity keeps one
                session per browser, so cookies copied while signed in as
                someone else are a valid login for the wrong account.
            </p>
            ${fields.map(f => `
                <label style="display:block;margin-bottom:6px">
                    <span style="font-size:12px">${this.esc(f.field)}</span>
                    <input type="${f.secret ? 'password' : 'text'}" data-cred-field="${this.esc(f.field)}"
                           placeholder="unchanged" autocomplete="off" class="search-input" style="width:100%">
                </label>`).join('')
              || '<span class="muted">No credential fields for this platform.</span>'}
            <div style="display:flex;gap:8px;margin-top:8px">
                <button class="btn btn-sm btn-primary" data-cred-save>Save</button>
                <button class="btn btn-sm" data-cred-cancel>Cancel</button>
                <span class="acct-cred-msg muted" style="font-size:12px"></span>
            </div>`;
        row.after(panel);

        panel.querySelector('[data-cred-cancel]').addEventListener('click', () => panel.remove());
        panel.querySelector('[data-cred-save]').addEventListener('click', async () => {
            const credentials = {};
            panel.querySelectorAll('[data-cred-field]').forEach(inp => {
                if (inp.value) credentials[inp.dataset.credField] = inp.value;
            });
            const msg = panel.querySelector('.acct-cred-msg');
            if (!Object.keys(credentials).length) {
                msg.textContent = 'Nothing to save — every field is empty.';
                return;
            }
            msg.textContent = 'Saving…';
            const fields = Object.keys(credentials).join(', ');
            try {
                await API.updateAccount(accountId, { credentials });
                msg.textContent = 'Saved ✓ — testing login…';
                // Prove the paste worked while the person is still looking.
                const t = await API.testAccountLogin(accountId).catch(() => null);

                // On success the panel CLOSES and the row carries the result.
                // Leaving it open with a one-line muted note was read as
                // "nothing happened" — the same three renewals were pasted
                // three times because nothing on screen changed.
                if (t && t.status === 'ok') {
                    this._setTestStatus(accountId, '✓ logged in' +
                        (t.username ? ' as ' + t.username : ''), 'var(--success)');
                    this._flashSaved(row, acctName, fields);
                    panel.remove();
                    return;
                }
                // Anything else keeps the panel open — the values are stored,
                // but they do not work yet and re-pasting is the next step.
                if (t && t.status === 'wrong_account') {
                    msg.style.color = 'var(--danger)';
                    msg.textContent = 'Saved, but ' + (t.detail || 'these cookies belong to another account.');
                    this._setTestStatus(accountId,
                        '✗ signed in as ' + (t.username || 'someone else'), 'var(--danger)');
                } else if (t && t.status === 'invalid') {
                    msg.style.color = 'var(--danger)';
                    msg.textContent = 'Saved, but the login test FAILED: ' + (t.detail || '');
                    this._setTestStatus(accountId, '✗ login failed', 'var(--danger)');
                } else {
                    // No test for this platform — the save itself is the news.
                    this._flashSaved(row, acctName, fields);
                    panel.remove();
                }
            } catch (err) {
                msg.style.color = 'var(--danger)';
                msg.textContent = 'Save failed: ' + (err.message || err);
            }
        });
    },

    /* Put a result on the account's row, where it survives the panel closing. */
    _setTestStatus(accountId, text, colour) {
        const out = document.querySelector(`[data-test-status="${accountId}"]`);
        if (!out) return;
        out.textContent = text;
        out.style.color = colour || '';
    },

    /* A confirmation that outlives the form it came from.
     *
     * Naming the account and the fields matters more than it looks: the report
     * that prompted this was "I put three lots of new cookies in and it didn't
     * change them over", from someone watching a form that gave no sign it had
     * done anything. Saying "cookies a and b saved for <account>" is the
     * difference between believing it and pasting again. */
    _flashSaved(row, acctName, fields) {
        if (window.toast && window.toast.success) {
            window.toast.success('Saved ' + (fields || 'credentials') + ' for ' + acctName);
        }
        if (!row) return;
        const prev = row.querySelector('.acct-saved-flash');
        if (prev) prev.remove();
        const flash = document.createElement('span');
        flash.className = 'acct-saved-flash';
        flash.textContent = '✓ saved';
        flash.style.cssText = 'color:var(--success);font-size:12px;margin-left:8px';
        (row.querySelector('.acct-name') || row).append(flash);
        setTimeout(() => flash.remove(), 6000);
    },

    async _testLogin(accountId) {
        const out = document.querySelector(`[data-test-status="${accountId}"]`);
        if (out) out.textContent = '…';
        try {
            const r = await API.testAccountLogin(accountId);
            if (!out) return;
            if (r.status === 'ok') {
                out.textContent = '✓ logged in' + (r.username ? ' as ' + r.username : '');
                out.style.color = 'var(--success)';
            }
            // "Logged in, but as somebody else" is a different problem from
            // "logged in" and from "expired", and only naming it stops the
            // fix being "paste the same cookies again".
            else if (r.status === 'wrong_account') {
                out.textContent = '✗ signed in as ' + (r.username || 'another account');
                out.style.color = 'var(--danger)';
                out.title = r.detail || '';
            }
            else if (r.status === 'invalid') { out.textContent = '✗ ' + (r.detail || 'login expired'); out.style.color = 'var(--danger)'; }
            else { out.textContent = r.detail || r.status; out.style.color = 'var(--text-muted)'; }
        } catch (err) {
            if (out) { out.textContent = '✗ ' + (err.message || err); out.style.color = 'var(--danger)'; }
        }
    },

    _wireDaAuthorise(el) {
        el.querySelectorAll('[data-da-status]').forEach(async span => {
            const id = span.dataset.daStatus;
            try {
                const s = await API.getDAPostingStatus(id);
                if (!s.has_app) {
                    span.textContent = 'no app credentials';
                    span.className = 'acct-da-status muted';
                } else if (s.has_refresh_token) {
                    span.textContent = 'posting authorised';
                    span.className = 'acct-da-status ok';
                } else {
                    span.textContent = 'not authorised';
                    span.className = 'acct-da-status warn';
                }
            } catch {
                span.textContent = '';
            }
        });

        el.querySelectorAll('[data-da-authorise]').forEach(btn =>
            btn.addEventListener('click', async () => {
                const id = btn.dataset.daAuthorise;
                btn.disabled = true;
                try {
                    const info = await API.getDAAuthorizeUrl(id);
                    // Shown every time, not only on failure: DeviantArt's
                    // redirect_uri mismatch appears on ITS page, so by the time
                    // it is wrong the user has already left this one.
                    alert('Approve in the tab that opens. If DeviantArt refuses, this exact '
                        + 'address must be in the app\'s OAuth2 Redirect URI Whitelist: '
                        + info.redirect_uri);
                    window.open(info.url, '_blank', 'noopener');
                } catch (err) {
                    let detail = err.message.replace(/^API \d+:\s*/, '');
                    try { detail = JSON.parse(detail).detail || detail; } catch {}
                    alert('Could not start DeviantArt authorisation: ' + detail);
                } finally {
                    btn.disabled = false;
                }
            }));
    },

    async _renameAccount(id, current) {
        const label = prompt('Account label', current || '');
        if (label == null) return;          // cancelled
        const trimmed = label.trim();
        if (!trimmed) return;               // don't blank the label
        try {
            await API.updateAccount(id, { label: trimmed });
            this._refresh();
        } catch (err) {
            alert('Failed to rename account: ' + err.message);
        }
    },

    /* renderPersonaDetail(id) — the per-persona overview page (#/persona/:id):
     * combined scalar totals + a per-platform breakdown + the member accounts,
     * each linking through to that platform's dashboard scoped to the account. */
    async renderPersonaDetail(id) {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header"><h1>Persona</h1></div>
            <div id="persona-detail">Loading…</div>`;
        let resp;
        try {
            resp = await API.getPersona(id);
        } catch (err) {
            document.getElementById('persona-detail').innerHTML =
                `<div class="card error">Failed to load persona: ${this.esc(err.message)}</div>`;
            return;
        }
        const p = resp.persona;
        if (!p) {
            document.getElementById('persona-detail').innerHTML =
                '<div class="card muted">Persona not found. <a href="#/accounts">Back to Accounts</a></div>';
            return;
        }
        const names = resp.platform_names || {};
        const combined = (p.stats && p.stats.combined) || {};
        const byPlat = (p.stats && p.stats.by_platform) || {};
        const accts = p.accounts || [];

        const swatch = `<span style="display:inline-block;width:16px;height:16px;border-radius:4px;`
            + `background:${this.esc(p.color || '#6c8cff')};vertical-align:middle;margin-right:.5rem;"></span>`;

        const cards = [
            Components.statCard('Submissions', combined.submissions || 0),
            Components.statCard('Views', combined.views || 0),
            Components.statCard('Favorites', combined.favorites || 0),
            Components.statCard('Comments', combined.comments || 0),
        ].join('');

        const platRows = Object.keys(byPlat).map(plat => {
            const s = byPlat[plat] || {};
            return `<tr>
                <td><strong>${this.esc(names[plat] || plat)}</strong></td>
                <td class="muted">${this._fmt(s.submissions)} subs</td>
                <td class="muted">${this._fmt(s.views)} views</td>
                <td class="muted">${this._fmt(s.favorites)} faves</td>
                <td class="muted">${this._fmt(s.comments)} comments</td>
            </tr>`;
        }).join('');

        const acctRows = accts.map(a => `
            <tr>
                <td><strong>${this.esc(a.label || '(unnamed)')}</strong></td>
                <td class="muted">${this.esc(names[a.platform] || a.platform)}</td>
                <td class="muted">${this.esc(a.handle || '')}</td>
                <td class="muted">${this._statsCell(a.stats, a.platform)}</td>
                <td style="text-align:right;">
                    <button class="btn btn-sm" data-view-acct="${a.account_id}" data-plat="${this.esc(a.platform)}">View →</button>
                </td>
            </tr>`).join('');

        document.getElementById('persona-detail').innerHTML = `
            <p style="margin:.2rem 0 .8rem;"><a href="#/accounts">← Accounts</a></p>
            <div class="card" style="margin-bottom:1rem;">
                <h2 style="margin:.2rem 0;">${swatch}${this.esc(p.name)}</h2>
                <p class="muted">${accts.length} account(s) across ${Object.keys(byPlat).length} platform(s) with data</p>
            </div>
            <div class="stats-grid" style="margin-bottom:1rem;">${cards}</div>
            <div class="card" style="margin-bottom:1rem;">
                <h3>Per-platform breakdown</h3>
                ${platRows ? `<table class="data-table"><tbody>${platRows}</tbody></table>`
                           : '<p class="muted">No platform data polled yet.</p>'}
            </div>
            <div class="card">
                <h3>Accounts in this persona</h3>
                ${acctRows ? `<table class="data-table"><tbody>${acctRows}</tbody></table>`
                           : '<p class="muted">No accounts assigned. Assign some on the <a href="#/accounts">Accounts</a> page.</p>'}
            </div>
            <div class="card" style="margin-top:1rem;">
                <h3>Posting defaults <span class="muted" style="font-weight:400;font-size:.8rem">— synced, used by ⚡ Quick Publish</span></h3>
                <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:.5rem 0;" id="pdef-plats">
                    ${(window.PLATFORMS || []).map(pl => `
                        <label style="font-size:12px;white-space:nowrap;cursor:pointer">
                            <input type="checkbox" class="pdef-plat" value="${this.esc(pl.code)}"
                                ${(p.default_platforms || '').split(',').includes(pl.code) ? 'checked' : ''}>
                            ${pl.emoji || ''} ${this.esc(pl.label)}</label>`).join('')}
                </div>
                <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
                    <label style="font-size:12px">Rating
                        <select id="pdef-rating">
                            <option value="" ${!p.default_rating ? 'selected' : ''}>(no default)</option>
                            ${['general', 'mature', 'adult'].map(r =>
                                `<option value="${r}" ${p.default_rating === r ? 'selected' : ''}>${r}</option>`).join('')}
                        </select></label>
                    <label style="font-size:12px">Preferred posting time
                        <input type="time" id="pdef-time" value="${this.esc(p.preferred_post_time || '')}"></label>
                    <button class="btn btn-sm btn-primary" id="pdef-save">Save defaults</button>
                    <span id="pdef-msg" class="muted" style="font-size:12px"></span>
                </div>
            </div>`;

        // Posting defaults save (gap-wave-3 §1).
        document.getElementById('pdef-save')?.addEventListener('click', async () => {
            const msg = document.getElementById('pdef-msg');
            const plats = Array.from(document.querySelectorAll('.pdef-plat:checked')).map(c => c.value);
            try {
                await API.updatePersona(id, {
                    default_platforms: plats.join(','),
                    default_rating: document.getElementById('pdef-rating')?.value || '',
                    preferred_post_time: document.getElementById('pdef-time')?.value || '',
                });
                if (msg) { msg.textContent = 'Saved.'; msg.style.color = 'var(--success)'; }
            } catch (err) {
                if (msg) { msg.textContent = 'Failed: ' + (err.message || err); msg.style.color = 'var(--danger)'; }
            }
        });

        // "View →" opens the platform dashboard pre-scoped to that account.
        document.querySelectorAll('[data-view-acct]').forEach(btn =>
            btn.addEventListener('click', () => {
                const aid = Number(btn.dataset.viewAcct);
                const plat = btn.dataset.plat;
                App._accountFilter = App._accountFilter || {};
                App._accountFilter[plat] = aid;
                window.location.hash = (window.platformRoute ? window.platformRoute(plat) : '#/' + plat);
            }));
    },

    async _toggle(accountId, currentlyEnabled) {
        try {
            await API.updateAccount(accountId, { enabled: !currentlyEnabled });
            this._refresh();
        } catch (err) {
            alert('Failed to update account: ' + err.message);
        }
    },

    async _delete(accountId) {
        if (!confirm('Delete this account? Its credentials will be removed. Polled history is left in place.')) return;
        try {
            await API.deleteAccount(accountId);
            this._refresh();
        } catch (err) {
            alert('Failed to delete account: ' + err.message);
        }
    },
};
