/* ── Posts hub (microblog / "tweet-like" publishing) ─────────────
 *
 * A compose box + feed for short-form posts, parallel to the Stories and
 * Artwork hubs but for microblog platforms. Write once, pick Bluesky / Mastodon
 * (Threads / Tumblr / X land in a later phase), publish to all at once, and see
 * each post's per-platform result in the feed. Renders into #app, dispatched
 * from the SPA router on #/posts.
 */
window.Posts = {

    /* Microblog platforms the module can post to. Bluesky, Mastodon and X post
     * images (up to 4); Threads/Tumblr are text-only for now; Instagram is the
     * opposite — it REQUIRES a photo (no text-only IG post). */
    _PLATFORMS: ['bsky', 'mast', 'thr', 'tum', 'tw', 'ig', 'tg'],
    /* Ticked by default — the rest need their posting creds set up first. */
    _DEFAULT_CHECKED: ['bsky', 'mast'],
    /* Platforms that accept image attachments (the rest keep the "text" badge). */
    _IMAGE_PLATFORMS: ['bsky', 'mast', 'tw', 'ig', 'tg'],
    /* Post-only broadcast targets that aren't in the pollable window.PLATFORMS
     * registry — give _plat() their label/emoji so compose renders them nicely. */
    _POST_ONLY_META: {
        tg: { code: 'tg', label: 'Telegram', emoji: '\u{1F4E3}', color: '#2AABEE' },
    },
    /* Platforms that REQUIRE an image — Instagram has no text-only feed post. */
    _IMAGE_REQUIRED: ['ig'],

    /* Bluesky caps a post at 300 graphemes; Mastodon's default is 500. Warn at
     * the tighter limit so a cross-post to Bluesky won't silently truncate. */
    _SOFT_LIMIT: 300,
    // Per-platform text caps (gap-wave-3 §4) — bsky 300 graphemes, mastodon's
    // default 500. Threads chain on these two platforms only.
    _PLAT_LIMITS: { bsky: 300, mast: 500 },
    _MAX_IMAGES: 4,       // X / Bluesky / Mastodon all cap a post at 4 images

    _pendingFiles: [],    // Files awaiting upload (ordered)
    _previewUrls: [],     // object URLs for the compose previews (index-aligned)

    _contacts: [],           // handle-book (loaded once per render)
    _mentionBindings: {},    // { token: contactId } — @aliases bound in this draft
    _addForToken: null,      // the @token the open "add contact" form will bind

    /* Per-platform handle fields shown in the add-contact form + why each. */
    _MENTION_FIELDS: [
        { code: 'bsky', label: 'Bluesky', key: 'handle_bsky', ph: 'name.bsky.social' },
        { code: 'tw', label: 'X / Twitter', key: 'handle_tw', ph: 'xhandle' },
        { code: 'mast', label: 'Mastodon', key: 'handle_mast', ph: 'user@instance.social' },
        { code: 'thr', label: 'Threads', key: 'handle_thr', ph: 'threadshandle' },
        { code: 'tum', label: 'Tumblr', key: 'handle_tum', ph: 'blogname' },
    ],

    esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },

    _plat(code) {
        return (window.PLATFORMS || []).find(p => p.code === code)
            || this._POST_ONLY_META[code]
            || { code, label: code, emoji: '', color: '#888' };
    },

    _toast(kind, msg) {
        if (window.toast && window.toast[kind]) window.toast[kind](msg);
    },

    /* ── Page: feed (catalogue) ─────────────────────────────────
     * Posts is now a view-only catalogue of what you've published; composing a
     * new post lives under Create → New post (#/posts/new, renderCompose). This
     * is the IA split (2.142.0): Submissions vs Posts, create-actions in Create. */

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Posts</h1>
                    <p class="muted">Your published short-form posts across your microblog accounts.
                    Compose a new one from <a href="#/posts/new">Create → New post</a>.</p>
                </div>
                <div style="flex-shrink:0;display:flex;gap:.5rem;">
                    <a class="btn" href="#/posts/contacts">Tag contacts</a>
                    <a class="btn btn-primary" href="#/posts/new">＋ New post</a>
                </div>
            </div>
            <h2 class="posts-feed-heading">Recent posts</h2>
            <div id="post-feed"><div class="loading-spinner">Loading…</div></div>`;
        await this._loadFeed();
    },

    /* ── Page: compose (Create → New post) ──────────────────────
     * The composer on its own page. After a successful publish it redirects to
     * the feed (#/posts) so the new post is visible. */
    async renderCompose() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>New post</h1>
                <p class="muted"><a href="#/posts">← Back to Posts</a> · Write once, publish to your microblog
                accounts. Bluesky, Mastodon and X post text and images (up to 4); Threads and Tumblr are text-only;
                Instagram needs a photo.</p>
            </div>
            <div id="post-compose"></div>`;

        this._mentionBindings = {};
        this._addForToken = null;
        try {
            const cd = await API.getContacts();
            this._contacts = (cd && cd.contacts) || [];
        } catch (e) { this._contacts = []; }   // tagging is additive — degrade to none

        this._renderCompose(document.getElementById('post-compose'));
        // An image handed over from the Promo Maker ("💬 Send to Posts"), else
        // re-sync previews for anything still pending from an earlier visit.
        if (this._handoffFiles && this._handoffFiles.length) {
            const files = this._handoffFiles;
            this._handoffFiles = null;
            this._addFiles(files);
        } else {
            this._renderPreviews();
        }
    },

    _renderCompose(el) {
        el.innerHTML = `
            <div class="card post-compose">
                <textarea id="post-body" class="post-body" rows="4"
                    placeholder="What's happening?  Tag someone with @alias."></textarea>
                <div id="post-mentions" class="post-mentions" hidden></div>
                <div id="post-contact-form" class="post-contact-form" hidden></div>
                <div id="post-image-preview" class="post-image-preview" hidden></div>
                <div class="post-compose-row">
                    <label class="btn btn-sm">📎 Images
                        <input type="file" id="post-image" accept="image/png,image/jpeg,image/gif,image/webp" hidden multiple>
                    </label>
                    <label class="post-inline">Rating
                        <select id="post-rating">
                            <option value="general" selected>General</option>
                            <option value="mature">Mature</option>
                            <option value="adult">Adult</option>
                        </select>
                    </label>
                    <span id="post-count" class="post-count muted">0/${this._SOFT_LIMIT}</span>
                    <div id="post-parts"></div>
                    <button type="button" class="btn btn-sm btn-outline" id="post-addpart"
                        title="Thread: each part posts as a reply to the previous (Bluesky + Mastodon; other platforms get part 1 only)">🧵 + Add part</button>
                </div>
                <div class="post-platforms" id="post-platforms"></div>
                <div class="post-compose-actions">
                    <button class="btn btn-primary" id="post-submit">Post now</button>
                    <button class="btn btn-outline" id="post-schedule-toggle">🕐 Schedule…</button>
                    <span id="post-msg" class="muted"></span>
                </div>
                <div class="schedule-form" id="post-schedule-form" style="display:none">
                    <div class="schedule-form-inner">
                        <label class="schedule-label" for="post-schedule-datetime">Publish the ticked platforms at:</label>
                        <input type="datetime-local" class="schedule-datetime" id="post-schedule-datetime">
                        <div class="schedule-form-actions">
                            <button class="btn btn-sm btn-primary" id="post-schedule-confirm">Confirm schedule</button>
                            <button class="btn btn-sm btn-outline" id="post-schedule-cancel">Cancel</button>
                        </div>
                    </div>
                </div>
            </div>`;

        this._renderPlatformRows(document.getElementById('post-platforms'));
        this._wireCompose();
        this._populateAccountSelectors();
    },

    _renderPlatformRows(el) {
        el.innerHTML = this._PLATFORMS.map(code => {
            const p = this._plat(code);
            const on = this._DEFAULT_CHECKED.includes(code) ? ' checked' : '';
            let note = '';
            if (this._IMAGE_REQUIRED.includes(code)) {
                note = ' <span class="post-plat-note" title="Instagram requires a photo">photo</span>';
            } else if (!this._IMAGE_PLATFORMS.includes(code)) {
                note = ' <span class="post-plat-note" title="Text-only for now">text</span>';
            }
            return `
            <label class="post-plat" data-platform="${code}">
                <input type="checkbox" class="post-plat-check" value="${code}"${on}>
                <span class="post-plat-emoji">${p.emoji || ''}</span>
                <span>${this.esc(p.label)}</span>${note}
                <span class="post-acct-slot" data-platform="${code}"></span>
            </label>`;
        }).join('');
    },

    async _populateAccountSelectors() {
        for (const code of this._PLATFORMS) {
            const slot = document.querySelector(`.post-acct-slot[data-platform="${code}"]`);
            if (!slot) continue;
            try {
                const data = await API.getAccounts(code);
                const accts = (data.accounts || []).filter(a => a.enabled);
                if (accts.length < 2) continue;   // single account → no picker
                const opts = accts.map(a =>
                    `<option value="${a.account_id}"${a.is_default ? ' selected' : ''}>` +
                    `${this.esc(a.label || a.handle || ('account ' + a.account_id))}</option>`).join('');
                slot.innerHTML = `<select class="post-acct-select" data-platform="${code}">${opts}</select>`;
            } catch (e) { /* default account on any failure */ }
        }
    },

    _wireCompose() {
        // Thread parts (gap-wave-3 §4): text-only parts 2+, each with a counter.
        document.getElementById('post-addpart')?.addEventListener('click', () => {
            const box = document.getElementById('post-parts');
            if (!box) return;
            const n = box.children.length + 2;
            const wrap = document.createElement('div');
            wrap.style.cssText = 'margin:6px 0;display:flex;gap:6px;align-items:flex-start';
            wrap.innerHTML = `<span class="muted" style="font-size:11px;margin-top:8px">${n}.</span>
                <textarea class="search-input post-part-text" rows="2" style="flex:1"
                    placeholder="Part ${n} (posts as a reply — Bluesky/Mastodon)"></textarea>
                <button type="button" class="btn btn-sm btn-outline post-part-del" title="Remove part">×</button>`;
            wrap.querySelector('.post-part-del').addEventListener('click', () => wrap.remove());
            box.appendChild(wrap);
            wrap.querySelector('textarea').focus();
        });
        const body = document.getElementById('post-body');
        const count = document.getElementById('post-count');
        const updateCount = () => {
            const n = [...body.value].length;   // grapheme-ish (code points)
            count.textContent = `${n}/${this._SOFT_LIMIT}`;
            const bskyOn = !!document.querySelector('.post-plat-check[value="bsky"]:checked');
            count.classList.toggle('over', bskyOn && n > this._SOFT_LIMIT);
        };
        body.addEventListener('input', updateCount);
        body.addEventListener('input', () => this._syncMentions());
        document.querySelectorAll('.post-plat-check').forEach(c =>
            c.addEventListener('change', updateCount));

        // Mention panel: a <select> per @alias binds it to a handle-book contact.
        document.getElementById('post-mentions').addEventListener('change', e => {
            const sel = e.target.closest('.post-mention-select');
            if (sel) this._onMentionSelect(sel.dataset.token, sel.value);
        });

        const fileInput = document.getElementById('post-image');
        fileInput.addEventListener('change', () => {
            if (fileInput.files && fileInput.files.length) this._addFiles(fileInput.files);
            fileInput.value = '';   // let the same file be re-picked / more added
        });
        document.getElementById('post-image-preview').addEventListener('click', (e) => {
            const rm = e.target.closest('.post-thumb-remove');
            if (rm) this._removeFileAt(parseInt(rm.dataset.idx, 10));
        });
        document.getElementById('post-submit').addEventListener('click', () => this._submit());

        // Scheduling: toggle the picker, confirm (create + queue), cancel.
        const schedForm = document.getElementById('post-schedule-form');
        const schedInput = document.getElementById('post-schedule-datetime');
        document.getElementById('post-schedule-toggle').addEventListener('click', () => {
            const showing = schedForm.style.display !== 'none';
            schedForm.style.display = showing ? 'none' : '';
            if (!showing && !schedInput.value) schedInput.value = this._defaultScheduleLocal();
        });
        document.getElementById('post-schedule-cancel').addEventListener('click', () => {
            schedForm.style.display = 'none';
        });
        document.getElementById('post-schedule-confirm').addEventListener('click', () => this._submit(schedInput.value));

        this._syncMentions();
    },

    /* datetime-local wants 'YYYY-MM-DDTHH:MM' in LOCAL time; default one hour out. */
    _defaultScheduleLocal() {
        const d = new Date(Date.now() + 60 * 60 * 1000);
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
            `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },

    /* ── @mentions (handle-book) ─────────────────────────────────
     * You type one alias (@luna); each platform needs that person's OWN handle.
     * The panel binds each @alias to a saved contact; the backend expands it per
     * platform at publish (and builds Bluesky's mention facet). Unbound aliases
     * stay plain text. */

    _mentionTokens(text) {
        const out = [], seen = new Set();
        const re = /@(\w+)/g;
        let m;
        while ((m = re.exec(text || ''))) {
            if (!seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
        }
        return out;
    },

    _syncMentions() {
        const body = document.getElementById('post-body');
        const panel = document.getElementById('post-mentions');
        if (!body || !panel) return;
        const tokens = this._mentionTokens(body.value);
        Object.keys(this._mentionBindings).forEach(t => {
            if (!tokens.includes(t)) delete this._mentionBindings[t];
        });
        if (!tokens.length) { panel.hidden = true; panel.innerHTML = ''; return; }
        // First time we see an alias, auto-bind it to an exact-name contact.
        tokens.forEach(t => {
            if (this._mentionBindings[t] === undefined) {
                const c = this._contacts.find(x => (x.name || '').toLowerCase() === t.toLowerCase());
                if (c) this._mentionBindings[t] = c.id;
            }
        });
        panel.hidden = false;
        panel.innerHTML = `
            <div class="post-mentions-head">Tag <span class="muted">— pick who each @ is so every platform
            gets their right handle.</span></div>
            ${tokens.map(t => this._mentionRow(t)).join('')}`;
    },

    _mentionRow(token) {
        const bound = this._mentionBindings[token];
        const opts = [`<option value="">— don't tag —</option>`]
            .concat(this._contacts.map(c =>
                `<option value="${c.id}"${String(bound) === String(c.id) ? ' selected' : ''}>${this.esc(c.name)}</option>`))
            .concat(`<option value="__new">＋ Add new contact…</option>`)
            .join('');
        const c = this._contacts.find(x => String(x.id) === String(bound));
        return `
            <div class="post-mention-row">
                <span class="post-mention-alias">@${this.esc(token)}</span>
                <select class="post-mention-select" data-token="${this.esc(token)}">${opts}</select>
                <span class="post-mention-hint muted">${c ? this._contactHint(c) : ''}</span>
            </div>`;
    },

    _contactHint(c) {
        return this._MENTION_FIELDS
            .filter(f => (c[f.key] || '').trim())
            .map(f => `${this._plat(f.code).emoji || f.label} @${this.esc(c[f.key])}`)
            .join('  ');
    },

    _onMentionSelect(token, value) {
        if (value === '__new') { this._openContactForm(token); return; }
        if (value) this._mentionBindings[token] = parseInt(value, 10);
        else delete this._mentionBindings[token];
        this._syncMentions();
    },

    _openContactForm(token) {
        this._addForToken = token;
        const form = document.getElementById('post-contact-form');
        if (!form) return;
        const rows = this._MENTION_FIELDS.map(f =>
            `<label class="post-cf-field">${this.esc(f.label)}
                <input type="text" class="post-cf-input" data-key="${f.key}" placeholder="${this.esc(f.ph)}">
            </label>`).join('');
        form.hidden = false;
        form.innerHTML = `
            <div class="post-cf-head">New contact for <strong>@${this.esc(token || '')}</strong>
                <span class="muted">— paste each platform's handle (leave blank to skip that one).</span></div>
            <label class="post-cf-field">Name / alias
                <input type="text" class="post-cf-input" data-key="name" value="${this.esc(token || '')}" placeholder="who is this?">
            </label>
            ${rows}
            <div class="post-cf-actions">
                <button type="button" class="btn btn-sm btn-primary" id="post-cf-save">Save contact</button>
                <button type="button" class="btn btn-sm" id="post-cf-cancel">Cancel</button>
                <span class="muted post-cf-msg" id="post-cf-msg"></span>
            </div>`;
        form.querySelector('#post-cf-save').addEventListener('click', () => this._saveContact());
        form.querySelector('#post-cf-cancel').addEventListener('click', () => this._closeContactForm());
        const nameInput = form.querySelector('.post-cf-input[data-key="name"]');
        if (nameInput) { nameInput.focus(); nameInput.select(); }
    },

    async _saveContact() {
        const form = document.getElementById('post-contact-form');
        const msg = form.querySelector('#post-cf-msg');
        const payload = {};
        form.querySelectorAll('.post-cf-input').forEach(inp => { payload[inp.dataset.key] = inp.value.trim(); });
        if (!payload.name) { msg.textContent = 'Give the contact a name.'; return; }
        const save = form.querySelector('#post-cf-save');
        save.disabled = true; msg.textContent = 'Saving…';
        try {
            const r = await API.createContact(payload);
            const contact = r && r.contact;
            if (contact) {
                this._contacts.push(contact);
                if (this._addForToken) this._mentionBindings[this._addForToken] = contact.id;
            }
            this._closeContactForm();
            this._toast('success', 'Contact saved');
        } catch (err) {
            save.disabled = false;
            msg.textContent = 'Save failed: ' + (err.message || err);
        }
    },

    _closeContactForm() {
        const form = document.getElementById('post-contact-form');
        if (form) { form.hidden = true; form.innerHTML = ''; }
        this._addForToken = null;
        this._syncMentions();
    },

    _collectMentions() {
        return this._mentionTokens(document.getElementById('post-body').value)
            .filter(t => this._mentionBindings[t])
            .map(t => ({ token: t, contact_id: this._mentionBindings[t] }));
    },

    _addFiles(fileList) {
        for (const file of Array.from(fileList)) {
            if (this._pendingFiles.length >= this._MAX_IMAGES) {
                this._toast('error', `Up to ${this._MAX_IMAGES} images per post.`);
                break;
            }
            if (!/\.(png|jpe?g|gif|webp)$/i.test(file.name)) {
                this._toast('error', 'Please choose PNG, JPG, GIF or WebP images.');
                continue;
            }
            this._pendingFiles.push(file);
            this._previewUrls.push(URL.createObjectURL(file));
        }
        this._renderPreviews();
    },

    _renderPreviews() {
        const box = document.getElementById('post-image-preview');
        if (!box) return;
        if (!this._pendingFiles.length) {
            box.innerHTML = '';
            box.hidden = true;
            return;
        }
        box.hidden = false;
        box.innerHTML = this._previewUrls.map((url, i) =>
            `<figure class="post-thumb">
                <img src="${url}" alt="attachment preview ${i + 1}">
                <button type="button" class="post-thumb-remove" data-idx="${i}"
                    title="Remove image" aria-label="Remove image">✕</button>
            </figure>`).join('')
            + `<span class="post-thumb-count muted">${this._pendingFiles.length}/${this._MAX_IMAGES}</span>`;
    },

    _removeFileAt(i) {
        if (i < 0 || i >= this._pendingFiles.length) return;
        URL.revokeObjectURL(this._previewUrls[i]);
        this._pendingFiles.splice(i, 1);
        this._previewUrls.splice(i, 1);
        this._renderPreviews();
    },

    _clearFiles() {
        this._previewUrls.forEach(u => URL.revokeObjectURL(u));
        this._pendingFiles = [];
        this._previewUrls = [];
        const fi = document.getElementById('post-image');
        if (fi) fi.value = '';
        this._renderPreviews();
    },

    _selectedPlatforms() {
        return Array.from(document.querySelectorAll('.post-plat-check:checked')).map(c => c.value);
    },

    _accountIds(platforms) {
        const ids = {};
        document.querySelectorAll('.post-acct-select').forEach(sel => {
            if (platforms.includes(sel.dataset.platform)) ids[sel.dataset.platform] = parseInt(sel.value, 10);
        });
        return ids;
    },

    /* Compose + publish. Pass a datetime-local string (from the Schedule picker)
     * to queue it for later instead of posting now. */
    async _submit(scheduledLocal) {
        const msg = document.getElementById('post-msg');
        const body = document.getElementById('post-body').value.trim();
        const rating = document.getElementById('post-rating').value;
        const platforms = this._selectedPlatforms();

        if (!body && !this._pendingFiles.length) { msg.textContent = 'Write something or attach an image.'; return; }
        if (!platforms.length) { msg.textContent = 'Pick at least one platform.'; return; }

        // When scheduling, validate the time before we create anything.
        let scheduledIso = null;
        if (scheduledLocal) {
            const when = new Date(scheduledLocal);
            if (isNaN(when.getTime())) { msg.textContent = 'Invalid date/time.'; return; }
            if (when.getTime() < Date.now()) { msg.textContent = 'Pick a time in the future.'; return; }
            scheduledIso = when.toISOString();   // LOCAL picker → UTC instant
        }

        const btn = document.getElementById('post-submit');
        btn.disabled = true;
        msg.textContent = scheduledIso ? 'Scheduling…' : 'Posting…';

        try {
            const fd = new FormData();
            fd.append('body', body);
            fd.append('rating', rating);
            const mentions = this._collectMentions();
            if (mentions.length) fd.append('mentions', JSON.stringify(mentions));
            const partTexts = Array.from(document.querySelectorAll('.post-part-text'))
                .map(t => t.value.trim()).filter(Boolean);
            if (partTexts.length) fd.append('parts', JSON.stringify(partTexts));
            this._pendingFiles.forEach(f => fd.append('files', f));
            const { post_id } = await API.createPost(fd);

            let fail = 0;
            if (scheduledIso) {
                await API.schedulePost(post_id, {
                    platforms, account_ids: this._accountIds(platforms), scheduled_at: scheduledIso,
                });
                const when = new Date(scheduledIso);
                this._toast('success', `Scheduled for ${when.toLocaleString()}`);
                msg.textContent = '';
                const sf = document.getElementById('post-schedule-form');
                if (sf) sf.style.display = 'none';
            } else {
                const res = await API.publishPost(post_id, {
                    platforms, account_ids: this._accountIds(platforms),
                });
                const ok = res.successes || 0; fail = res.failures || 0;
                this._toast(fail ? 'error' : 'success', `Posted: ${ok} ok, ${fail} failed`);
                if (fail) {
                    const errs = (res.results || []).filter(r => !r.success)
                        .map(r => `${this._plat(r.platform).label}: ${r.error}`).join(' · ');
                    msg.textContent = errs;
                } else {
                    msg.textContent = '';
                }
            }
            // Reset the composer, keep platform selection.
            document.getElementById('post-body').value = '';
            document.getElementById('post-count').textContent = `0/${this._SOFT_LIMIT}`;
            this._mentionBindings = {};
            this._closeContactForm();
            this._syncMentions();
            this._clearFiles();
            // On the standalone composer page (no feed here) a clean success jumps
            // to the feed so the new post is visible; a partial failure stays put
            // so the user can retry the failed platforms. A scheduled post has no
            // feed entry yet, so it always navigates on success.
            if (document.getElementById('post-feed')) {
                await this._loadFeed();
            } else if (!fail) {
                window.location.hash = '#/posts';
            }
        } catch (err) {
            msg.textContent = 'Failed: ' + (err.message || err);
        } finally {
            btn.disabled = false;
        }
    },

    /* ── Feed ───────────────────────────────────────────────── */

    async _loadFeed() {
        const feed = document.getElementById('post-feed');
        if (!feed) return;
        let posts;
        try {
            const data = await API.getPosts();
            posts = (data && data.posts) || [];
        } catch (err) {
            feed.innerHTML = `<div class="card error">Failed to load posts: ${this.esc(err.message)}</div>`;
            return;
        }
        if (!posts.length) {
            feed.innerHTML = `<div class="empty-state"><p class="muted">No posts yet — write your first one above.</p></div>`;
            return;
        }
        feed.innerHTML = posts.map(p => this._postCard(p)).join('');
        feed.querySelectorAll('.post-del').forEach(b =>
            b.addEventListener('click', () => this._delete(b.dataset.id)));
    },

    _postCard(p) {
        const img = p.image_path
            ? `<img class="post-card-img" src="${API.postImageUrl(p.post_id)}" alt="${this.esc(p.image_alt)}">` : '';
        const rating = p.rating && p.rating !== 'general'
            ? `<span class="artwork-badge artwork-badge--${this.esc(p.rating)}">${this.esc(p.rating)}</span>` : '';
        const pubs = (p.publications || []).map(pub => {
            const plat = this._plat(pub.platform);
            if (pub.status === 'posted') {
                const link = pub.external_url
                    ? `<a href="${this.esc(pub.external_url)}" target="_blank" rel="noopener">${plat.emoji || ''} ${this.esc(plat.label)} ↗</a>`
                    : `${plat.emoji || ''} ${this.esc(plat.label)}`;
                return `<span class="post-pub post-pub--ok">${link}</span>`;
            }
            return `<span class="post-pub post-pub--fail" title="${this.esc(pub.error)}">${plat.emoji || ''} ${this.esc(plat.label)} — failed</span>`;
        }).join('');
        return `
            <div class="card post-card">
                <div class="post-card-main">
                    <div class="post-card-body">
                        ${rating}
                        <p class="post-card-text">${this.esc(p.body) || '<span class="muted">(image only)</span>'}${p.thread_count ? ` <span class="muted" style="font-size:11px">🧵 ${p.thread_count + 1} parts</span>` : ''}</p>
                        <div class="post-card-pubs">${pubs || '<span class="muted">not published</span>'}</div>
                        <div class="post-card-meta muted">${this.esc(p.created_at)}</div>
                    </div>
                    ${img}
                </div>
                <button class="btn btn-sm btn-danger post-del" data-id="${p.post_id}">Delete</button>
            </div>`;
    },

    async _delete(id) {
        if (!confirm('Delete this post from your library? Any already-published posts stay live on each platform.')) return;
        try {
            await API.deletePost(id);
            this._toast('success', 'Deleted');
            await this._loadFeed();
        } catch (err) {
            this._toast('error', 'Delete failed: ' + (err.message || err));
        }
    },

    /* ── Tag contacts (handle-book manager) — #/posts/contacts ─── */

    async renderContacts() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;">
                <div>
                    <h1>Tag contacts</h1>
                    <p class="muted"><a href="#/posts">← Back to Posts</a> · Save someone's handle on each
                    platform once. Then tag them with an <strong>@alias</strong> while composing and every
                    network gets their right handle.</p>
                </div>
                <div style="flex-shrink:0;">
                    <button class="btn btn-primary" id="pc-add">+ New contact</button>
                </div>
            </div>
            <div id="pc-form-slot"></div>
            <div id="pc-list"><div class="loading-spinner">Loading…</div></div>`;
        document.getElementById('pc-add').addEventListener('click', () => this._openManagerForm(null));
        await this._loadContactList();
    },

    async _loadContactList() {
        const list = document.getElementById('pc-list');
        if (!list) return;
        let contacts = [];
        try {
            const d = await API.getContacts();
            contacts = (d && d.contacts) || [];
        } catch (err) {
            list.innerHTML = `<div class="card error">Failed to load contacts: ${this.esc(err.message)}</div>`;
            return;
        }
        this._contacts = contacts;
        if (!contacts.length) {
            list.innerHTML = `<div class="empty-state"><p class="muted">No contacts yet — add someone to tag
            them across platforms.</p></div>`;
            return;
        }
        list.innerHTML = contacts.map(c => this._contactCard(c)).join('');
        list.onclick = (e) => {
            const ed = e.target.closest('.pc-edit');
            if (ed) { this._openManagerForm(this._contacts.find(x => String(x.id) === ed.dataset.id)); return; }
            const del = e.target.closest('.pc-del');
            if (del) this._deleteContact(del.dataset.id);
        };
    },

    _contactCard(c) {
        const chips = this._MENTION_FIELDS
            .filter(f => (c[f.key] || '').trim())
            .map(f => `<span class="pc-chip" title="${this.esc(f.label)}">${this._plat(f.code).emoji || ''} @${this.esc(c[f.key])}</span>`)
            .join('');
        return `
            <div class="card pc-card">
                <div class="pc-card-main">
                    <div class="pc-name">${this.esc(c.name)}</div>
                    <div class="pc-chips">${chips || '<span class="muted">no handles yet</span>'}</div>
                </div>
                <div class="pc-actions">
                    <button class="btn btn-sm pc-edit" data-id="${c.id}">Edit</button>
                    <button class="btn btn-sm btn-danger pc-del" data-id="${c.id}">Delete</button>
                </div>
            </div>`;
    },

    _openManagerForm(contact) {
        const slot = document.getElementById('pc-form-slot');
        if (!slot) return;
        const editing = !!(contact && contact.id);
        const val = (k) => this.esc((contact && contact[k]) || '');
        const rows = this._MENTION_FIELDS.map(f =>
            `<label class="post-cf-field">${this.esc(f.label)}
                <input type="text" class="post-cf-input pc-input" data-key="${f.key}" value="${val(f.key)}" placeholder="${this.esc(f.ph)}">
            </label>`).join('');
        slot.innerHTML = `
            <div class="post-contact-form" style="margin-bottom:1rem;">
                <div class="post-cf-head">${editing ? 'Edit contact' : 'New contact'}
                    <span class="muted">— paste each platform's handle (leave blank to skip that one).</span></div>
                <label class="post-cf-field">Name / alias
                    <input type="text" class="post-cf-input pc-input" data-key="name" value="${val('name')}" placeholder="who is this?">
                </label>
                ${rows}
                <div class="post-cf-actions">
                    <button type="button" class="btn btn-sm btn-primary" id="pc-save">${editing ? 'Save changes' : 'Save contact'}</button>
                    <button type="button" class="btn btn-sm" id="pc-cancel">Cancel</button>
                    <span class="muted" id="pc-msg"></span>
                </div>
            </div>`;
        slot.querySelector('#pc-save').addEventListener('click', () => this._saveManagerContact(editing ? contact.id : null));
        slot.querySelector('#pc-cancel').addEventListener('click', () => { slot.innerHTML = ''; });
        const nameInput = slot.querySelector('.pc-input[data-key="name"]');
        if (nameInput) { nameInput.focus(); nameInput.select(); }
    },

    async _saveManagerContact(id) {
        const slot = document.getElementById('pc-form-slot');
        const msg = slot.querySelector('#pc-msg');
        const payload = {};
        slot.querySelectorAll('.pc-input').forEach(inp => { payload[inp.dataset.key] = inp.value.trim(); });
        if (!payload.name) { msg.textContent = 'Give the contact a name.'; return; }
        const save = slot.querySelector('#pc-save');
        save.disabled = true; msg.textContent = 'Saving…';
        try {
            if (id) await API.updateContact(id, payload);
            else await API.createContact(payload);
            slot.innerHTML = '';
            this._toast('success', 'Saved');
            await this._loadContactList();
        } catch (err) {
            save.disabled = false;
            msg.textContent = 'Save failed: ' + (err.message || err);
        }
    },

    async _deleteContact(id) {
        if (!confirm('Delete this contact? Posts that tagged them keep their text, but the tag stops linking.')) return;
        try {
            await API.deleteContact(id);
            this._toast('success', 'Deleted');
            await this._loadContactList();
        } catch (err) {
            this._toast('error', 'Delete failed: ' + (err.message || err));
        }
    },
};
