/*
 * Paired comments (4.56.0, spec 021) — a comment under your own post, from the same account.
 *
 * One small shared module so every surface says the same thing the same way:
 *   - the publish dialogs' comment boxes (Components.confirmPublish `commentBoxes`), pre-filled
 *     from each site's default template;
 *   - the state of a comment after posting (posted / failed + why + Retry / skipped / waiting /
 *     "trying again at …"), used by the publish results, the Posts feed and the post page;
 *   - Settings → Comment templates (saved wording + each site's default).
 *
 * Templates come from GET /api/comments/templates once per page load (cached; Settings refreshes it).
 */
(function () {
    'use strict';

    const esc = (s) => (window.Utils && Utils.escapeHtml ? Utils.escapeHtml(String(s ?? '')) : String(s ?? ''));
    const plat = (c) => (window.platformByCode && window.platformByCode(c)) || { label: c, emoji: '' };

    let _cache = null;

    const Comments = {
        // Mirrors posting/paired_comment.REPLY_PLATFORMS; replaced by the server's list on load.
        REPLY: ['bsky', 'mast', 'tw', 'thr', 'tg', 'ig'],
        LIMITS: { bsky: 300, tw: 280, mast: 500, thr: 500, tg: 4096, ig: 2200 },

        async load(force) {
            if (_cache && !force) return _cache;
            try {
                _cache = await API.get('/api/comments/templates');
                if (Array.isArray(_cache.reply_platforms)) this.REPLY = _cache.reply_platforms;
            } catch (e) {
                _cache = { templates: [], defaults: {} };
            }
            return _cache;
        },

        canReply(code) { return this.REPLY.includes(code); },

        /** This site's default template: { text, template } or empty strings. */
        defaultFor(code) {
            const c = _cache || { templates: [], defaults: {} };
            const name = (c.defaults || {})[code] || '';
            const t = (c.templates || []).find(x => x.name === name);
            return t ? { text: t.text, template: t.name } : { text: '', template: '' };
        },

        /** Boxes for confirmPublish: one per chosen site that can take a reply. */
        async boxesFor(platforms) {
            await this.load();
            return (platforms || []).filter(c => this.canReply(c)).map(code => {
                const d = this.defaultFor(code);
                return { code, label: plat(code).label, cap: this.LIMITS[code] || 2000,
                         value: d.text, template: d.template };
            });
        },

        /** The dialog's boxes → the body's `comments` ({} when every box is blank: no comment). */
        fromDialog(conf) {
            return (conf && conf.comments) || undefined;
        },

        /** Template <option>s for a picker. */
        templateOptions(selected) {
            const ts = (_cache && _cache.templates) || [];
            return `<option value="">Template…</option>` + ts.map(t =>
                `<option value="${esc(t.name)}"${t.name === selected ? ' selected' : ''}>${esc(t.name)}</option>`).join('');
        },
        templateText(name) {
            const t = ((_cache && _cache.templates) || []).find(x => x.name === name);
            return t ? t.text : '';
        },

        /** One line for a comment's state, used under a site's result / chip. */
        stateHtml(c, opts) {
            if (!c) return '';
            const retry = c.id && (c.status === 'failed' || c.status === 'cancelled')
                ? ` <button type="button" class="btn btn-xs btn-outline" data-comment-retry="${esc(c.id)}">Retry comment</button>` : '';
            let what;
            if (c.status === 'posted') {
                // The link comes from the site's API (a Mastodon instance can say anything): scheme-checked.
                const safe = c.external_url && window.Utils && Utils.safeUrl ? Utils.safeUrl(c.external_url) : '';
                what = safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener">Comment posted ↗</a>` : 'Comment posted';
            } else if (c.status === 'sending') {
                what = 'Comment going up…';
            } else if (c.waiting) {
                what = 'Comment waiting on the post';
            } else if (c.status === 'pending') {
                what = 'Comment goes up after the post';
            } else if (c.status === 'skipped') {
                what = 'Comment left off: ' + esc(c.error || '');
            } else if (c.status === 'cancelled') {
                what = 'Comment not sent (cancelled)';
            } else {
                const again = c.next_try_at && window.Utils && Utils.time
                    ? ` — trying again at ${esc(Utils.time.fmt.time(c.next_try_at + 'Z'))}` : '';
                what = 'Comment didn’t go up: ' + esc(c.error || 'failed') + again;
            }
            const cls = c.status === 'posted' ? 'is-ok' : (c.status === 'failed' ? 'is-fail' : 'is-skip');
            return `<span class="pp-comment-state ${cls}${(opts && opts.block) ? ' is-block' : ''}">💬 ${what}${retry}</span>`;
        },

        /** Wire Retry buttons inside `root`; `after` runs on success (e.g. re-render). */
        wireRetry(root, after) {
            if (!root || root._commentRetryWired) return;
            root._commentRetryWired = true;
            root.addEventListener('click', async (e) => {
                const b = e.target.closest('[data-comment-retry]');
                if (!b) return;
                e.preventDefault(); e.stopPropagation();
                b.disabled = true; b.textContent = 'Sending…';
                try {
                    const r = await API.post(`/api/comments/${encodeURIComponent(b.dataset.commentRetry)}/retry`, {});
                    if (window.toast) window.toast[r.status === 'posted' ? 'success' : 'error'](
                        r.status === 'posted' ? 'Comment posted' : 'Comment didn’t go up: ' + (r.error || ''));
                    if (after) after(r);
                } catch (err) {
                    if (window.toast) window.toast.error(err.message || 'Retry failed');
                    b.disabled = false; b.textContent = 'Retry comment';
                }
            });
        },

        /* ── Settings → Comment templates ─────────────────────────────── */

        async mountSettings(section) {
            if (!section) return;
            let root = section.querySelector('.pp-comments-settings');
            if (!root) {
                root = document.createElement('div');
                root.className = 'pp-comments-settings';
                section.appendChild(root);
            }
            const data = await this.load(true);
            this._draft = { templates: (data.templates || []).map(t => ({ ...t })), defaults: { ...(data.defaults || {}) } };
            this._renderSettings(root);
        },

        _defaultsHtml() {
            const d = this._draft;
            const names = d.templates.map(t => t.name.trim()).filter(Boolean);
            return this.REPLY.map(code => `
                <label class="pp-cdef"><span>${esc(plat(code).emoji || '')} ${esc(plat(code).label)}</span>
                    <select data-cdef="${esc(code)}" aria-label="Default comment for ${esc(plat(code).label)}">
                        <option value="">No comment by default</option>
                        ${names.map(n => `<option value="${esc(n)}"${d.defaults[code] === n ? ' selected' : ''}>${esc(n)}</option>`).join('')}
                    </select></label>`).join('');
        },

        _renderSettings(root) {
            const d = this._draft;
            const rows = d.templates.map((t, i) => `
                <div class="pp-ctpl" data-i="${i}">
                    <label class="pp-ctpl-name"><span>Name</span>
                        <input type="text" maxlength="60" value="${esc(t.name)}" data-ctpl-name aria-label="Template name"></label>
                    <label class="pp-ctpl-text"><span>Comment</span>
                        <textarea rows="2" maxlength="2000" data-ctpl-text aria-label="Template text">${esc(t.text)}</textarea></label>
                    <button type="button" class="btn btn-xs btn-outline" data-ctpl-del aria-label="Delete template ${esc(t.name)}">Delete</button>
                </div>`).join('');
            const defaults = this._defaultsHtml();
            root.innerHTML = `
                <div class="settings-block">
                    <h4>How it works</h4>
                    <ol class="pp-csteps">
                        <li>Save the wording you use often below, for example <em>Full version: {link}</em>.</li>
                        <li>Pick a template as a site's default. Ticking that site when you post fills its comment in for you.</li>
                        <li>Change or clear the comment before you post. Clearing it means no comment on that post.</li>
                    </ol>
                    <p class="muted">Fill-ins: <code>{link}</code> the piece's main link · <code>{links}</code> every link ·
                        <code>{site:fa}</code> its FurAffinity link (any site code) · <code>{title}</code> · <code>{artist}</code>.
                        Tumblr can't take comments, so it isn't listed.</p>
                </div>
                <div class="settings-block">
                    <h4>Templates</h4>
                    ${rows || '<p class="muted">No templates yet.</p>'}
                    <button type="button" class="btn btn-sm btn-outline" data-ctpl-add>+ Add a template</button>
                </div>
                <div class="settings-block">
                    <h4>Each site's default</h4>
                    <div class="pp-cdefs">${defaults}</div>
                </div>
                <div class="pp-csave"><button type="button" class="btn btn-primary" data-ctpl-save>Save</button>
                    <span class="muted" data-ctpl-msg role="status"></span></div>`;
            const sync = () => {
                root.querySelectorAll('.pp-ctpl').forEach(el => {
                    const t = d.templates[+el.dataset.i];
                    t.name = el.querySelector('[data-ctpl-name]').value;
                    t.text = el.querySelector('[data-ctpl-text]').value;
                });
                root.querySelectorAll('[data-cdef]').forEach(el => { d.defaults[el.dataset.cdef] = el.value; });
            };
            root.querySelector('[data-ctpl-add]').addEventListener('click', () => {
                sync(); d.templates.push({ name: '', text: '' }); this._renderSettings(root);
            });
            root.querySelectorAll('[data-ctpl-del]').forEach(b => b.addEventListener('click', () => {
                sync(); d.templates.splice(+b.closest('.pp-ctpl').dataset.i, 1); this._renderSettings(root);
            }));
            // Renaming refreshes only the default pickers — re-drawing the whole page here would
            // replace the field the person is tabbing into and drop what they type next.
            root.querySelectorAll('[data-ctpl-name]').forEach(el => el.addEventListener('input', () => {
                sync();
                root.querySelector('.pp-cdefs').innerHTML = this._defaultsHtml();
            }));
            root.querySelector('[data-ctpl-save]').addEventListener('click', async () => {
                sync();
                const msg = root.querySelector('[data-ctpl-msg]');
                const defaults = {};
                Object.keys(d.defaults).forEach(k => { if (d.defaults[k]) defaults[k] = d.defaults[k]; });
                try {
                    const r = await API.put('/api/comments/templates',
                        { templates: d.templates.filter(t => t.name.trim() || t.text.trim()), defaults });
                    _cache = { ..._cache, templates: r.templates, defaults: r.defaults };
                    this._draft = { templates: r.templates.map(t => ({ ...t })), defaults: { ...r.defaults } };
                    this._renderSettings(root);
                    root.querySelector('[data-ctpl-msg]').textContent = 'Saved.';
                } catch (e) {
                    msg.textContent = e.message || 'Couldn’t save';
                }
            });
        },
    };

    if (typeof window !== 'undefined') window.Comments = Comments;
})();
